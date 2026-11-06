"""Real Google request/downloader classes; synthetic credentials and mocked HTTP only."""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
from threading import Barrier
import unittest
from unittest.mock import MagicMock, patch

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from httplib2 import HttpLib2Error, Response

import drive_client as drive
import drive_media as media
import test_drive_media as fixtures


def missing_connection():
    try:
        connection = None
        connection.close()
    except AttributeError as error:
        return error


class TransportErrorTests(unittest.TestCase):
    def test_missing_connection_becomes_safe_retryable_error(self):
        request = MagicMock()
        request.execute.side_effect = missing_connection()
        with self.assertRaisesRegex(drive.DriveError, 'interrupted'):
            drive._call(request)

    def test_closed_connection_and_protocol_errors_are_safe(self):
        for error in (HTTPException('PRIVATE TOKEN'), HttpLib2Error('PRIVATE TOKEN'),
                      ValueError('read of closed file'), OSError('PRIVATE TOKEN')):
            request = MagicMock()
            request.execute.side_effect = error
            with self.subTest(error=type(error)):
                with self.assertRaises(drive.DriveError) as caught:
                    drive._call(request)
                self.assertNotIn('PRIVATE', str(caught.exception))

    def test_unrelated_programming_errors_are_not_hidden(self):
        for error in (AttributeError('incorrect attribute'), ValueError('incorrect value')):
            request = MagicMock()
            request.execute.side_effect = error
            with self.assertRaises(type(error)) as caught:
                drive._call(request)
            self.assertIs(caught.exception, error)


class IsolatedTransportTests(unittest.TestCase):
    def setUp(self):
        self.transports = []
        self.payload = fixtures.picture()
        self.response = Response({'status': '200', 'content-length': str(len(self.payload))})

        def new_http():
            http = MagicMock()
            http.request.return_value = (self.response, self.payload)
            self.transports.append(http)
            return http

        patcher = patch.object(drive, 'build_http', side_effect=new_http)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.credentials = Credentials(token='SYNTHETIC_TOKEN')
        self.base_http = MagicMock()
        self.base_http.credentials = self.credentials
        # Static bundled discovery: no credential files or network are accessed.
        self.service = build('drive', 'v3', http=self.base_http, cache_discovery=False,
                             static_discovery=True,
                             requestBuilder=drive.isolated_request_builder(self.credentials))
        files = self.service.files()
        patcher = patch.object(self.service, 'files', return_value=files)
        patcher.start()
        self.addCleanup(patcher.stop)

    def download(self):
        return media.download_media(self.service, fixtures.file(size=len(self.payload)), media.MIB)

    def test_successful_photo_authenticated_and_transport_closed(self):
        self.assertEqual(self.download(), self.payload)
        http, = self.transports
        http.close.assert_called_once()
        self.assertEqual(http.request.call_args.kwargs['headers']['authorization'],
                         'Bearer SYNTHETIC_TOKEN')
        self.base_http.request.assert_not_called()

    def test_all_chunks_use_one_private_transport_until_finished(self):
        payload = b'x' * 300_000

        def response(uri, method, **kwargs):
            http.close.assert_not_called()
            first = 0 if http.request.call_count == 1 else 256 * 1024
            data = payload[first:first + 256 * 1024]
            return Response({'status': '206',
                             'content-range': f'bytes {first}-{first + len(data) - 1}/{len(payload)}'}), data

        request = self.service.files().get_media(fileId='synthetic', supportsAllDrives=True)
        http, = self.transports
        http.request.side_effect = response
        with patch.object(self.service.files(), 'get_media', return_value=request):
            self.assertEqual(media.download_media(self.service, fixtures.file(size=len(payload)), media.MIB), payload)
        self.assertEqual(http.request.call_count, 2)
        http.close.assert_called_once()

    def test_metadata_and_document_requests_also_own_and_close_transports(self):
        self.payload = b'{"files": []}'
        self.response = Response({'status': '200', 'content-type': 'application/json'})
        self.assertEqual(drive.children(self.service, 'synthetic'), [])
        self.assertEqual(drive._call(self.service.files().get_media(fileId='document')), self.payload)
        self.assertEqual(len(self.transports), 2)
        self.assertIsNot(self.transports[0], self.transports[1])
        for http in self.transports:
            http.close.assert_called_once()

    def test_failure_then_retry_uses_fresh_connection_without_partial_cache(self):
        request = self.service.files().get_media(fileId='synthetic')
        first, = self.transports
        first.request.side_effect = missing_connection()
        entry = fixtures.file(size=len(self.payload))
        cache = {}
        with patch.object(self.service.files(), 'get_media', return_value=request):
            with self.assertRaises(drive.DriveError):
                media.cached_preview(self.service, 'azra', entry, cache, media.MIB)
        self.assertEqual(cache, {})
        first.close.assert_called_once()
        self.assertTrue(media.cached_preview(self.service, 'azra', entry, cache, media.MIB))
        self.assertEqual(len(self.transports), 2)
        self.transports[1].close.assert_called_once()
        self.assertEqual(len(cache), 1)

    def test_http_error_is_redacted_and_transport_closed(self):
        self.response = Response({'status': '403'})
        self.payload = b'PRIVATE TOKEN SYNTHETIC_TOKEN'
        with self.assertRaises(drive.DriveError) as caught:
            self.download()
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertNotIn('SYNTHETIC_TOKEN', str(caught.exception))
        self.transports[0].close.assert_called_once()

    def test_downloader_initialization_failure_closes_transport(self):
        with patch.object(media, 'MediaIoBaseDownload', side_effect=HTTPException('PRIVATE TOKEN')):
            with self.assertRaises(drive.DriveError):
                self.download()
        self.transports[0].close.assert_called_once()

    def test_limit_failure_closes_transport_and_never_caches_partial_bytes(self):
        cache = {}
        with self.assertRaisesRegex(drive.DriveError, 'exceeded'):
            media.cached_preview(self.service, 'azra', fixtures.file(size=1), cache, 2)
        self.assertEqual(cache, {})
        self.transports[0].close.assert_called_once()

    def test_cached_photo_avoids_connections_until_expired_or_modified(self):
        cache = {}
        entry = fixtures.file(size=len(self.payload))
        with patch.object(media.time, 'monotonic', return_value=0):
            preview = media.cached_preview(self.service, 'azra', entry, cache, media.MIB)
            for _ in range(4):
                self.assertEqual(media.cached_preview(self.service, 'azra', entry, cache, media.MIB), preview)
        self.assertEqual(len(self.transports), 1)
        with patch.object(media.time, 'monotonic', return_value=media.CACHE_TTL + 1):
            media.cached_preview(self.service, 'azra', entry, cache, media.MIB)
            media.cached_preview(self.service, 'azra', dict(entry, modifiedTime='new'), cache, media.MIB)
        self.assertEqual(len(self.transports), 3)

    def test_concurrent_downloads_on_same_service_use_distinct_transports(self):
        barrier = Barrier(4)
        transports = []

        def response(*args, **kwargs):
            barrier.wait(timeout=5)
            return self.response, self.payload

        with patch.object(drive, 'build_http') as factory:
            def new_http():
                http = MagicMock()
                http.request.side_effect = response
                transports.append(http)
                return http
            factory.side_effect = new_http
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda _: self.download(), range(4)))
        self.assertEqual(results, [self.payload] * 4)
        self.assertEqual(factory.call_count, 4)
        self.assertEqual(len({id(http) for http in transports}), 4)
        for http in transports:
            http.request.assert_called_once()
            http.close.assert_called_once()
        self.base_http.request.assert_not_called()

    def test_transport_close_failure_is_safe(self):
        request = self.service.files().get_media(fileId='synthetic')
        self.transports[0].close.side_effect = missing_connection()
        with self.assertRaises(drive.DriveError):
            drive._call(request)


if __name__ == '__main__':
    unittest.main()
