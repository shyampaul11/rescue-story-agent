"""Local Desktop OAuth and read-only Drive access. Never log API exceptions/secrets."""

from pathlib import Path
from http.client import HTTPException
import re
import unicodedata
from urllib.parse import urlparse, parse_qs

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest, build_http
from google_auth_httplib2 import AuthorizedHttp
from httplib2 import HttpLib2Error
from oauthlib.oauth2 import OAuth2Error
from requests.exceptions import RequestException

from history_extractor import extract_animal_info

ROOT = Path(__file__).resolve().parent
CLIENT_FILE = ROOT / 'secrets' / 'google_client.json'
TOKEN_FILE = ROOT / 'local_state' / 'google_token.json'
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
FOLDER = 'application/vnd.google-apps.folder'
DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'


class DriveError(ValueError):
    """Safe, user-facing error without raw server or credential details."""


class IsolatedHttpRequest(HttpRequest):
    """One operation owns this transport; never share it with another request."""

    def close(self):
        if not getattr(self, '_transport_closed', False):
            self._transport_closed = True
            self.http.close()

    def execute(self, *args, **kwargs):
        if getattr(self, '_transport_closed', False):
            raise DriveError('This Drive request has finished. Retry the operation for a fresh connection.')
        try:
            return super().execute(*args, **kwargs)
        finally:
            self.close()


def isolated_request_builder(credentials):
    """Google's documented per-request transport pattern, including media chunks.

    The service may survive Streamlit reruns, but its shared base HTTP transport
    must never be used for requests. Keep default timeouts and TLS verification.
    https://googleapis.github.io/google-api-python-client/docs/thread_safety.html
    """
    def build_request(_shared_http, *args, **kwargs):
        http = AuthorizedHttp(credentials, http=build_http())
        return IsolatedHttpRequest(http, *args, **kwargs)
    return build_request


INTERRUPTED = ('The connection to Google Drive was interrupted. Retry failed previews. '
               'If it continues, check Python internet access from your own PowerShell.')


def folder_id(value):
    value = value.strip()
    if re.fullmatch(r'[A-Za-z0-9_-]+', value):
        return value
    parsed = urlparse(value)
    if parsed.scheme == 'https' and parsed.hostname == 'drive.google.com':
        match = re.search(r'/folders/([A-Za-z0-9_-]+)', parsed.path)
        candidate = match.group(1) if match else parse_qs(parsed.query).get('id', [''])[0]
        if re.fullmatch(r'[A-Za-z0-9_-]+', candidate):
            return candidate
    raise DriveError('Paste a Google Drive folder URL or folder ID.')


def connect(interactive=True):
    """Called only on explicit Connect. Google libraries read local secrets privately."""
    stage = 'loading the saved login'
    try:
        credentials = None
        if TOKEN_FILE.exists():
            credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE))
            # Do not reuse a token with additional scopes from another application.
            if set(credentials.scopes or []) != set(SCOPES):
                credentials = None
        if credentials and not credentials.valid and credentials.refresh_token:
            stage = 'refreshing the Google login'
            try:
                credentials.refresh(Request())
            except GoogleAuthError:
                credentials = None
        if not credentials or not credentials.valid:
            if not interactive:
                raise DriveError('No usable saved Google login yet. Click Connect Google Drive, finish sign-in, then click Load Animal Folders. If you just finished, wait a few seconds and try loading again.')
            if not CLIENT_FILE.is_file():
                raise DriveError('Place your Desktop OAuth JSON at secrets/google_client.json, then connect again.')
            stage = 'loading Desktop OAuth configuration'
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
            stage = 'completing Google sign-in and token exchange'
            credentials = flow.run_local_server(
                host='localhost', port=0, open_browser=True, timeout_seconds=600,
                authorization_prompt_message='',
                success_message='Google authorization received. Return to the Rescue Story Agent tab and wait for folders to load. If no list appears, click Load Animal Folders there.',
                access_type='offline', prompt='consent',
            )
        granted = credentials.granted_scopes
        if granted is not None and set(granted) != set(SCOPES):
            raise DriveError('Google did not grant exactly the read-only Drive permission. Connect again.')
        stage = 'saving the login locally'
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(credentials.to_json(), encoding='utf-8')
        stage = 'starting the Google Drive API client'
        return build('drive', 'v3', credentials=credentials, cache_discovery=False,
                     requestBuilder=isolated_request_builder(credentials))
    except DriveError:
        raise
    except WSGITimeoutError:
        raise DriveError(
            'Google sign-in was not completed within 10 minutes. Close the old sign-in tab, '
            'click Connect Google Drive again, and finish every Google sign-in/consent step '
            'until you see Connected. Then return here to select an animal folder.'
        ) from None
    except RequestException as error:
        raise DriveError(network_error(error)) from None
    except OAuth2Error as error:
        messages = {
            'invalid_client': 'Google rejected the Desktop OAuth client. Download a current Desktop client JSON from your Google Cloud project and replace secrets/google_client.json yourself.',
            'invalid_grant': 'Google rejected the sign-in code. Close old sign-in tabs and start a fresh connection; check your computer clock.',
            'access_denied': 'Google permission was declined. Check the selected account and OAuth test-user settings, then reconnect.',
        }
        raise DriveError(messages.get(error.error, 'Google rejected the authorization request. Check Desktop client and consent settings, then reconnect.')) from None
    except (OSError, ValueError, GoogleAuthError, HttpError, HttpLib2Error):
        raise DriveError(f'Connection failed while {stage}. No authentication details are displayed. Check the local configuration or network access for that step.') from None


def network_error(error):
    """Classify nested transport errors without reading/printing their messages."""
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, PermissionError):
            return ('Python network access was denied by Windows or this execution environment. '
                    'Stop the app and launch it from your own PowerShell terminal outside the coding agent. '
                    'Then reconnect. This is not a Google password or folder-sharing error.')
        pending.extend(value for value in (
            current.__cause__, current.__context__, getattr(current, 'reason', None), *current.args
        ) if isinstance(value, BaseException))
    return ('Python could not reach Google. Check internet access, firewall or proxy settings for Python, '
            'then reconnect. Google sign-in in your browser alone does not confirm Python has network access.')


def _call(request):
    try:
        return request.execute()
    except HttpError as exc:
        status = exc.resp.status
        if status == 401:
            raise DriveError('Authentication expired. Click Connect Google Drive again.') from None
        if status == 404:
            raise DriveError('Folder or document not found, or your Google account cannot access it.') from None
        if status == 403:
            raise DriveError('Access denied. Check folder sharing, read-only consent, and that the Google Drive API is enabled.') from None
        raise DriveError('Google Drive is temporarily unavailable. Try again later.') from None
    except RequestException as error:
        raise DriveError(network_error(error)) from None
    except GoogleAuthError:
        raise DriveError('Google login could not be refreshed. Click Connect Google Drive, then retry the failed previews.') from None
    except OSError as error:
        raise DriveError(network_error(error)) from None
    except (HttpLib2Error, HTTPException):
        raise DriveError(INTERRUPTED) from None
    except AttributeError as error:
        # Known http.client response-close failure. Do not hide arbitrary bugs.
        if ((error.name == 'close' and error.obj is None) or
                error.args == ("'NoneType' object has no attribute 'close'",)):
            raise DriveError(INTERRUPTED) from None
        raise
    except ValueError as error:
        if error.args in (('read of closed file',), ('I/O operation on closed file.',)):
            raise DriveError(INTERRUPTED) from None
        raise


def children(service, parent):
    parent = folder_id(parent)
    items, page = [], None
    while True:
        response = _call(service.files().list(
            q=f"'{parent}' in parents and trashed = false",
            fields='nextPageToken,files(id,name,mimeType,size,modifiedTime,imageMediaMetadata(time))', pageSize=1000,
            pageToken=page, supportsAllDrives=True, includeItemsFromAllDrives=True,
        ))
        items.extend(response.get('files', []))
        page = response.get('nextPageToken')
        if not page:
            return items


def discover_folders(service, value):
    parent = folder_id(value)
    metadata = _call(service.files().get(fileId=parent, fields='id,mimeType,trashed', supportsAllDrives=True))
    if metadata.get('mimeType') != FOLDER or metadata.get('trashed'):
        raise DriveError('The selected item is not an available Drive folder.')
    return sorted((item for item in children(service, parent) if item['mimeType'] == FOLDER),
                  key=lambda item: (item['name'].casefold(), item['id']))


def is_media_folder(item):
    normalized = ''.join(c for c in unicodedata.normalize('NFKD', item['name'].casefold()) if c.isalnum())
    return item['mimeType'] == FOLDER and normalized in {
        'fotos', 'foto', 'photos', 'photo', 'pictures', 'images', 'media', 'videos', 'video', 'fotosvideos',
    }


def load_animal(service, animal):
    items = children(service, animal['id'])
    matches = [item for item in items if 'kenmerkenlijst' in item['name'].casefold()
               and item['mimeType'] != FOLDER]
    if not matches:
        raise DriveError('No Kenmerkenlijst document was found in this animal folder.')
    if len(matches) != 1:
        raise DriveError('Multiple Kenmerkenlijst documents were found. Use manual upload to choose the correct one.')
    document = matches[0]
    if not (document['name'].lower().endswith('.docx') or document['mimeType'] == DOCX):
        raise DriveError('The Kenmerkenlijst must be a DOCX file. Download/export it as .docx and use manual upload.')
    # No disk copy, no export/write operation, no media-folder traversal.
    data = _call(service.files().get_media(fileId=document['id'], supportsAllDrives=True))
    info = extract_animal_info(data)
    return {**info, 'animal': animal['name'], 'document': document['name'],
            'media': [item['name'] for item in items if is_media_folder(item)]}
