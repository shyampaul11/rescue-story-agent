"""Per-animal session writing workspaces and explicit editor history. No I/O or API."""
from datetime import datetime, timezone
import hashlib
import json

from content_models import DRAFT_KEYS
from content_service import profile_fingerprint, safe_assessment
from copy import deepcopy
from animal_profile import AnimalProfile, Evidence
from draft_evidence import private_text


def writing_scope(state, profile=None):
    animal = state.get('media_animal') or {}
    if animal.get('id'):
        value = 'drive:' + animal['id']
    else:
        document = state.get('history_document')
        file_id = getattr(document, 'file_id', None)
        if file_id:
            value = 'document:' + str(file_id)
        else:
            name = profile.identity.animal_name if profile else None
            value = 'manual:' + (' '.join(name.casefold().split()) if name else 'unidentified')
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]


def workspace(state, scope):
    return state.setdefault('writing_workspaces', {}).setdefault(scope, {
        'bundle': None, 'final': {'nl': '', 'en': ''}, 'facebook': {'nl': '', 'en': ''},
        'facebook_baseline': {'nl': '', 'en': ''}, 'undo': {}, 'outdated': False,
        'issues': [], 'profile_warnings': [], 'usage': {}, 'model': None, 'generated_at': None,
        'profile_fingerprint': None, 'analysis_fingerprint': None, 'animal_name': None,
    })


def adopt_unnamed_manual_edits(state, scope):
    """When the first manual profile gains a name, keep pre-analysis human writing visible."""
    records = state.get('writing_workspaces', {})
    unnamed_scope = writing_scope({})
    unnamed = records.get(unnamed_scope)
    if (scope != unnamed_scope and scope not in records and not state.get('media_animal')
            and not state.get('history_document') and unnamed and unnamed['bundle'] is None
            and has_human_edits(unnamed)):
        records[scope] = records.pop(unnamed_scope)


def has_human_edits(record):
    return any(record['final'].values()) or any(record['undo'].values()) or any(
        record['facebook'][lang] != record['facebook_baseline'][lang] for lang in ('nl', 'en'))


def remember_edit(record, target, language, value):
    if target not in ('final', 'facebook') or language not in ('nl', 'en'):
        raise ValueError('Invalid editor')
    previous = record[target][language]
    if previous == value:
        return
    key = target + '_' + language
    record['undo'].setdefault(key, []).append(previous)
    record['undo'][key] = record['undo'][key][-10:]
    record[target][language] = value
    record.setdefault('editor_statuses', {}).pop(target + '_' + language, None)


def restore_edit(record, target, language):
    previous = record['undo'].get(target + '_' + language, [])
    if previous:
        record[target][language] = previous.pop()
        record.setdefault('editor_statuses', {}).pop(target + '_' + language, None)
    return record[target][language]


def commit_generation(record, bundle, profile, settings, issues, usage, analysis_fingerprint, generated_at=None):
    if record.get('bundle') is not None:
        record.setdefault('history', []).append({'bundle': record['bundle'].model_dump(mode='json'),
            'generated_at': record.get('generated_at'), 'profile_fingerprint': record.get('profile_fingerprint'),
            'assessment': deepcopy(record.get('assessment')), 'profile_snapshot': deepcopy(record.get('profile_snapshot'))})
    # A regenerated Facebook version replaces only pristine generated text, never a human edit (including a deliberate empty string).
    for lang in ('nl', 'en'):
        if record['facebook'][lang] == record['facebook_baseline'][lang]:
            record['facebook'][lang] = getattr(bundle, 'facebook_' + lang).text
        record['facebook_baseline'][lang] = getattr(bundle, 'facebook_' + lang).text
    record.update(bundle=bundle, profile_fingerprint=profile_fingerprint(profile),
        analysis_fingerprint=analysis_fingerprint, model=settings.model, generated_at=generated_at or datetime.now(timezone.utc).isoformat(),
        issues=[item.model_dump(mode='json') for item in issues], usage=usage,
        profile_warnings=[item.model_dump(mode='json') for item in profile.review_warnings],
        unknown_information=list(profile.unknown_information), animal_name=profile.identity.animal_name, outdated=False)
    record['profile_snapshot'] = profile.model_dump(mode='json')
    record['assessment'] = safe_assessment(bundle, profile, record.get('manual_sources', ()))
    record['assessment_profile_fingerprint'] = profile_fingerprint(profile)
    record['assessment_profile_snapshot'] = profile.model_dump(mode='json')
    record['assessment_stale'] = False
    record['assessment_manual_sources_fingerprint'] = hashlib.sha256(json.dumps(
        record.get('manual_sources', []), sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    record.setdefault('confirmations', [])
    record.setdefault('manual_sources', [])
    for key in ('error', 'validation_details', 'validation_report', 'pending_response'):
        record.pop(key, None)


def export_workspace(record):
    """Explicit allowlist: no session dump, credentials, source paths or OAuth data."""
    bundle = record['bundle']
    value = {'status': 'Draft — Human review required', 'animal_name': record['animal_name'],
        'exported_at': datetime.now(timezone.utc).isoformat(), 'generated_at': record['generated_at'],
        'model': record['model'], 'potentially_outdated': record['outdated'],
        'final_website_nl': record['final']['nl'], 'final_website_en': record['final']['en'],
        'facebook_nl': record['facebook']['nl'], 'facebook_en': record['facebook']['en'],
        'generated_drafts': {key: getattr(bundle, key).text for key in DRAFT_KEYS} if bundle else {},
        'evidence_annotations': {key: [claim.model_dump(mode='json') for claim in getattr(bundle, key).claims] for key in DRAFT_KEYS} if bundle else {},
        'profile_review_warnings': record['profile_warnings'], 'writing_review_issues': record['issues'],
        'unknown_information': record.get('unknown_information', []), 'token_usage': record['usage']}
    value.update(editorial_assessment=record.get('assessment'),
        manual_confirmations=record.get('confirmations', []), supplementary_sources=record.get('manual_sources', []),
        editor_review_statuses=record.get('editor_statuses', {}))
    return json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')


def private_review_value(value):
    """Redact every string in a detached private-review value, including dictionary keys."""
    if isinstance(value, str):
        return private_text(value)
    if isinstance(value, dict):
        return {private_text(key): private_review_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [private_review_value(item) for item in value]
    return value


def private_diagnostic_data(record, profile):
    """Explicit private allowlist; retained output remains blocked and separate from normal export."""
    pending = record.get('pending_response') or {}
    raw = pending.get('candidate') or {}
    # A legacy diagnostic may predate tolerant wire parsing. Normalize metadata
    # before rendering it; this never assigns evidence or changes the prose.
    from content_models import retained_drafts
    try:
        raw = retained_drafts(raw).model_dump(mode='json')
    except (ValueError, TypeError):
        raw = raw if isinstance(raw, dict) else {}
    candidate = {'perspective': raw.get('perspective')}
    for key in DRAFT_KEYS:
        draft = raw.get(key) or {}
        if not isinstance(draft, dict):
            draft = {}
        claims = draft.get('claims', [])
        notes = draft.get('review_notes', [])
        candidate[key] = {'text': draft.get('text', ''),
            'claims': [{field: claim[field] for field in ('excerpt', 'source_references', 'qualification') if field in claim}
                       for claim in (claims if isinstance(claims, list) else []) if isinstance(claim, dict)],
            'review_notes': [note for note in notes if isinstance(note, str)] if isinstance(notes, list) else []}
    report_fields = ('code', 'draft', 'language', 'claim_number', 'excerpt', 'source_references', 'reason', 'origin',
        'action', 'failure_type', 'required_fact', 'essential_reason', 'text_presence', 'annotation_status',
        'classification', 'claim_qualification')
    report = []
    for item in record.get('validation_report') or []:
        entry = {field: item[field] for field in report_fields if field in item}
        if item.get('relevant_evidence'):
            entry['relevant_evidence'] = [{field: evidence[field] for field in Evidence.model_fields if field in evidence}
                                          for evidence in item['relevant_evidence']]
        if item.get('relevant_unknowns'):
            entry['relevant_unknowns'] = [{field: unknown[field] for field in ('reference', 'text', 'topics', 'status', 'state',
                'scope', 'subject', 'source_type', 'verification_status', 'temporal_status') if field in unknown}
                                         for unknown in item['relevant_unknowns']]
        report.append(entry)
    current_profile = profile if isinstance(profile, AnimalProfile) else AnimalProfile.model_validate(profile)
    usage = pending.get('usage') or {}
    safe_usage = {field: usage[field] for field in ('input_tokens', 'output_tokens', 'total_tokens')
                  if type(usage.get(field)) is int and usage[field] >= 0}
    return private_review_value({'status': 'Private rejection diagnostic — not approved for publication', 'approved': False,
        'profile': current_profile.model_dump(mode='json'), 'candidate': candidate,
        'token_usage': safe_usage, 'validation_report': report})


def export_private_diagnostic(record, profile):
    """Prepare bytes only for the explicit private download; never save files or modify state."""
    return json.dumps(private_diagnostic_data(record, profile), ensure_ascii=False, indent=2).encode('utf-8')
