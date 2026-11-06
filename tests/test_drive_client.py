"""Drive and Desktop OAuth tests; no network or real credential files."""
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from threading import Event

from docx import Document
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from httplib2 import Response
from streamlit.testing.v1 import AppTest

import drive_client as drive
import drive_auth_job as auth_job
from requests.exceptions import ConnectionError as RequestsConnectionError
from history_extractor import HistoryError
from editorial_test_support import isolated_editorial_storage


def item(name, mime=drive.FOLDER, id='animal'):
    return dict(id=id, name=name, mimeType=mime)


def docx(*paragraphs):
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


class DriveTests(unittest.TestCase):
    def test_transport_and_auth_failures_have_distinct_safe_messages(self):
        from httplib2 import HttpLib2Error
        request = MagicMock()
        for error, expected in [(RefreshError('PRIVATE'), 'login could not be refreshed'),
                                (HttpLib2Error('PRIVATE'), 'connection to Google Drive was interrupted'),
                                (PermissionError(13, 'PRIVATE'), 'Python network access was denied')]:
            request.execute.side_effect = error
            with self.assertRaises(drive.DriveError) as caught:
                drive._call(request)
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn('PRIVATE', str(caught.exception))

    def test_network_denial_is_specific_without_exposing_details(self):
        error = RequestsConnectionError(OSError(PermissionError(13, 'PRIVATE AUTH DATA')))
        message = drive.network_error(error)
        self.assertIn('Python network access was denied', message)
        self.assertIn('PowerShell', message)
        self.assertNotIn('PRIVATE', message)

    def test_network_failure_without_permission_error(self):
        self.assertIn('Python could not reach Google', drive.network_error(RequestsConnectionError('PRIVATE')))

    def setUp(self):
        self.service = MagicMock()
        self.files = self.service.files.return_value

    def test_folder_urls_and_invalid_input(self):
        self.assertEqual(drive.folder_id('https://drive.google.com/drive/folders/synthetic_folder_id_for_parser_test_123'), 'synthetic_folder_id_for_parser_test_123')
        self.assertEqual(drive.folder_id('https://drive.google.com/drive/folders/abc_123?usp=sharing'), 'abc_123')
        self.assertEqual(drive.folder_id('https://drive.google.com/open?id=abc'), 'abc')
        for value in ('', "bad'id", 'https://evil.example/folders/abc'):
            with self.assertRaises(drive.DriveError):
                drive.folder_id(value)

    def test_discovery_pagination_immediate_folders_only(self):
        self.files.get.return_value.execute.return_value = dict(mimeType=drive.FOLDER)
        self.files.list.return_value.execute.side_effect = [
            {'files': [item('Zara'), item('notes', 'text/plain')], 'nextPageToken': 'next'},
            {'files': [item('Azra', id='azra')]},
        ]
        result = drive.discover_folders(self.service, 'main')
        self.assertEqual([entry['name'] for entry in result], ['Azra', 'Zara'])
        calls = self.files.list.call_args_list
        self.assertEqual(calls[1].kwargs['pageToken'], 'next')
        self.assertEqual(calls[0].kwargs['q'], "'main' in parents and trashed = false")

    def test_empty_main_folder(self):
        self.files.get.return_value.execute.return_value = dict(mimeType=drive.FOLDER)
        self.files.list.return_value.execute.return_value = {'files': []}
        self.assertEqual(drive.discover_folders(self.service, 'main'), [])

    def test_nonfolder(self):
        self.files.get.return_value.execute.return_value = dict(mimeType='text/plain')
        with self.assertRaises(drive.DriveError):
            drive.discover_folders(self.service, 'main')

    def test_docx_retrieval_history_only_media_and_no_writes(self):
        self.files.list.return_value.execute.side_effect = [
            {'files': [item('KENMERKENLIJST Azra.DOCX', drive.DOCX, 'document')], 'nextPageToken': 'page2'},
            {'files': [item("Foto’s"), item('unrelated.pdf', 'application/pdf')]},
        ]
        self.files.get_media.return_value.execute.return_value = docx(
            'Name: Azra', 'History', 'Azra was rescued.', 'Text for website', 'PRIVATE WEBSITE',
            'Text for Facebook', 'PRIVATE FACEBOOK')
        result = drive.load_animal(self.service, item('Azra'))
        self.assertEqual(result, dict(history='Azra was rescued.', other_info='', updates='', animal='Azra',
                                     document='KENMERKENLIJST Azra.DOCX', media=['Foto’s']))
        self.files.get_media.assert_called_once_with(fileId='document', supportsAllDrives=True)
        self.assertEqual({call[0].split('(')[0].split('.')[0] for call in self.files.mock_calls},
                         {'list', 'get_media'})

    def test_media_names(self):
        for name in ("Foto's", 'Fotos', 'FOTO’S', 'Photos', 'Media', 'Video’s'):
            self.assertTrue(drive.is_media_folder(item(name)))
        self.assertFalse(drive.is_media_folder(item('Fotos', 'text/plain')))

    def test_missing_ambiguous_and_unsupported_documents(self):
        for files in ([], [item('notes.docx', drive.DOCX)],
                      [item('Kenmerkenlijst.docx', drive.DOCX)] * 2,
                      [item('Kenmerkenlijst', 'application/vnd.google-apps.document')]):
            with self.subTest(files=files):
                self.files.list.return_value.execute.return_value = {'files': files}
                with self.assertRaises(drive.DriveError):
                    drive.load_animal(self.service, item('Azra'))
        self.files.get_media.assert_not_called()

    def test_empty_missing_invalid_history(self):
        self.files.list.return_value.execute.return_value = {'files': [item('Kenmerkenlijst.docx', drive.DOCX)]}
        for data in (docx('History', 'Text for website', 'PRIVATE'), docx('Name: Azra'), b'not docx'):
            self.files.get_media.return_value.execute.return_value = data
            with self.assertRaises(HistoryError):
                drive.load_animal(self.service, item('Azra'))

    def test_safe_http_errors(self):
        for status in (401, 403, 404, 429, 500):
            self.files.list.return_value.execute.side_effect = HttpError(Response({'status': status}), b'PRIVATE SERVER DETAILS')
            with self.assertRaises(drive.DriveError) as caught:
                drive.children(self.service, 'main')
            self.assertNotIn('PRIVATE', str(caught.exception))

    def test_authentication_and_network_errors(self):
        for error in (RefreshError('PRIVATE TOKEN'), OSError('PRIVATE')):
            self.files.list.return_value.execute.side_effect = error
            with self.assertRaises(drive.DriveError):
                drive.children(self.service, 'main')


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = Path(self.temp.name) / 'client.json'
        self.token = Path(self.temp.name) / 'state' / 'token.json'
        for name, value in [('CLIENT_FILE', self.client), ('TOKEN_FILE', self.token)]:
            patcher = patch.object(drive, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def credentials(self):
        credentials = MagicMock(valid=True, scopes=drive.SCOPES, granted_scopes=drive.SCOPES)
        credentials.to_json.return_value = '{"fake": true}'
        return credentials

    def test_missing_credentials_safe(self):
        with self.assertRaisesRegex(drive.DriveError, 'secrets/google_client.json'):
            drive.connect()

    @patch.object(drive.InstalledAppFlow, 'from_client_secrets_file')
    def test_noninteractive_missing_login_does_not_open_oauth(self, flow):
        with self.assertRaisesRegex(drive.DriveError, 'No usable saved Google login'):
            drive.connect(interactive=False)
        flow.assert_not_called()

    @patch.object(drive, 'build')
    @patch.object(drive.InstalledAppFlow, 'from_client_secrets_file')
    @patch.object(drive.Credentials, 'from_authorized_user_file')
    def test_noninteractive_reloads_saved_login(self, read, flow, build):
        self.token.parent.mkdir()
        self.token.touch()
        read.return_value = self.credentials()
        self.assertEqual(drive.connect(interactive=False), build.return_value)
        read.assert_called_once_with(str(self.token))
        flow.assert_not_called()
        self.assertTrue(callable(build.call_args.kwargs['requestBuilder']))

    @patch.object(drive.InstalledAppFlow, 'from_client_secrets_file')
    def test_signin_timeout_is_safe_and_does_not_save_token(self, flow):
        self.client.touch()
        flow.return_value.run_local_server.side_effect = drive.WSGITimeoutError('PRIVATE DETAILS')
        with self.assertRaisesRegex(drive.DriveError, 'Google sign-in was not completed') as caught:
            drive.connect()
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertFalse(self.token.exists())
        self.assertEqual(flow.return_value.run_local_server.call_args.kwargs['timeout_seconds'], 600)

    @patch.object(drive, 'build')
    @patch.object(drive.InstalledAppFlow, 'from_client_secrets_file')
    def test_desktop_flow_exact_scope_and_local_token(self, flow, build):
        self.client.touch()  # Empty fake file; mock library never reads it.
        flow.return_value.run_local_server.return_value = self.credentials()
        drive.connect()
        flow.assert_called_once_with(str(self.client), drive.SCOPES)
        self.assertEqual(drive.SCOPES, ['https://www.googleapis.com/auth/drive.readonly'])
        self.assertEqual(flow.return_value.run_local_server.call_args.kwargs['host'], 'localhost')
        self.assertTrue(self.token.exists())
        build.assert_called_once()

    @patch.object(drive, 'build')
    @patch.object(drive.Credentials, 'from_authorized_user_file')
    def test_cached_credentials_and_refresh(self, read, build):
        self.token.parent.mkdir()
        self.token.touch()
        credentials = self.credentials()
        read.return_value = credentials
        drive.connect()
        credentials.refresh.assert_not_called()
        credentials.valid = False
        credentials.refresh.side_effect = lambda request: setattr(credentials, 'valid', True)
        drive.connect()
        credentials.refresh.assert_called_once()

    @patch.object(drive.Credentials, 'from_authorized_user_file')
    def test_revoked_and_broader_tokens_require_reconnect(self, read):
        self.token.parent.mkdir()
        self.token.touch()
        credentials = self.credentials()
        read.return_value = credentials
        credentials.valid = False
        credentials.refresh.side_effect = RefreshError('SECRET')
        with self.assertRaises(drive.DriveError):
            drive.connect()
        credentials.valid = True
        credentials.scopes = ['https://www.googleapis.com/auth/drive']
        with self.assertRaises(drive.DriveError):
            drive.connect()


@isolated_editorial_storage
class DriveAppTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('drive_media.discover_media', return_value={'folders': [], 'files': []})
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch.object(auth_job, 'start_connection')
    def test_connect_starts_independent_job_without_blocking_app(self, start):
        app = AppTest.from_file(str(drive.ROOT / 'app.py')).run()
        app.text_input(key='drive_url').input('https://drive.google.com/drive/folders/synthetic_folder_id_for_parser_test_123').run()
        app.button(key='connect_drive').click().run()
        start.assert_called_once()
        self.assertFalse(app.exception)
        self.assertTrue(app.button(key='load_drive_folders'))

    @patch.object(auth_job, 'connection_status', return_value={'status': 'error', 'message': 'Google connection could not finish.'})
    @patch.object(drive, 'connect')
    def test_background_error_survives_new_session(self, connect, status):
        app = AppTest.from_file(str(drive.ROOT / 'app.py')).run()
        app.button(key='load_drive_folders').click().run()
        connect.assert_not_called()
        self.assertIn('Google connection could not finish', app.error[0].value)
        self.assertFalse(app.exception)

    @patch.object(drive, 'discover_folders')
    @patch.object(drive, 'connect')
    def test_new_session_loads_saved_login_and_displays_dropdown(self, connect, discover):
        discover.return_value = [item('Azra', id='azra')]
        app = AppTest.from_file(str(drive.ROOT / 'app.py')).run()
        self.assertEqual(len(app.selectbox), 0)
        app.button(key='load_drive_folders').click().run()
        connect.assert_called_once_with(interactive=False)
        self.assertEqual(app.selectbox[0].options, ['Azra'])
        self.assertIn('Folder access confirmed', app.success[0].value)
        self.assertFalse(app.exception)

    @patch.object(drive, 'discover_folders', side_effect=drive.DriveError('Folder or document not found, or your Google account cannot access it.'))
    @patch.object(drive, 'connect')
    def test_authenticated_but_folder_inaccessible_is_not_success(self, connect, discover):
        app = AppTest.from_file(str(drive.ROOT / 'app.py')).run()
        app.button(key='load_drive_folders').click().run()
        self.assertFalse(app.success)
        self.assertFalse(app.selectbox)
        self.assertIn('cannot access', app.error[0].value)
        self.assertFalse(app.exception)

    @patch.object(drive, 'discover_folders', return_value=[])
    @patch.object(drive, 'connect')
    def test_authenticated_empty_folder_has_clear_status(self, connect, discover):
        app = AppTest.from_file(str(drive.ROOT / 'app.py')).run()
        app.button(key='load_drive_folders').click().run()
        self.assertIn('Found 0', app.success[0].value)
        self.assertIn('No immediate animal', app.warning[0].value)
        self.assertFalse(app.exception)

    @patch.object(drive, 'load_animal')
    @patch.object(drive, 'discover_folders')
    @patch.object(drive, 'connect')
    def test_select_edit_switch_failure_and_manual_upload(self, connect, discover, load):
        discover.return_value = [item('Azra', id='azra'), item('Empty', id='empty')]
        load.return_value = dict(history='Azra was rescued.', animal='Azra', document='Kenmerkenlijst Azra.docx', media=['Fotos'])
        app = AppTest.from_file(str(drive.ROOT / 'app.py')).run()
        connect.assert_not_called()
        app.button(key='load_drive_folders').click().run()
        app.selectbox(key='drive_animal').select('azra').run()
        self.assertFalse(app.exception)
        self.assertEqual(app.text_area[0].value, 'Azra was rescued.')
        app.text_area[0].input('Edited story.').run()
        app.button(key='generate_drafts').click().run()
        self.assertEqual(app.text_area[0].value, 'Edited story.')
        load.assert_called_once()
        load.side_effect = drive.DriveError('No Kenmerkenlijst document was found.')
        app.selectbox(key='drive_animal').select('empty').run()
        self.assertEqual(app.text_area[0].value, '')
        self.assertNotIn('website_text', app.session_state)
        self.assertNotIn('drive_source', app.session_state)
        app.get('file_uploader')[0].upload('manual.docx', docx('History', 'Manual story.')).run()
        self.assertEqual(app.text_area[0].value, 'Manual story.')
        self.assertFalse(app.exception)

    @patch.object(drive, 'connect', side_effect=drive.DriveError('Could not connect.'))
    def test_connection_failure_manual_still_works(self, connect):
        app = AppTest.from_file(str(drive.ROOT / 'app.py')).run()
        app.button(key='load_drive_folders').click().run()
        self.assertFalse(app.exception)
        self.assertIn('Could not connect', app.error[0].value)
        app.text_area[0].input('Manual story.').run()
        app.button(key='generate_drafts').click().run()
        self.assertEqual(app.text_area[0].value, 'Manual story.')
        self.assertTrue(any('Profile' in error.value for error in app.error))


@isolated_editorial_storage
class BackgroundOAuthTests(unittest.TestCase):
    def setUp(self):
        with auth_job._lock:
            auth_job._state.update(status='idle', message='')

    def tearDown(self):
        with auth_job._lock:
            auth_job._state.update(status='idle', message='')

    @patch.object(drive, 'connect')
    def test_worker_survives_app_session_change_and_prevents_duplicate_login(self, connect):
        entered, release, finished = Event(), Event(), Event()
        original = auth_job._authenticate

        def wait_for_consent(**kwargs):
            entered.set()
            if not release.wait(5):
                raise drive.DriveError('Test timed out')

        def worker():
            try:
                original()
            finally:
                finished.set()

        connect.side_effect = wait_for_consent
        with patch.object(auth_job, '_authenticate', worker):
            auth_job.start_connection()
            try:
                self.assertTrue(entered.wait(2))
                auth_job.start_connection()
                connect.assert_called_once()
                app = AppTest.from_file(str(drive.ROOT / 'app.py')).run()
                self.assertTrue(any('in progress' in message.value for message in app.info))
            finally:
                release.set()
                self.assertTrue(finished.wait(3))
        self.assertEqual(auth_job.connection_status()['status'], 'ready')
        with patch.object(drive, 'discover_folders', return_value=[item('Azra')]):
            app.button(key='load_drive_folders').click().run()
            self.assertEqual(app.selectbox[0].options, ['Azra'])
            self.assertFalse(app.exception)

    @patch.object(drive, 'connect', side_effect=RuntimeError('PRIVATE TOKEN CONTENT'))
    def test_worker_never_exposes_unexpected_error_details(self, connect):
        auth_job._authenticate()
        status = auth_job.connection_status()
        self.assertEqual(status['status'], 'error')
        self.assertNotIn('PRIVATE', status['message'])


if __name__ == '__main__':
    unittest.main()
