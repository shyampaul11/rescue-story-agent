"""Safe diagnostic regressions for failed draft annotations; never call a real API."""
import unittest
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from content_service import (validate_drafts, recheck_drafts, generate_drafts, ContentSettings, ContentValidationError)
import content_service as writing
import content_ui as ui
import analysis_ui
from content_state import writing_scope, workspace
from streamlit.testing.v1 import AppTest
from test_content_ui import APP
from test_content_service import profile_fixture, bundle_fixture


class WritingExcerptTests(unittest.TestCase):
    def test_whitespace_only_annotation_difference_matches_original_draft(self):
        bundle = bundle_fixture()
        old = bundle.website_nl_1.claims[2].excerpt
        bundle.website_nl_1.text = bundle.website_nl_1.text.replace(old, old.replace(' ', '\n'))
        validate_drafts(bundle, profile_fixture(), 'third_person')
        self.assertEqual(bundle.website_nl_1.claims[2].excerpt, old.replace(' ', '\n'))

    def test_three_failure_cases_have_specific_safe_diagnostics(self):
        for code, excerpt, references in [
            ('EMPTY_DRAFT_EXCERPT', '', ['name']),
            ('MISSING_DRAFT_REFERENCES', 'Azra', []),
            ('DRAFT_EXCERPT_MISMATCH', 'PRIVATE UNMATCHED WORDING', ['name'])]:
            bundle = bundle_fixture()
            bundle.website_en_2.claims[0].excerpt = excerpt
            bundle.website_en_2.claims[0].source_references = references
            with self.subTest(code=code), self.assertRaises(ContentValidationError) as caught:
                validate_drafts(bundle, profile_fixture(), 'third_person')
            details = caught.exception.details
            self.assertEqual(details['code'], code)
            self.assertEqual(details['draft'], 'website_en_2')
            self.assertEqual(details['claim_number'], 1)
            self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertNotIn('PRIVATE', json.dumps(details))

    def test_changed_negation_is_not_repaired(self):
        bundle = bundle_fixture()
        bundle.website_nl_1.claims[0].excerpt = 'Azra is not vaccinated.'
        with self.assertRaises(ContentValidationError) as caught:
            recheck_drafts(bundle, profile_fixture(), 'third_person')
        self.assertEqual(caught.exception.details['code'], 'DRAFT_EXCERPT_MISMATCH')

    def test_profile_wording_used_instead_of_draft_wording_is_identified(self):
        bundle = bundle_fixture()
        bundle.website_nl_1.claims[3].excerpt = 'Recovery was reported in 2026.'
        with self.assertRaises(ContentValidationError) as caught:
            recheck_drafts(bundle, profile_fixture(), 'third_person')
        self.assertTrue(caught.exception.details['excerpt_found_in_profile'])
        self.assertTrue(caught.exception.details['excerpt_found_in_another_draft'])

    def test_failed_structured_response_is_retained_without_an_extra_request(self):
        bundle = bundle_fixture()
        bundle.website_nl_1.claims[0].excerpt = 'PRIVATE UNMATCHED WORDING'
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic'}, clear=True), patch.object(writing, 'OpenAI') as factory:
            client = factory.return_value.__enter__.return_value
            client.responses.parse.return_value = SimpleNamespace(status='completed', output_parsed=bundle,
                usage=SimpleNamespace(input_tokens=100, output_tokens=200, total_tokens=300))
            retained, issues, usage = generate_drafts(profile_fixture(), ContentSettings())
            client.responses.parse.assert_called_once()
        self.assertEqual(retained.website_nl_1.text, bundle.website_nl_1.text)
        self.assertEqual(usage['total_tokens'], 300)
        self.assertIn('DRAFT_EXCERPT_MISMATCH', [issue.code for issue in issues])
        self.assertNotIn('PRIVATE', ' '.join(issue.message for issue in issues))


class WritingRecheckUiTests(unittest.TestCase):
    def setUp(self):
        self.profile, self.bundle = profile_fixture(), bundle_fixture()
        failure = ContentValidationError('DRAFT_EXCERPT_MISMATCH', 'An annotation does not match.', 'website_nl_1', 1)
        failure.candidate = self.bundle.model_dump(mode='json')
        failure.usage = {'input_tokens': 100, 'output_tokens': 200, 'total_tokens': 300}
        patches = [patch.object(analysis_ui, 'render_analysis', return_value=None),
            patch.object(ui, 'load_content_settings', return_value=ContentSettings(model='synthetic')),
            patch.object(ui, 'load_record', return_value=None), patch.object(ui, 'save_record'),
            patch.object(ui, 'generate_drafts', side_effect=failure), patch.object(ui, 'copy_button')]
        self.analysis, self.settings, _, _, self.generate, self.copy = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        self.app = AppTest.from_file(APP, default_timeout=10).run()
        self.app.session_state['ai_result'] = {'profile': self.profile, 'fingerprint': 'analysis'}
        self.app.session_state['ai_outdated'] = False
        self.app.run()
        self.scope = writing_scope({}, self.profile)
        self.app.button(key='generate_drafts').click().run()

    def test_local_recheck_preserves_final_edits_and_never_requests_regeneration(self):
        self.assertIn('pending_response', self.app.session_state['writing_workspaces'][self.scope])
        self.assertIsNone(self.app.session_state['writing_workspaces'][self.scope]['bundle'])
        self.app.text_area(key=ui.editor_key(self.scope, 'final', 'nl')).input('Preserve my final wording.').run()
        self.app.button(key=f'writing_recheck_{self.scope}').click().run()
        self.assertFalse(self.app.exception)
        record = self.app.session_state['writing_workspaces'][self.scope]
        self.assertIsNotNone(record['bundle'])
        self.assertNotIn('pending_response', record)
        self.assertEqual(record['final']['nl'], 'Preserve my final wording.')
        self.generate.assert_called_once()

    def test_recheck_cannot_bypass_the_validation_rules(self):
        with patch.object(ui, 'recheck_drafts', side_effect=ContentValidationError('DRAFT_EXCERPT_MISMATCH', 'Still unmatched.', 'website_nl_1', 1)) as check:
            self.app.run()
            check.assert_not_called()
            self.app.button(key=f'writing_recheck_{self.scope}').click().run()
            check.assert_called_once()
        self.assertIsNone(self.app.session_state['writing_workspaces'][self.scope]['bundle'])
        self.assertTrue(any('Writing validation details' in item.label for item in self.app.expander))
        self.generate.assert_called_once()

    def test_profile_changes_mark_saved_candidate_stale_and_preserve_received_text(self):
        self.app.session_state['ai_result'] = {'profile': self.profile, 'fingerprint': 'changed'}
        self.app.run()
        record = self.app.session_state['writing_workspaces'][self.scope]
        self.assertEqual(record['pending_response']['candidate']['facebook_en']['text'], self.bundle.facebook_en.text)
        self.assertTrue(record['assessment_stale'])
        self.generate.assert_called_once()


if __name__ == '__main__':
    unittest.main()
