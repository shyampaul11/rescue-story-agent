"""Milestone 5 UI. Analysis happens only inside the explicit button branch."""
from dataclasses import asdict
import hashlib
import json

import streamlit as st

import ai_service
from ai_config import AnalysisError, load_settings, require_credentials
from analysis_media import selected_files, prepare_selected_media, PreparedImage
from animal_profile import AnimalSources, AnimalProfile, AnimalContext, PhotoDateReference
from analysis_validation import ProfileOutputError, ProfileRuleError, schema_details
from pydantic import ValidationError
import drive_client as drive
from source_context import parse_animal_label, photo_date_reference


def input_fingerprint(sources, files, context, settings):
    # Full file metadata stays local; only parsed identity/attribution and photo dates enter the payload.
    value = {'sources': sources.model_dump(), 'files': [
        {key: file.get(key) for key in ('id', 'name', 'size', 'modifiedTime', 'kind', 'imageMediaMetadata')} for file in files],
        'context': context, 'settings': asdict(settings)}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode('utf-8')).hexdigest()


def render_profile(profile, source_map):
    st.markdown('**Animal Profile — human review required**')
    st.text('Animal name: ' + (profile.identity.animal_name or 'Unknown from permitted text'))
    st.caption('Identity status: ' + profile.identity.verification_status)
    if profile.source_attribution:
        st.text('Rescuer / information provider: ' + (profile.source_attribution.rescuer_name or 'Unknown'))
        st.caption(profile.source_attribution.verification_note)
    evidence = {item.id: item for item in profile.evidence}
    review_only = set(getattr(profile, 'review_only_evidence_ids', []))

    def show_claims(ids):
        if not ids:
            st.caption('No supported information available.')
        for ref in ids:
            item = evidence[ref]
            with st.container(border=True):
                if ref in review_only:
                    st.caption('REVIEW ONLY — excluded from reported facts and adoption draft generation.')
                st.text(item.claim)
                st.caption(f'{item.id} · {item.source_type} · {source_map.get(item.source_reference, item.source_reference)}')
                st.caption(f'{item.verification_status} · {item.temporal_status} · subject: {item.subject}')
                if item.reported_by:
                    st.caption('Reported by rescuer: ' + item.reported_by)
                st.text('Evidence: ' + item.supporting_text_or_description)

    with st.expander('Identity and identification'):
        show_claims(([profile.identity.name_evidence_id] if profile.identity.name_evidence_id else [])
                    + profile.identity.identification_evidence_ids)
    with st.expander('Photo reference dates and rough age', expanded=True):
        if not profile.photo_date_references:
            st.caption('No selected photo dates available in this analysis.')
        for metadata in profile.photo_date_references:
            st.text(f'{source_map.get(metadata.source_reference, metadata.source_reference)}: '
                    f'{metadata.reference_date or "Unknown photo date"}')
            st.caption(f'{metadata.date_source} · original: {metadata.original_value or "Not available"} · {metadata.verification_status}')
            st.text(metadata.note)
        age = profile.age_assessment
        if age:
            st.text('Rough age / life stage: ' + (age.estimate or 'Unknown — insufficient evidence'))
            st.caption(f'{age.status} · reference date: {age.reference_date or "Unknown"}')
            st.text(age.limitations)
            if age.basis_evidence_ids:
                show_claims(age.basis_evidence_ids)
    for field, label in [('rescue_background', 'Rescue background'),
                         ('personality_and_behavior', 'Personality and behavior'),
                         ('physical_appearance', 'Visual appearance'),
                         ('health_and_special_needs', 'Health and special needs'),
                         ('adoption_considerations', 'Adoption considerations')]:
        with st.expander(label, expanded=True):
            show_claims(getattr(profile, field))
    with st.expander('Updates', expanded=True):
        if not profile.updates:
            st.caption('No updates extracted.')
        for update in profile.updates:
            st.text('Original date: ' + (update.original_date or 'Undated'))
            st.caption(f'Normalized date: {update.normalized_date or "Not normalized"} · {update.change_type}')
            st.text(update.source_excerpt)
            show_claims(update.evidence_ids)
            if update.superseded_evidence_ids:
                st.text('Superseded earlier reports (retained for history):')
                show_claims(update.superseded_evidence_ids)
            for conflict in update.unresolved_contradictions:
                st.text('Unresolved contradiction: ' + conflict)
    with st.expander('Unknown information', expanded=True):
        for item in profile.unknown_information:
            st.text('• ' + item)
    with st.expander('Review warnings', expanded=True):
        for warning in profile.review_warnings:
            st.text(f'{warning.category}: {warning.message}')
            if warning.evidence_ids:
                st.caption('Evidence: ' + ', '.join(warning.evidence_ids))
    if review_only:
        with st.expander('Review-only observations (excluded from adoption drafts)', expanded=True):
            st.caption('The model placed these non-text items in reported-fact sections. Confirm interpretations with the rescuer. '
                       'Supported text facts and correctly categorized visual observations remain available.')
            show_claims([item.id for item in profile.evidence if item.id in review_only])
    with st.expander('All evidence records (including other animals)'):
        show_claims(list(evidence))
    with st.expander('Animal Profile JSON (debugging and evaluation)'):
        st.json(profile.model_dump(mode='json'))
        st.caption('Local source reference map (not sent as factual input to OpenAI)')
        st.json(source_map)


def save_profile(profile, fingerprint, source_map):
    st.session_state.ai_result = {'profile': profile, 'fingerprint': fingerprint,
        'source_map': dict(history='History', other_info='Other Info',
                         updates='Updates (please note the date of the update)',
                         animal_label='Selected dog name (user-explained naming convention)',
                         rescuer_attribution='Bracketed rescuer name (user-explained naming convention)', **source_map)}
    st.session_state.ai_outdated = False
    for key in ('ai_error', 'ai_validation_details', 'ai_pending_validation'):
        st.session_state.pop(key, None)


def pending_validation(error, sources, images, context, fingerprint, source_map):
    """One session-only rejected candidate; keep media references/dates, never image bytes."""
    if error.candidate is None:
        return None
    return {'candidate': error.candidate.model_dump(mode='json'), 'sources': sources.model_dump(),
        'context': context.model_dump(), 'fingerprint': fingerprint, 'source_map': dict(source_map),
        'media': [{'reference': item.reference, 'source_type': item.source_type,
                   'timestamp_seconds': item.timestamp_seconds,
                   'photo_date': item.photo_date.model_dump(mode='json') if item.photo_date else None} for item in images]}


def recheck_candidate(pending):
    images = [PreparedImage(item['reference'], item['source_type'], b'', item['timestamp_seconds'],
                           PhotoDateReference.model_validate(item['photo_date']) if item['photo_date'] else None)
              for item in pending['media']]
    return ai_service.validate_candidate(AnimalProfile.model_validate(pending['candidate']),
        AnimalSources.model_validate(pending['sources']), images, AnimalContext.model_validate(pending['context']))


def render_analysis(history, other_info, updates):
    st.markdown('### 6. AI Animal Profile Analysis')
    sources = AnimalSources(history=history, other_info=other_info, updates=updates)
    animal_context = parse_animal_label(st.session_state.get('media_animal', {}).get('name'))
    problem, settings, files, fingerprint = None, None, [], None
    try:
        settings = load_settings()
        ai_service.validate_sources(sources, settings)
        files = selected_files(st.session_state.get('media_catalog', {}),
                               st.session_state.get('media_selected', []), settings)
        context = {'animal': st.session_state.get('media_animal', {}).get('id'),
                   'identity_and_attribution': animal_context.model_dump(),
                   'document': st.session_state.get('drive_source', {}),
                   'manual_document': getattr(st.session_state.get('history_document'), 'file_id', None)}
        fingerprint = input_fingerprint(sources, files, context, settings)
    except AnalysisError as exc:
        problem = str(exc)
    st.caption('Text sources: ' + ', '.join(f'{label}: {len(value)} characters' for label, value in
        [('History', history), ('Other Info', other_info), ('Updates (dates retained)', updates)]))
    st.text(f'Selected media: {len(st.session_state.get("media_selected", []))} file(s).')
    for file in files:
        st.text(f'{file["kind"]}: {file["name"]}')
        if file['kind'] == 'image':
            metadata = photo_date_reference('selected photo', [('drive_photo_time', file.get('imageMediaMetadata', {}).get('time'))])
            st.caption(f'Drive photo reference date: {metadata.reference_date or "Not available"}. Embedded EXIF is checked when you analyze.')
    if animal_context.rescuer_name:
        st.text('Rescuer / provider of History, Other Info and Updates: ' + animal_context.rescuer_name)
    if settings:
        st.caption(f'Model: {settings.model or "Set OPENAI_MODEL in .env"}. Limits: {settings.max_photos} photos, '
            f'{settings.max_videos} video, {settings.max_frames} sampled frames; {settings.photo_mb} MB/photo, '
            f'{settings.video_mb} MB/video; {settings.image_side} px; {settings.video_seconds}s video timeout; '
            f'{settings.output_tokens} output tokens. No audio analysis or automatic AI retries.')
    st.info('Analyze Animal sends these three text sections, the parsed dog/rescuer names, selected photos/video frames '
            'and available photo reference dates to the external OpenAI API. '
            'Only selected media is included. Review the sources before sending. API usage is billable.')
    consent = st.checkbox('I confirm that the selected sources may be sent to OpenAI for this analysis.', key='ai_consent')
    if problem:
        st.error(problem)
    clicked = st.button('Analyze Animal', key='analyze_animal', disabled=not consent or problem is not None)
    if clicked and consent and problem is None:
        for key in ('ai_error', 'ai_validation_details', 'ai_pending_validation'):
            st.session_state.pop(key, None)
        st.session_state.ai_attempt_fingerprint = fingerprint
        try:
            require_credentials(settings)  # Check before downloading/decoding private media.
            with st.spinner('Preparing selected media and analyzing the animal…'):
                images, source_map = prepare_selected_media(st.session_state.get('drive_service'), files, settings)
                profile = ai_service.analyze_animal(sources, images, settings, context=animal_context)
            # Commit only a complete, validated result. Reruns simply render this snapshot.
            save_profile(profile, fingerprint, source_map)
        except ProfileOutputError as exc:
            st.session_state.ai_error = str(exc)
            st.session_state.ai_validation_details = exc.details
            pending = pending_validation(exc, sources, images, animal_context, fingerprint, source_map)
            if pending:
                st.session_state.ai_pending_validation = pending
        except (AnalysisError, drive.DriveError) as exc:
            st.session_state.ai_error = str(exc)
    attempt = st.session_state.get('ai_attempt_fingerprint')
    if attempt is not None and attempt != fingerprint:
        for key in ('ai_error', 'ai_validation_details', 'ai_pending_validation'):
            st.session_state.pop(key, None)
    pending = st.session_state.get('ai_pending_validation')
    if pending and pending['fingerprint'] == fingerprint:
        st.caption('The rejected structured response is kept only in this session. Rechecking it is local and makes no API request; it cannot change or bypass the evidence rules.')
        if st.button('Recheck saved response (no API charge)', key='recheck_analysis'):
            try:
                profile = recheck_candidate(pending)
                save_profile(profile, fingerprint, pending['source_map'])
                st.success('The saved response passed local validation. No API request was made.')
            except ProfileRuleError as exc:
                st.session_state.ai_error = str(ProfileOutputError('evidence', exc.code, exc.explanation, exc.location))
                st.session_state.ai_validation_details = {'stage': 'evidence', 'code': exc.code, **exc.location}
            except ValidationError as exc:
                st.session_state.ai_error = 'The saved response failed structured validation. No API request was made.'
                st.session_state.ai_validation_details = {'stage': 'structured', 'code': 'SCHEMA', **schema_details(exc)}
            except (ValueError, TypeError, KeyError):
                st.session_state.ai_error = 'The saved response could not be rechecked safely. No API request was made.'
    if st.session_state.get('ai_error'):
        st.error(st.session_state.ai_error)
    if st.session_state.get('ai_validation_details'):
        with st.expander('Analysis validation details (safe to share)'):
            st.json(st.session_state.ai_validation_details)
            st.caption('Diagnostic codes, field names and positions only. No API keys, credentials, source text or media are included.')
    result = st.session_state.get('ai_result')
    if result:
        if result['fingerprint'] != fingerprint:
            st.session_state.ai_outdated = True
        if st.session_state.get('ai_outdated'):
            st.warning('Outdated analysis: sources, animal, selections or AI settings changed. This is the previous result. Click Analyze Animal to create a new profile.')
        render_profile(result['profile'], result['source_map'])
