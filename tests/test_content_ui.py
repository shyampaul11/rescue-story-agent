"""Per-animal editor persistence, explicit generation and local export."""
import json
from pathlib import Path
import unittest
import tempfile
from functools import partial
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.errors import AppTestError

import analysis_ui
import content_ui as ui
from content_service import ContentSettings, ContentError, validate_drafts
from content_state import (writing_scope, workspace, remember_edit, restore_edit, commit_generation,
                           has_human_edits, export_workspace, adopt_unnamed_manual_edits)
from test_content_service import profile_fixture, bundle_fixture

APP = str(Path(__file__).resolve().parents[1] / 'app.py')


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.profile, self.bundle = profile_fixture(), bundle_fixture()
        self.record = workspace({}, 'animal')

    def commit(self):
        commit_generation(self.record, self.bundle, self.profile, ContentSettings(), [], {}, 'analysis')

    def test_generation_leaves_final_editor_empty_and_seeds_facebook(self):
        self.commit()
        self.assertEqual(self.record['final'], {'nl': '', 'en': ''})
        self.assertEqual(self.record['facebook']['nl'], self.bundle.facebook_nl.text)
        self.assertFalse(has_human_edits(self.record))

    def test_regeneration_preserves_final_and_facebook_edits_including_empty(self):
        self.commit()
        remember_edit(self.record, 'final', 'nl', 'Mijn eigen tekst.')
        remember_edit(self.record, 'facebook', 'nl', '')
        self.bundle = bundle_fixture(revision=' New wording.')
        self.commit()
        self.assertEqual(self.record['final']['nl'], 'Mijn eigen tekst.')
        self.assertEqual(self.record['facebook']['nl'], '')
        self.assertEqual(self.record['facebook']['en'], self.bundle.facebook_en.text)

    def test_restore_after_edit_and_clear(self):
        remember_edit(self.record, 'final', 'nl', 'First draft.')
        remember_edit(self.record, 'final', 'nl', '')
        self.assertEqual(restore_edit(self.record, 'final', 'nl'), 'First draft.')
        self.assertTrue(has_human_edits(self.record))

    def test_scope_separates_drive_animals_and_manual_named_animals(self):
        self.assertNotEqual(writing_scope({'media_animal': {'id': 'azra'}}, self.profile),
                            writing_scope({'media_animal': {'id': 'luna'}}, self.profile))
        self.assertNotEqual(writing_scope({}, self.profile), writing_scope({}, profile_fixture('Luna')))

    def test_export_allowlist_contains_text_warnings_usage_and_no_session_secrets(self):
        self.commit()
        self.record['credential'] = 'DO NOT EXPORT'
        self.record['source_path'] = 'C:/sensitive/local/file'
        self.record['final']['nl'] = 'Vrij bewerkte tekst.'
        data = json.loads(export_workspace(self.record))
        self.assertEqual(data['final_website_nl'], 'Vrij bewerkte tekst.')
        self.assertEqual(len(data['generated_drafts']), 8)
        self.assertNotIn('DO NOT EXPORT', json.dumps(data))
        self.assertNotIn('C:/sensitive', json.dumps(data))
        self.assertIn('exported_at', data)

    def test_manual_edits_survive_when_first_profile_identifies_the_animal(self):
        state = {}
        record = workspace(state, writing_scope({}))
        remember_edit(record, 'final', 'nl', 'Prepared before analysis.')
        scope = writing_scope({}, self.profile)
        adopt_unnamed_manual_edits(state, scope)
        self.assertEqual(workspace(state, scope)['final']['nl'], 'Prepared before analysis.')
        # Another named animal must start separately.
        other = writing_scope({}, profile_fixture('Luna'))
        adopt_unnamed_manual_edits(state, other)
        self.assertEqual(workspace(state, other)['final']['nl'], '')


class WritingUiTests(unittest.TestCase):
    def setUp(self):
        self.profile, self.bundle = profile_fixture(), bundle_fixture()
        patches = [patch.object(analysis_ui, 'render_analysis', return_value=None),
                   patch.object(ui, 'load_content_settings', return_value=ContentSettings(model='synthetic')),
                   patch.object(ui, 'generate_drafts', return_value=(self.bundle, validate_drafts(self.bundle, self.profile, 'third_person'),
                                                                   {'input_tokens': 123, 'output_tokens': 456, 'total_tokens': 579})),
                   patch.object(ui, 'load_record', return_value=None, create=True),
                   patch.object(ui, 'save_record', create=True),
                   patch.object(ui, 'copy_button')]
        self.analysis, self.settings, self.generate, self.load, self.save, self.copy = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        self.app = AppTest.from_file(APP, default_timeout=10).run()
        self.app.session_state['ai_result'] = {'profile': self.profile, 'fingerprint': 'analysis-v1'}
        self.app.session_state['ai_outdated'] = False
        self.app.run()
        self.scope = writing_scope({}, self.profile)

    def generate_once(self):
        self.app.button(key='generate_drafts').click().run()
        self.assertFalse(self.app.exception)

    def editor(self, target='final', lang='nl'):
        return self.app.text_area(key=ui.editor_key(self.scope, target, lang))

    def language(self, value):
        self.app.segmented_control(key=f'writing_language_{self.scope}').set_value(value).run()

    def test_no_generation_on_startup_or_ordinary_rerun(self):
        self.app.run()
        self.generate.assert_not_called()
        self.assertEqual(self.editor().value, '')

    def test_edited_text_local_assessment_goes_stale_after_edit_and_keeps_result(self):
        self.editor().input('Azra is ready for a loving home.').run()
        self.app.button(key=f'writing_assess_{self.scope}_final_nl').click().run()
        record = self.app.session_state['writing_workspaces'][self.scope]
        self.assertIn('final_nl', record['editor_assessments'])
        retained = record['editor_assessments']['final_nl']['text_fingerprint']
        self.editor().input('Changed wording requiring another review.').run()
        record = self.app.session_state['writing_workspaces'][self.scope]
        self.assertEqual(record['editor_assessments']['final_nl']['text_fingerprint'], retained)
        self.assertTrue(any('assessment is stale' in item.value for item in self.app.warning))
        self.generate.assert_not_called()

    def test_local_bundle_assessment_available_without_writing_credentials(self):
        self.generate_once()
        self.settings.side_effect = ContentError('No writing credentials configured.')
        self.app.run()
        self.app.button(key=f'writing_reassess_{self.scope}').click().run()
        record = self.app.session_state['writing_workspaces'][self.scope]
        self.assertEqual(set(record['assessment']['drafts']), set(ui.DRAFT_KEYS))
        self.generate.assert_called_once()

    def test_manual_finalization_keeps_findings_and_edit_revokes_status(self):
        self.editor().input('Azra is completely healthy.').run()
        self.app.button(key=f'writing_assess_{self.scope}_final_nl').click().run()
        before = self.app.session_state['writing_workspaces'][self.scope]['editor_assessments']['final_nl'].copy()
        self.app.checkbox(key=f'writing_finalize_{self.scope}_final_nl').check().run()
        record = self.app.session_state['writing_workspaces'][self.scope]
        self.assertTrue(record['editor_statuses']['final_nl']['finalized'])
        self.assertEqual(record['editor_assessments']['final_nl'], before)
        self.editor().input('New wording.').run()
        record = self.app.session_state['writing_workspaces'][self.scope]
        self.assertFalse(record.get('editor_statuses', {}).get('final_nl', {}).get('finalized', False))

    def test_private_autosave_restores_edit_after_new_session(self):
        from editorial_store import load_record, save_record
        with tempfile.TemporaryDirectory() as directory:
            self.save.side_effect = partial(save_record, root=directory)
            self.load.side_effect = partial(load_record, root=directory)
            self.editor().input('Durable private edit for Azra.').run()
            restarted = AppTest.from_file(APP, default_timeout=10).run()
            restarted.session_state['ai_result'] = {'profile': self.profile, 'fingerprint': 'analysis-v1'}
            restarted.session_state['ai_outdated'] = False
            restarted.run()
            self.assertFalse(restarted.exception)
            self.assertEqual(restarted.text_area(key=ui.editor_key(self.scope, 'final', 'nl')).value,
                             'Durable private edit for Azra.')
            self.generate.assert_not_called()

    def test_privacy_sync_keeps_preanalysis_edits_when_first_profile_supplies_name(self):
        fresh = AppTest.from_file(APP, default_timeout=10).run()
        anonymous_scope = writing_scope({})
        fresh.text_area(key=ui.editor_key(anonymous_scope, 'final', 'nl')).input('Prepared before the profile.').run()
        fresh.session_state['ai_result'] = {'profile': self.profile, 'fingerprint': 'analysis-new'}
        fresh.session_state['ai_outdated'] = False
        fresh.run()
        self.assertFalse(fresh.exception)
        self.assertEqual(fresh.text_area(key=ui.editor_key(self.scope, 'final', 'nl')).value, 'Prepared before the profile.')
        self.generate.assert_not_called()

    def test_deleted_unnamed_work_cannot_migrate_into_new_animal_scope(self):
        import editorial_privacy
        fresh = AppTest.from_file(APP, default_timeout=10).run()
        anonymous_scope = writing_scope({})
        fresh.text_area(key=ui.editor_key(anonymous_scope, 'final', 'nl')).input('Old text deleted in another tab.').run()
        def token(scope):
            return 'deleted-source' if scope == anonymous_scope else ''
        with patch.object(editorial_privacy, 'deletion_token', side_effect=token):
            fresh.session_state['ai_result'] = {'profile': self.profile, 'fingerprint': 'new-analysis'}
            fresh.session_state['ai_outdated'] = False
            fresh.run()
        self.assertFalse(fresh.exception)
        self.assertEqual(fresh.text_area(key=ui.editor_key(self.scope, 'final', 'nl')).value, '')
        self.generate.assert_not_called()

    def test_adopted_autosave_cannot_resurrect_after_named_animal_is_deleted(self):
        import editorial_privacy
        from editorial_store import load_record, save_record, delete_record, deletion_token, migrate_record
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(editorial_privacy, 'deletion_token', side_effect=partial(deletion_token, root=directory)), \
             patch.object(editorial_privacy, 'delete_record', side_effect=partial(delete_record, root=directory)), \
             patch.object(ui, 'migrate_record', side_effect=partial(migrate_record, root=directory)):
            self.save.side_effect = partial(save_record, root=directory)
            self.load.side_effect = partial(load_record, root=directory)
            fresh = AppTest.from_file(APP, default_timeout=10).run()
            anonymous_scope = writing_scope({})
            fresh.text_area(key=ui.editor_key(anonymous_scope, 'final', 'nl')).input('Anonymous preparation for this animal.').run()
            self.assertIsNotNone(load_record(anonymous_scope, root=directory))
            fresh.session_state['ai_result'] = {'profile': self.profile, 'fingerprint': 'analysis'}
            fresh.session_state['ai_outdated'] = False
            fresh.run()
            self.assertFalse(fresh.exception)
            self.assertEqual(fresh.text_area(key=ui.editor_key(self.scope, 'final', 'nl')).value,
                             'Anonymous preparation for this animal.')
            self.assertIsNone(load_record(anonymous_scope, root=directory))
            fresh.checkbox(key='writing_delete_' + self.scope + '_confirm').check().run()
            fresh.button(key='writing_delete_' + self.scope).click().run()
            self.assertIsNone(load_record(self.scope, root=directory))
            restarted = AppTest.from_file(APP, default_timeout=10).run()
            self.assertEqual(restarted.text_area(key=ui.editor_key(anonymous_scope, 'final', 'nl')).value, '')
            self.generate.assert_not_called()

    def test_confirmed_local_deletion_does_not_recreate_old_work_and_other_animal_survives(self):
        import editorial_privacy
        from editorial_store import load_record, save_record, delete_record, deletion_token, WorkspaceDeletedError
        from copy import deepcopy
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(editorial_privacy, 'deletion_token', side_effect=partial(deletion_token, root=directory)), \
             patch.object(editorial_privacy, 'delete_record', side_effect=partial(delete_record, root=directory)):
            self.save.side_effect = partial(save_record, root=directory)
            self.load.side_effect = partial(load_record, root=directory)
            self.generate_once()
            self.editor().input('Private volunteer version.').run()
            old_record = deepcopy(self.app.session_state['writing_workspaces'][self.scope])
            other_scope = 'b' * 20
            other = workspace({}, other_scope)
            other['final']['nl'] = 'Other animal saved text'
            save_record(other_scope, other, root=directory)
            key = 'writing_delete_' + self.scope
            self.assertTrue(self.app.button(key=key).disabled)
            self.app.checkbox(key=key + '_confirm').check().run()
            self.app.button(key=key).click().run()
            self.assertFalse(self.app.exception)
            self.assertIsNone(load_record(self.scope, root=directory))
            self.assertIsNone(self.app.session_state['writing_workspaces'][self.scope]['bundle'])
            self.assertEqual(self.editor().value, '')
            self.assertEqual(load_record(other_scope, root=directory)['final']['nl'], 'Other animal saved text')
            with self.assertRaises(WorkspaceDeletedError):
                save_record(self.scope, old_record, root=directory)
            self.app.run()
            self.assertIsNone(load_record(self.scope, root=directory))
            self.editor().input('Deliberately starting new work.').run()
            self.assertEqual(load_record(self.scope, root=directory)['final']['nl'], 'Deliberately starting new work.')
            self.generate.assert_called_once()

    def test_drive_pending_batch_recovers_free_after_restart_without_current_analysis(self):
        from editorial_store import load_record, save_record
        with tempfile.TemporaryDirectory() as directory:
            self.save.side_effect = partial(save_record, root=directory)
            self.load.side_effect = partial(load_record, root=directory)
            animal = {'id': 'synthetic-azra-drive', 'name': 'Azra'}
            self.app.session_state['media_animal'] = animal
            self.app.run()
            scope = writing_scope({'media_animal': animal}, self.profile)
            record = self.app.session_state['writing_workspaces'][scope]
            record['pending_response'] = {'candidate': self.bundle.model_dump(mode='json'), 'usage': {},
                'profile_fingerprint': ui.profile_fingerprint(self.profile), 'analysis_fingerprint': 'analysis-v1',
                'perspective': 'third_person', 'received_at': '2026-11-05T10:00:00+00:00'}
            remember_edit(record, 'final', 'nl', 'Keep the durable website edit.')
            remember_edit(record, 'facebook', 'nl', 'Keep the durable Facebook edit.')
            self.app.run()
            saved = load_record(scope, root=directory)
            self.assertEqual(saved['pending_response']['profile_snapshot']['identity']['animal_name'], 'Azra')
            self.settings.side_effect = ContentError('No writing key configured.')
            restarted = AppTest.from_file(APP, default_timeout=10).run()
            restarted.session_state['media_animal'] = animal
            restarted.run()
            self.assertFalse(restarted.exception)
            for key in ui.DRAFT_KEYS:
                self.assertTrue(any(item.value == getattr(self.bundle, key).text for item in restarted.text))
            restarted.button(key=f'writing_recheck_{scope}').click().run()
            self.assertFalse(restarted.exception)
            recovered = restarted.session_state['writing_workspaces'][scope]
            self.assertEqual(recovered['bundle'], self.bundle)
            self.assertEqual(recovered['final']['nl'], 'Keep the durable website edit.')
            self.assertEqual(recovered['facebook']['nl'], 'Keep the durable Facebook edit.')
            self.assertTrue(recovered['outdated'])
            self.assertTrue(recovered['assessment_stale'])
            self.generate.assert_not_called()

    def test_retained_snapshot_for_another_named_drive_animal_cannot_be_recovered(self):
        animal = {'id': 'synthetic-azra-drive', 'name': 'Azra'}
        self.app.session_state['media_animal'] = animal
        self.app.run()
        scope = writing_scope({'media_animal': animal}, self.profile)
        record = self.app.session_state['writing_workspaces'][scope]
        other = profile_fixture('Luna')
        record['animal_name'] = 'Azra'
        record['pending_response'] = {'candidate': bundle_fixture('Luna').model_dump(mode='json'),
            'profile_snapshot': other.model_dump(mode='json'), 'profile_fingerprint': ui.profile_fingerprint(other),
            'analysis_fingerprint': 'luna-analysis', 'perspective': 'third_person'}
        self.app.session_state['ai_result'] = None
        self.app.run()
        self.assertFalse(self.app.exception)
        self.assertFalse(any(button.key == f'writing_recheck_{scope}' for button in self.app.button))
        self.assertIsNone(record['bundle'])

    def test_stale_manual_reference_never_shows_new_response_as_old_support(self):
        self.generate_once()
        record = self.app.session_state['writing_workspaces'][self.scope]
        record['manual_sources'] = [{'id': 'manual:one', 'text': 'Earlier response.', 'attribution': 'Mira'}]
        record['assessment_manual_sources_fingerprint'] = ui.manual_sources_fingerprint(record)
        record['assessment'] = {'drafts': {'website_nl_1': {'passages': [{'start': 0, 'end': 4,
            'color': 'green', 'source_references': ['manual:one']}], 'findings': []}}, 'questions': []}
        record['manual_sources'][0]['text'] = 'Changed response that did not support this assessment.'
        record['assessment_stale'] = True
        self.app.run()
        self.assertFalse(any('Changed response that did not support this assessment.' in item.value for item in self.app.text))

    def test_explicit_generation_once_then_no_calls_during_language_or_style_switch(self):
        self.generate_once()
        self.language('English')
        self.app.segmented_control(key=f'writing_draft_{self.scope}').set_value('Draft 3').run()
        self.app.run()
        self.generate.assert_called_once()
        self.assertTrue(any(value.value == self.bundle.website_en_3.text for value in self.app.text))

    def test_final_edit_survives_style_and_language_switches(self):
        self.generate_once()
        self.editor().input('Mijn gecombineerde alinea.').run()
        self.app.segmented_control(key=f'writing_draft_{self.scope}').set_value('Draft 2').run()
        self.assertEqual(self.editor().value, 'Mijn gecombineerde alinea.')
        self.language('English')
        self.assertEqual(self.editor(lang='en').value, '')
        self.editor(lang='en').input('My independent English version.').run()
        self.language('Nederlands')
        self.assertEqual(self.editor().value, 'Mijn gecombineerde alinea.')
        self.language('English')
        self.assertEqual(self.editor(lang='en').value, 'My independent English version.')
        self.generate.assert_called_once()

    def test_pending_editor_change_survives_immediate_language_toggle(self):
        self.editor().input('Latest text before changing language.')
        self.app.segmented_control(key=f'writing_language_{self.scope}').set_value('English').run()
        self.language('Nederlands')
        self.assertEqual(self.editor().value, 'Latest text before changing language.')
        self.generate.assert_not_called()

    def test_facebook_edits_are_independent_by_language(self):
        self.generate_once()
        self.editor('facebook').input('Mijn Facebook-post.').run()
        self.language('English')
        self.assertEqual(self.editor('facebook', 'en').value, self.bundle.facebook_en.text)
        self.editor('facebook', 'en').input('My English Facebook post.').run()
        self.language('Nederlands')
        self.assertEqual(self.editor('facebook').value, 'Mijn Facebook-post.')
        self.generate.assert_called_once()

    def test_copy_controls_receive_current_draft_and_editor_text(self):
        self.generate_once()
        self.editor().input('Exact final wording.').run()
        self.assertTrue(any(call.args[:2] == ('Copy Draft', self.bundle.website_nl_1.text) for call in self.copy.call_args_list))
        self.assertTrue(any(call.args[:2] == ('Copy Final Text', 'Exact final wording.') for call in self.copy.call_args_list))
        self.assertTrue(any(call.args[:2] == ('Copy Facebook Text', self.bundle.facebook_nl.text) for call in self.copy.call_args_list))
        self.generate.assert_called_once()

    def test_generated_copy_omits_known_markers_and_keeps_original_and_human_edits(self):
        self.bundle.website_nl_1.text += '\n[home] source: old'
        self.bundle.facebook_nl.text += '\n{social}'
        self.generate_once()
        self.editor().input('My human text [home] remains unchanged.').run()
        self.assertTrue(any(call.args[:2] == ('Copy Draft', self.bundle.website_nl_1.text.replace('[home]', '').replace('source: old', ''))
                            for call in self.copy.call_args_list))
        self.assertTrue(any(call.args[:2] == ('Copy generated Facebook draft', self.bundle.facebook_nl.text.replace('{social}', ''))
                            for call in self.copy.call_args_list))
        self.assertTrue(any(call.args[:2] == ('Copy Final Text', 'My human text [home] remains unchanged.')
                            for call in self.copy.call_args_list))
        self.assertTrue(any(item.value == self.bundle.website_nl_1.text for item in self.app.text))
        self.assertTrue(any('Technical citation markers' in item.value for item in self.app.info))

    def test_clear_requires_confirmation_and_restore_recovers_previous_text(self):
        self.editor().input('Keep me until confirmed.').run()
        key = f'writing_clear_{self.scope}_nl'
        self.assertTrue(self.app.button(key=key).disabled)
        with self.assertRaises(AppTestError):
            self.app.button(key=key).click()
        self.app.checkbox(key=f'writing_clear_confirm_{self.scope}_nl').check().run()
        self.app.button(key=key).click().run()
        self.assertEqual(self.editor().value, '')
        self.app.button(key=f'writing_restore_{self.scope}_final_nl').click().run()
        self.assertEqual(self.editor().value, 'Keep me until confirmed.')
        self.generate.assert_not_called()

    def test_profile_changes_mark_drafts_and_final_text_stale_without_overwriting(self):
        self.generate_once()
        self.editor().input('My preserved edit.').run()
        changed = self.profile.model_copy(deep=True)
        changed.unknown_information.append('Current location unknown.')
        self.app.session_state['ai_result'] = {'profile': changed, 'fingerprint': 'analysis-v2'}
        self.app.run()
        self.assertTrue(any('Potentially outdated' in warning.value for warning in self.app.warning))
        self.assertEqual(self.editor().value, 'My preserved edit.')
        self.generate.assert_called_once()

    def test_free_reassessment_uses_current_profile_without_paid_regeneration(self):
        self.generate_once()
        self.editor().input('Azra needs a quiet home.').run()
        self.app.button(key=f'writing_assess_{self.scope}_final_nl').click().run()
        changed = self.profile.model_copy(deep=True)
        changed.unknown_information.append('Current care requires confirmation.')
        self.app.session_state['ai_result'] = {'profile': changed, 'fingerprint': 'analysis-v2'}
        self.app.run()
        self.app.button(key=f'writing_reassess_{self.scope}').click().run()
        self.app.button(key=f'writing_assess_{self.scope}_final_nl').click().run()
        self.app.run()
        record = self.app.session_state['writing_workspaces'][self.scope]
        self.assertTrue(record['outdated'])
        self.assertFalse(record['assessment_stale'])
        self.assertFalse(any('assessment is stale' in warning.value for warning in self.app.warning))
        self.assertEqual(record['assessment_profile_fingerprint'], ui.profile_fingerprint(changed))
        self.assertEqual(self.editor().value, 'Azra needs a quiet home.')
        self.generate.assert_called_once()

    def test_legacy_bundle_without_profile_snapshot_survives_invalid_current_provenance(self):
        self.generate_once()
        self.editor().input('Keep legacy human wording.').run()
        record = self.app.session_state['writing_workspaces'][self.scope]
        record.pop('profile_snapshot', None)
        record.pop('assessment_profile_snapshot', None)
        invalid = self.profile.model_copy(deep=True)
        invalid.evidence[0].subject = 'other_animal'
        self.app.session_state['ai_result'] = {'profile': invalid, 'fingerprint': 'invalid-provenance'}
        self.app.run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.editor().value, 'Keep legacy human wording.')
        self.assertTrue(any(item.value == self.bundle.website_nl_1.text for item in self.app.text))
        self.assertTrue(self.app.button(key=f'writing_reassess_{self.scope}').disabled)
        self.assertTrue(self.app.button(key=f'writing_assess_{self.scope}_final_nl').disabled)
        self.assertTrue(record['assessment_stale'])
        self.generate.assert_called_once()

    def test_stale_sources_block_generation_until_new_analysis(self):
        self.app.session_state['ai_outdated'] = True
        self.app.button(key='generate_drafts').click().run()
        self.generate.assert_not_called()
        self.assertTrue(any('outdated' in error.value for error in self.app.error))

    def test_regeneration_needs_confirmation_and_preserves_human_edits(self):
        self.generate_once()
        self.editor().input('Keep my website final.').run()
        self.editor('facebook').input('Keep my Facebook edit.').run()
        self.assertTrue(self.app.button(key='generate_drafts').disabled)
        confirmation = next(item for item in self.app.checkbox if item.key.startswith('writing_regenerate_confirm_'))
        confirmation.check().run()
        replacement = bundle_fixture(revision=' New version.')
        self.generate.return_value = (replacement, [], {})
        self.app.button(key='generate_drafts').click().run()
        self.assertEqual(self.generate.call_count, 2)
        self.assertEqual(self.editor().value, 'Keep my website final.')
        self.assertEqual(self.editor('facebook').value, 'Keep my Facebook edit.')
        self.assertTrue(any(value.value == replacement.website_nl_1.text for value in self.app.text))
        self.app.run()
        self.assertTrue(self.app.button(key='generate_drafts').disabled)

    def test_generation_failure_preserves_previous_drafts_and_edits(self):
        self.generate_once()
        self.generate.side_effect = ContentError('Safe writing failure.')
        self.app.button(key='generate_drafts').click().run()
        self.assertTrue(any(value.value == self.bundle.website_nl_1.text for value in self.app.text))
        self.assertTrue(any('Safe writing failure' in error.value for error in self.app.error))
        self.app.run()
        self.assertEqual(self.generate.call_count, 2)

    def test_invalid_profile_makes_no_request(self):
        self.app.session_state['ai_result'] = {'profile': {'invalid': 'value'}, 'fingerprint': 'invalid'}
        self.app.button(key='generate_drafts').click().run()
        self.generate.assert_not_called()
        self.assertFalse(self.app.exception)

    def test_switching_animals_separates_and_restores_editor_workspaces(self):
        self.generate_once()
        self.editor().input('Azra text only.').run()
        luna = profile_fixture('Luna')
        self.app.session_state['ai_result'] = {'profile': luna, 'fingerprint': 'luna'}
        self.app.run()
        luna_scope = writing_scope({}, luna)
        self.assertEqual(self.app.text_area(key=ui.editor_key(luna_scope, 'final', 'nl')).value, '')
        self.assertFalse(any(value.value == self.bundle.website_nl_1.text for value in self.app.text))
        self.app.text_area(key=ui.editor_key(luna_scope, 'final', 'nl')).input('Luna text only.').run()
        self.app.session_state['ai_result'] = {'profile': self.profile, 'fingerprint': 'analysis-v1'}
        self.app.run()
        self.assertEqual(self.editor().value, 'Azra text only.')
        self.generate.assert_called_once()

    def test_first_person_is_explicit_configuration_for_generation(self):
        self.app.radio(key=f'writing_perspective_{self.scope}').set_value('First person').run()
        self.generate.assert_not_called()
        self.generate.return_value = (bundle_fixture(perspective='first_person'), [], {})
        self.generate_once()
        self.assertEqual(self.generate.call_args.args[2], 'first_person')

    def test_export_and_usage_are_displayed_without_an_api_call(self):
        self.generate_once()
        self.editor().input('Export this final text.').run()
        self.assertTrue(self.app.get('download_button'))
        self.assertTrue(any('input: 123' in value.value and 'total: 579' in value.value for value in self.app.text))
        self.assertTrue(any('Confirm current care needs.' in value.value for value in self.app.text))
        self.generate.assert_called_once()

    def test_source_profile_changing_during_generation_rejects_stale_output(self):
        def changed(*args):
            self.app.session_state['ai_outdated'] = True
            return self.bundle, [], {}
        self.generate.side_effect = changed
        self.generate_once()
        self.assertIsNone(self.app.session_state['writing_workspaces'][self.scope]['bundle'])
        self.assertEqual(self.app.session_state['writing_workspaces'][self.scope]['pending_response']['candidate']['website_nl_1']['text'],
                         self.bundle.website_nl_1.text)
        self.assertTrue(any('changed while writing' in error.value for error in self.app.error))

    def test_profile_removed_during_generation_retains_received_text(self):
        def changed(*args):
            self.app.session_state['ai_result'] = None
            return self.bundle, [], {}
        self.generate.side_effect = changed
        self.generate_once()
        record = self.app.session_state['writing_workspaces'][self.scope]
        self.assertIsNone(record['bundle'])
        self.assertEqual(record['pending_response']['candidate']['facebook_en']['text'], self.bundle.facebook_en.text)


if __name__ == '__main__':
    unittest.main()
