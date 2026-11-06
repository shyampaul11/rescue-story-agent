"""Local CPU inference using the package's bundled models. No network or API calls."""
import json
from pathlib import Path
import sys


def run(image_path, output_path):
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return 2
    # Zero recognition threshold keeps uncertain lines visible for mandatory review.
    engine = RapidOCR(text_score=0.0, print_verbose=False)
    result, _ = engine(str(image_path))
    lines = [{'text': row[1], 'confidence': float(row[2])} for row in (result or [])]
    output_path.write_text(json.dumps(lines, ensure_ascii=False), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(run(Path(sys.argv[1]), Path(sys.argv[2])))
