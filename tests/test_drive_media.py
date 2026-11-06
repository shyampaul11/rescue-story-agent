"""Mock-only media tests. No credentials, private rescue photos or network."""
from io import BytesIO
import unittest
from editorial_test_support import isolated_editorial_storage
from unittest.mock import MagicMock, patch

from PIL import Image
from googleapiclient.errors import HttpError
from httplib2 import Response
from streamlit.testing.v1 import AppTest
from history_extractor import HistoryError

import drive_client as drive
import drive_media as media


def file(name='photo.jpg', id='photo', mime='image/jpeg', size=500):
    return {'id': id, 'name': name, 'mimeType': mime, 'size': str(size), 'modifiedTime': '2026-11-03'}


def folder(name="Foto's", id='photos'):
    return {'id': id, 'name': name, 'mimeType': drive.FOLDER}


def picture():
    output = BytesIO()
    Image.new('RGB', (1000, 800), 'blue').save(output, 'JPEG')
    return output.getvalue()


class MediaTests(unittest.TestCase):
    def test_size_exclusion_summary_and_suggested_limit(self):
        summary = media.preview_summary([file(size=media.MIB), file(size=9 * media.MIB),
                                        file(size=10 * media.MIB + 1), file(size=-1)], 8 * media.MIB)
        self.assertEqual(summary, {'total': 4, 'eligible': 1, 'oversize': 2, 'unknown': 1, 'next_limit_mb': 9})
        self.assertEqual(media.preview_summary([], media.MIB)['total'], 0)
    def test_media_folder_variations(self):
        for name in ("Foto's", 'FOTOS', 'Foto’s', 'Photos', 'PhOtOs', 'fótos', 'Images'):
            self.assertTrue(drive.is_media_folder(folder(name)))

    def test_image_and_video_detection(self):
        for name in ('x.jpg', 'x.JPEG', 'x.PNG', 'x.webp'):
            self.assertEqual(media.media_kind(file(name, mime='application/octet-stream')), 'image')
        for name in ('x.MP4', 'x.mov', 'x.webm'):
            self.assertEqual(media.media_kind(file(name, mime='application/octet-stream')), 'video')
        self.assertEqual(media.media_kind(file('x', mime='image/png')), 'image')
        self.assertEqual(media.media_kind(file('x', mime='video/quicktime')), 'video')
        for name in ('x.pdf', 'x.heic', 'x.gif', 'x.avi'):
            self.assertEqual(media.media_kind(file(name, mime='application/octet-stream')), 'unsupported')

    def test_discovery_pagination_and_read_only_operations(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.side_effect = [
            {'files': [folder()], 'nextPageToken': 'animals2'},
            {'files': [folder('Photos', 'other')]},
            {'files': [file()], 'nextPageToken': 'media2'},
            {'files': [file('clip.mp4', 'clip', 'video/mp4'), folder('Nested', 'nested')]},
            {'files': [file('notes.pdf', 'notes', 'application/pdf'), file()]},
        ]
        result = media.discover_media(service, 'azra')
        self.assertEqual(len(result['folders']), 2)
        self.assertEqual(len(result['files']), 3)
        self.assertEqual({entry['kind'] for entry in result['files']}, {'image', 'video', 'unsupported'})
        calls = service.files.return_value.list.call_args_list
        self.assertEqual(calls[1].kwargs['pageToken'], 'animals2')
        self.assertEqual(calls[3].kwargs['pageToken'], 'media2')
        self.assertEqual({call[0].split('(')[0].split('.')[0] for call in service.files.return_value.mock_calls}, {'list'})

    def test_missing_and_empty_folders(self):
        with patch.object(drive, 'children', return_value=[]):
            self.assertEqual(media.discover_media(MagicMock(), 'azra'), {'folders': [], 'files': []})
        with patch.object(drive, 'children', side_effect=[[folder()], []]):
            self.assertEqual(media.discover_media(MagicMock(), 'azra'), {'folders': ["Foto's"], 'files': []})

    @patch.object(media, 'MediaIoBaseDownload')
    def test_large_unknown_and_unsupported_files_never_download(self, downloader):
        service = MagicMock()
        for entry in (file(size=1000), file(size=-1), file(size='bad'), file('x.pdf', mime='application/pdf')):
            with self.subTest(entry=entry):
                with self.assertRaises(drive.DriveError):
                    media.download_media(service, entry, 100)
        downloader.assert_not_called()
        service.files.assert_not_called()

    @patch.object(media, 'MediaIoBaseDownload')
    def test_chunked_retrieval_and_actual_download_cap(self, downloader):
        service = MagicMock()

        def initialize(output, request, chunksize):
            result = MagicMock()
            result.next_chunk.side_effect = lambda **kwargs: (output.write(b'abcd'), True)
            return result

        downloader.side_effect = initialize
        self.assertEqual(media.download_media(service, file(size=4), 4), b'abcd')
        self.assertEqual(downloader.call_args.kwargs['chunksize'], 256 * 1024)
        service.files.return_value.get_media.assert_called_with(fileId='photo', supportsAllDrives=True)
        with self.assertRaisesRegex(drive.DriveError, 'exceeded'):
            media.download_media(service, file(size=1), 3)

    @patch.object(media, 'MediaIoBaseDownload')
    def test_safe_download_error(self, downloader):
        downloader.return_value.next_chunk.side_effect = HttpError(Response({'status': 403}), b'PRIVATE')
        with self.assertRaises(drive.DriveError) as caught:
            media.download_media(MagicMock(), file(), 1000)
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_resized_photo_and_invalid_photo(self):
        data = media.thumbnail(picture())
        with Image.open(BytesIO(data)) as image:
            self.assertLessEqual(max(image.size), 640)
            self.assertFalse(image.getexif())
        with self.assertRaises(drive.DriveError):
            media.thumbnail(b'not an image')

    @patch.object(media, 'download_media', return_value=b'video')
    def test_cache_avoids_repeat_and_separates_animals_and_versions(self, download):
        entry = file('clip.mp4', mime='video/mp4', size=5)
        cache = {}
        for _ in range(2):
            self.assertEqual(media.cached_preview(None, 'azra', entry, cache, 100), b'video')
        self.assertEqual(download.call_count, 1)
        media.cached_preview(None, 'other-animal', entry, cache, 100)
        media.cached_preview(None, 'azra', dict(entry, modifiedTime='new'), cache, 100)
        self.assertEqual(download.call_count, 3)
        with self.assertRaises(drive.DriveError):
            media.cached_preview(None, 'azra', entry, cache, 4)

    @patch.object(media, 'download_media', return_value=b'12345')
    def test_cache_expiration_and_memory_budget(self, download):
        cache = {}
        with patch.object(media.time, 'monotonic', return_value=0):
            media.cached_preview(None, 'azra', file('a.mp4', 'a', 'video/mp4', 5), cache, 10, 6)
            media.cached_preview(None, 'azra', file('b.mp4', 'b', 'video/mp4', 5), cache, 10, 6)
            self.assertEqual(len(cache), 1)
        with patch.object(media.time, 'monotonic', return_value=601):
            media.cached_preview(None, 'azra', file('b.mp4', 'b', 'video/mp4', 5), cache, 10, 6)
        self.assertEqual(download.call_count, 3)


@isolated_editorial_storage
class MediaAppTests(unittest.TestCase):
    def test_one_photo_connection_failure_preserves_gallery_text_and_manual_retry(self):
        # Exercise the actual download/error boundary, not a pre-wrapped DriveError.
        self.patches[4].stop()
        self.mocks[3].return_value = {'folders': ['Photos'], 'files': [
            dict(file('failed.jpg', 'failed'), kind='image', folder='Photos'),
            dict(file('good.jpg', 'good'), kind='image', folder='Photos'),
        ]}
        self.mocks[2].return_value.update(other_info='Other note.', updates='05/11/2026: Update.')
        service = self.mocks[0].return_value
        service.files.return_value.get_media.side_effect = lambda **kwargs: MagicMock(file_id=kwargs['fileId'])
        failed = [True]
        attempts = []
        data = picture()

        def initialize(output, request, chunksize):
            attempts.append(request.file_id)
            result = MagicMock()
            if request.file_id == 'failed' and failed[0]:
                def missing_connection(**kwargs):
                    connection = None
                    connection.close()
                result.next_chunk.side_effect = missing_connection
            else:
                result.next_chunk.side_effect = lambda **kwargs: (output.write(data), True)
            return result

        with patch.object(media, 'MediaIoBaseDownload', side_effect=initialize):
            self.select_azra()
            self.assertEqual(len(self.app.get('image')), 1)
            warning = next(item.value for item in self.app.warning if 'failed.jpg' in item.value)
            self.assertIn('Retry failed previews', warning)
            self.assertNotIn('NoneType', warning)
            self.app.text_area(key='animal_history').input('Edited history.').run()
            self.app.checkbox(key='media_choose_azra_good').check().run()
            for _ in range(3):
                self.app.run()
                self.assertFalse(self.app.exception)
            self.assertEqual(attempts, ['failed', 'good'])  # No automatic retry on reruns.
            self.assertEqual(self.app.text_area(key='animal_other_info').value, 'Other note.')
            self.assertEqual(self.app.text_area(key='animal_updates').value, '05/11/2026: Update.')
            failed[0] = False
            self.app.button(key='retry_previews').click().run()
            self.assertEqual(attempts, ['failed', 'good', 'failed'])
            self.assertEqual(len(self.app.get('image')), 2)
            self.assertFalse(self.app.session_state.media_preview_errors)
            self.assertEqual(self.app.text_area(key='animal_history').value, 'Edited history.')
            self.assertEqual(self.app.session_state.media_selected, ['good'])
            self.assertFalse(self.app.exception)

    def test_failed_preview_keeps_valid_analysis_and_draft_generation_available(self):
        import analysis_ui
        import content_ui
        from ai_config import AnalysisSettings
        from content_service import ContentSettings, validate_drafts
        from test_content_service import profile_fixture, bundle_fixture

        self.mocks[4].side_effect = drive.DriveError('The connection to Google Drive was interrupted.')
        profile, bundle = profile_fixture(), bundle_fixture()
        with (patch.object(analysis_ui, 'load_settings', return_value=AnalysisSettings(model='synthetic')),
              patch.object(analysis_ui, 'require_credentials', return_value='synthetic-key'),
              patch.object(analysis_ui.ai_service, 'analyze_animal', return_value=profile) as analyze,
              patch.object(content_ui, 'load_content_settings', return_value=ContentSettings(model='synthetic')),
              patch.object(content_ui, 'generate_drafts', return_value=(bundle,
                  validate_drafts(bundle, profile, 'third_person'), {})) as generate):
            self.select_azra()
            self.assertTrue(self.app.session_state.media_preview_errors)
            # Text-only analysis has valid inputs; the failed photo is unselected.
            self.app.checkbox(key='ai_consent').check().run()
            self.assertFalse(self.app.button(key='analyze_animal').disabled)
            self.app.button(key='analyze_animal').click().run()
            analyze.assert_called_once()
            self.assertEqual(analyze.call_args.args[1], [])
            self.app.button(key='generate_drafts').click().run()
            generate.assert_called_once()
            self.assertFalse(self.app.exception)

    def test_failed_manual_retry_keeps_existing_photo_cache_and_edited_text(self):
        self.select_azra()
        self.app.text_area(key='animal_history').input('Edited history.').run()
        self.mocks[4].side_effect = drive.DriveError('The connection to Google Drive was interrupted.')
        self.app.number_input(key='limit_video_count').set_value(1).run()
        cache = dict(self.app.session_state.media_cache)
        self.mocks[0].side_effect = drive.DriveError('Google login could not be refreshed.')
        self.app.button(key='retry_previews').click().run()
        self.assertEqual(self.app.session_state.media_cache, cache)
        self.assertTrue(self.app.session_state.media_preview_errors)
        self.assertTrue(any('login could not be refreshed' in item.value for item in self.app.error))
        self.assertEqual(self.app.text_area(key='animal_history').value, 'Edited history.')
        self.assertEqual(len(self.app.get('image')), 1)
        self.assertFalse(self.app.exception)

    def test_failed_video_can_retry_without_losing_edits_selections_or_photo_cache(self):
        self.select_azra()
        self.app.text_area(key='animal_history').input('Edited rescue history.').run()
        self.app.checkbox(key='media_choose_azra_photo').check().run()
        self.mocks[4].side_effect = drive.DriveError('The connection to Google Drive was interrupted.')
        self.app.number_input(key='limit_video_count').set_value(1).run()
        self.assertTrue(self.app.session_state.media_preview_errors)
        self.assertTrue(any('Failed previews: :violet[**1**]' in box.value for box in self.app.info))
        self.mocks[4].side_effect = None
        self.app.button(key='retry_previews').click().run()
        self.assertEqual(len(self.app.get('video')), 1)
        self.assertEqual(self.app.text_area(key='animal_history').value, 'Edited rescue history.')
        self.assertEqual(self.app.session_state.media_selected, ['photo'])
        self.assertEqual(self.mocks[4].call_count, 3)  # Photo once, video failure, video retry.
        self.assertFalse(self.app.session_state.media_preview_errors)
        self.assertFalse(self.app.exception)

    def test_highlighted_summary_distinguishes_count_and_size_exclusions(self):
        self.mocks[3].return_value = {'folders': ['Photos'], 'files': [
            dict(file('a.jpg', 'a'), kind='image', folder='Photos'),
            dict(file('b.jpg', 'b'), kind='image', folder='Photos'),
            dict(file('c.jpg', 'c', size=9 * media.MIB), kind='image', folder='Photos'),
        ]}
        self.app.number_input(key='limit_photo_count').set_value(1).run()
        self.select_azra()
        boxes = [box.value for box in self.app.info if 'Photos preview summary' in box.value]
        self.assertEqual(len(boxes), 1)
        self.assertIn('Left out by preview count/checkbox choice: :violet[**1**]', boxes[0])
        self.assertIn('Left out by file size: :violet[**1**]', boxes[0])
        self.assertFalse(self.app.exception)

    def test_additional_fields_load_edit_and_clear_on_animal_change(self):
        self.mocks[2].return_value.update(other_info='An extra note.', updates='04/11/2026: New update.')
        self.select_azra()
        self.assertEqual(self.app.text_area(key='animal_other_info').value, 'An extra note.')
        self.assertEqual(self.app.text_area(key='animal_updates').value, '04/11/2026: New update.')
        self.app.button(key='generate_drafts').click().run()
        self.app.text_area(key='animal_other_info').input('Edited extra note.').run()
        self.assertNotIn('website_text', self.app.session_state)
        self.app.button(key='generate_drafts').click().run()
        self.app.text_area(key='animal_updates').input('05/11/2026: Edited update.').run()
        self.assertNotIn('facebook_text', self.app.session_state)
        self.app.button(key='generate_drafts').click().run()
        self.assertEqual(self.app.text_area(key='animal_updates').value, '05/11/2026: Edited update.')
        self.mocks[2].return_value = {'history': 'Luna story.', 'animal': 'Luna', 'document': 'Luna.docx', 'media': []}
        self.mocks[3].return_value = {'folders': [], 'files': []}
        self.app.selectbox(key='drive_animal').select('luna').run()
        self.assertEqual(self.app.text_area(key='animal_other_info').value, '')
        self.assertEqual(self.app.text_area(key='animal_updates').value, '')
        self.assertFalse(self.app.exception)

    def test_manual_upload_imports_optional_rows_and_failure_clears_them(self):
        from docx import Document
        document = Document()
        table = document.add_table(rows=3, cols=2)
        for row, (label, value) in zip(table.rows, [('History', 'Manual story.'),
                                                  ('Other info', 'Manual note.'),
                                                  ('Updates (please note the date of the update)', '04-11-2026: Manual update.')]):
            row.cells[0].text = label
            row.cells[1].text = value
        output = BytesIO()
        document.save(output)
        self.app.get('file_uploader')[0].upload('manual.docx', output.getvalue()).run()
        self.assertEqual(self.app.text_area(key='animal_other_info').value, 'Manual note.')
        self.assertEqual(self.app.text_area(key='animal_updates').value, '04-11-2026: Manual update.')
        self.app.get('file_uploader')[0].upload('bad.docx', b'not a docx').run()
        for key in ('animal_history', 'animal_other_info', 'animal_updates'):
            self.assertEqual(self.app.text_area(key=key).value, '')
        self.assertFalse(self.app.exception)

    def setUp(self):
        self.patches = [
            patch.object(drive, 'connect', return_value=MagicMock()),
            patch.object(drive, 'discover_folders', return_value=[folder('Azra', 'azra'), folder('Luna', 'luna')]),
            patch.object(drive, 'load_animal', return_value={'history': 'Azra was rescued.', 'animal': 'Azra', 'document': 'Kenmerkenlijst.docx', 'media': ["Foto's"]}),
            patch.object(media, 'discover_media', return_value={'folders': ["Foto's"], 'files': [
                dict(file(), kind='image', folder="Foto's"),
                dict(file('clip.mov', 'clip', 'video/quicktime'), kind='video', folder="Foto's"),
            ]}),
            patch.object(media, 'download_media', return_value=picture()),
        ]
        self.mocks = [patcher.start() for patcher in self.patches]
        for patcher in self.patches:
            self.addCleanup(patcher.stop)
        self.app = AppTest.from_file(str(drive.ROOT / 'app.py'), default_timeout=10).run()
        self.app.button(key='load_drive_folders').click().run()

    def select_azra(self):
        self.app.selectbox(key='drive_animal').select('azra').run()
        self.assertFalse(self.app.exception)

    def test_photos_auto_preview_and_video_is_lazy(self):
        self.select_azra()
        self.assertEqual(self.mocks[4].call_count, 1)
        self.assertEqual(len(self.app.get('image')), 1)
        self.assertEqual(len(self.app.get('video')), 0)
        self.app.number_input(key='limit_video_count').set_value(1).run()
        self.assertFalse(self.app.exception)
        self.assertEqual(len(self.app.get('video')), 1)
        self.app.button(key='generate_drafts').click().run()
        self.assertEqual(self.mocks[4].call_count, 2)
        self.mocks[3].assert_called_once()
        self.assertTrue(any('Profile' in error.value for error in self.app.error))

    def test_independent_counts_auto_apply_and_keep_analysis_choices(self):
        self.mocks[3].return_value = {'folders': ['Photos'], 'files': [
            dict(file(f'{i}.jpg', str(i)), kind='image', folder='Photos') for i in range(8)
        ] + [dict(file('clip.mp4', 'clip', 'video/mp4'), kind='video', folder='Photos')]}
        self.select_azra()
        self.assertEqual(len(self.app.get('image')), 6)
        self.assertFalse(self.app.get('video'))
        self.app.checkbox(key='media_choose_azra_0').check().run()
        self.app.number_input(key='limit_photo_count').set_value(8).run()
        self.assertEqual(len(self.app.get('image')), 8)
        self.app.number_input(key='limit_video_count').set_value(1).run()
        self.assertEqual(len(self.app.get('video')), 1)
        self.assertEqual(len(self.app.get('image')), 8)
        self.app.number_input(key='limit_photo_count').set_value(0).run()
        self.assertFalse(self.app.get('image'))
        self.assertEqual(len(self.app.get('video')), 1)
        self.assertEqual(self.app.session_state.media_selected, ['0'])
        self.assertFalse(self.app.exception)

    def test_size_summary_and_limit_increase_adds_eligible_previews(self):
        self.mocks[3].return_value = {'folders': ['Photos'], 'files': [
            dict(file('large.jpg', 'large', size=9 * media.MIB), kind='image', folder='Photos'),
            dict(file('small.jpg', 'small'), kind='image', folder='Photos'),
            dict(file('unknown.jpg', 'unknown', size=-1), kind='image', folder='Photos'),
        ]}
        self.select_azra()
        self.assertEqual(len(self.app.get('image')), 1)
        self.assertTrue(any('1 photos excluded' in entry.value and '9 MB' in entry.value for entry in self.app.info))
        self.assertTrue(any('1 with unknown size' in entry.value for entry in self.app.caption))
        self.app.number_input(key='limit_image').set_value(9).run()
        self.assertEqual(len(self.app.get('image')), 2)
        self.assertTrue(any('0 excluded by size' in entry.value for entry in self.app.caption))
        self.assertFalse(self.app.exception)

    def test_selection_survives_paging_and_manual_upload_clears_it(self):
        self.mocks[3].return_value = {'folders': ['Photos'], 'files': [
            dict(file(f'{index}.jpg', str(index), size=9 * media.MIB), kind='image', folder='Photos')
            for index in range(15)
        ]}
        self.select_azra()
        self.app.checkbox(key='media_choose_azra_0').check().run()
        self.app.selectbox(key='image_page').select(1).run()
        self.app.selectbox(key='image_page').select(0).run()
        self.assertTrue(self.app.checkbox(key='media_choose_azra_0').value)
        self.assertEqual(self.app.session_state.media_selected, ['0'])
        from docx import Document
        document = Document()
        document.add_paragraph('History: Manual rescue story.')
        output = BytesIO()
        document.save(output)
        self.app.get('file_uploader')[0].upload('manual.docx', output.getvalue()).run()
        self.assertEqual(self.app.text_area[0].value, 'Manual rescue story.')
        self.assertNotIn('media_selected', self.app.session_state)
        self.assertNotIn('media_animal', self.app.session_state)
        self.assertFalse([box for box in self.app.checkbox if box.key.startswith('media_')])
        self.assertFalse(self.app.exception)

    def test_animal_change_clears_cache_selection_and_old_history(self):
        self.select_azra()
        self.app.checkbox(key='media_choose_azra_photo').check().run()
        self.assertEqual(self.app.session_state.media_selected, ['photo'])
        self.mocks[2].return_value = {'history': 'Luna was rescued.', 'animal': 'Luna', 'document': 'Luna.docx', 'media': []}
        self.mocks[3].return_value = {'folders': [], 'files': []}
        self.app.selectbox(key='drive_animal').select('luna').run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.text_area[0].value, 'Luna was rescued.')
        self.assertNotIn('media_selected', self.app.session_state)
        self.assertNotIn('media_cache', self.app.session_state)
        self.assertFalse([box for box in self.app.checkbox if box.key.startswith('media_')])

    def test_animal_switch_and_pending_checkbox_event_do_not_crash(self):
        self.select_azra()
        self.app.checkbox(key='media_choose_azra_photo').check()
        self.mocks[3].return_value = {'folders': [], 'files': []}
        self.app.selectbox(key='drive_animal').select('luna').run()
        self.assertFalse(self.app.exception)
        self.assertNotIn('media_selected', self.app.session_state)

    def test_refresh_and_pending_checkbox_event_do_not_crash(self):
        self.select_azra()
        self.app.checkbox(key='media_choose_azra_photo').check()
        self.app.button(key='refresh_media').click().run()
        self.assertFalse(self.app.exception)
        self.assertNotIn('media_selected', self.app.session_state)

    def test_unsupported_empty_and_missing_folder_messages(self):
        for catalog, message in [
            ({'folders': [], 'files': []}, 'No Foto'),
            ({'folders': ["Foto's"], 'files': []}, 'folder is empty'),
        ]:
            self.mocks[3].return_value = catalog
            self.select_azra()
            self.app.button(key='refresh_media').click().run()
            self.assertTrue(any(message in entry.value for entry in self.app.info))
        self.mocks[3].return_value = {'folders': ['Photos'], 'files': [dict(file('x.heic', mime='image/heic'), kind='unsupported', folder='Photos')]}
        self.app.button(key='refresh_media').click().run()
        self.assertTrue(any('Unsupported files' in expander.label for expander in self.app.expander))
        self.mocks[4].assert_not_called()

    def test_media_still_lists_when_history_missing_and_refresh_keeps_edits(self):
        self.mocks[2].side_effect = HistoryError('History was not found.')
        self.select_azra()
        self.assertTrue(self.app.checkbox)
        self.app.text_area[0].input('Manually edited history.').run()
        self.app.button(key='refresh_media').click().run()
        self.assertEqual(self.app.text_area[0].value, 'Manually edited history.')

    def test_sidebar_limit_prevents_large_preview_and_count_is_bounded(self):
        self.mocks[3].return_value = {'folders': ['Photos'], 'files': [
            dict(file(f'{index}.jpg', str(index), size=9 * media.MIB), kind='image', folder='Photos')
            for index in range(15)
        ]}
        self.select_azra()
        self.mocks[4].assert_not_called()
        self.assertEqual(len([box for box in self.app.checkbox if box.key.startswith('media_')]), 12)
        # Twelve media choices exist, with no media preview toggles; AI consent is separate.
        self.assertEqual(self.app.selectbox(key='image_page').options, ['1 / 2', '2 / 2'])
        self.app.number_input(key='limit_image').set_value(10).run()
        self.assertEqual(self.mocks[4].call_count, 6)
        self.assertFalse(self.app.exception)


if __name__ == '__main__':
    unittest.main()
