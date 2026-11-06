"""Local configuration. Never print credentials or include them in fingerprints."""
from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


class AnalysisError(ValueError):
    """A safe, user-facing analysis error (no raw provider messages)."""


@dataclass(frozen=True)
class AnalysisSettings:
    model: str = ''
    reasoning_effort: str = ''
    max_photos: int = 3
    max_videos: int = 1
    max_frames: int = 3
    photo_mb: int = 8
    video_mb: int = 25
    image_side: int = 1024
    video_seconds: int = 30
    output_tokens: int = 6000
    text_chars: int = 30000

    def __post_init__(self):
        if self.reasoning_effort not in ('', 'none', 'minimal', 'low', 'medium', 'high'):
            raise AnalysisError('Unsupported AI_REASONING_EFFORT setting.')
        bounds = {'max_photos': (0, 3), 'max_videos': (0, 1), 'max_frames': (1, 3),
                  'photo_mb': (1, 20), 'video_mb': (1, 64), 'image_side': (256, 1536),
                  'video_seconds': (1, 60), 'output_tokens': (1000, 12000),
                  'text_chars': (1000, 60000)}
        for key, (minimum, maximum) in bounds.items():
            if not minimum <= getattr(self, key) <= maximum:
                raise AnalysisError(f'AI setting {key} must be between {minimum} and {maximum}.')


def load_settings():
    load_dotenv(Path(__file__).resolve().parent / '.env', override=False)
    names = {'max_photos': 'AI_MAX_PHOTOS', 'max_videos': 'AI_MAX_VIDEOS',
             'max_frames': 'AI_MAX_VIDEO_FRAMES', 'photo_mb': 'AI_PHOTO_MAX_MB',
             'video_mb': 'AI_VIDEO_MAX_MB', 'image_side': 'AI_IMAGE_MAX_SIDE',
             'video_seconds': 'AI_VIDEO_TIMEOUT_SECONDS', 'output_tokens': 'AI_MAX_OUTPUT_TOKENS',
             'text_chars': 'AI_MAX_TEXT_CHARS'}
    try:
        values = {key: int(os.environ[name]) for key, name in names.items() if name in os.environ}
        return AnalysisSettings(model=os.getenv('OPENAI_MODEL', '').strip(),
                                reasoning_effort=os.getenv('AI_REASONING_EFFORT', '').strip(), **values)
    except (TypeError, ValueError):
        raise AnalysisError('Invalid AI configuration. Check the limits in .env against .env.example.') from None


def require_credentials(settings):
    key = os.getenv('OPENAI_API_KEY', '').strip()
    if not key or key == 'replace-with-your-private-api-key':
        raise AnalysisError('OPENAI_API_KEY is missing. Configure it locally in .env and restart Streamlit.')
    if not settings.model:
        raise AnalysisError('OPENAI_MODEL is missing. Set a text/image Responses model in .env.')
    return key
