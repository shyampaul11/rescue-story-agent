"""Explicit per-animal private-data controls. No generation or external I/O."""
import streamlit as st

from content_state import workspace
from editorial_store import delete_record, deletion_token


def clear_scope_state(state, scope, token):
    """Remove this animal's live writing as well as its persisted revisions."""
    records = state.setdefault('writing_workspaces', {})
    records.pop(scope, None)
    for key in list(state):
        if (isinstance(key, str) and scope in key and key.startswith(('writing_', 'copy_'))
                and not key.startswith('writing_delete_' + scope)):
            del state[key]
    state.setdefault('writing_saved_hashes', {}).pop(scope, None)
    record = workspace(state, scope)
    record.update(scope=scope, storage_deletion_token=token)
    return record


def synchronize_scope(state, scope):
    """An older open tab must not revive work that another tab deleted."""
    record = workspace(state, scope)
    token = deletion_token(scope)
    previous = record.get('storage_deletion_token', '')
    if token != previous:
        record = clear_scope_state(state, scope, token)
        state.setdefault('writing_deletion_notices', {})[scope] = (
            'This animal’s locally saved writing was deleted. The earlier drafts and editors have been cleared.')
    else:
        record.setdefault('storage_deletion_token', token)
    return record


def has_writing_content(record):
    """Do not recreate a deleted animal's blank workspace on ordinary reruns."""
    return bool(record.get('bundle') or record.get('pending_response') or record.get('history') or
        record.get('confirmations') or record.get('manual_sources') or record.get('editor_assessments') or
        any(record.get('final', {}).values()) or any(record.get('facebook', {}).values()) or
        any(record.get('undo', {}).values()))


def delete_current_animal(scope):
    confirm = 'writing_delete_' + scope + '_confirm'
    if not st.session_state.get(confirm):
        return
    try:
        result = delete_record(scope)
    except (OSError, ValueError, TypeError):
        st.session_state.setdefault('writing_deletion_errors', {})[scope] = (
            'Local deletion could not finish. Some files may remain; retry deletion. No successful deletion is being claimed.')
        return
    clear_scope_state(st.session_state, scope, result['deletion_token'])
    st.session_state[confirm] = False
    st.session_state.setdefault('writing_deletion_errors', {}).pop(scope, None)
    st.session_state.setdefault('writing_deletion_notices', {})[scope] = (
        'Deleted this animal’s locally saved drafts, edits, source snapshots, review notes and previous versions.')


def render_privacy_controls(record, scope):
    with st.expander('Local data and privacy'):
        st.caption('Writing and review data are saved privately on this computer in Git-excluded local_state. '
                   'Credentials and login tokens are excluded from draft records; recognized credential strings are redacted.')
        st.caption('Deletion removes this animal’s saved writing, source snapshots, notes and all saved versions, '
                   'and clears its writing editors. Other animals, Google Drive documents, login credentials and '
                   'information loaded in the source-analysis sections are unchanged. Downloaded/exported copies must be deleted separately.')
        key = 'writing_delete_' + scope
        confirmed = st.checkbox('Confirm permanent deletion of this animal’s locally saved writing and review data',
                                key=key + '_confirm')
        st.button('Delete this animal’s local writing data', key=key, disabled=not confirmed,
                  on_click=delete_current_animal, args=(scope,))
        error = st.session_state.get('writing_deletion_errors', {}).get(scope)
        notice = st.session_state.get('writing_deletion_notices', {}).get(scope)
        if error:
            st.warning(error)
        elif notice:
            st.success(notice)
