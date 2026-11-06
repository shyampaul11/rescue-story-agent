"""Isolated OpenCV decoder. Receives only a temporary local video path; no audio."""
import json
import math
from pathlib import Path
import sys

import cv2


def sample(path, count, side):
    capture = cv2.VideoCapture(str(path))
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
        total = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        width, height = capture.get(cv2.CAP_PROP_FRAME_WIDTH), capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
        if (not capture.isOpened() or not all(math.isfinite(x) and x > 0 for x in (fps, total, width, height))
                or width * height > 24_000_000):
            raise ValueError('Invalid or oversized video')
        # Evenly spaced samples across the interior, at 10%, 50%, 90% for three frames.
        fractions = [0.5] if count == 1 else [0.1 + 0.8 * i / (count - 1) for i in range(count)]
        targets = sorted({min(int(total) - 1, int((total - 1) * fraction)) for fraction in fractions})
        manifest = []
        for target in targets:
            capture.set(cv2.CAP_PROP_POS_FRAMES, target)
            ok, frame = capture.read()
            if not ok or frame is None:
                raise ValueError('Frame decode failed')
            position = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000
            # Some codecs provide no presentation timestamp; use decoder frame position / FPS.
            if not math.isfinite(position) or (position == 0 and target > 0):
                position = max(0, capture.get(cv2.CAP_PROP_POS_FRAMES) - 1) / fps
            h, w = frame.shape[:2]
            ratio = min(1, side / max(h, w))
            frame = cv2.resize(frame, (max(1, int(w * ratio)), max(1, int(h * ratio))))
            if not cv2.imwrite(str(path.parent / f'frame_{len(manifest)}.jpg'), frame, [cv2.IMWRITE_JPEG_QUALITY, 80]):
                raise ValueError('Frame encode failed')
            manifest.append({'timestamp_seconds': round(position, 3)})
        (path.parent / 'frames.json').write_text(json.dumps(manifest), encoding='utf-8')
    finally:
        capture.release()


if __name__ == '__main__':
    sample(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]))
