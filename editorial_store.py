"""Private, revisioned editorial workspaces. No session/settings dumps or API calls."""
from datetime import date, datetime, timezone
from contextlib import contextmanager, ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import uuid

from draft_evidence import private_text


_SCOPE = re.compile(r'[0-9a-f]{20}')
_SESSION = re.compile(r'[0-9a-f]{32}')
_SNAPSHOT = re.compile(r'\d{8}T\d{12}Z_[0-9a-f]{32}_\d{8}_[0-9a-f]{32}\.json')
_ROOT = Path(__file__).resolve().parent / 'local_state' / 'editorial'
_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_STATUSES = {'needs_clarification', 'confirmed_by_rescuer', 'corrected',
             'removed_from_final', 'not_relevant'}
_DRAFT_KEYS = ('website_nl_1', 'website_nl_2', 'website_nl_3',
               'website_en_1', 'website_en_2', 'website_en_3', 'facebook_nl', 'facebook_en')
_EDITORS = ('final_nl', 'final_en', 'facebook_nl', 'facebook_en')
_LIST_FIELDS = {'source_references', 'evidence_ids', 'identification_evidence_ids', 'basis_evidence_ids',
                'superseded_evidence_ids', 'unresolved_contradictions', 'provided_sections',
                'review_only_evidence_ids', 'rescue_background', 'personality_and_behavior',
                'physical_appearance', 'health_and_special_needs', 'adoption_considerations',
                'unknown_information', 'topics', 'rejection_codes'}


def _fields(names):
    return {name: (None,) if name in _LIST_FIELDS else None for name in names.split()}


_CLAIM = _fields('excerpt source_references qualification')
_DRAFT = {'text': None, 'claims': (_CLAIM,), 'review_notes': (None,)}
_BUNDLE = {'perspective': None, **dict.fromkeys(_DRAFT_KEYS, _DRAFT)}
_EVIDENCE = _fields('id claim source_type source_reference supporting_text_or_description subject '
                    'verification_status temporal_status reported_by')
_WARNING = _fields('category message evidence_ids')
_PROFILE = {
    **_fields('rescue_background personality_and_behavior physical_appearance health_and_special_needs '
              'adoption_considerations unknown_information review_only_evidence_ids'),
    'identity': _fields('animal_name name_evidence_id identification_evidence_ids verification_status'),
    'evidence': (_EVIDENCE,), 'review_warnings': (_WARNING,),
    'updates': (_fields('original_date normalized_date source_excerpt evidence_ids change_type '
                        'superseded_evidence_ids unresolved_contradictions'),),
    'source_attribution': _fields('rescuer_name source_reference provided_sections verification_note'),
    'photo_date_references': (_fields('source_reference original_value reference_date date_source '
                                      'verification_status note'),),
    'age_assessment': _fields('estimate reference_date photo_reference basis_evidence_ids status limitations'),
}
_FINDING = _fields('id code kind color excerpt reason source_references action draft language '
                   'claim_number origin failure_type required_fact essential_reason text_presence '
                   'annotation_status classification claim_qualification')
_FINDING['support_candidates'] = (None,)
_FINDING.update(relevant_evidence=(_EVIDENCE,), relevant_unknowns=(
    _fields('reference text topics status state scope subject source_type verification_status temporal_status'),))
_PASSAGE = _fields('start end color label reason source_references code')
_PASSAGE['support_candidates'] = (None,)
_PER_DRAFT = {'status': None, 'passages': (_PASSAGE,), 'findings': (_FINDING,),
              'text_fingerprint': None, 'profile_fingerprint': None,
              'manual_sources_fingerprint': None, 'stale': None, 'profile_snapshot': _PROFILE}
_QUESTION = _fields('id topic question reason priority source_references')
_ASSESSMENT = {'version': None, 'drafts': dict.fromkeys(_DRAFT_KEYS + _EDITORS + ('editor',), _PER_DRAFT),
               'questions': (_QUESTION,), 'profile_fingerprint': None, 'stale': None, 'incomplete': None}
_CONFIRMATION = _fields('id issue_id note volunteer_note rescuer_response confirmation_date attribution '
                        'status recorded_at topic scope animal_name')
_MANUAL = _fields('id issue_id topic scope animal_name text attribution confirmation_date verification_status '
                  'subject temporal_status created_at')
_USAGE = _fields('input_tokens output_tokens total_tokens')
_WORKSPACE = {
    **_fields('animal_name model generated_at profile_fingerprint analysis_fingerprint outdated '
              'assessment_error assessment_stale assessment_profile_fingerprint '
              'assessment_manual_sources_fingerprint storage_session_id storage_revision '
              'storage_deletion_token saved_at scope error'),
    'bundle': _BUNDLE, 'final': _fields('nl en'), 'facebook': _fields('nl en'),
    'facebook_baseline': _fields('nl en'), 'undo': dict.fromkeys(_EDITORS, (None,)),
    'usage': _USAGE, 'profile_snapshot': _PROFILE, 'assessment_profile_snapshot': _PROFILE,
    'assessment': _ASSESSMENT,
    'editor_assessments': dict.fromkeys(_EDITORS, _PER_DRAFT),
    'editor_statuses': dict.fromkeys(_EDITORS, _fields('finalized text_hash')),
    'history': ({'bundle': _BUNDLE, 'generated_at': None, 'profile_fingerprint': None,
                 'assessment': _ASSESSMENT, 'profile_snapshot': _PROFILE},),
    'issues': (_fields('code message draft source_references'),),
    'profile_warnings': (_WARNING,), 'unknown_information': (None,),
    'confirmations': (_CONFIRMATION,), 'manual_sources': (_MANUAL,),
    'validation_report': (_FINDING,),
    'validation_details': _fields('code origin draft language claim_number rejection_count rejection_codes'),
    'pending_response': {
        **_fields('profile_fingerprint analysis_fingerprint perspective received_at'),
        'candidate': _BUNDLE, 'usage': _USAGE, 'profile_snapshot': _PROFILE,
    },
}


def _filtered(value, shape):
    """Recursive explicit schemas prevent unknown nested fields reaching disk."""
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    if value is None:
        return None
    if isinstance(shape, dict):
        if not isinstance(value, dict):
            return _filtered(value, None)  # Retain malformed annotation scalars for review.
        return {key: _filtered(value[key], spec) for key, spec in shape.items() if key in value}
    if isinstance(shape, tuple):
        if not isinstance(value, (list, tuple)):
            return _filtered(value, None)
        return [_filtered(item, shape[0]) for item in value]
    if isinstance(value, str):
        return private_text(value)
    if isinstance(value, (list, tuple)):
        return [_filtered(item, None) for item in value]
    if type(value) in (int, bool) or isinstance(value, float) and math.isfinite(value):
        return value
    # Free-form dictionaries, arbitrary objects and nonfinite numbers are not persisted.
    return None


def _directory(scope, root):
    if not isinstance(scope, str) or not _SCOPE.fullmatch(scope):
        raise ValueError('Invalid animal workspace scope.')
    base = Path(root) if root is not None else _ROOT
    if any(_linked(path) for path in (base, *base.parents)):
        raise ValueError('Animal workspace must stay inside its storage root.')
    base = base.resolve()
    directory = base / scope
    if _linked(directory) or directory.resolve() != directory:
        raise ValueError('Animal workspace must stay inside its storage root.')
    return directory


class WorkspaceDeletedError(ValueError):
    """An older open workspace attempted to save after local deletion."""


def _linked(path):
    """Detect symlinks and Windows reparse points without following them."""
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def _marker_path(directory):
    private = directory.parent / '_deletions'
    if _linked(private) or private.resolve() != private:
        raise ValueError('Deletion markers must stay inside private local storage.')
    identity = str(directory.parent) + '\0' + directory.name
    return private / (hashlib.sha256(identity.encode('utf-8')).hexdigest() + '.json')


def _read_token(marker):
    if _linked(marker):
        raise ValueError('Linked deletion markers are not permitted.')
    try:
        value = json.loads(marker.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return ''
    if (not isinstance(value, dict) or not isinstance(value.get('token'), str)
            or not _SESSION.fullmatch(value['token'])):
        raise ValueError('The local deletion marker could not be read safely.')
    return value['token']


def deletion_token(scope, root=None):
    """Return the current non-content deletion generation, or '' before deletion."""
    return _read_token(_marker_path(_directory(scope, root)))


@contextmanager
def _scope_lock(directory):
    """Serialize snapshots/deletion across threads and cooperating OS processes."""
    marker = _marker_path(directory)
    lock_path = marker.with_suffix('.lock')
    if _linked(marker):
        raise ValueError('Linked deletion markers are not permitted.')
    marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _THREAD_LOCKS_GUARD:
        mutex = _THREAD_LOCKS.setdefault(str(lock_path), threading.RLock())
    with mutex:
        if _linked(lock_path):
            raise ValueError('Linked workspace locks are not permitted.')
        with lock_path.open('a+b') as handle:
            os.chmod(lock_path, 0o600)
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                _directory(directory.name, directory.parent)
                yield marker
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _deletion_files(directory):
    """Preflight every descendant before removal; directory links are refused."""
    if not directory.exists():
        return []
    files = []
    with os.scandir(directory) as entries:
        for entry in entries:
            path = Path(entry.path)
            if _linked(path):
                # File links can be unlinked without reading their target; directory
                # reparse points are refused, including Windows junctions.
                if entry.is_dir() or bool(getattr(entry.stat(follow_symlinks=False),
                                                 'st_file_attributes', 0) & 0x10):
                    raise ValueError('Linked directories cannot be deleted as animal workspaces.')
                files.append(path)
            elif entry.is_dir(follow_symlinks=False):
                files.extend(_deletion_files(path))
            else:
                files.append(path)
    files.append(directory)
    return files


def delete_record(scope, root=None):
    """Delete this scope's local files and invalidate every previously opened record.

    A marker is committed before removal, so partial filesystem failures remain
    errors while also preventing surviving old snapshots from being restored.
    """
    directory = _directory(scope, root)
    with _scope_lock(directory) as marker:
        return _delete_record_locked(directory, marker)


def _delete_record_locked(directory, marker, files=None):
    """Retire a generation while its caller owns the scope's OS/thread lock."""
    files = _deletion_files(directory) if files is None else files
    count = sum(bool(_SNAPSHOT.fullmatch(path.name)) for path in files if not path.is_dir())
    token = uuid.uuid4().hex
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix='.deleting-', dir=marker.parent)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            os.chmod(temporary, 0o600)
            json.dump({'token': token}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)
    for path in files:
        if _linked(path):
            if path.is_dir():
                raise ValueError('Linked directories cannot be deleted as animal workspaces.')
            os.unlink(path)
        elif path.is_dir():
            os.rmdir(path)
        else:
            os.unlink(path)
    return {'deleted_snapshots': count, 'deletion_token': token}


def _require_preanalysis(record):
    if (not isinstance(record, dict) or record.get('bundle') is not None
            or record.get('history') or record.get('pending_response') is not None):
        raise ValueError('Only an ungenerated volunteer workspace can be assigned to an animal.')


def migrate_record(source_scope, target_scope, record, root=None):
    """Save unassigned volunteer text under its animal, then retire its old scope.

    The input stays unchanged. The returned ``record`` owns the target token and
    a fresh session; current editors and their undo lists are preserved. Old
    anonymous snapshots are retired. A nonempty ``migration_warning`` means the
    destination was saved and source generation invalidated, but physical source
    cleanup failed. Save or marker-commit errors raise, preserving recoverable
    saved data rather than claiming adoption succeeded.
    """
    source = _directory(source_scope, root)
    target = _directory(target_scope, root)
    if source_scope == target_scope:
        raise ValueError('Workspace assignment requires a different animal scope.')
    _require_preanalysis(record)
    if record.get('scope') not in (None, source_scope):
        raise ValueError('The volunteer workspace belongs to a different source scope.')
    with ExitStack() as locks:
        markers = {directory.name: locks.enter_context(_scope_lock(directory))
                   for directory in sorted((source, target), key=lambda path: path.name)}
        source_token = _read_token(markers[source_scope])
        if record.get('storage_deletion_token', '') != source_token:
            raise WorkspaceDeletedError('This unassigned workspace was deleted. Start with a new empty workspace.')
        if target.exists() and any(target.iterdir()):
            raise ValueError('The selected animal already has saved local work. It was preserved.')
        saved_source = _load_record(source_scope, source, source_token)
        if saved_source is not None:
            _require_preanalysis(saved_source)
        files = _deletion_files(source)
        target_token = _read_token(markers[target_scope])
        detached = _filtered(record, _WORKSPACE)
        detached.update(scope=target_scope, storage_deletion_token=target_token,
                        storage_session_id=uuid.uuid4().hex, storage_revision=0)
        _save_record(target_scope, detached, target, target_token)
        warning = None
        try:
            retired = _delete_record_locked(source, markers[source_scope], files)
            retired_token = retired['deletion_token']
        except (OSError, ValueError):
            retired_token = _read_token(markers[source_scope])
            if retired_token == source_token:
                raise
            warning = ('The animal workspace was saved and the unassigned copy was retired, '
                       'but some old local files could not be removed. Retry local deletion '
                       'of the unassigned workspace to finish removing those files.')
        return {'record': detached, 'migration_warning': warning,
                'source_deletion_token': retired_token}


def _now():
    return datetime.now(timezone.utc)


def save_record(scope, record, root=None):
    """Atomically append a private snapshot; earlier sessions/revisions are never replaced."""
    directory = _directory(scope, root)
    with _scope_lock(directory) as marker:
        token = _read_token(marker)
        if record.get('storage_deletion_token', '') != token:
            raise WorkspaceDeletedError('This animal workspace was deleted. Start with a new empty workspace.')
        _save_record(scope, record, directory, token)


def _save_record(scope, record, directory, token):
    session = record.get('storage_session_id')
    if not isinstance(session, str) or not _SESSION.fullmatch(session):
        session = uuid.uuid4().hex
    previous = record.get('storage_revision', 0)
    revision = (previous if type(previous) is int and previous >= 0 else 0) + 1
    now = _now()
    metadata = {'storage_session_id': session, 'storage_revision': revision,
                'saved_at': now.isoformat(), 'scope': scope, 'storage_deletion_token': token}
    safe = _filtered(record, _WORKSPACE)
    safe.update(metadata)
    payload = {'version': 1, 'scope': scope, 'saved_at': metadata['saved_at'], 'record': safe}
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = f'{now:%Y%m%dT%H%M%S%fZ}_{session}_{revision:08d}_{uuid.uuid4().hex}.json'
    temp_name = None
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix='.saving-', dir=directory)
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
            os.chmod(temp_name, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, directory / filename)
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    record.update(metadata)


def _restore(raw, scope):
    if (not isinstance(raw, dict) or raw.get('version') != 1 or raw.get('scope') != scope
            or not isinstance(raw.get('record'), dict)):
        raise ValueError('Invalid workspace snapshot.')
    record = _filtered(raw['record'], _WORKSPACE)
    _validate_containers(record, _WORKSPACE)
    if record.get('scope') not in (None, scope):
        raise ValueError('Workspace snapshot has a different animal scope.')
    for target in ('final', 'facebook', 'facebook_baseline'):
        value = record.get(target, {})
        if not isinstance(value, dict) or any(not isinstance(item, str) for item in value.values()):
            raise ValueError('Invalid saved editor text.')
        record[target] = {'nl': '', 'en': '', **value}
    undo = record.get('undo', {})
    if not isinstance(undo, dict) or any(not isinstance(history, list) or
            any(not isinstance(text, str) for text in history) for history in undo.values()):
        raise ValueError('Invalid saved editor history.')
    if record.get('bundle') is not None:
        from content_models import retained_drafts
        record['bundle'] = retained_drafts(record['bundle'])
    defaults = {'bundle': None, 'final': {'nl': '', 'en': ''}, 'facebook': {'nl': '', 'en': ''},
                'facebook_baseline': {'nl': '', 'en': ''}, 'undo': {}, 'usage': {}, 'issues': [],
                'profile_warnings': [], 'outdated': False, 'animal_name': None, 'model': None,
                'generated_at': None, 'profile_fingerprint': None, 'analysis_fingerprint': None}
    defaults.update(record)
    # Loading means a new browser/session, so subsequent saves retain independent history.
    defaults.update(scope=scope, storage_session_id=uuid.uuid4().hex)
    return defaults


def _validate_containers(value, shape, path=()):
    """Reject structurally corrupt UI values before installing a saved record.

    Draft annotations are deliberately excluded: retained_drafts quarantines bad
    metadata while keeping its eight readable texts. Other containers must match
    their explicit schemas, so corrupted reviews cannot break a restored editor.
    """
    nullable = {'bundle', 'assessment', 'profile_snapshot', 'assessment_profile_snapshot',
                'pending_response', 'source_attribution', 'age_assessment'}
    if value is None and (shape is None or path and path[-1] in nullable):
        return
    if isinstance(shape, dict):
        if not isinstance(value, dict):
            raise ValueError('Invalid saved workspace container.')
        if shape is _BUNDLE:
            return
        for key, spec in shape.items():
            if key in value:
                _validate_containers(value[key], spec, path + (key,))
    elif isinstance(shape, tuple):
        if not isinstance(value, list):
            raise ValueError('Invalid saved workspace list.')
        for item in value:
            _validate_containers(item, shape[0], path + ('item',))
    elif isinstance(value, (dict, list)):
        raise ValueError('Invalid saved workspace scalar.')


def load_record(scope, root=None):
    """Return latest usable workspace, with warnings if a newer snapshot was corrupt.

    No saved record returns None. If all snapshots are corrupt, ValueError lets the
    UI preserve its current session and show a recoverable local-storage warning.
    """
    directory = _directory(scope, root)
    marker = _marker_path(directory)
    if not directory.exists():
        return None
    with _scope_lock(directory):
        return _load_record(scope, directory, _read_token(marker))


def _load_record(scope, directory, token):
    paths = sorted((path for path in directory.glob('*.json')
                    if _SNAPSHOT.fullmatch(path.name)), key=lambda path: path.name, reverse=True)
    skipped = 0
    for path in paths:
        try:
            if path.is_symlink():
                raise ValueError('Linked snapshots are not permitted.')
            record = _restore(json.loads(path.read_text(encoding='utf-8')), scope)
            if record.get('storage_deletion_token', '') != token:
                continue
        except (ValueError, TypeError, KeyError, AttributeError):
            skipped += 1
            continue
        if skipped:
            record['storage_warnings'] = [
                f'{skipped} newer workspace snapshot(s) could not be read. An earlier revision was restored.']
        record['storage_deletion_token'] = token
        return record
    if paths and skipped:
        raise ValueError('Saved workspace revisions could not be read. Existing files were preserved.')
    return None


def record_confirmation(record, issue_id, details):
    """Append an attributed audit entry; this does not alter assessments or evidence."""
    if not isinstance(issue_id, str) or not issue_id.strip():
        raise ValueError('A review issue is required.')
    if (not isinstance(details, dict) or not isinstance(details.get('status'), str)
            or details['status'] not in _STATUSES):
        raise ValueError('Choose a valid review status.')
    confirmation_date = details.get('confirmation_date')
    if confirmation_date is not None and confirmation_date != '':
        if not isinstance(confirmation_date, str):
            raise ValueError('Use a calendar date in YYYY-MM-DD format.')
        if confirmation_date.strip():
            _validate_date(confirmation_date)
    scope = details.get('scope')
    if scope and (not isinstance(scope, str) or not _SCOPE.fullmatch(scope)
                  or record.get('scope') and record['scope'] != scope):
        raise ValueError('The confirmation belongs to a different animal workspace.')
    name = details.get('animal_name')
    if name and record.get('animal_name') and (
            not isinstance(name, str) or name.casefold().strip() != record['animal_name'].casefold().strip()):
        raise ValueError('The confirmation belongs to a different animal.')
    entry = _filtered(details, _CONFIRMATION)
    entry.update(id=uuid.uuid4().hex, issue_id=private_text(issue_id), recorded_at=_now().isoformat())
    if record.get('scope'):
        entry['scope'] = record['scope']
    if record.get('animal_name'):
        entry['animal_name'] = private_text(record['animal_name'])
    record.setdefault('confirmations', []).append(entry)
    return dict(entry)


def _validate_date(value):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('Use a calendar date in YYYY-MM-DD format.')
    date.fromisoformat(value)


def add_manual_source(record, issue_id):
    """Explicitly add the latest attributed response without changing the original Profile."""
    entry = next((item for item in reversed(record.get('confirmations', []))
                  if item.get('issue_id') == issue_id), None)
    required = ('rescuer_response', 'attribution', 'confirmation_date')
    if not entry or any(not isinstance(entry.get(key), str) or not entry[key].strip() for key in required):
        raise ValueError('A rescuer response, attribution and confirmation date are required.')
    _validate_date(entry['confirmation_date'])
    if entry.get('scope') and record.get('scope') and entry['scope'] != record['scope']:
        raise ValueError('The response belongs to a different animal workspace.')
    if entry['attribution'].strip().casefold() in {'unknown', 'onbekend', 'n/a', 'tbd', '?'}:
        raise ValueError('Name the rescuer or source who supplied this response.')
    question = next((item for item in (record.get('assessment') or {}).get('questions', [])
                     if item.get('id') == issue_id), {})
    topic = question.get('topic') or entry.get('topic') or issue_id
    scope = record.get('scope') or entry.get('scope') or record.get('animal_name') or 'unidentified'
    identity = json.dumps([scope, issue_id, topic], ensure_ascii=False)
    source = {'id': 'manual:' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20],
              'issue_id': issue_id, 'topic': topic, 'scope': scope, 'animal_name': record.get('animal_name'),
              'text': entry['rescuer_response'], 'attribution': entry['attribution'],
              'confirmation_date': entry['confirmation_date'],
              'verification_status': 'reported_not_independently_verified',
              'subject': 'selected_animal', 'temporal_status': 'unclear',
              'created_at': _now().isoformat()}
    source = _filtered(source, _MANUAL)
    sources = record.setdefault('manual_sources', [])
    for index, existing in enumerate(sources):
        if existing.get('id') == source['id']:
            sources[index] = source
            break
    else:
        sources.append(source)
    return dict(source)
