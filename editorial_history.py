"""Read-only saved version comparison and explicit restoration. No model/API calls."""
from copy import deepcopy
import difflib
import hashlib

import streamlit as st

from animal_profile import AnimalProfile
from clipboard_component import copy_button
from content_state import remember_edit
from editorial_ui import publication_copy_text


def _editor(target, language):
    if target not in ('final', 'facebook') or language not in ('nl', 'en'):
        raise ValueError('Invalid editor')


def editor_versions(record, target, language):
    """Return detached versions; never collapse separate AI generation labels.

    ``source`` is current, ai, or volunteer. AI entries additionally carry their
    own profile_snapshot, solely for publication-copy citation-token removal.
    Malformed legacy entries are skipped individually so healthy siblings remain.
    """
    _editor(target, language)
    prefix = f'{target}_{language}'
    current = record.get(target, {}).get(language, '')
    versions = [{'id': prefix + ':current', 'label': 'Current editor',
                 'text': current if isinstance(current, str) else '',
                 'source': 'current', 'generated_at': None}]
    history = record.get('history')
    history = history if isinstance(history, (list, tuple)) else []
    generations = [(f'previous_{index}', f'Previous AI generation {index + 1}', entry)
                   for index, entry in reversed(list(enumerate(history))) if isinstance(entry, dict)]
    generations.insert(0, ('latest', 'Latest AI generation', record))
    keys = ([f'website_{language}_{number}' for number in (1, 2, 3)] if target == 'final'
            else [f'facebook_{language}'])
    for generation_id, generation_label, entry in generations:
        bundle = entry.get('bundle')
        timestamp = entry.get('generated_at')
        timestamp = timestamp if isinstance(timestamp, str) else None
        for key in keys:
            draft = bundle.get(key) if isinstance(bundle, dict) else getattr(bundle, key, None)
            text = draft.get('text') if isinstance(draft, dict) else getattr(draft, 'text', None)
            if not isinstance(text, str):
                continue
            style = f'Website draft {key[-1]}' if target == 'final' else 'Facebook draft'
            label = f'{generation_label} · {style}' + (f' · {timestamp}' if timestamp else '')
            versions.append({'id': f'{prefix}:{generation_id}:{key}', 'label': label,
                             'text': text, 'source': 'ai', 'generated_at': timestamp,
                             'profile_snapshot': deepcopy(entry.get('profile_snapshot'))})
    undo = record.get('undo')
    states = undo.get(prefix, []) if isinstance(undo, dict) else []
    states = states[-10:] if isinstance(states, (list, tuple)) else []
    seen = set()
    for index in range(len(states) - 1, -1, -1):
        text = states[index]
        if not isinstance(text, str) or text in seen:
            continue
        seen.add(text)
        versions.append({'id': f'{prefix}:volunteer:{index}',
                         'label': f'Recent volunteer version {index + 1}',
                         'text': text, 'source': 'volunteer', 'generated_at': None})
    return versions


def _publication_text(version):
    if version['source'] != 'ai':
        return version['text'], False
    try:
        snapshot = version.get('profile_snapshot')
        profile = snapshot if isinstance(snapshot, AnimalProfile) else AnimalProfile.model_validate(snapshot)
    except (ValueError, TypeError):
        return version['text'], True
    return publication_copy_text(version['text'], profile), False


def _saved_version(record, target, language, version):
    if isinstance(version, dict):
        for saved in editor_versions(record, target, language):
            if saved['id'] == version.get('id') and saved['text'] == version.get('text'):
                return saved
    raise ValueError('Version is unavailable for this editor and language')


def restore_version(record, target, language, version):
    """Restore one saved text, retaining the replaced editor in its normal undo."""
    saved = _saved_version(record, target, language, version)
    text, _ = _publication_text(saved)
    remember_edit(record, target, language, text)


def _restore_selected(record, scope, target, language, version, expected_current, confirm_key, on_save):
    # Callbacks run before text areas render, making assignment to their keys safe.
    if not st.session_state.get(confirm_key):
        return
    editor_key = f'writing_editor_{scope}_{target}_{language}'
    current = st.session_state.get(editor_key, record[target][language])
    if current != expected_current or record[target][language] != expected_current:
        if isinstance(current, str):
            remember_edit(record, target, language, current)
        st.session_state[confirm_key] = False
        if on_save:
            on_save(scope, record)
        return
    restore_version(record, target, language, version)
    st.session_state[editor_key] = record[target][language]
    st.session_state[confirm_key] = False
    if on_save:
        on_save(scope, record)


def render_version_history(record, scope, language, on_save=None):
    """Render before the editors; selecting/reading versions never changes text."""
    _editor('final', language)
    available = {target: editor_versions(record, target, language) for target in ('final', 'facebook')}
    if not any(version['text'] or version['source'] != 'current'
               for versions in available.values() for version in versions):
        return
    with st.expander('Version history and comparison'):
        st.caption('Compare saved AI originals and recent volunteer edits. Restoring changes only the chosen editor and language; the replaced text remains available through undo.')
        target = st.selectbox('Editor to review', ('final', 'facebook'),
                              format_func=lambda value: 'Website' if value == 'final' else 'Facebook',
                              key=f'writing_history_target_{scope}_{language}')
        if target not in ('final', 'facebook'):
            return
        versions = available[target]
        by_id = {version['id']: version for version in versions}
        prefix = f'{scope}_{target}_{language}'
        # A generation replacement or bounded undo may remove an earlier option.
        review_key = f'writing_history_version_{prefix}'
        compare_key = f'writing_history_compare_{prefix}'
        for key in (review_key, compare_key):
            if st.session_state.get(key) not in by_id:
                st.session_state.pop(key, None)
        selected_id = st.selectbox('Version to review', list(by_id), key=review_key,
                                   format_func=lambda value: by_id[value]['label'])
        comparison_id = st.selectbox('Compare with', list(by_id), key=compare_key,
                                     format_func=lambda value: by_id[value]['label'])
        if selected_id not in by_id or comparison_id not in by_id:
            return
        selected, comparison = by_id[selected_id], by_id[comparison_id]
        left, right = st.columns(2)
        with left:
            st.caption('Selected version — read only')
            st.text(selected['text'])
        with right:
            st.caption('Comparison version — read only')
            st.text(comparison['text'])
        diff_lines = difflib.unified_diff(comparison['text'].splitlines(keepends=True),
            selected['text'].splitlines(keepends=True), fromfile=comparison['label'], tofile=selected['label'])
        # Drafts need not end in a newline; keep added and removed lines readable.
        difference = ''.join(line if line.endswith('\n') else line + '\n' for line in diff_lines)
        if difference:
            st.code(difference, language='diff')
        else:
            st.caption('The displayed versions have identical text.')
        publication_text, missing_profile = _publication_text(selected)
        if missing_profile:
            st.info('This saved AI original has no usable source profile snapshot. Copy and restore preserve its text verbatim; review any technical citation markers manually.')
        elif publication_text != selected['text']:
            st.info('Technical citation markers are omitted when copying or restoring this AI version, using its own saved source profile. The original displayed above remains unchanged.')
        copy_button('Copy selected version', publication_text, f'copy_history_{prefix}')
        current = record[target][language]
        token = hashlib.sha256((selected_id + '\0' + selected['text'] + '\0' + current).encode()).hexdigest()[:16]
        confirm_key = f'writing_history_confirm_{prefix}_{token}'
        differs = publication_text != current
        confirmed = st.checkbox('Confirm replacing this editor with the selected version', key=confirm_key,
                                 disabled=not differs)
        st.button('Restore selected version', key=f'writing_history_restore_{prefix}',
                  disabled=not differs or not confirmed, on_click=_restore_selected,
                  args=(record, scope, target, language, selected, current, confirm_key, on_save))
