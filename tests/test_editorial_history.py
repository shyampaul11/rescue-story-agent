"""Saved AI generations and explicit, reversible editor restoration. No live API."""
from copy import deepcopy
from functools import partial
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from content_service import ContentSettings
from content_state import workspace, commit_generation, remember_edit, restore_edit
from test_content_service import profile_fixture, bundle_fixture


def record_fixture():
    record = workspace({}, 'test')
    commit_generation(record, bundle_fixture(revision=' Older.'), profile_fixture(),
                      ContentSettings(), [], {}, 'analysis', generated_at='2026-11-04')
    commit_generation(record, bundle_fixture(revision=' Latest.'), profile_fixture(),
                      ContentSettings(), [], {}, 'analysis', generated_at='2026-11-05')
    return record


class VersionTests(unittest.TestCase):
    def test_each_generation_retains_all_matching_styles_and_language(self):
        from editorial_history import editor_versions
        record = record_fixture()
        for target, language, count in [('final', 'nl', 6), ('final', 'en', 6),
                                        ('facebook', 'nl', 2), ('facebook', 'en', 2)]:
            versions = editor_versions(record, target, language)
            ai = [item for item in versions if item['source'] == 'ai']
            self.assertEqual(len(ai), count)
            self.assertEqual({item['generated_at'] for item in ai}, {'2026-11-04', '2026-11-05'})
            self.assertEqual(len({item['id'] for item in versions}), len(versions))
            self.assertTrue(all('label' in item and isinstance(item['text'], str) for item in versions))
            if language == 'en':
                self.assertFalse(any('Een persoonlijk' in item['text'] for item in ai))

    def test_current_and_recent_volunteer_versions_survive_duplicate_ai_generations(self):
        from editorial_history import editor_versions
        record = record_fixture()
        record['history'].append(deepcopy(record['history'][0]))
        for number in range(14):
            remember_edit(record, 'final', 'nl', f'Volunteer {number}')
        versions = editor_versions(record, 'final', 'nl')
        self.assertEqual(versions[0]['text'], 'Volunteer 13')
        self.assertEqual(versions[0]['source'], 'current')
        self.assertEqual(len([v for v in versions if v['source'] == 'ai']), 9)
        self.assertEqual({v['text'] for v in versions if v['source'] == 'volunteer'},
                         {f'Volunteer {number}' for number in range(3, 13)})

    def test_malformed_entries_do_not_hide_other_saved_drafts(self):
        from editorial_history import editor_versions
        record = record_fixture()
        record['history'] += [None, 'bad', {'bundle': {'website_nl_1': {'text': 'Retained legacy text'},
            'website_nl_2': {'text': 7}}}, {'bundle': None}]
        record['undo']['final_nl'] = [None, {'text': 'bad'}, 'Volunteer text', 'Volunteer text']
        versions = editor_versions(record, 'final', 'nl')
        self.assertIn('Retained legacy text', [v['text'] for v in versions])
        self.assertEqual([v['text'] for v in versions].count('Volunteer text'), 1)
        self.assertEqual(len([v for v in versions if v['source'] == 'ai']), 7)

    def test_restore_changes_only_chosen_editor_preserves_originals_and_undo(self):
        from editorial_history import editor_versions, restore_version
        record = record_fixture()
        remember_edit(record, 'final', 'nl', 'Volunteer current [home]')
        record['editor_statuses'] = {'final_nl': {'finalized': True}, 'final_en': {'finalized': True}}
        before = deepcopy(record)
        version = next(v for v in editor_versions(record, 'final', 'nl') if v['source'] == 'ai')
        restore_version(record, 'final', 'nl', version)
        self.assertEqual(record['final']['nl'], version['text'])
        self.assertEqual(record['undo']['final_nl'][-1], 'Volunteer current [home]')
        self.assertNotIn('final_nl', record['editor_statuses'])
        self.assertEqual(record['editor_statuses']['final_en'], {'finalized': True})
        for key in ('bundle', 'history', 'facebook'):
            self.assertEqual(record[key], before[key])
        self.assertEqual(record['final']['en'], before['final']['en'])
        self.assertEqual(restore_edit(record, 'final', 'nl'), 'Volunteer current [home]')

    def test_ai_restore_uses_own_profile_but_preserves_display_original(self):
        from editorial_history import editor_versions, restore_version
        record = record_fixture()
        record['history'][0]['bundle']['website_nl_1']['text'] = 'Original [home] {not_known} source: old'
        record['profile_snapshot'] = None
        version = next(v for v in editor_versions(record, 'final', 'nl') if v['text'].startswith('Original'))
        restore_version(record, 'final', 'nl', version)
        self.assertEqual(record['final']['nl'], 'Original  {not_known} ')
        self.assertEqual(version['text'], 'Original [home] {not_known} source: old')
        self.assertEqual(record['history'][0]['bundle']['website_nl_1']['text'], version['text'])

    def test_legacy_without_profile_and_volunteer_markers_remain_verbatim(self):
        from editorial_history import editor_versions, restore_version
        record = record_fixture()
        record['profile_snapshot'] = {'invalid': True}
        record['bundle'].website_nl_1.text = 'Legacy [home]'
        version = next(v for v in editor_versions(record, 'final', 'nl') if v['text'] == 'Legacy [home]')
        restore_version(record, 'final', 'nl', version)
        self.assertEqual(record['final']['nl'], 'Legacy [home]')
        remember_edit(record, 'final', 'nl', 'Human [home]')
        remember_edit(record, 'final', 'nl', 'Later')
        version = next(v for v in editor_versions(record, 'final', 'nl') if v['text'] == 'Human [home]')
        restore_version(record, 'final', 'nl', version)
        self.assertEqual(record['final']['nl'], 'Human [home]')

    def test_other_language_or_forged_version_is_rejected(self):
        from editorial_history import editor_versions, restore_version
        record = record_fixture()
        version = next(v for v in editor_versions(record, 'final', 'en') if v['source'] == 'ai')
        before = deepcopy(record)
        with self.assertRaises(ValueError):
            restore_version(record, 'final', 'nl', version)
        with self.assertRaises(ValueError):
            editor_versions(record, 'invalid', 'nl')
        self.assertEqual(record, before)


UI_SCRIPT = '''
import streamlit as st
from editorial_history import render_version_history
from content_state import remember_edit
from editorial_store import save_record
record = st.session_state['record']
scope = 'aaaaaaaaaaaaaaaaaaaa'
language = st.selectbox('Language', ['nl', 'en'], key='language')
render_version_history(record, scope, language, on_save=st.session_state['save'])
def edited(target, language):
    remember_edit(record, target, language, st.session_state[f'writing_editor_{scope}_{target}_{language}'])
for target in ('final', 'facebook'):
    key = f'writing_editor_{scope}_{target}_{language}'
    st.session_state[key] = record[target][language]
    st.text_area(target, key=key, on_change=edited, args=(target, language))
'''


class HistoryUiTests(unittest.TestCase):
    def setUp(self):
        from editorial_store import save_record
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.record = record_fixture()
        self.record['bundle'].website_nl_1.text = '<script>alert(1)</script> [home]'
        remember_edit(self.record, 'final', 'nl', 'Human final')
        remember_edit(self.record, 'final', 'en', 'Independent English')
        patches = [patch('openai.OpenAI', side_effect=AssertionError('No API calls allowed')),
                   patch('editorial_history.copy_button')]
        self.api, self.copy = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.app = AppTest.from_string(UI_SCRIPT, default_timeout=10)
        self.app.session_state['record'] = self.record
        self.app.session_state['save'] = partial(save_record, root=self.directory.name)
        self.app.run()

    def select_ai(self):
        from editorial_history import editor_versions
        version = next(v for v in editor_versions(self.record, 'final', 'nl') if v['text'].startswith('<script>'))
        self.app.selectbox(key='writing_history_version_aaaaaaaaaaaaaaaaaaaa_final_nl').set_value(version['id']).run()
        return version

    def test_empty_workspace_has_no_meaningless_history_selectors(self):
        self.app.session_state['record'] = workspace({}, 'empty')
        self.app.run()
        self.assertFalse(self.app.exception)
        self.assertEqual([item.key for item in self.app.selectbox], ['language'])

    def test_select_compare_and_language_switch_never_overwrite(self):
        self.select_ai()
        self.app.selectbox(key='language').set_value('en').run()
        self.app.selectbox(key='language').set_value('nl').run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.text_area(key='writing_editor_aaaaaaaaaaaaaaaaaaaa_final_nl').value, 'Human final')
        self.assertEqual(self.record['final']['en'], 'Independent English')
        self.api.assert_not_called()

    def test_plain_readonly_comparison_copy_and_confirmed_reversible_restore(self):
        from editorial_store import load_record
        version = self.select_ai()
        self.assertFalse(self.app.exception)
        self.assertTrue(any(item.value == version['text'] for item in self.app.text))
        self.assertTrue(any('-Human final\n+<script>' in item.value for item in self.app.code))
        self.assertFalse(any('<script>' in item.value for item in self.app.markdown))
        self.assertTrue(self.app.button(key='writing_history_restore_aaaaaaaaaaaaaaaaaaaa_final_nl').disabled)
        confirmation = next(item for item in self.app.checkbox if item.key.startswith('writing_history_confirm_'))
        confirmation.check().run()
        self.app.button(key='writing_history_restore_aaaaaaaaaaaaaaaaaaaa_final_nl').click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.text_area(key='writing_editor_aaaaaaaaaaaaaaaaaaaa_final_nl').value, '<script>alert(1)</script> ')
        self.assertEqual(self.record['undo']['final_nl'][-1], 'Human final')
        self.assertEqual(self.record['bundle'].website_nl_1.text, version['text'])
        self.assertEqual(load_record('aaaaaaaaaaaaaaaaaaaa', root=self.directory.name)['final']['nl'], '<script>alert(1)</script> ')
        self.assertTrue(any(call.args[:2] == ('Copy selected version', '<script>alert(1)</script> ')
                            for call in self.copy.call_args_list))
        self.assertTrue(any('Technical citation markers' in item.value for item in self.app.info))
        self.api.assert_not_called()

    def test_confirmation_is_reset_when_selected_version_changes(self):
        from editorial_history import editor_versions
        self.select_ai()
        confirmation = next(item for item in self.app.checkbox if item.key.startswith('writing_history_confirm_'))
        confirmation.check().run()
        other = next(v for v in editor_versions(self.record, 'final', 'nl') if 'Older.' in v['text'])
        self.app.selectbox(key='writing_history_version_aaaaaaaaaaaaaaaaaaaa_final_nl').set_value(other['id']).run()
        self.assertTrue(self.app.button(key='writing_history_restore_aaaaaaaaaaaaaaaaaaaa_final_nl').disabled)
        self.assertEqual(self.record['final']['nl'], 'Human final')

    def test_facebook_restore_preserves_other_target_and_other_language(self):
        from editorial_history import editor_versions
        remember_edit(self.record, 'facebook', 'nl', 'Volunteer Facebook')
        self.app.selectbox(key='writing_history_target_aaaaaaaaaaaaaaaaaaaa_nl').set_value('facebook').run()
        version = next(v for v in editor_versions(self.record, 'facebook', 'nl') if 'Older.' in v['text'])
        self.app.selectbox(key='writing_history_version_aaaaaaaaaaaaaaaaaaaa_facebook_nl').set_value(version['id']).run()
        confirmation = next(item for item in self.app.checkbox if item.key.startswith('writing_history_confirm_'))
        confirmation.check().run()
        self.app.button(key='writing_history_restore_aaaaaaaaaaaaaaaaaaaa_facebook_nl').click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.record['facebook']['nl'], version['text'])
        self.assertEqual(self.record['undo']['facebook_nl'][-1], 'Volunteer Facebook')
        self.assertEqual(self.record['final']['nl'], 'Human final')
        self.assertEqual(self.record['final']['en'], 'Independent English')


if __name__ == '__main__':
    unittest.main()
