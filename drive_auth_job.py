"""Single-user local OAuth worker, independent of Streamlit tab/session lifetime.

Only safe status strings live here; no credentials, API clients or raw errors.
"""
from threading import Lock, Thread

import drive_client as drive

_lock = Lock()
_state = {'status': 'idle', 'message': ''}


def connection_status():
    with _lock:
        return dict(_state)


def _authenticate():
    try:
        drive.connect()  # Google libraries privately save the token locally.
        result = {'status': 'ready', 'message': 'Google login saved. Click Load Animal Folders.'}
    except drive.DriveError as error:
        result = {'status': 'error', 'message': str(error)}
    except Exception:
        # Never let a background-thread traceback expose authentication data.
        result = {'status': 'error', 'message': 'Google connection could not finish. Click Connect Google Drive to retry.'}
    with _lock:
        _state.update(result)


def start_connection():
    with _lock:
        if _state['status'] == 'running':
            return
        _state.update(status='running', message='Google sign-in is in progress. Finish consent in your browser, then return to http://127.0.0.1:8501 and click Load Animal Folders.')
        Thread(target=_authenticate, name='local-drive-oauth', daemon=True).start()
