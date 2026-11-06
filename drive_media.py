"""Private bounded media downloads and session-owned preview caching. No AI calls."""
from io import BytesIO
from pathlib import PurePosixPath
import time
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
from googleapiclient.http import MediaIoBaseDownload

import drive_client as drive

IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
VIDEO_TYPES = {'video/mp4', 'video/quicktime', 'video/webm'}
IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.webp'}
VIDEO_EXT = {'.mp4', '.mov', '.webm'}
MIB = 1024 * 1024
CACHE_TTL = 600


def media_kind(file):
    extension = PurePosixPath(file['name']).suffix.casefold()
    mime = file.get('mimeType', '').casefold()
    if mime == drive.FOLDER:
        return 'folder'
    if mime in IMAGE_TYPES or extension in IMAGE_EXT:
        return 'image'
    if mime in VIDEO_TYPES or extension in VIDEO_EXT:
        return 'video'
    return 'unsupported'


def discover_media(service, animal_id):
    folders = [entry for entry in drive.children(service, animal_id) if drive.is_media_folder(entry)]
    files, seen = [], set()
    for folder in folders:
        for file in drive.children(service, folder['id']):
            if file['id'] not in seen and file.get('mimeType') != drive.FOLDER:
                seen.add(file['id'])
                files.append(dict(file, kind=media_kind(file), folder=folder['name']))
    files.sort(key=lambda file: (file['name'].casefold(), file['id']))
    return {'folders': [folder['name'] for folder in folders], 'files': files}


def file_size(file):
    try:
        size = int(file.get('size', -1))
        return size if size >= 0 else None
    except (TypeError, ValueError):
        return None


def preview_problem(file, max_bytes):
    if media_kind(file) not in ('image', 'video'):
        return 'Unsupported format; preview not available.'
    size = file_size(file)
    if size is None:
        return 'Size unavailable; not downloaded for safety.'
    if size > max_bytes:
        return f'Above the {max_bytes / MIB:g} MB preview limit; not downloaded.'
    return None


def preview_summary(files, max_bytes):
    """Describe size exclusions from metadata only; never downloads content."""
    sizes = [file_size(file) for file in files]
    larger = [size for size in sizes if size is not None and size > max_bytes]
    return {
        'total': len(files),
        'eligible': sum(size is not None and size <= max_bytes for size in sizes),
        'oversize': len(larger),
        'unknown': sum(size is None for size in sizes),
        'next_limit_mb': (min(larger) + MIB - 1) // MIB if larger else None,
    }


class LimitedBuffer(BytesIO):
    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise drive.DriveError('Download exceeded the configured size limit and was stopped.')
        return super().write(data)


def download_media(service, file, max_bytes):
    problem = preview_problem(file, max_bytes)
    if problem:
        raise drive.DriveError(problem)
    class Download:
        def execute(self):
            with LimitedBuffer(max_bytes) as output:
                request = service.files().get_media(fileId=file['id'], supportsAllDrives=True)
                try:
                    downloader = MediaIoBaseDownload(output, request, chunksize=256 * 1024)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk(num_retries=2)
                    return output.getvalue()
                finally:
                    # MediaIoBaseDownload uses request.http directly, bypassing
                    # HttpRequest.execute(). Close only after all chunks finish.
                    if isinstance(request, drive.IsolatedHttpRequest):
                        request.close()

    return drive._call(Download())


def thumbnail(data):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format not in {'JPEG', 'PNG', 'WEBP'}:
                    raise drive.DriveError('The file is not a supported JPEG, PNG or WebP photo.')
                if image.width * image.height > 24_000_000:
                    raise drive.DriveError('Photo exceeds the 24-megapixel preview limit; not decoded.')
                image = ImageOps.exif_transpose(image)
                image.thumbnail((640, 640))
                output = BytesIO()
                image.convert('RGB').save(output, format='JPEG', quality=82)
                return output.getvalue()
    except drive.DriveError:
        raise
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise drive.DriveError('Photo could not be previewed safely. It may be damaged or too large.') from None


def cached_preview(service, animal_id, file, cache, max_bytes, cache_bytes=64 * MIB):
    """Caller supplies a per-session dict. No global/private-media cache."""
    problem = preview_problem(file, max_bytes)
    if problem:
        raise drive.DriveError(problem)
    now = time.monotonic()
    for key in list(cache):
        if now - cache[key]['time'] > CACHE_TTL:
            del cache[key]
    key = (animal_id, file['id'], file.get('modifiedTime'), file_size(file), max_bytes)
    if key in cache:
        value = cache.pop(key)
        cache[key] = value
        return value['data']
    data = download_media(service, file, max_bytes)
    if media_kind(file) == 'image':
        data = thumbnail(data)
    if len(data) > cache_bytes:
        raise drive.DriveError('Preview exceeds the session cache budget.')
    while cache and (len(cache) >= 24 or sum(len(entry['data']) for entry in cache.values()) + len(data) > cache_bytes):
        del cache[next(iter(cache))]
    cache[key] = {'data': data, 'time': now}
    return data
