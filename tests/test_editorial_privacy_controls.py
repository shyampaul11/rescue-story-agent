"""Source-support wording and private controls; no live service or real deletion."""
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from draft_evidence import private_text
from content_state import workspace


SCOPE = 'a' * 20
OTHER = 'b' * 20


def privacy_app():
    import streamlit as st
    from content_state import workspace
    from test_editorial_privacy_controls import SCOPE, OTHER
    from editorial_privacy import render_privacy_controls
    record = workspace(st.session_state, SCOPE)
    if not st.session_state.get('seeded'):
        record['final']['nl'] = 'Current volunteer text'
        record['history'] = [{'bundle': 'private original'}]
        workspace(st.session_state, OTHER)['final']['nl'] = 'Other animal text'
        st.session_state['seeded'] = True
    render_privacy_controls(record, SCOPE)


class LocalDeletionUiTests(unittest.TestCase):
    def setUp(self):
        from editorial_privacy import delete_record
        self.deletion = patch('editorial_privacy.delete_record', return_value={
            'deleted_snapshots': 3, 'deletion_token': 'new-marker'})
        self.delete = self.deletion.start()
        self.addCleanup(self.deletion.stop)
        self.app = AppTest.from_function(privacy_app).run()
        self.assertFalse(self.app.exception)

    def test_confirmation_required_and_only_current_animal_cleared(self):
        key = 'writing_delete_' + SCOPE
        self.assertTrue(self.app.button(key=key).disabled)
        self.app.checkbox(key=key + '_confirm').check().run()
        self.app.button(key=key).click().run()
        self.assertFalse(self.app.exception)
        self.delete.assert_called_once_with(SCOPE)
        records = self.app.session_state['writing_workspaces']
        self.assertEqual(records[SCOPE]['final'], {'nl': '', 'en': ''})
        self.assertNotIn('history', records[SCOPE])
        self.assertEqual(records[SCOPE]['storage_deletion_token'], 'new-marker')
        self.assertEqual(records[OTHER]['final']['nl'], 'Other animal text')
        self.assertTrue(any('deleted' in item.value.lower() for item in self.app.success))

    def test_failed_deletion_does_not_claim_success_or_clear_live_text(self):
        self.delete.side_effect = OSError('A file could not be removed')
        self.app.checkbox(key='writing_delete_' + SCOPE + '_confirm').check().run()
        self.app.button(key='writing_delete_' + SCOPE).click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.session_state['writing_workspaces'][SCOPE]['final']['nl'], 'Current volunteer text')
        self.assertFalse(self.app.success)
        self.assertTrue(self.app.warning)

    def test_other_tab_deletion_invalidates_stale_workspace_and_widgets(self):
        from editorial_privacy import synchronize_scope
        state = {'writing_workspaces': {}}
        record = workspace(state, SCOPE)
        record.update(storage_deletion_token='old', bundle='private draft', confirmations=[{'note': 'private'}])
        state['writing_editor_' + SCOPE + '_final_nl'] = 'Old editor'
        state['writing_editor_' + OTHER + '_final_nl'] = 'Keep other editor'
        with patch('editorial_privacy.deletion_token', return_value='new'):
            current = synchronize_scope(state, SCOPE)
        self.assertIsNone(current['bundle'])
        self.assertNotIn('confirmations', current)
        self.assertNotIn('writing_editor_' + SCOPE + '_final_nl', state)
        self.assertEqual(state['writing_editor_' + OTHER + '_final_nl'], 'Keep other editor')
        self.assertEqual(current['storage_deletion_token'], 'new')


class PrivacyWordingTests(unittest.TestCase):
    def test_green_explains_source_support_and_automated_limits(self):
        def app():
            from editorial_ui import render_review
            render_review('example', 'A source-supported statement.', {'status': 'green', 'passages': [], 'findings': []})
        rendered = AppTest.from_function(app).run()
        self.assertFalse(rendered.exception)
        captions = ' '.join(item.value for item in rendered.caption).lower()
        self.assertIn('green: supported by the available source', captions)
        self.assertIn('not independently verified', captions)
        self.assertIn('cannot guarantee factual accuracy', captions)

    def test_labeled_opaque_credentials_are_redacted_from_private_text(self):
        text = ('access_token=opaque-access-value refresh_token: opaque-refresh-value '
                'OPENAI_API_KEY="opaque-api-value" authorization: Basic opaque-basic-value')
        redacted = private_text(text)
        for value in ('opaque-access-value', 'opaque-refresh-value', 'opaque-api-value', 'opaque-basic-value'):
            self.assertNotIn(value, redacted)
        self.assertIn('[credential removed]', redacted)

    def test_ordinary_age_status_and_animal_words_are_not_credentials(self):
        text = 'The age is unknown. Treatment status: reported. Adoption needs confirmation.'
        self.assertEqual(private_text(text), text)


if __name__ == '__main__':
    unittest.main()
