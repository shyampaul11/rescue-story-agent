"""Rescue Story Agent: private Drive History and bounded media previews."""
import streamlit as st

import drive_auth_job as auth_job
import drive_client as drive
import drive_media as media
from history_extractor import HistoryError, extract_animal_info
from analysis_ui import render_analysis
from source_context import parse_animal_label
from message_import_ui import render_message_import, reset_message_import
from content_ui import render_content_editor

st.set_page_config(page_title='Rescue Story Agent', page_icon='🐾', layout='centered')


def clear_outputs():
    if 'ai_result' in st.session_state:
        st.session_state.ai_outdated = True
    for key in ('website_text', 'facebook_text'):
        st.session_state.pop(key, None)


def clear_media():
    for key in list(st.session_state):
        if key.startswith(('media_choose_', 'media_show_')) or key in (
            'media_catalog', 'media_animal', 'media_error', 'media_cache', 'media_preview_errors',
            'media_selected', 'image_page', 'video_page', 'media_retry_error',
        ):
            st.session_state.pop(key, None)


def clear_previews():
    st.session_state.pop('media_cache', None)
    st.session_state.pop('media_preview_errors', None)


def retry_previews():
    """Restore the private API client without losing edited text or media choices."""
    try:
        st.session_state.drive_service = drive.connect(interactive=False)
        st.session_state.pop('media_preview_errors', None)
        st.session_state.pop('media_retry_error', None)
    except drive.DriveError as exc:
        st.session_state.media_retry_error = str(exc)


def reset_preview_choices(kind, size_changed=False):
    """Apply new count/size settings without altering analysis selections."""
    animal = st.session_state.get('media_animal')
    if animal:
        for file in st.session_state.get('media_catalog', {}).get('files', []):
            if file['kind'] == kind:
                st.session_state.pop(f"media_show_{animal['id']}_{file['id']}", None)
    if size_changed:
        clear_previews()


def reset_drive():
    clear_outputs()
    reset_message_import()
    clear_media()
    st.session_state.animal_history = ''
    st.session_state.animal_other_info = ''
    st.session_state.animal_updates = ''
    for key in ('drive_source', 'drive_error', 'document_error'):
        st.session_state.pop(key, None)


def change_main_folder():
    reset_drive()
    for key in ('drive_folders', 'drive_animal', 'drive_access_confirmed'):
        st.session_state.pop(key, None)


def connect_drive():
    change_main_folder()
    try:
        drive.folder_id(st.session_state.drive_url)
        auth_job.start_connection()
    except drive.DriveError as exc:
        st.session_state.drive_error = str(exc)


def load_drive_folders():
    change_main_folder()
    st.session_state.pop('drive_service', None)
    status = auth_job.connection_status()
    if status['status'] in ('running', 'error'):
        st.session_state.drive_error = status['message']
        return
    try:
        service = drive.connect(interactive=False)
        st.session_state.drive_service = service
        st.session_state.drive_folders = drive.discover_folders(service, st.session_state.drive_url)
        st.session_state.drive_access_confirmed = True
    except drive.DriveError as exc:
        st.session_state.drive_error = str(exc)


def refresh_media():
    animal = st.session_state.get('media_animal')
    clear_media()
    if animal is None:
        return
    st.session_state.media_animal = animal
    try:
        st.session_state.media_catalog = media.discover_media(st.session_state.drive_service, animal['id'])
    except drive.DriveError as exc:
        st.session_state.media_error = str(exc)


def select_animal():
    reset_drive()
    selected = st.session_state.drive_animal
    if selected is None:
        return
    animal = next((item for item in st.session_state.drive_folders if item['id'] == selected), None)
    if animal is None:
        st.session_state.drive_error = 'Select an animal from the current folder list.'
        return
    st.session_state.media_animal = animal
    try:
        result = drive.load_animal(st.session_state.drive_service, animal)
        st.session_state.animal_history = result['history']
        st.session_state.animal_other_info = result.get('other_info', '')
        st.session_state.animal_updates = result.get('updates', '')
        st.session_state.drive_source = {key: value for key, value in result.items()
                                        if key not in ('history', 'other_info', 'updates')}
    except (drive.DriveError, HistoryError) as exc:
        st.session_state.drive_error = str(exc)
    # A missing History document must not prevent browsing the animal's media.
    refresh_media()


def load_document():
    clear_outputs()
    reset_message_import()
    clear_media()  # Manual document may describe a different animal.
    for key in ('drive_source', 'drive_error', 'document_error'):
        st.session_state.pop(key, None)
    uploaded = st.session_state.history_document
    if uploaded is None:
        return
    st.session_state.animal_history = ''
    st.session_state.animal_other_info = ''
    st.session_state.animal_updates = ''
    try:
        info = extract_animal_info(uploaded.getvalue())
        st.session_state.animal_history = info['history']
        st.session_state.animal_other_info = info['other_info']
        st.session_state.animal_updates = info['updates']
    except HistoryError as exc:
        st.session_state.document_error = str(exc)


def select_media(file_id, widget_key, animal_id):
    # Refresh/animal-change callbacks can remove widgets before pending clicks run.
    # Ignore stale events rather than reviving a selection for the wrong animal.
    if (widget_key not in st.session_state or
            st.session_state.get('media_animal', {}).get('id') != animal_id):
        return
    current_files = {file['id'] for file in st.session_state.get('media_catalog', {}).get('files', [])
                     if file['kind'] in ('image', 'video')}
    if file_id not in current_files:
        return
    selected = set(st.session_state.get('media_selected', []))
    if st.session_state[widget_key]:
        selected.add(file_id)
    else:
        selected.discard(file_id)
    st.session_state.media_selected = sorted(selected)
    clear_outputs()


def show_gallery(kind, label, max_bytes, preview_limit, budget):
    catalog = st.session_state.get('media_catalog')
    if not catalog:
        st.caption('Select a Drive animal to list its media.')
        return
    files = [file for file in catalog['files'] if file['kind'] == kind]
    if not files:
        st.caption(f'No supported {label.lower()} found.')
        return
    summary = media.preview_summary(files, max_bytes)
    st.caption(f"{label}: {summary['total']} total; {summary['eligible']} within the size limit; "
               f"{summary['oversize']} excluded by size; {summary['unknown']} with unknown size. Counts cover all pages.")
    if summary['oversize']:
        setting = 'Maximum photo size (MB)' if kind == 'image' else 'Maximum video size (MB)'
        upper = 20 if kind == 'image' else 64
        hint = (f"Increase {setting} to at least {summary['next_limit_mb']} MB to include more files."
                if summary['next_limit_mb'] <= upper else f'The remaining files exceed the application safety cap of {upper} MB.')
        st.info(f"{summary['oversize']} {label.lower()} excluded by the {max_bytes / media.MIB:g} MB per-file limit. {hint}")
    if summary['unknown']:
        st.caption('Files with unknown size cannot be previewed by increasing the size limit.')
    page_key = 'image_page' if kind == 'image' else 'video_page'
    pages = max(1, (len(files) + 11) // 12)
    page = st.selectbox(f'{label} page', range(pages), format_func=lambda value: f'{value + 1} / {pages}', key=page_key)
    visible = files[page * 12:(page + 1) * 12]
    highlighted_summary = st.empty()
    cache = st.session_state.setdefault('media_cache', {})
    errors = st.session_state.setdefault('media_preview_errors', {})
    columns = st.columns(3) if kind == 'image' else None
    shown = 0
    automatic = 0
    counts = {'loaded': 0, 'unchecked': 0, 'count': 0, 'budget': 0, 'failed': 0}
    for index, file in enumerate(visible):
        container = columns[index % 3].container(border=True) if columns else st.container(border=True)
        with container:
            st.text(file['name'])
            size = media.file_size(file)
            st.caption(f"{size / media.MIB:.2f} MB" if size is not None else 'Unknown size')
            st.text(f"Type: {file.get('mimeType', 'unknown')}\nFolder: {file['folder']}\nModified: {file.get('modifiedTime', 'unknown')}")
            if kind == 'image':
                st.caption('Photo taken (Drive metadata): ' + file.get('imageMediaMetadata', {}).get('time', 'Unknown; embedded EXIF checked during analysis'))
            animal_id = st.session_state.media_animal['id']
            identity = f"{animal_id}_{file['id']}"
            choice = 'media_choose_' + identity
            st.session_state.setdefault(choice, file['id'] in st.session_state.get('media_selected', []))
            st.checkbox('Select for future analysis', key=choice,
                        on_change=select_media, args=(file['id'], choice, animal_id))
            problem = media.preview_problem(file, max_bytes)
            if problem:
                st.caption(problem)
                continue
            automatic += 1
            show = st.checkbox('Show preview', value=(automatic <= preview_limit),
                               key='media_show_' + identity)
            if not show:
                counts['unchecked'] += 1
                continue
            if shown >= preview_limit:
                counts['count'] += 1
                st.caption('Preview count limit reached. Hide another preview to show this file.')
                continue
            shown += 1
            if size > budget[0]:
                counts['budget'] += 1
                st.caption('Combined 64 MB preview budget reached. Hide other previews first.')
                continue
            error_key = (identity, file.get('modifiedTime'), max_bytes)
            try:
                if error_key in errors:
                    raise drive.DriveError(errors[error_key])
                data = media.cached_preview(st.session_state.drive_service, animal_id, file, cache, max_bytes)
                budget[0] -= len(data)
                counts['loaded'] += 1
                if kind == 'image':
                    st.image(data, width='stretch', alt=f"Photo preview: {file['name']}")
                else:
                    extension = file['name'].lower()
                    mime = ('video/webm' if extension.endswith('.webm') else 'video/quicktime' if extension.endswith('.mov')
                            else 'video/mp4' if extension.endswith('.mp4') else file.get('mimeType', 'video/mp4'))
                    st.video(data, format=mime, alt=f"Video preview: {file['name']}")
                    st.caption('Playback depends on your browser and the video codec. AI analysis uses sampled frames only when you click Analyze Animal.')
            except drive.DriveError as exc:
                counts['failed'] += 1
                errors[error_key] = str(exc)
                st.warning(f"Preview unavailable for {file['name']}. {exc} Use Retry failed previews below.")
    page_summary = media.preview_summary(visible, max_bytes)
    highlighted_summary.info(
        f"**{label} preview summary — this page**\n\n"
        f"Previewed: :violet[**{counts['loaded']}**]  ·  "
        f"Left out by preview count/checkbox choice: :violet[**{counts['unchecked'] + counts['count']}**]  ·  "
        f"Left out by file size: :violet[**{page_summary['oversize']}**]\n\n"
        f"Unknown size: :violet[**{page_summary['unknown']}**]  ·  "
        f"Memory limit: :violet[**{counts['budget']}**]  ·  "
        f"Failed previews: :violet[**{counts['failed']}**]\n\n"
        f"Across all pages, :violet[**{summary['oversize']}**] {label.lower()} exceed the file-size limit."
    )
    st.caption(f"{label} on this page: {counts['loaded']} previewed; "
               f"{counts['unchecked'] + counts['count']} not previewed by count/checkbox choice; "
               f"{page_summary['oversize']} excluded by size; {page_summary['unknown']} unknown size; "
               f"{counts['budget']} excluded by the combined memory budget; {counts['failed']} failed.")
    if counts['budget']:
        st.caption('Increasing the per-file size limit will not help the combined memory budget. Reduce the photo/video preview counts or hide other previews.')


st.title('Rescue Story Agent')
st.subheader('AI-powered animal adoption content creator')
st.info('Milestone 6: analyze permitted sources, then generate Dutch/English adoption drafts from the Animal Profile. Review, edit and copy for manual publication.')

with st.sidebar:
    st.markdown('### Preview limits')
    image_mb = st.number_input('Maximum photo size (MB)', min_value=1, max_value=20, value=8, key='limit_image', on_change=reset_preview_choices, args=('image', True))
    photo_count = st.number_input('Photo previews per page', min_value=0, max_value=12, value=6, key='limit_photo_count', on_change=reset_preview_choices, args=('image',))
    video_mb = st.number_input('Maximum video size (MB)', min_value=1, max_value=64, value=25, key='limit_video', on_change=reset_preview_choices, args=('video', True))
    video_count = st.number_input('Video previews per page', min_value=0, max_value=12, value=0, key='limit_video_count', on_change=reset_preview_choices, args=('video',))
    st.caption('Each page lists 12 files. Counts automatically select that many eligible previews on the current page, in filename order. Videos default to 0 to avoid unexpected downloads. Size limits control which files qualify, not image resolution. Photos resize to 640 px. The combined preview/cache budget remains 64 MB.')

st.markdown('### 1. Google Drive Connection')
st.caption('Run this app from your own PowerShell. Click Connect and finish Google sign-in. If Google replaces this tab, return to http://127.0.0.1:8501 and click Load Animal Folders.')
st.text_input('Main Google Drive folder URL', value='', key='drive_url', on_change=change_main_folder)
st.button('Connect Google Drive', key='connect_drive', on_click=connect_drive)
st.button('Load Animal Folders', key='load_drive_folders', on_click=load_drive_folders)
status = auth_job.connection_status()
if status['status'] in ('running', 'ready'):
    st.info(status['message'])
elif status['status'] == 'error':
    st.error(status['message'])
if st.session_state.get('drive_service') is not None and not st.session_state.get('drive_access_confirmed'):
    st.info('Google login restored, but folder access/listing could not be confirmed. See the error below.')

st.markdown('### 2. Select Animal')
folders = st.session_state.get('drive_folders')
if folders is not None:
    st.success(f'Folder access confirmed for the authorized Google account. Found {len(folders)} immediate animal folder(s).')
    if not folders:
        st.warning('No immediate animal subfolders found. Check the main folder URL and account access.')
    else:
        names = {item['id']: item['name'] for item in folders}
        st.selectbox('Select animal folder', list(names), index=None, format_func=lambda value: names[value], key='drive_animal', on_change=select_animal)
else:
    st.caption('Connect and load animal folders first, or use manual History below.')
if st.session_state.get('drive_error'):
    st.error(st.session_state.drive_error)
if st.session_state.get('media_animal'):
    selected_context = parse_animal_label(st.session_state.media_animal['name'])
    st.text('Selected animal: ' + (selected_context.animal_name or st.session_state.media_animal['name']))
    if selected_context.rescuer_name:
        st.text('Rescuer: ' + selected_context.rescuer_name)
        st.caption('The bracketed name identifies the rescuer, who provides History, Other Info and Updates. These reports may include your edits.')

st.markdown('### 3. Animal History and Additional Information')
st.file_uploader('Upload Kenmerkenlijst Document', type=['docx'], key='history_document', on_change=load_document,
                 help='Imports only History, Other info and Updates. Manual upload clears Drive media to avoid mixing animals.')
if st.session_state.get('document_error'):
    st.error(st.session_state.document_error)
source = st.session_state.get('drive_source')
if source:
    st.text(f"Source document: {source['document']}")
history = st.text_area('Animal History', height=220, key='animal_history', on_change=clear_outputs,
                       placeholder='Type or paste rescue history here. You can edit imported History.')
other_info = st.text_area('Other info', height=120, key='animal_other_info', on_change=clear_outputs,
                          placeholder='Optional information from the neighbouring Other info cell.')
updates = st.text_area('Updates (please note the date of the update)', height=150,
                       key='animal_updates', on_change=clear_outputs,
                       placeholder='Optional updates. Keep the date alongside each update.')
st.caption('Dates are preserved exactly as written; undated updates are not assigned a date. Website/Facebook source rows are not imported.')
render_message_import(on_sources_changed=clear_outputs)

st.markdown('### 4. Animal Photos')
if st.session_state.get('media_animal'):
    st.button('Refresh media list', key='refresh_media', on_click=refresh_media)
    catalog = st.session_state.get('media_catalog')
    if catalog is not None and not catalog['folders']:
        st.info("No Foto's / Fotos / Photos or equivalent media folder found for this animal.")
    elif catalog is not None and not catalog['files']:
        st.info('The media folder is empty.')
if st.session_state.get('media_error'):
    st.error(st.session_state.media_error)
preview_budget = [64 * media.MIB]
show_gallery('image', 'Photos', int(image_mb) * media.MIB, int(photo_count), preview_budget)

st.markdown('### 5. Animal Videos')
show_gallery('video', 'Videos', int(video_mb) * media.MIB, int(video_count), preview_budget)
catalog = st.session_state.get('media_catalog', {})
unsupported = [file for file in catalog.get('files', []) if file['kind'] == 'unsupported']
if unsupported:
    with st.expander(f'Unsupported files ({len(unsupported)}) — not downloaded'):
        for file in unsupported:
            st.text(f"{file['name']} — {file.get('mimeType', 'unknown')}")
st.caption(f"Selected for analysis: {len(st.session_state.get('media_selected', []))} file(s). Selection alone sends nothing to AI.")
if st.session_state.get('media_preview_errors'):
    st.button('Retry failed previews', key='retry_previews', on_click=retry_previews)
    st.caption('Restores your saved login and retries failed previews without clearing your edited text, selections or successful preview cache.')
if st.session_state.get('media_retry_error'):
    st.error(st.session_state.media_retry_error)

render_analysis(history, other_info, updates)

render_content_editor()
st.caption('Drive access remains read-only. AI requests require consent and an Analyze Animal click. Private media is not made public. No publishing is connected.')
