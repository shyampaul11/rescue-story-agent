"""Bilingual draft selection and durable per-animal manual editors."""
import streamlit as st
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from copy import deepcopy
from pydantic import ValidationError

from clipboard_component import copy_button
from content_models import DRAFT_KEYS, STYLES
from content_service import (ContentError, ContentValidationError, ContentSettings, generate_drafts, load_content_settings,
                             validated_profile, profile_fingerprint, reassess_drafts as recheck_drafts, safe_assessment)
from content_state import (writing_scope, workspace, remember_edit, restore_edit,
                           has_human_edits, commit_generation, export_workspace, adopt_unnamed_manual_edits,
                           private_review_value, private_diagnostic_data, export_private_diagnostic)
from source_context import parse_animal_label
from animal_profile import AnimalProfile
from draft_evidence import rejection
from editorial_store import load_record, save_record, migrate_record, WorkspaceDeletedError
from editorial_privacy import (render_privacy_controls, synchronize_scope, has_writing_content, clear_scope_state)
from editorial_history import render_version_history
from editorial_ui import (render_review, render_questions, render_editor_review,
                          manual_sources_fingerprint, publication_copy_text)


def _record_digest(record):
    value = {key: item for key, item in record.items() if key not in
             ('storage_session_id', 'storage_revision', 'saved_at', 'storage_error')}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        default=lambda item: item.model_dump(mode='json') if hasattr(item, 'model_dump') else str(item)).encode()).hexdigest()


def persist_record(scope, record):
    """Save changed work only; storage failures never discard the live session."""
    hashes = st.session_state.setdefault('writing_saved_hashes', {})
    if not has_writing_content(record):
        return
    digest = _record_digest(record)
    if hashes.get(scope) == digest:
        return
    try:
        save_record(scope, record)
        record.pop('storage_error', None)
        hashes[scope] = _record_digest(record)
    except WorkspaceDeletedError:
        synchronize_scope(st.session_state, scope)
    except (OSError, ValueError, TypeError):
        record['storage_error'] = 'Private autosave could not finish. Your current work is still in this session; download a backup.'


def load_workspace_once(scope):
    loaded = st.session_state.setdefault('writing_loaded_scopes', [])
    if scope not in loaded:
        loaded.append(scope)
        try:
            saved = load_record(scope)
            records = st.session_state.setdefault('writing_workspaces', {})
            if saved and scope not in records:
                records[scope] = saved
                st.session_state.setdefault('writing_saved_hashes', {})[scope] = _record_digest(saved)
        except (OSError, ValueError, TypeError):
            workspace(st.session_state, scope).setdefault('storage_warnings', []).append(
                'Saved private work could not be loaded. Existing files were preserved; current session edits are still available.')


def retained_recovery_profile(record, scope, current_profile=None):
    """Bind retained text to its locally validated source snapshot, without analysis."""
    pending = record.get('pending_response')
    if not pending or record.get('scope') not in (None, scope):
        return None
    selected_name = parse_animal_label(st.session_state.get('media_animal', {}).get('name')).animal_name
    normalize_name = lambda value: ' '.join(str(value or '').casefold().split())
    for candidate in (current_profile, pending.get('profile_snapshot'), record.get('profile_snapshot')):
        if candidate is None:
            continue
        try:
            retained = validated_profile(candidate)
            name = retained.identity.animal_name
            if (profile_fingerprint(retained) != pending.get('profile_fingerprint')
                    or writing_scope(st.session_state, retained) != scope
                    or any(normalize_name(expected) != normalize_name(name)
                           for expected in (selected_name, record.get('animal_name')) if expected)):
                continue
        except (ContentError, ValidationError, ValueError, TypeError):
            continue
        snapshot = retained.model_dump(mode='json')
        pending['profile_snapshot'] = snapshot
        if record.get('bundle') is None:
            record['profile_snapshot'] = snapshot
        if not record.get('animal_name'):
            record['animal_name'] = name
        return retained
    return None


def editor_key(scope, target, language):
    return f'writing_editor_{scope}_{target}_{language}'


def save_editor(scope, target, language):
    record = workspace(st.session_state, scope)
    previous = record
    try:
        record = synchronize_scope(st.session_state, scope)
    except (OSError, ValueError):
        record['storage_error'] = 'Local privacy state could not be checked. Saved work is preserved; download a backup.'
    if record is not previous:
        return  # Do not revive a keystroke from an editor another tab deleted.
    remember_edit(record, target, language, st.session_state[editor_key(scope, target, language)])
    persist_record(scope, record)


def clear_final(scope, language, confirm_key):
    if st.session_state.get(confirm_key):
        record = workspace(st.session_state, scope)
        remember_edit(record, 'final', language, '')
        st.session_state[editor_key(scope, 'final', language)] = ''
        st.session_state[confirm_key] = False
        persist_record(scope, record)


def restore_previous(scope, target, language):
    record = workspace(st.session_state, scope)
    st.session_state[editor_key(scope, target, language)] = restore_edit(record, target, language)
    persist_record(scope, record)


def render_editor(scope, record, target, language, language_label):
    label = ('Final Website Text' if target == 'final' else 'Facebook text') + ' — ' + language_label
    key = editor_key(scope, target, language)
    # Durable record is authoritative; hidden widgets may be deleted by Streamlit cleanup.
    st.session_state[key] = record[target][language]
    st.text_area(label, key=key, height=300, max_chars=60000,
                 on_change=save_editor, args=(scope, target, language))
    copy_button('Copy Final Text' if target == 'final' else 'Copy Facebook Text', record[target][language],
                f'copy_{scope}_{target}_{language}', editor_label=label)
    if target == 'final':
        confirm_key = f'writing_clear_confirm_{scope}_{language}'
        confirmed = st.checkbox('Confirm clearing this language’s Final Website Text', key=confirm_key)
        st.button('Clear Final Text', key=f'writing_clear_{scope}_{language}', disabled=not confirmed,
                  on_click=clear_final, args=(scope, language, confirm_key))
    st.button('Restore previous text', key=f'writing_restore_{scope}_{target}_{language}',
              disabled=not record['undo'].get(target + '_' + language),
              on_click=restore_previous, args=(scope, target, language))


def render_validation_report(record):
    if not record.get('validation_report'):
        return
    with st.expander('Rejected claims — private review'):
        st.caption('These rejected passages are private review material, not approved drafts. Do not publish them or share this panel as a diagnostic log. Credentials are redacted.')
        for item in private_review_value(record['validation_report']):
            st.text('Draft: ' + (item['draft'] or 'Animal Profile / batch') +
                    ' · Language: ' + (item['language'] or 'Not draft-specific'))
            st.text('Origin: ' + item['origin'] + ' · Reason: ' + item['code'])
            if item.get('claim_number') is not None:
                st.text('Claim number: ' + str(item['claim_number']))
            if item.get('excerpt'):
                st.text('Rejected passage / profile field: ' + item['excerpt'])
            st.text('Referenced evidence: ' + (', '.join(item['source_references']) or 'None'))
            if item.get('failure_type'):
                st.text('Failure type: ' + item['failure_type'])
            if item.get('classification'):
                st.text('Classification: ' + item['classification'])
            if item.get('claim_qualification'):
                st.text('Claim qualification: ' + item['claim_qualification'])
            if item.get('required_fact'):
                st.text('Required fact: ' + item['required_fact'])
            if item.get('essential_reason'):
                st.text('Why required: ' + item['essential_reason'])
            if item.get('text_presence'):
                st.text(item['text_presence'])
            if item.get('annotation_status'):
                st.text('Annotation: ' + item['annotation_status'])
            if item.get('relevant_evidence'):
                st.caption('Relevant profile records for review only. These are not automatically assigned citations or proof that the rejected statement is supported.')
                for evidence in item['relevant_evidence']:
                    st.text('Evidence ' + evidence['id'] + ' · Animal attribution: ' + evidence['subject'] +
                            ' · Time: ' + evidence['temporal_status'])
                    st.text('Validated profile claim: ' + evidence['claim'])
                    st.text('Permitted source: ' + evidence['source_reference'] + ' · ' + evidence['supporting_text_or_description'])
                    if evidence.get('verification_status'):
                        st.text('Source verification status: ' + evidence['verification_status'])
            if item.get('relevant_unknowns'):
                st.caption('Relevant unknowns and warnings for review. These do not establish affirmative facts.')
                for unknown in item['relevant_unknowns']:
                    status = unknown.get('status') or unknown.get('state') or 'unknown'
                    st.text('Reference: ' + unknown['reference'] + ' · Status: ' + status)
                    if unknown.get('scope'):
                        st.text('Scope: ' + unknown['scope'])
                    st.text('Source wording: ' + unknown['text'])
                    if unknown.get('verification_status'):
                        st.text('Source verification status: ' + unknown['verification_status'])
                    if unknown.get('topics'):
                        st.text('Topics: ' + ', '.join(unknown['topics']))
            st.text(item['reason'])
            st.text('Recommended action: ' + item['action'])


def render_pending_diagnostics(record, profile):
    if not record.get('pending_response') or profile is None:
        return
    diagnostic = private_diagnostic_data(record, profile)
    with st.expander('Private retained response — all eight rejected drafts'):
        st.caption('Private rejection diagnostic — not approved for publication. These retained texts and annotations are blocked. '
                   'They are separate from the generated drafts, editors and normal download. Credentials are redacted.')
        for key in DRAFT_KEYS:
            parts = key.split('_')
            label = ('Website draft ' + parts[2]) if parts[0] == 'website' else 'Facebook draft'
            language = 'Dutch' if parts[1] == 'nl' else 'English'
            draft = diagnostic['candidate'][key]
            st.text(label + ' — ' + language + ' · ' + key)
            st.text(draft['text'])
            for number, claim in enumerate(draft['claims'], start=1):
                st.text('Claim ' + str(number) + ' · Qualification: ' + claim.get('qualification', 'Not provided'))
                st.text('Annotated passage: ' + claim.get('excerpt', ''))
                st.text('Exact references: ' + (', '.join(claim.get('source_references', [])) or 'None'))
            for note in draft['review_notes']:
                st.text('Review note: ' + note)
        st.caption('Optional private download: includes the matching retained Animal Profile, rejected response, safe token counts and rejection report. '
                   'Keep it private; it is not an approved adoption-text export. Nothing is saved automatically.')
        st.download_button('Download private rejection diagnostic (JSON)', data=export_private_diagnostic(record, profile),
                           file_name='private-rejection-diagnostic.json', mime='application/json',
                           key='writing_private_rejection_export', on_click='ignore')


def render_content_editor():
    st.markdown('### 7. Bilingual adoption drafts and final website editor')
    st.caption('Draft — Human review required. Copy and publish manually after review. Writing uses only the Animal Profile; it does not re-analyze media.')
    result = st.session_state.get('ai_result')
    profile, problem, profile_failure, scope_hint = None, None, None, None
    if result:
        try:
            value = result.get('profile')
            scope_hint = AnimalProfile.model_validate(value.model_dump() if isinstance(value, AnimalProfile) else value)
        except (ValidationError, ValueError, TypeError):
            pass  # The hint only preserves the editor scope; it never authorizes writing.
    try:
        if not result:
            raise ContentError('Run AI Animal Profile Analysis first to create a valid Animal Profile.')
        profile = validated_profile(result['profile'])
        selected = parse_animal_label(st.session_state.get('media_animal', {}).get('name'))
        if selected.animal_name and profile.identity.animal_name and selected.animal_name != profile.identity.animal_name:
            reason = 'The Animal Profile belongs to another animal. Run AI Animal Profile Analysis for the selected animal first.'
            raise ContentValidationError('PROFILE_WRONG_ANIMAL', reason, origin='animal_profile',
                report=[rejection('PROFILE_WRONG_ANIMAL', reason, origin='animal_profile')])
        if st.session_state.get('ai_outdated'):
            raise ContentError('The Animal Profile is outdated. Run AI Animal Profile Analysis again before generating drafts.')
    except (ContentError, KeyError, TypeError) as exc:
        problem = str(exc) if isinstance(exc, ContentError) else 'Run AI Animal Profile Analysis first to create a valid Animal Profile.'
        if isinstance(exc, ContentValidationError):
            profile_failure = exc
    scope = writing_scope(st.session_state, profile or scope_hint)
    load_workspace_once(scope)
    try:
        if profile is not None:
            records = st.session_state.setdefault('writing_workspaces', {})
            anonymous_scope = writing_scope({})
            source = None
            if anonymous_scope != scope and anonymous_scope in records:
                source = synchronize_scope(st.session_state, anonymous_scope)
            existed = scope in records
            can_migrate = (not existed and source is not None and source.get('bundle') is None
                           and has_human_edits(source) and not st.session_state.get('media_animal')
                           and not st.session_state.get('history_document'))
            if can_migrate and source.get('storage_session_id'):
                prepared = deepcopy(source)
                prepared['animal_name'] = profile.identity.animal_name
                migration = migrate_record(anonymous_scope, scope, prepared)
                clear_scope_state(st.session_state, anonymous_scope, migration['source_deletion_token'])
                records[scope] = migration['record']
                if migration.get('migration_warning'):
                    records[scope].setdefault('storage_warnings', []).append(migration['migration_warning'])
                st.session_state.setdefault('writing_saved_hashes', {})[scope] = _record_digest(records[scope])
            else:
                adopt_unnamed_manual_edits(st.session_state, scope)
            if not existed and source is not None and records.get(scope) is source:
                # This is new work entering a named workspace, after checking
                # that its original unnamed scope has not been deleted.
                from editorial_privacy import deletion_token
                source.update(scope=scope, storage_deletion_token=deletion_token(scope))
                source.pop('storage_session_id', None)
                source.pop('storage_revision', None)
        synchronize_scope(st.session_state, scope)
    except (OSError, ValueError):
        workspace(st.session_state, scope)['storage_error'] = (
            'Local privacy state could not be checked. Saved work is preserved; download a backup.')
    record = workspace(st.session_state, scope)
    analysis_id = result.get('fingerprint') if result else None
    if record.get('error_analysis_id') != analysis_id:
        record.pop('error', None)
        record.pop('validation_details', None)
        record.pop('validation_report', None)
    pending = record.get('pending_response')
    retained_recovery_profile(record, scope, profile)
    if pending and (problem or pending.get('analysis_fingerprint') != analysis_id
                    or profile_fingerprint(profile) != pending.get('profile_fingerprint')):
        record['assessment_stale'] = True
    if profile_failure:
        record['validation_details'] = profile_failure.details
        record['validation_report'] = profile_failure.report
    if record['bundle'] and (problem or profile_fingerprint(profile) != record['profile_fingerprint']
                             or result.get('fingerprint') != record['analysis_fingerprint']):
        record['outdated'] = True
    settings = None
    try:
        settings = load_content_settings()
    except ContentError as exc:
        st.error(str(exc))
    if problem:
        st.info(problem)
    perspective_label = st.radio('Narration for the next generation', ['Third person', 'First person'],
                                  key=f'writing_perspective_{scope}', horizontal=True)
    perspective = 'third_person' if perspective_label == 'Third person' else 'first_person'
    if record['bundle'] and record['bundle'].perspective != perspective:
        st.warning('The selected narration applies only to the next explicit regeneration. Existing drafts and edits are unchanged.')
    if settings:
        st.caption(f'Writing model: {settings.model} · maximum {settings.output_tokens} output tokens · '
                   'one request for all eight drafts · no automatic retries. Generation/regeneration may incur an API charge.')
    confirmed = True
    if record['bundle'] and has_human_edits(record):
        confirmed = st.checkbox('Confirm regeneration: replace generated drafts and preserve my final website text and Facebook edits.',
                                 key=f'writing_regenerate_confirm_{scope}_{record["generated_at"]}')
    label = 'Regenerate eight drafts (paid API request)' if record['bundle'] else 'Generate eight drafts (paid API request)'
    if st.button(label, key='generate_drafts', disabled=not confirmed, type='primary'):
        for field in ('error', 'validation_details', 'validation_report'):
            record.pop(field, None)
        record['error_analysis_id'] = analysis_id
        if not confirmed:
            record['error'] = 'Confirm regeneration before replacing generated drafts.'
        elif problem:
            record['error'] = problem
            if profile_failure:
                record['validation_details'], record['validation_report'] = profile_failure.details, profile_failure.report
        elif settings is None:
            record['error'] = 'Check the writing configuration before generating.'
        else:
            try:
                with st.spinner('Writing six website drafts and two Facebook drafts from the Animal Profile…'):
                    bundle, issues, usage = generate_drafts(profile, settings, perspective)
                # Retain received core text before checking whether the active source context moved.
                # A successful commit removes this recovery record.
                record['pending_response'] = {'candidate': bundle.model_dump(mode='json'), 'usage': usage,
                    'profile_fingerprint': profile_fingerprint(profile), 'analysis_fingerprint': analysis_id,
                    'profile_snapshot': profile.model_dump(mode='json'),
                    'perspective': perspective, 'settings': asdict(settings),
                    'received_at': datetime.now(timezone.utc).isoformat()}
                current = st.session_state.get('ai_result') or {}
                current_profile = validated_profile(current.get('profile'))
                if (st.session_state.get('ai_outdated') or writing_scope(st.session_state, current_profile) != scope
                        or profile_fingerprint(current_profile) != profile_fingerprint(profile)
                        or current.get('fingerprint') != result.get('fingerprint')):
                    record['assessment_stale'] = True
                    raise ContentError('The animal/profile changed while writing. New output was not installed; previous drafts and edits were preserved.')
                commit_generation(record, bundle, profile, settings, issues, usage, result.get('fingerprint'))
            except ContentValidationError as exc:
                record['error'] = str(exc)
                record['validation_details'] = exc.details
                record['validation_report'] = exc.report
                if exc.candidate is not None:
                    record['pending_response'] = {'candidate': exc.candidate, 'usage': exc.usage,
                        'profile_fingerprint': profile_fingerprint(profile), 'analysis_fingerprint': analysis_id,
                        'profile_snapshot': profile.model_dump(mode='json'),
                        'perspective': perspective, 'settings': asdict(settings),
                        'received_at': datetime.now(timezone.utc).isoformat()}
            except ContentError as exc:
                record['error'] = str(exc)
    pending = record.get('pending_response')
    recovery_profile = retained_recovery_profile(record, scope, profile)
    recovery_is_current = (recovery_profile is not None and not problem and profile is not None
                           and profile_fingerprint(recovery_profile) == profile_fingerprint(profile))
    if pending and recovery_profile is not None:
        st.caption('A retained draft response is available for local recovery and assessment. No AI request is made. '
                   'Recovery preserves final website text and Facebook edits, and keeps previous generations in history.')
        if not recovery_is_current:
            st.warning('Recovery uses the saved profile for this animal. Current source freshness is unknown; '
                       'recovered drafts and assessments remain marked as potentially outdated. No new analysis is required for this free recovery.')
        if st.button('Recheck saved drafts (no API charge)', key=f'writing_recheck_{scope}', disabled=not confirmed):
            if not confirmed:
                record['error'] = 'Confirm replacement of generated drafts before accepting the saved response.'
            else:
                try:
                    bundle, issues = recheck_drafts(pending['candidate'], recovery_profile, pending['perspective'])
                    saved_settings = {key: value for key, value in pending.get('settings', {}).items()
                                      if key in ContentSettings.__dataclass_fields__}
                    commit_generation(record, bundle, recovery_profile, ContentSettings(**saved_settings), issues,
                                      pending.get('usage', {}), pending.get('analysis_fingerprint'), generated_at=pending.get('received_at'))
                    if not recovery_is_current:
                        record['outdated'] = record['assessment_stale'] = True
                    st.success('Saved drafts recovered with local review findings. No AI request was made.')
                except ContentValidationError as exc:
                    record['error'], record['validation_details'] = str(exc), exc.details
                    record['validation_report'] = exc.report
                except ContentError as exc:
                    record['error'] = str(exc)
                    record['validation_details'] = {'code': 'DRAFT_RULE', 'origin': 'validation_code'}
                    record['validation_report'] = [rejection('DRAFT_RULE', 'An unclassified validation check failed.', origin='validation_code',
                        action='Keep drafts blocked and share the safe diagnostic code for investigation; do not retry repeatedly or change evidence IDs.')]
                except (ValidationError, ValueError, TypeError, KeyError):
                    record['error'] = 'The saved draft response could not be checked safely. No AI request was made.'
                    record['validation_details'] = {'code': 'SAVED_DRAFT_FORMAT'}
    if record.get('error'):
        st.error(record['error'])
    if record.get('validation_details'):
        with st.expander('Writing validation details (safe to share)'):
            st.json(record['validation_details'])
            st.caption('Share this diagnostic code, draft key and claim number. It contains no draft wording, animal source notes, credentials or API key.')
    render_validation_report(record)
    render_pending_diagnostics(record, recovery_profile)
    review_profile = profile if not problem else None
    if review_profile is None and record.get('profile_snapshot'):
        try:
            review_profile = AnimalProfile.model_validate(record['profile_snapshot'])
        except (ValidationError, ValueError, TypeError):
            pass
    if record['bundle']:
        current_review_fingerprint = profile_fingerprint(review_profile) if review_profile is not None else None
        saved_manual_fingerprint = record.get('assessment_manual_sources_fingerprint')
        manual_sources_changed = (saved_manual_fingerprint != manual_sources_fingerprint(record)
                                 if saved_manual_fingerprint is not None else bool(record.get('manual_sources')))
        record['assessment_stale'] = (bool(problem) or current_review_fingerprint is None or
            (record.get('assessment_profile_fingerprint') or record.get('profile_fingerprint')) != current_review_fingerprint
            or manual_sources_changed)
        if st.button('Reassess all eight drafts locally (free)', key=f'writing_reassess_{scope}', disabled=review_profile is None):
            record['assessment'] = safe_assessment(record['bundle'], review_profile, record.get('manual_sources', []))
            record['assessment_profile_fingerprint'] = profile_fingerprint(review_profile)
            record['assessment_profile_snapshot'] = review_profile.model_dump(mode='json')
            record['assessment_manual_sources_fingerprint'] = manual_sources_fingerprint(record)
            record['assessment_stale'] = bool(problem)
        if record.get('assessment_stale'):
            st.warning('The draft assessment is stale because its source context changed. Earlier results are preserved; reassess against the current sources locally.')
        if (record.get('assessment') or {}).get('incomplete') or record.get('assessment_error'):
            st.warning('Local assessment could not finish. All draft texts and editor work are preserved; try the free local reassessment control.')
        render_questions(record, scope, review_profile, persist_record)
    assessment_profile = None
    expected_fingerprint = record.get('assessment_profile_fingerprint') or record.get('profile_fingerprint')
    for candidate in (review_profile, record.get('assessment_profile_snapshot'), record.get('profile_snapshot')):
        if candidate:
            try:
                value = AnimalProfile.model_validate(candidate)
                if profile_fingerprint(value) == expected_fingerprint:
                    assessment_profile = value
                    break
            except (ValidationError, ValueError, TypeError):
                pass
    assessment_manual_sources = record.get('manual_sources', [])
    if (record.get('assessment_manual_sources_fingerprint') != manual_sources_fingerprint(record)
            and assessment_manual_sources):
        # A source ID may have been revised; never show its new wording as support
        # for a retained assessment of the earlier response.
        assessment_manual_sources = []
    if record['outdated']:
        st.warning('Potentially outdated: these drafts were generated from an earlier profile/source state. '
                   'Preserved text can be reviewed locally against the current profile for free. '
                   'Analyze changed sources if needed; generating new wording is optional and may incur a charge.')
    publication_profile = assessment_profile or review_profile
    if record.get('profile_snapshot'):
        try:
            publication_profile = AnimalProfile.model_validate(record['profile_snapshot'])
        except (ValidationError, ValueError, TypeError):
            pass
    language_label = st.segmented_control('Language', ['Nederlands', 'English'], default='Nederlands',
                                          key=f'writing_language_{scope}') or 'Nederlands'
    language = 'nl' if language_label == 'Nederlands' else 'en'
    render_version_history(record, scope, language, on_save=persist_record)
    bundle = record['bundle']
    if bundle:
        selected_draft = st.segmented_control('Website draft', ['Draft 1', 'Draft 2', 'Draft 3'], default='Draft 1',
                                              key=f'writing_draft_{scope}') or 'Draft 1'
        number = selected_draft[-1]
        draft = getattr(bundle, f'website_{language}_{number}')
        st.caption(f'{selected_draft} — {STYLES[number]} · {len(draft.text.split())} words · {bundle.perspective.replace("_", " ")}')
        with st.container(border=True):
            render_review(f'website_{language}_{number}', draft.text,
                          (record.get('assessment') or {}).get('drafts', {}).get(f'website_{language}_{number}'),
                          assessment_profile, assessment_manual_sources)
        with st.expander('Original plain draft text'):
            st.text(draft.text)
        copied_draft = publication_copy_text(draft.text, publication_profile)
        if copied_draft != draft.text:
            st.info('Technical citation markers are excluded from Copy Draft. The original draft is retained above. '
                    'When selecting text manually, remove these markers before publishing; factual wording still requires human review.')
        copy_button('Copy Draft', copied_draft, f'copy_draft_{scope}_{language}_{number}')
        with st.expander('Current draft evidence and notes'):
            st.json({'claims': [item.model_dump(mode='json') for item in draft.claims], 'review_notes': draft.review_notes})
    st.markdown('**Final Website Text**')
    st.caption('Copy selected sentences/paragraphs from any draft (Ctrl+C), paste here (Ctrl+V), and edit freely. Each language has its own final text. Generation never fills or overwrites this editor.')
    render_editor(scope, record, 'final', language, language_label)
    render_editor_review(record, scope, 'final', language, review_profile, persist_record)
    st.markdown('### 8. Facebook draft editor')
    st.caption('Draft — Human review required. This language has its own editable Facebook text. Human edits survive regeneration.')
    render_editor(scope, record, 'facebook', language, language_label)
    render_editor_review(record, scope, 'facebook', language, review_profile, persist_record)
    if bundle:
        with st.expander('Latest generated Facebook version (edited text above is kept separately)'):
            generated_facebook = getattr(bundle, 'facebook_' + language)
            render_review('facebook_' + language, generated_facebook.text,
                          (record.get('assessment') or {}).get('drafts', {}).get('facebook_' + language),
                          assessment_profile, assessment_manual_sources)
            with st.expander('Original plain Facebook draft text'):
                st.text(generated_facebook.text)
            copied_facebook = publication_copy_text(generated_facebook.text, publication_profile)
            if copied_facebook != generated_facebook.text:
                st.info('Technical citation markers are excluded from Copy generated Facebook draft. '
                        'The original draft is retained above. Remove markers from manually selected text before publishing; review all factual wording.')
            copy_button('Copy generated Facebook draft', copied_facebook, f'copy_generated_facebook_{scope}_{language}')
            st.json({'claims': [item.model_dump(mode='json') for item in generated_facebook.claims], 'review_notes': generated_facebook.review_notes})
        with st.expander('Review warnings, missing information and API usage', expanded=True):
            st.caption('Review every version for factual accuracy and translation equivalence. Automated checks are limited to structure, citations, coverage and selected consistency signals.')
            for warning in record['profile_warnings']:
                st.text('Animal Profile — ' + warning['category'] + ': ' + warning['message'])
            for unknown in record.get('unknown_information', []):
                st.text('Unknown: ' + unknown)
            for issue in record['issues']:
                st.text((issue['draft'] + ': ' if issue.get('draft') else '') + issue['message'])
            st.text(f'Generation model: {record["model"]} · UTC: {record["generated_at"]}')
            usage = record['usage']
            counts = {field: str(usage[field]) if usage.get(field) is not None else 'Unavailable'
                      for field in ('input_tokens', 'output_tokens', 'total_tokens')}
            st.text('API tokens — input: ' + counts['input_tokens'] +
                    ' · output: ' + counts['output_tokens'] + ' · total: ' + counts['total_tokens'])
            st.caption('Token counts are usage, not a verified monetary price. A failed request may also incur usage.')
    st.caption('Finish an edit by clicking outside its text box before downloading the saved texts.')
    st.download_button('Download texts and drafts (JSON)', data=export_workspace(record),
                        file_name='adoption-drafts.json', mime='application/json', key=f'writing_export_{scope}', on_click='ignore')
    persist_record(scope, record)
    render_privacy_controls(record, scope)
    if record.get('storage_error'):
        st.warning(record['storage_error'])
    for warning in record.get('storage_warnings', []):
        st.warning(warning)
    st.caption('Autosaved privately on this computer for this animal. Downloads provide an additional backup. Copy and publish manually after human review.')
