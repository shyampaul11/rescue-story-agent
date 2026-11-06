# Rescue Story Agent — Milestone 6

A local Python and Streamlit application for preparing animal adoption text.
It imports selected animal information, builds an evidence-based Animal Profile,
and prepares three Dutch and three English website drafts plus a Facebook draft
in each language. Volunteers review, edit, copy and export text for manual use.

The application does not publish to a website or Facebook. Google Drive access
uses the read-only scope. Analysis and draft generation require explicit actions
and may incur AI API charges. Reviewing saved work, editing and local assessment
do not require new AI requests.

## Preserve your existing work first

Saved writing, revisions and review notes belong to the application folder in
which they were created, under `local_state/editorial/`. OAuth tokens and API
configuration are also local to that folder. Transferring source code does not
transfer or migrate these private files.

Keep using your existing working application to access its saved animal work.
Do not delete or replace its folder, `.env`, credentials, virtual environment or
`local_state`. Starting this checkout creates a separate local workspace. Saved
work from another copy will not automatically appear here. Keep private exports
as an additional backup; never commit them.

## Install in a new checkout

Use a current Python version compatible with the packages in `requirements.txt`.
From PowerShell in the folder containing `app.py`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

These commands create a new environment for this checkout. Do not copy another
application's `.venv` or overwrite an existing environment. If Python is not on
PATH, use your installed Python launcher's equivalent command.

## Local configuration

Create a new `.env` file only if this checkout does not already have one. Never
overwrite an existing configuration. Configure these values locally:

```dotenv
OPENAI_API_KEY=replace-with-your-private-api-key
OPENAI_MODEL=replace-with-an-available-text-and-image-model
AI_REASONING_EFFORT=
OPENAI_CONTENT_MODEL=replace-with-an-available-writing-model
CONTENT_REASONING_EFFORT=
CONTENT_MAX_OUTPUT_TOKENS=16000
```

Choose models available to your API project that support the application's
Responses API and Structured Outputs requirements. Leave reasoning settings
blank for models without that control. Shell environment variables take
precedence over `.env`. Restart the application after configuration changes.
Do not paste actual keys into source files, tests, screenshots or GitHub.

For Google Drive, configure a Desktop OAuth client in your own Google Cloud
project and place its downloaded JSON at `secrets/google_client.json` locally.
The app stores the resulting token at `local_state/google_token.json`. Enter
your own main Google Drive folder URL in the app; no private folder is built
into this checkout. Connect Google Drive and select the animal to work on.
Manual document upload and text entry are also available.

## Run

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Open the local URL shown by Streamlit in your browser. Keep the terminal open.
The tracked Streamlit configuration binds to `127.0.0.1` and disables usage
statistics. Use Ctrl+C in that terminal to stop that instance. Do not stop a
different instance holding unsaved work.

## Prepare and review text

1. Select the correct animal, or upload its source document. Review the History,
   Other Info and Updates fields. Select only relevant photos or video frames.
2. Optionally import a cropped message screenshot with local OCR. Correct the
   recognized text and review its date before applying it to a source field.
3. Check existing saved work before requesting any new analysis or generation.
4. Explicitly analyze the selected evidence when a new Animal Profile is needed.
   Review names, dates, attribution, health, behavior and unknown information.
5. Explicitly generate drafts when ready. Review all languages and versions.
   Automated editorial findings support human review; they do not certify truth.
6. Copy suitable wording into the independent final website editors and edit
   the Facebook versions. Switching languages or drafts does not regenerate them.
7. Review findings and questions, record clarifications, and compare saved
   versions as needed. Restoring a version requires an explicit confirmation.
8. Download a private backup before major changes, and manually publish only
   text that a responsible person has checked.

Final website editors begin empty. Existing edits survive regeneration; changes
to evidence mark earlier work potentially outdated. Local revision history and
undo controls support recovery but do not replace an independent private backup.
Delete controls affect the selected animal's local saved writing and require
confirmation; do not use them as part of installing or updating source code.

## Automated tests

The complete suite uses Python `unittest`, mocked provider responses, synthetic
fixtures, Streamlit AppTest and temporary storage. It includes local video/OCR
checks. It does not require live AI requests or real Google credentials.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Run tests in a checkout containing no real `.env`, credentials or animal data.
For additional assurance, run with outbound network access blocked. The source
transfer was verified separately with network-blocking guards and protected
access to real local workspaces. That verification harness remains local.

## Files

- `app.py`: Streamlit interface and source selection.
- `drive_client.py`, `drive_media.py`, `drive_auth_job.py`: read-only Drive access.
- `ai_config.py`, `ai_service.py`, `animal_profile.py`, `analysis_*.py`: analysis.
- `content_*.py`, `clipboard_component.py`: bilingual drafts and manual editors.
- `editorial_*.py`, `draft_evidence.py`: assessment, history and private storage.
- Other root Python modules: extraction, evidence validation and local media/OCR.
- `tests/`: automated tests and synthetic test support.
- `requirements.txt`: dependencies; `.streamlit/config.toml`: existing local settings.

## Privacy

`.gitignore` excludes environment files, credentials, tokens, `local_state`,
virtual environments, animal records, media, diagnostics, backups and local tools.
Ignore rules do not remove files already tracked by Git. Always inspect both the
changed files and the exact staged diff before committing. Keep real animal
records and confidential foundation information out of source code and tests.
Repository visibility is managed separately on GitHub; local setup does not
change it.
