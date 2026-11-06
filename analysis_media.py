"""Private image preparation and bounded subprocess video sampling."""
from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import warnings

from PIL import Image, ImageOps, ExifTags

from ai_config import AnalysisError
import drive_media as media
from animal_profile import PhotoDateReference
from source_context import photo_date_reference


@dataclass(frozen=True)
class PreparedImage:
    reference: str
    source_type: str
    jpeg: bytes
    timestamp_seconds: float | None = None
    photo_date: PhotoDateReference | None = None


def prepare_photo(data, settings, reference='photo:1', drive_photo_time=None):
    if len(data) > settings.photo_mb * media.MIB:
        raise AnalysisError('Photo exceeds the AI file-size limit.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format not in {'JPEG', 'PNG', 'WEBP'}:
                    raise AnalysisError('AI photos must be JPEG, PNG or WebP.')
                if source.width * source.height > 24_000_000:
                    raise AnalysisError('Photo exceeds the 24-megapixel decode limit.')
                # Read only date tags before recompression strips EXIF. GPS/device/owner are never forwarded.
                candidates = []
                try:
                    exif = source.getexif()
                    details = exif.get_ifd(ExifTags.IFD.Exif)
                    candidates = [('exif_datetime_original', details.get(36867) or exif.get(36867)),
                                  ('drive_photo_time', drive_photo_time),
                                  ('exif_datetime_digitized', details.get(36868) or exif.get(36868)),
                                  ('exif_datetime', exif.get(306))]
                except (OSError, ValueError, TypeError, KeyError, SyntaxError):
                    candidates = [('drive_photo_time', drive_photo_time)]
                photo_date = photo_date_reference(reference, candidates)
                image = ImageOps.exif_transpose(source)
                image.thumbnail((settings.image_side, settings.image_side))
                output = BytesIO()
                image.convert('RGB').save(output, format='JPEG', quality=80)
                return output.getvalue(), photo_date
    except AnalysisError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AnalysisError('Photo is damaged or cannot be decoded safely.') from None


def prepare_image(data, settings):
    """Compatibility helper for callers needing only a metadata-free JPEG."""
    return prepare_photo(data, settings)[0]


def extract_video_frames(data, suffix, settings):
    if suffix.lower() not in {'.mp4', '.mov', '.webm'}:
        raise AnalysisError('Video format is unsupported. Select MP4, MOV or WebM.')
    if len(data) > settings.video_mb * media.MIB:
        raise AnalysisError('Video exceeds the AI file-size limit.')
    # A separate process can be terminated even if a native decoder hangs.
    try:
        with TemporaryDirectory(prefix='rescue_frames_') as directory:
            root = Path(directory)
            video = root / ('selected' + suffix.lower())
            video.write_bytes(data)
            subprocess.run(
                [sys.executable, str(Path(__file__).with_name('video_frame_worker.py')),
                 str(video), str(settings.max_frames), str(settings.image_side)],
                check=True, timeout=settings.video_seconds, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
            )
            manifest = json.loads((root / 'frames.json').read_text(encoding='utf-8'))
            return [(entry['timestamp_seconds'], (root / f'frame_{index}.jpg').read_bytes())
                    for index, entry in enumerate(manifest)]
    except subprocess.TimeoutExpired:
        raise AnalysisError('Video processing timed out. Select a smaller or shorter video. No AI request was sent.') from None
    except (subprocess.SubprocessError, OSError, ValueError, KeyError):
        raise AnalysisError('Video could not be sampled safely. It may be damaged or use an unsupported codec. No AI request was sent.') from None


def selected_files(catalog, selected_ids, settings):
    """Validate all selections before downloading anything; never silently truncate."""
    by_id = {file['id']: file for file in catalog.get('files', [])}
    if not set(selected_ids) <= by_id.keys():
        raise AnalysisError('A selected file is no longer in the media list. Refresh and select it again.')
    files = [by_id[file_id] for file_id in sorted(set(selected_ids))]
    if any(file['kind'] not in ('image', 'video') for file in files):
        raise AnalysisError('Unsupported media selected. Deselect it before analysis.')
    for kind, maximum in [('image', settings.max_photos), ('video', settings.max_videos)]:
        if sum(file['kind'] == kind for file in files) > maximum:
            raise AnalysisError(f'AI limit: select at most {maximum} {kind} file(s). Deselect extras.')
    for file in files:
        maximum = settings.photo_mb if file['kind'] == 'image' else settings.video_mb
        if media.preview_problem(file, maximum * media.MIB):
            raise AnalysisError('A selected file has unknown size or exceeds the AI size limit. Deselect it or adjust .env.')
    return files


def prepare_selected_media(service, files, settings):
    images, source_map = [], {}
    counts = {'image': 0, 'video': 0}
    for file in files:
        kind = file['kind']
        counts[kind] += 1
        limit = settings.photo_mb if kind == 'image' else settings.video_mb
        data = media.download_media(service, file, limit * media.MIB)
        if kind == 'image':
            ref = f'photo:{counts[kind]}'
            jpeg, photo_date = prepare_photo(data, settings, ref, file.get('imageMediaMetadata', {}).get('time'))
            images.append(PreparedImage(ref, 'PHOTO_OBSERVATION', jpeg, photo_date=photo_date))
            source_map[ref] = file['name']
            source_map[ref + '#date'] = file['name'] + ' — photo date metadata'
        else:
            for timestamp, jpeg in extract_video_frames(data, Path(file['name']).suffix, settings):
                ref = f'video:{counts[kind]}@{timestamp:.3f}s'
                images.append(PreparedImage(ref, 'VIDEO_FRAME_OBSERVATION', jpeg, timestamp))
                source_map[ref] = f"{file['name']} at {timestamp:.3f}s (sampled frame)"
    return images, source_map
