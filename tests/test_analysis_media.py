from io import BytesIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

from ai_config import AnalysisError, AnalysisSettings
import analysis_media as media


def photo_bytes(size=(1600, 800), format='PNG'):
    output = BytesIO()
    Image.new('RGB', size, '#cc7744').save(output, format=format)
    return output.getvalue()


def file(id='p1', name='private.jpg', kind='image', **values):
    return dict({'id': id, 'name': name, 'kind': kind, 'size': '1024',
                 'mimeType': 'image/jpeg' if kind == 'image' else 'video/mp4'}, **values)


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.settings = AnalysisSettings()

    def test_resize_compression_and_metadata_removal(self):
        prepared = media.prepare_image(photo_bytes(), self.settings)
        with Image.open(BytesIO(prepared)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.size, (1024, 512))
            self.assertFalse(image.getexif())

    def test_small_images_not_enlarged(self):
        with Image.open(BytesIO(media.prepare_image(photo_bytes((32, 16)), self.settings))) as image:
            self.assertEqual(image.size, (32, 16))

    def test_unsupported_format(self):
        with self.assertRaisesRegex(AnalysisError, 'JPEG, PNG or WebP'):
            media.prepare_image(photo_bytes((16, 16), 'GIF'), self.settings)

    def test_damaged_image(self):
        with self.assertRaisesRegex(AnalysisError, 'damaged'):
            media.prepare_image(b'not an image', self.settings)

    def test_photo_file_size_limit(self):
        with self.assertRaisesRegex(AnalysisError, 'file-size'):
            media.prepare_image(b'x' * (1024 * 1024 + 1), AnalysisSettings(photo_mb=1))

    def test_large_pixel_dimensions_rejected(self):
        with self.assertRaisesRegex(AnalysisError, 'megapixel'):
            media.prepare_image(photo_bytes((5001, 5000)), self.settings)


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.settings = AnalysisSettings()

    @staticmethod
    def synthetic_video():
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'test.mp4'
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'mp4v'), 10, (64, 48))
            if not writer.isOpened():
                raise AssertionError('Synthetic MP4 fixture encoder unavailable')
            try:
                for index in range(30):
                    writer.write(np.full((48, 64, 3), index * 7, dtype=np.uint8))
            finally:
                writer.release()
            return path.read_bytes()

    def test_real_subprocess_extracts_three_documented_timestamps(self):
        frames = media.extract_video_frames(self.synthetic_video(), '.mp4', self.settings)
        self.assertEqual(len(frames), 3)
        for (timestamp, jpeg), expected in zip(frames, [0.2, 1.4, 2.6]):
            self.assertAlmostEqual(timestamp, expected, places=2)
            with Image.open(BytesIO(jpeg)) as image:
                self.assertEqual(image.format, 'JPEG')
                self.assertEqual(image.size, (64, 48))

    def test_real_decoder_rejects_damaged_video(self):
        with self.assertRaisesRegex(AnalysisError, 'could not be sampled'):
            media.extract_video_frames(b'damaged', '.mp4', self.settings)

    @patch.object(media.subprocess, 'run')
    def test_unsupported_format_never_starts_decoder(self, run):
        with self.assertRaisesRegex(AnalysisError, 'unsupported'):
            media.extract_video_frames(b'data', '.avi', self.settings)
        run.assert_not_called()

    @patch.object(media.subprocess, 'run')
    def test_video_size_limit_before_decoder(self, run):
        with self.assertRaisesRegex(AnalysisError, 'file-size'):
            media.extract_video_frames(b'x' * (1024 * 1024 + 1), '.mp4', AnalysisSettings(video_mb=1))
        run.assert_not_called()

    @patch.object(media.subprocess, 'run', side_effect=subprocess.TimeoutExpired('decoder', 30))
    def test_hung_decoder_is_bounded_and_temporary_files_cleaned(self, run):
        with self.assertRaisesRegex(AnalysisError, 'timed out'):
            media.extract_video_frames(b'data', '.mp4', self.settings)
        args = run.call_args
        self.assertEqual(args.kwargs['timeout'], 30)
        self.assertFalse(Path(args.args[0][2]).exists())


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.settings = AnalysisSettings()

    def test_only_selected_media_downloaded(self):
        catalog = {'files': [file('p1'), file('p2')]}
        files = media.selected_files(catalog, ['p2'], self.settings)
        with patch.object(media.media, 'download_media', return_value=photo_bytes((16, 16))) as download:
            images, source_map = media.prepare_selected_media(object(), files, self.settings)
        self.assertEqual(download.call_args.args[1]['id'], 'p2')
        download.assert_called_once()
        self.assertEqual(images[0].reference, 'photo:1')
        self.assertEqual(source_map['photo:1'], 'private.jpg')

    def test_empty_selection_processes_nothing(self):
        files = media.selected_files({'files': [file()]}, [], self.settings)
        with patch.object(media.media, 'download_media') as download:
            self.assertEqual(media.prepare_selected_media(None, files, self.settings), ([], {}))
        download.assert_not_called()

    def test_stale_and_unsupported_selections_rejected(self):
        for catalog, ids in [({'files': []}, ['stale']),
                             ({'files': [file(kind='unsupported')]}, ['p1'])]:
            with self.assertRaises(AnalysisError):
                media.selected_files(catalog, ids, self.settings)

    def test_photo_and_video_count_limits_rejected_before_processing(self):
        for files in [[file(str(i)) for i in range(4)],
                      [file(str(i), name='clip.mp4', kind='video') for i in range(2)]]:
            with self.assertRaisesRegex(AnalysisError, 'at most'):
                media.selected_files({'files': files}, [item['id'] for item in files], self.settings)

    def test_unknown_and_oversized_metadata_rejected(self):
        for size in [None, 'not-a-size', str(9 * 1024 * 1024)]:
            with self.subTest(size=size), self.assertRaises(AnalysisError):
                media.selected_files({'files': [file(size=size)]}, ['p1'], self.settings)

    @patch.object(media, 'extract_video_frames', return_value=[(1.25, b'jpeg')])
    @patch.object(media.media, 'download_media', return_value=b'video')
    def test_video_source_mapping_preserves_timestamp(self, download, extract):
        images, source_map = media.prepare_selected_media(None, [file(name='private.mov', kind='video')], self.settings)
        self.assertEqual(images[0].source_type, 'VIDEO_FRAME_OBSERVATION')
        self.assertEqual(images[0].timestamp_seconds, 1.25)
        self.assertEqual(images[0].reference, 'video:1@1.250s')
        self.assertIn('private.mov at 1.250s', source_map[images[0].reference])
        self.assertEqual(extract.call_args.args[1], '.mov')


if __name__ == '__main__':
    unittest.main()
