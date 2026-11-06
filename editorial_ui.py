"""Escaped evidence viewers and local, attributed editorial review controls."""
from html import escape
import hashlib
import json
import re
from datetime import date

import streamlit as st

from clipboard_component import copy_button
from content_service import profile_fingerprint
from editorial_store import record_confirmation, add_manual_source


COLORS = {
    'green': ('Source supported — not independently verified', '#d7efdf', '#163b23'),
    'yellow': ('Needs clarification', '#fff0bb', '#523b00'),
    'red': ('Unsupported or conflicting', '#fbd5d5', '#611c1c'),
}


def publication_copy_text(text, profile):
    """Remove only known technical citation tokens; preserve all other prose verbatim."""
    if profile is None:
        return text
    from draft_evidence import reference_catalog
    for reference in sorted(reference_catalog(profile), key=len, reverse=True):
        token = re.escape(reference)
        explicit = (r'\[\s*' + token + r'\s*\]|\{\s*' + token + r'\s*\}|'
                    r'\b(?:evidence|source|bron|reference)\s*:\s*' + token + r'(?!\w)')
        text = re.sub(explicit, '', text, flags=re.I)
        if re.search(r'[A-Za-z].*\d|[_:]', reference):
            text = re.sub(r'(?<!\w)' + token + r'(?!\w)', '', text)
    return text


def highlighted_html(text, passages):
    """Build only app-owned markup; offsets always refer to the original prose."""
    valid = []
    for item in passages or []:
        if not isinstance(item, dict):
            continue
        start, end, color = item.get('start'), item.get('end'), item.get('color')
        if (type(start) is int and type(end) is int and 0 <= start < end <= len(text)
                and isinstance(color, str) and color in COLORS):
            valid.append((start, end, color))
    priority = {'green': 1, 'yellow': 2, 'red': 3}
    output, offset = [], 0
    for paragraph in text.split('\n\n'):
        stop = offset + len(paragraph)
        boundaries = sorted({offset, stop} | {max(offset, start) for start, end, _ in valid if start < stop and end > offset}
                            | {min(stop, end) for start, end, _ in valid if start < stop and end > offset})
        parts = []
        for start, end in zip(boundaries, boundaries[1:]):
            part = escape(text[start:end], quote=True)
            active = [color for left, right, color in valid if left <= start and right >= end]
            if active:
                color = max(active, key=priority.get)
                label, background, foreground = COLORS[color]
                part = (f'<mark data-review="{color}" aria-label="{label}" '
                        f'style="background:{background};color:{foreground};border-bottom:2px solid {foreground}">{part}</mark>')
            parts.append(part)
        output.append('<p style="white-space:pre-wrap;overflow-wrap:anywhere">' + ''.join(parts) + '</p>')
        offset = stop + 2
    return '<div class="editorial-review">' + ''.join(output) + '</div>'


def _render_sources(references, profile, manual_sources):
    """Resolve only permitted selected-animal records, against the assessment's profile."""
    from draft_evidence import reference_catalog
    catalog = reference_catalog(profile) if profile else {}
    selected = {item.id: item for item in profile.evidence
                if item.subject == 'selected_animal' and item.id not in profile.review_only_evidence_ids} if profile else {}
    manual = {item['id']: item for item in manual_sources if isinstance(item, dict) and item.get('id')}
    for reference in dict.fromkeys(references or []):
        if reference in selected:
            evidence = selected[reference]
            sections = [section.replace('_', ' ') for section in ('rescue_background', 'personality_and_behavior',
                'physical_appearance', 'health_and_special_needs', 'adoption_considerations')
                if reference in getattr(profile, section)]
            st.text('Source: ' + evidence.source_reference + ' · Section: ' + (', '.join(sections) or 'Identity / supporting evidence'))
            st.text('Profile claim: ' + evidence.claim)
            st.text('Source wording / observation: ' + evidence.supporting_text_or_description)
            st.text('Evidence status: ' + evidence.verification_status + ' · Time: ' + evidence.temporal_status)
        elif reference in manual:
            source = manual[reference]
            st.text('Supplementary reported response — ' + str(source.get('attribution', '')) + ' · ' + str(source.get('confirmation_date', '')))
            st.text(str(source.get('text', '')))
            st.text('Reported, not independently verified; preserve the response’s time and uncertainty.')
        elif reference in catalog:
            st.text('Profile context (' + catalog[reference]['kind'] + '): ' + catalog[reference]['text'])
        else:
            st.text('This reference is unavailable in the assessment’s permitted source snapshot.')


def render_review(key, text, assessment, profile=None, manual_sources=()):
    """Display the immutable text with accessible colors and every independent concern."""
    assessment = assessment or {}
    status = {'red': 'Contains unsupported or contradictory claims',
              'yellow': 'Needs clarification',
              'green': 'Generated — Awaiting volunteer review'}.get(
                  assessment.get('status'), 'Generated — Awaiting review')
    st.caption('Review status: ' + status + '. Automated findings guide human review.')
    st.caption('Green: supported by the available source, not independently verified. '
               'Local automated assessment cannot guarantee factual accuracy. '
               'Yellow: needs clarification · Red: unsupported or conflicting. '
               'Unmarked text has not been established as supported. Human review required.')
    st.html(highlighted_html(text, assessment.get('passages', [])))
    with st.expander('Reasons and source references', expanded=False):
        entries = assessment.get('findings') or []
        for item in entries:
            color = item.get('color', 'yellow')
            label = COLORS.get(color, COLORS['yellow'])[0]
            st.text(label + (' — ' + str(item['excerpt']) if item.get('excerpt') else ' — Whole draft / unmapped concern'))
            st.text(str(item.get('reason', 'Review this passage.')))
            st.text('Sources: ' + (', '.join(map(str, item.get('source_references') or [])) or 'No supporting reference'))
            _render_sources(item.get('source_references'), profile, manual_sources)
            if item.get('support_candidates'):
                st.text('Possible source records for volunteer comparison — not assigned citations')
                _render_sources(item['support_candidates'], profile, manual_sources)
            if item.get('action'):
                st.text('Next step: ' + str(item['action']))
        # Positively supported passages need explanations too, even without findings.
        for number, item in enumerate(assessment.get('passages') or [], start=1):
            start, end = item.get('start'), item.get('end')
            if type(start) is int and type(end) is int and 0 <= start < end <= len(text):
                st.text(str(number) + '. ' + COLORS.get(item.get('color'), COLORS['yellow'])[0] + ' — ' + text[start:end])
            if item.get('color') == 'green':
                st.text('Supported — ' + str(item.get('reason', 'Supported by the listed source.')))
                st.text('Sources: ' + (', '.join(map(str, item.get('source_references') or [])) or 'None'))
                _render_sources(item.get('source_references'), profile, manual_sources)
            if item.get('support_candidates'):
                st.text('Possible source records for volunteer comparison — not assigned citations')
                _render_sources(item['support_candidates'], profile, manual_sources)
        if not entries and not assessment.get('passages'):
            st.text('No local assessment is available. Use the free local assessment control.')


def text_fingerprint(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def manual_sources_fingerprint(record):
    return text_fingerprint(json.dumps(record.get('manual_sources', []), sort_keys=True, ensure_ascii=False))


def _save(record, scope, on_save):
    if on_save:
        on_save(scope, record)


def _set_finalized(record, scope, target, language, on_save):
    slot = target + '_' + language
    key = f'writing_finalize_{scope}_{slot}'
    record.setdefault('editor_statuses', {})[slot] = {
        'finalized': bool(st.session_state.get(key)), 'text_hash': text_fingerprint(record[target][language])}
    _save(record, scope, on_save)


def render_editor_review(record, scope, target, language, profile, on_save=None):
    """Local review is independent from generation and never rewrites the editor."""
    slot = target + '_' + language
    text = record[target][language]
    current_text = text_fingerprint(text)
    current_profile = profile_fingerprint(profile) if profile else None
    current_sources = manual_sources_fingerprint(record)
    reviews = record.setdefault('editor_assessments', {})
    if st.button('Assess this edited text locally (free)', key=f'writing_assess_{scope}_{slot}',
                 disabled=profile is None or not text.strip()):
        try:
            from editorial_assessment import assess_text
            review = assess_text(text, profile, draft_key=slot, manual_sources=record.get('manual_sources', []))
            review.update(text_fingerprint=current_text, profile_fingerprint=current_profile,
                          manual_sources_fingerprint=current_sources)
            reviews[slot] = review
            record.pop('editor_assessment_error', None)
            _save(record, scope, on_save)
        except Exception:
            st.warning('Local assessment could not finish. Your text and previous review are preserved.')
    previous = reviews.get(slot)
    if previous:
        stale = (previous.get('text_fingerprint') != current_text or previous.get('profile_fingerprint') != current_profile
                 or previous.get('manual_sources_fingerprint') != current_sources
                 or st.session_state.get('ai_outdated', False))
        if stale:
            st.warning('This edited-text assessment is stale. The wording or sources changed; assess again locally for current highlights.')
            with st.expander('Previous assessment findings (earlier wording / sources)'):
                for item in previous.get('findings', []):
                    st.text(str(item.get('reason', 'Review needed.')))
        else:
            render_review(slot, text, previous, profile, record.get('manual_sources', []))
    finalized = record.get('editor_statuses', {}).get(slot, {})
    key = f'writing_finalize_{scope}_{slot}'
    st.session_state[key] = bool(finalized.get('finalized') and finalized.get('text_hash') == current_text)
    st.checkbox('Mark this text manually finalized after review', key=key,
                on_change=_set_finalized, args=(record, scope, target, language, on_save))
    if st.session_state[key]:
        st.caption('Manually finalized by the volunteer. Automated findings remain visible and are not cleared.')


def _render_audit(record):
    if record.get('confirmations'):
        with st.expander('Review response audit — saved entries'):
            for entry in record['confirmations']:
                st.text(str(entry.get('confirmation_date', 'Date not supplied')) + ' · ' +
                        str(entry.get('status', '')).replace('_', ' ') + ' · ' +
                        str(entry.get('attribution') or 'Attribution not supplied'))
                if entry.get('volunteer_note', entry.get('note')):
                    st.text('Volunteer note: ' + str(entry.get('volunteer_note', entry.get('note'))))
                if entry.get('rescuer_response'):
                    st.text('Rescuer response: ' + str(entry['rescuer_response']))


def _render_response_form(record, scope, issue_id, topic, prefix, on_save):
    latest = next((item for item in reversed(record.get('confirmations', [])) if item.get('issue_id') == issue_id), {})
    with st.form(prefix):
        statuses = ['needs_clarification', 'confirmed_by_rescuer', 'corrected', 'removed_from_final', 'not_relevant']
        status = st.selectbox('Review status', statuses,
            index=statuses.index(latest['status']) if latest.get('status') in statuses else 0,
            format_func=lambda value: value.replace('_', ' ').capitalize(), key=prefix + '_status')
        note = st.text_area('Volunteer note', value=latest.get('volunteer_note', latest.get('note', '')), key=prefix + '_note')
        response = st.text_area('Rescuer response', value=latest.get('rescuer_response', ''), key=prefix + '_response')
        attribution = st.text_input('Response attribution (person / source)', value=latest.get('attribution', ''), key=prefix + '_attribution')
        confirmation_date = st.text_input('Confirmation date (YYYY-MM-DD)', value=latest.get('confirmation_date', date.today().isoformat()), key=prefix + '_date')
        if st.form_submit_button('Save review response', key=prefix + '_save'):
            try:
                record_confirmation(record, issue_id, {'status': status, 'volunteer_note': note,
                    'rescuer_response': response, 'attribution': attribution, 'confirmation_date': confirmation_date,
                    'topic': topic, 'scope': scope, 'animal_name': record.get('animal_name')})
                _save(record, scope, on_save)
                st.success('Review response saved. Automated findings remain unchanged.')
            except ValueError:
                st.warning('The review response could not be saved. Check the confirmation date (YYYY-MM-DD) and animal attribution.')
    if st.button('Add saved response as supplementary evidence', key=prefix + '_source'):
        try:
            add_manual_source(record, issue_id)
            record['assessment_stale'] = True
            _save(record, scope, on_save)
            st.info('Supplementary evidence saved as reported, not independently verified. Reassess locally to review it.')
        except ValueError as exc:
            st.warning(str(exc))


def _render_finding_reviews(record, scope, on_save):
    findings = {}
    for draft_key, review in (record.get('assessment') or {}).get('drafts', {}).items():
        for finding in review.get('findings', []):
            issue_id = finding.get('id')
            if isinstance(issue_id, str) and issue_id.strip():
                findings.setdefault(issue_id, (draft_key, finding))
    if not findings:
        return
    with st.expander('Record review for an editorial finding'):
        st.caption('Every flagged point can receive an optional review status and note, including annotation concerns. '
                   'Recording a correction does not change the retained draft or its automated colors.')
        def finding_label(issue_id):
            draft_key, finding = findings[issue_id]
            color = COLORS.get(finding.get('color'), COLORS['yellow'])[0]
            excerpt = finding.get('excerpt') or finding.get('reason') or 'Whole-draft concern'
            return draft_key.replace('_', ' ') + ' · ' + color + ' — ' + str(excerpt)[:140]
        selected = st.selectbox('Editorial finding', list(findings), key=f'writing_finding_{scope}', format_func=finding_label)
        if selected in findings:
            _, finding = findings[selected]
            st.text(str(finding.get('reason', 'Review this concern.')))
            st.text('Sources: ' + (', '.join(map(str, finding.get('source_references') or [])) or 'No assigned reference'))
            prefix = f'writing_finding_response_{scope}_{text_fingerprint(selected)[:16]}'
            _render_response_form(record, scope, selected, finding.get('topic', finding.get('code', 'editorial_review')), prefix, on_save)


def render_questions(record, scope, profile, on_save=None):
    """Keep clarification responses separate from explicit supplementary evidence."""
    questions = (record.get('assessment') or {}).get('questions') or []
    if not questions:
        _render_finding_reviews(record, scope, on_save)
        _render_audit(record)
        return
    priority = {'high': 10, 'medium': 20, 'low': 30}
    def priority_number(item):
        value = item.get('priority')
        return value if type(value) in (int, float) else priority.get(str(value).lower(), 20)
    questions = sorted(questions, key=priority_number)
    st.markdown('**Clarification questions**')
    st.caption('Copy questions for your own follow-up. Saving a response does not change automated colors. '
               'Adding an attributed response as supplementary evidence requires a separate action and local reassessment.')
    copy_button('Copy clarification questions', '\n'.join(str(item.get('question', '')) for item in questions),
                f'copy_questions_{scope}')
    for question in questions:
        issue_id = str(question.get('id', ''))
        token = text_fingerprint(issue_id)[:16]
        prefix = f'writing_question_{scope}_{token}'
        rank = priority_number(question)
        rank_label = 'High' if rank <= 10 else 'Medium' if rank <= 20 else 'Low'
        with st.expander(rank_label + ' priority — ' + str(question.get('question', 'Clarification'))):
            st.text(str(question.get('reason', '')))
            st.text('Sources: ' + (', '.join(map(str, question.get('source_references') or [])) or 'None'))
            _render_response_form(record, scope, issue_id, question.get('topic', ''), prefix, on_save)
    _render_finding_reviews(record, scope, on_save)
    _render_audit(record)
