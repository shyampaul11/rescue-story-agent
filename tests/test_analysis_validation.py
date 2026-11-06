"""Reproduce safe-formatting failures and distinguish rejected model responses without paid calls."""
import os
import json
import httpx
from types import SimpleNamespace
import unittest
from editorial_test_support import isolated_editorial_storage
from unittest.mock import patch

from ai_config import AnalysisError, AnalysisSettings
from animal_profile import AnimalContext, AnimalProfile, AnimalSources
import ai_service as ai
from analysis_validation import ProfileOutputError, ProfileRuleError
from analysis_media import PreparedImage
import analysis_ui as ui
from streamlit.testing.v1 import AppTest
from test_ai_service import empty_profile, record
from test_analysis_ui import APP


class AnalysisValidationTests(unittest.TestCase):
    def call_response(self, profile, sources=None, context=None, status='completed'):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-key'}, clear=True), patch.object(ai, 'OpenAI') as factory:
            client = factory.return_value.__enter__.return_value
            client.responses.parse.return_value = SimpleNamespace(status=status, output_parsed=profile)
            return ai.analyze_animal(sources or AnimalSources(history='Azra was rescued.'), [],
                                     AnalysisSettings(model='synthetic'), context)

    def test_incomplete_response_keeps_specific_reason(self):
        with self.assertRaisesRegex(AnalysisError, 'incomplete'):
            self.call_response(None, status='incomplete')

    def test_formatting_only_quote_difference_uses_original_source_text(self):
        data = empty_profile().model_dump()
        data['evidence'] = [record(quote='Azra was rescued. More information follows.')]
        profile = self.call_response(AnimalProfile.model_validate(data),
            AnimalSources(history='Azra was rescued.\n\nMore information follows.'))
        self.assertEqual(profile.evidence[0].supporting_text_or_description,
                         'Azra was rescued.\n\nMore information follows.')

    def test_explicit_selected_name_does_not_require_model_to_invent_a_citation(self):
        data = empty_profile().model_dump()
        data['identity'].update(animal_name='Azra', name_evidence_id=None)
        profile = self.call_response(AnimalProfile.model_validate(data), context=AnimalContext(animal_name='Azra', rescuer_name='Sophie'))
        self.assertEqual(profile.identity.animal_name, 'Azra')
        name_record = next(item for item in profile.evidence if item.id == profile.identity.name_evidence_id)
        self.assertEqual(name_record.source_reference, 'animal_label')
        self.assertEqual(name_record.supporting_text_or_description, 'Azra')

    def test_changed_negation_or_numbers_are_not_repaired(self):
        for quote in ['Azra is vaccinated.', 'Azra weighs 20 kg.']:
            data = empty_profile().model_dump()
            data['evidence'] = [record(quote=quote)]
            with self.subTest(quote=quote), self.assertRaises(ProfileOutputError) as caught:
                self.call_response(AnimalProfile.model_validate(data),
                    AnimalSources(history='Azra is not vaccinated. Azra weighs 10 kg.'))
            self.assertEqual(caught.exception.details['code'], 'TEXT_QUOTE')
            self.assertNotIn(quote, str(caught.exception))

    def test_safe_diagnostics_identify_section_and_record_without_source_content(self):
        data = empty_profile().model_dump()
        data['evidence'] = [record(quote='PRIVATE FABRICATED CONTENT')]
        with self.assertRaises(ProfileOutputError) as caught:
            self.call_response(AnimalProfile.model_validate(data))
        self.assertEqual(caught.exception.details, {'stage': 'evidence', 'code': 'TEXT_QUOTE',
                                                   'evidence_number': 1, 'section': 'history'})
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertNotIn('PRIVATE', json.dumps(caught.exception.details))
        self.assertIsNotNone(caught.exception.candidate)

    def test_schema_error_identifies_safe_field_path_and_omits_values(self):
        data = empty_profile().model_dump()
        data['evidence'] = [record(verification_status='PRIVATE INVALID VALUE')]
        with self.assertRaises(ProfileOutputError) as caught:
            self.call_response(data)
        self.assertEqual(caught.exception.details['code'], 'SCHEMA')
        self.assertEqual(caught.exception.details['fields'][0]['field'], 'evidence.0.verification_status')
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertNotIn('PRIVATE', json.dumps(caught.exception.details))
        self.assertIsNone(caught.exception.candidate)

    def test_ambiguous_model_normalization_is_discarded_without_changing_source_date(self):
        data = empty_profile().model_dump()
        data['updates'] = [dict(original_date='03/04/2026', normalized_date='2026-04-03',
            source_excerpt='03/04/2026: Recovery reported.', evidence_ids=[], change_type='adds_information',
            superseded_evidence_ids=[], unresolved_contradictions=[])]
        profile = self.call_response(AnimalProfile.model_validate(data), AnimalSources(updates='03/04/2026: Recovery reported.'))
        self.assertEqual(profile.updates[0].original_date, '03/04/2026')
        self.assertIsNone(profile.updates[0].normalized_date)
        self.assertTrue(any(warning.category == 'ambiguous_date' for warning in profile.review_warnings))

    def test_locally_derived_iso_date_uses_source_not_model_guess(self):
        data = empty_profile().model_dump()
        data['updates'] = [dict(original_date='2026-04-03', normalized_date='2026-04-04',
            source_excerpt='2026-04-03: Recovery reported.', evidence_ids=[], change_type='adds_information',
            superseded_evidence_ids=[], unresolved_contradictions=[])]
        profile = self.call_response(AnimalProfile.model_validate(data), AnimalSources(updates='2026-04-03: Recovery reported.'))
        self.assertEqual(profile.updates[0].normalized_date, '2026-04-03')

    def test_another_animal_name_still_rejected(self):
        data = empty_profile().model_dump()
        data['identity'].update(animal_name='Luna', name_evidence_id='e1')
        data['evidence'] = [record(quote='Luna was rescued.')]
        with self.assertRaises(ProfileOutputError) as caught:
            self.call_response(AnimalProfile.model_validate(data), AnimalSources(history='Luna was rescued.'),
                               AnimalContext(animal_name='Azra'))
        self.assertEqual(caught.exception.details['code'], 'WRONG_ANIMAL')

    def test_pending_candidate_does_not_retain_image_bytes(self):
        error = ProfileOutputError('evidence', 'TEXT_QUOTE', 'Test failure.', candidate=empty_profile())
        pending = ui.pending_validation(error, AnimalSources(history='Text.'),
            [PreparedImage('photo:1', 'PHOTO_OBSERVATION', b'PRIVATE IMAGE BYTES')], AnimalContext(), 'fp', {'photo:1': 'p.jpg'})
        self.assertNotIn('PRIVATE IMAGE BYTES', json.dumps(pending))
        self.assertNotIn('jpeg', pending['media'][0])

    def test_real_sdk_partial_json_has_output_limit_diagnostic_not_quote_failure(self):
        from openai import OpenAI
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={'id': 'resp_synthetic', 'object': 'response', 'created_at': 0,
                'status': 'incomplete', 'model': 'synthetic', 'incomplete_details': {'reason': 'max_output_tokens'},
                'output': [{'id': 'msg_synthetic', 'type': 'message', 'role': 'assistant', 'status': 'incomplete',
                            'content': [{'type': 'output_text', 'text': '{"identity":', 'annotations': []}]}]})
        client = OpenAI(api_key='synthetic', max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic'}, clear=True), patch.object(ai, 'OpenAI', return_value=client):
            with self.assertRaises(ProfileOutputError) as caught:
                ai.analyze_animal(AnimalSources(history='Azra was rescued.'), [], AnalysisSettings(model='synthetic'))
        self.assertEqual(caught.exception.details['code'], 'JSON_OUTPUT')
        self.assertIn('output-token limit', str(caught.exception))
        self.assertEqual(len(requests), 1)


@isolated_editorial_storage
class LocalValidationUiTests(unittest.TestCase):
    def setUp(self):
        profile = empty_profile()
        data = profile.model_dump()
        data['evidence'] = [record()]
        self.candidate = AnimalProfile.model_validate(data)
        patches = [patch.object(ui, 'load_settings', return_value=AnalysisSettings(model='synthetic')),
                   patch.object(ui, 'require_credentials', return_value='synthetic-key'),
                   patch.object(ui, 'prepare_selected_media', return_value=([], {})),
                   patch.object(ai, 'analyze_animal', side_effect=ProfileOutputError('evidence', 'TEXT_QUOTE',
                       'A quote did not match.', {'section': 'history'}, candidate=self.candidate))]
        self.settings, self.credentials, self.media, self.analyze = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        self.app = AppTest.from_file(APP, default_timeout=10).run()
        self.app.text_area(key='animal_history').input('Azra was rescued.').run()
        self.app.checkbox(key='ai_consent').check().run()
        self.app.button(key='analyze_animal').click().run()

    def test_rejected_candidate_is_not_published_as_valid_profile(self):
        self.assertNotIn('ai_result', self.app.session_state)
        self.assertIn('ai_pending_validation', self.app.session_state)
        self.assertTrue(any(item.label == 'Analysis validation details (safe to share)' for item in self.app.expander))

    def test_explicit_local_recheck_can_accept_after_a_validator_fix_without_api(self):
        self.app.button(key='recheck_analysis').click().run()
        self.assertFalse(self.app.exception)
        self.assertIn('ai_result', self.app.session_state)
        self.assertNotIn('ai_pending_validation', self.app.session_state)
        self.analyze.assert_called_once()
        self.media.assert_called_once()
        self.assertFalse(self.app.error)

    def test_local_recheck_does_not_bypass_rules_or_repeat_automatically(self):
        with patch.object(ai, 'validate_candidate', side_effect=ProfileRuleError('TEXT_QUOTE', 'Quote does not match.')) as validate:
            self.app.run()
            validate.assert_not_called()
            self.app.button(key='recheck_analysis').click().run()
            validate.assert_called_once()
        self.assertNotIn('ai_result', self.app.session_state)
        self.assertIn('ai_pending_validation', self.app.session_state)
        self.analyze.assert_called_once()

    def test_source_change_discards_pending_response(self):
        self.app.text_area(key='animal_history').input('Different source.').run()
        self.assertNotIn('ai_pending_validation', self.app.session_state)
        self.assertNotIn('ai_validation_details', self.app.session_state)
        self.analyze.assert_called_once()


if __name__ == '__main__':
    unittest.main()
