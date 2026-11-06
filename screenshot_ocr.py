"""Bounded local OCR. Screenshots and recognized text never go to an OCR service."""
from dataclasses import dataclass, replace
from io import BytesIO
import json
import math
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import warnings

from PIL import Image, ImageOps


class OcrError(ValueError):
    """A safe OCR/import error with no private text or raw engine output."""


@dataclass(frozen=True)
class OcrResult:
    text: str
    mean_confidence: float
    low_confidence_lines: int
    line_count: int
    preview_png: bytes | None = None


MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_SIDE = 2400
MAX_TEXT = 30000
TIMEOUT_SECONDS = 45


def prepare_screenshot(data):
    if not data or len(data) > MAX_BYTES:
        raise OcrError('Screenshot must be non-empty and at most 8 MB.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format not in {'PNG', 'JPEG', 'WEBP'}:
                    raise OcrError('Upload a PNG, JPEG or WebP screenshot.')
                if source.width * source.height > MAX_PIXELS:
                    raise OcrError('Screenshot exceeds the 16-megapixel limit. Crop the message first.')
                image = ImageOps.exif_transpose(source).convert('RGBA')
                # White compositing preserves dark text in transparent screenshots.
                background = Image.new('RGBA', image.size, 'white')
                image = Image.alpha_composite(background, image).convert('RGB')
                image.thumbnail((MAX_SIDE, MAX_SIDE))
                output = BytesIO()
                image.save(output, format='PNG')  # Strip all screenshot metadata.
                return output.getvalue()
    except OcrError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise OcrError('Screenshot is damaged or cannot be decoded safely.') from None


def decode_result(value):
    """Keep low-confidence text for human review; never silently drop recognized lines."""
    if not isinstance(value, list) or len(value) > 2000:
        raise OcrError('OCR returned an invalid or oversized result.')
    lines, scores = [], []
    for entry in value:
        if not isinstance(entry, dict) or not isinstance(entry.get('text'), str):
            raise OcrError('OCR returned an invalid result.')
        text = entry['text'].strip()
        if not text:
            continue
        try:
            score = float(entry['confidence'])
        except (KeyError, TypeError, ValueError):
            raise OcrError('OCR returned an invalid confidence value.') from None
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise OcrError('OCR returned an invalid confidence value.')
        lines.append(text)
        scores.append(score)
    if not lines:
        raise OcrError('No readable text found. Crop closer to the message, use a clearer screenshot, or type it manually.')
    text = '\n'.join(lines)
    if len(text) > MAX_TEXT:
        raise OcrError('Recognized text exceeds 30,000 characters. Crop to the relevant message.')
    return OcrResult(text, sum(scores) / len(scores), sum(score < 0.8 for score in scores), len(lines))


def extract_screenshot_text(data):
    png = prepare_screenshot(data)
    try:
        with TemporaryDirectory(prefix='rescue_ocr_') as directory:
            root = Path(directory)
            image_path, output_path = root / 'message.png', root / 'result.json'
            image_path.write_bytes(png)
            # Isolation makes the native ONNX inference stoppable and keeps raw engine logs private.
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('ocr_worker.py')),
                str(image_path), str(output_path)], timeout=TIMEOUT_SECONDS,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            if result.returncode == 2:
                raise OcrError('Local OCR is not installed. Install requirements.txt and restart Streamlit.')
            if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size > 512000:
                raise OcrError('Local OCR could not read this screenshot. Try a clearer crop or enter the text manually.')
            return replace(decode_result(json.loads(output_path.read_text(encoding='utf-8'))), preview_png=png)
    except OcrError:
        raise
    except subprocess.TimeoutExpired:
        raise OcrError('OCR exceeded its 45-second limit and was stopped. Crop to a smaller message and try again.') from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise OcrError('Local OCR could not complete. Check the installed dependencies or enter the text manually.') from None


def merge_reviewed_text(existing, reviewed, mode, message_date=''):
    if mode not in ('Append', 'Replace'):
        raise OcrError('Choose Append or Replace.')
    text, date_text = reviewed.strip(), message_date.strip()
    if not text:
        raise OcrError('Review and enter some message text before using it.')
    if len(text) > MAX_TEXT or len(date_text) > 120 or '\n' in date_text or '\r' in date_text:
        raise OcrError('Message text or date exceeds the import limits.')
    # Dates are deliberately not parsed, inferred from upload time, or rewritten.
    if date_text and date_text not in text:
        text = date_text + '\n' + text
    if mode == 'Append' and existing.strip():
        return existing.rstrip() + '\n\n' + text
    return text
