"""Review a local OCR draft, then explicitly apply it to one permitted text field."""
import hashlib

import streamlit as st

from screenshot_ocr import OcrError, extract_screenshot_text, merge_reviewed_text


TARGETS = {'History': 'animal_history', 'Other Info': 'animal_other_info', 'Updates': 'animal_updates'}


def reset_message_import():
    """Called on animal/document changes; rotate upload key to prevent stale screenshots."""
    version = st.session_state.get('ocr_upload_version', 0) + 1
    for key in list(st.session_state):
        if key.startswith('ocr_'):
            st.session_state.pop(key, None)
    st.session_state.ocr_upload_version = version


def clear_draft():
    for key in ('ocr_reviewed_text', 'ocr_reviewed', 'ocr_result', 'ocr_error', 'ocr_applied',
                'ocr_draft_signature', 'ocr_message_date', 'ocr_success'):
        st.session_state.pop(key, None)


def require_review():
    st.session_state.ocr_reviewed = False
    st.session_state.pop('ocr_success', None)


def draft_signature(upload, text, target, mode, message_date):
    # An opaque local identifier only; screenshot bytes are never AI media inputs.
    return hashlib.sha256(upload.getvalue() + ('\0' + '\0'.join((text, target, mode, message_date))).encode('utf-8')).hexdigest()


def apply_reviewed_text(upload_key, on_sources_changed):
    """Callback runs before the source text widgets render on the next rerun."""
    st.session_state.pop('ocr_error', None)
    st.session_state.pop('ocr_success', None)
    try:
        upload = st.session_state.get(upload_key)
        if upload is None or 'ocr_result' not in st.session_state or not st.session_state.get('ocr_reviewed'):
            raise OcrError('Extract the screenshot and confirm your review before using the text.')
        label = st.session_state.get('ocr_target')
        if label not in TARGETS:
            raise OcrError('Choose History, Other Info or Updates.')
        mode = st.session_state.get('ocr_mode')
        reviewed = st.session_state.get('ocr_reviewed_text', '')
        message_date = st.session_state.get('ocr_message_date', '')
        signature = draft_signature(upload, reviewed, label, mode, message_date)
        if signature == st.session_state.get('ocr_applied'):
            raise OcrError('This reviewed message was already imported with these options.')
        if hashlib.sha256(upload.getvalue()).hexdigest() != st.session_state.get('ocr_draft_signature'):
            raise OcrError('The screenshot changed. Extract and review the new screenshot first.')
        key = TARGETS[label]
        merged = merge_reviewed_text(st.session_state.get(key, ''), reviewed, mode, message_date)
        on_sources_changed()
        st.session_state[key] = merged
        st.session_state.ocr_applied = signature
        st.session_state.ocr_reviewed = False
        records = list(st.session_state.get('ocr_imports', []))[-19:]
        records.append({'section': label, 'screenshot': upload.name, 'mode': mode,
                        'message_date': message_date, 'status': 'OCR text reviewed and applied by user'})
        st.session_state.ocr_imports = records
        st.session_state.ocr_success = f'Reviewed message added to {label}. No AI request was made.'
    except OcrError as exc:
        st.session_state.ocr_error = str(exc)


def render_message_import(on_sources_changed):
    with st.expander('Import rescuer message screenshot (OCR)'):
        st.caption('Alternative source for History, Other Info or Updates. Crop to the relevant rescuer message; '
                   'remove unrelated chat, phone numbers and personal details. OCR runs locally without an API key. '
                   'Only text you review and apply joins the chosen field; the screenshot is not sent to OpenAI.')
        version = st.session_state.get('ocr_upload_version', 0)
        upload_key = f'ocr_screenshot_{version}'
        upload = st.file_uploader('Upload rescuer message screenshot', type=['png', 'jpg', 'jpeg', 'webp'],
            key=upload_key, on_change=clear_draft, help='One screenshot at a time, at most 8 MB and 16 megapixels.')
        if upload is not None:
            if st.button('Extract screenshot text', key='ocr_extract'):
                clear_draft()
                try:
                    screenshot_bytes = upload.getvalue()
                    screenshot_signature = hashlib.sha256(screenshot_bytes).hexdigest()
                    with st.spinner('Reading the screenshot locally…'):
                        result = extract_screenshot_text(screenshot_bytes)
                    current_upload = st.session_state.get(upload_key)
                    if (st.session_state.get('ocr_upload_version', 0) != version or current_upload is None
                            or hashlib.sha256(current_upload.getvalue()).hexdigest() != screenshot_signature):
                        return  # Ignore a result that finished after changing animal, document or screenshot.
                    st.session_state.ocr_result = result
                    st.session_state.ocr_reviewed_text = result.text
                    st.session_state.ocr_draft_signature = screenshot_signature
                except OcrError as exc:
                    st.session_state.ocr_error = str(exc)
        result = st.session_state.get('ocr_result')
        if result is not None and upload is not None:
            if result.preview_png:
                st.image(result.preview_png, width='stretch', alt='Uploaded rescuer message screenshot for checking OCR text')
            st.caption(f'Recognized {result.line_count} line(s); mean OCR score {result.mean_confidence:.0%}. '
                       'This score measures recognition confidence, not factual accuracy.')
            if result.low_confidence_lines:
                st.warning(f'{result.low_confidence_lines} line(s) have low OCR confidence. Check every name, number, date and negation.')
            st.text_area('Review and correct recognized message', key='ocr_reviewed_text', height=200,
                         on_change=require_review)
            st.selectbox('Use message as', list(TARGETS), key='ocr_target', on_change=require_review)
            st.radio('How to add the reviewed text', ['Append', 'Replace'], key='ocr_mode', on_change=require_review)
            st.caption('Append keeps existing source text. Replace overwrites only the chosen text field.')
            st.text_input('Message date (optional — copy exactly as shown)', key='ocr_message_date', max_chars=120,
                          on_change=require_review)
            st.caption('Keep dates attached to the corresponding update. Leave blank if unknown; upload time is never used as a message date. '
                       'For multiple differently dated messages, import each separately or retain all dates in the reviewed text.')
            reviewed = st.checkbox('I checked the text against the screenshot and removed unrelated chat details.', key='ocr_reviewed')
            st.button('Use reviewed message', key='ocr_apply', disabled=not reviewed,
                      on_click=apply_reviewed_text, args=(upload_key, on_sources_changed))
        if st.session_state.get('ocr_error'):
            st.error(st.session_state.ocr_error)
        if st.session_state.get('ocr_success'):
            st.success(st.session_state.ocr_success)
        if st.session_state.get('ocr_imports'):
            st.caption('Reviewed screenshot imports in this animal/document session (subsequent field edits may be present):')
            for record in st.session_state.ocr_imports:
                st.text(f'{record["section"]}: {record["screenshot"]} · {record["mode"]} · message date: {record["message_date"] or "Undated"}')
