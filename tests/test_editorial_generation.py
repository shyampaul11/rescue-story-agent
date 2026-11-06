"""Retain useful writing independently from editorial assessment; mocked provider only."""
import unittest
import json
import httpx
from types import SimpleNamespace
from unittest.mock import patch

import content_models
import content_service as service
from content_models import DRAFT_KEYS
from content_state import commit_generation, workspace, private_diagnostic_data
from test_content_service import profile_fixture, bundle_fixture


class EditorialGenerationTests(unittest.TestCase):
    def setUp(self):
        self.profile, self.bundle = profile_fixture(), bundle_fixture()

    def generate(self, candidate):
        with patch.object(service, 'require_credentials', return_value='synthetic'), patch.object(service, 'OpenAI') as factory:
            client = factory.return_value.__enter__.return_value
            client.responses.parse.return_value = SimpleNamespace(status='completed', output_parsed=candidate, usage=None)
            result = service.generate_drafts(self.profile, service.ContentSettings())
            client.responses.parse.assert_called_once()
            self.assertEqual(factory.call_args.kwargs['max_retries'], 0)
            return result

    def test_unsupported_prose_and_bad_references_retain_all_eight(self):
        draft = self.bundle.website_en_1
        draft.text += '\nAzra is fully vaccinated and ready to travel.'
        draft.claims[0].source_references = ['invented-id']
        snapshot = self.bundle.model_dump()
        result, issues, _ = self.generate(self.bundle)
        self.assertEqual(result.model_dump(), snapshot)
        self.assertEqual(len([getattr(result, key) for key in DRAFT_KEYS]), 8)
        self.assertTrue(issues)

    def test_missing_and_malformed_annotations_do_not_lose_prose(self):
        value = self.bundle.model_dump()
        value['website_nl_1']['claims'] = [{'excerpt': 'Azra', 'source_references': 12, 'qualification': 'nonsense'}, 'broken']
        value['website_en_2'].pop('claims')
        result, issues, _ = self.generate(value)
        self.assertEqual(result.website_nl_1.text, self.bundle.website_nl_1.text)
        self.assertTrue(any('Annotation metadata issue:' in note for note in result.website_nl_1.review_notes))
        self.assertTrue(issues)

    def test_empty_core_text_is_technical_and_preserves_existing_work(self):
        value = self.bundle.model_dump()
        value['facebook_en']['text'] = '  '
        with self.assertRaises(service.ContentError):
            self.generate(value)

    def test_assessment_failure_retains_text_and_marks_incomplete(self):
        with patch('editorial_assessment.assess_bundle', side_effect=RuntimeError('PRIVATE TOKEN')):
            result, issues, _ = self.generate(self.bundle)
        self.assertEqual(result, self.bundle)
        self.assertIn('assessment_incomplete', [issue.code for issue in issues])
        self.assertNotIn('PRIVATE', str(issues))

    def test_malformed_assessment_result_preserves_every_draft(self):
        with patch('editorial_assessment.assess_bundle', return_value=None):
            result, issues, _ = self.generate(self.bundle)
        self.assertEqual(result, self.bundle)
        self.assertIn('assessment_incomplete', [issue.code for issue in issues])

    def test_actual_sdk_parsing_preserves_text_with_broken_annotation_types(self):
        from openai import OpenAI
        value = self.bundle.model_dump()
        value['website_nl_1']['claims'] = 'broken annotations'
        value['website_en_1']['claims'] = [{'excerpt': None, 'source_references': 5, 'qualification': False}]
        value['facebook_en'].pop('review_notes')
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'id': 'resp_mock', 'object': 'response', 'created_at': 0,
                'status': 'completed', 'model': 'test', 'output': [{'id': 'msg_mock', 'type': 'message',
                'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text',
                'text': json.dumps(value), 'annotations': []}]}]})
        with patch.object(service, 'require_credentials', return_value='synthetic'), patch.object(service, 'OpenAI') as factory:
            factory.return_value = OpenAI(api_key='synthetic', max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(respond)))
            result, issues, _ = service.generate_drafts(self.profile, service.ContentSettings())
        self.assertEqual(result.website_nl_1.text, self.bundle.website_nl_1.text)
        self.assertTrue(any(note.startswith('Annotation metadata issue:') for note in result.website_en_1.review_notes))
        self.assertEqual(len(requests), 1)

    def test_commit_keeps_final_edits_and_previous_generated_revision(self):
        record = workspace({}, 'test')
        settings = service.ContentSettings()
        commit_generation(record, self.bundle, self.profile, settings, [], {}, 'analysis1')
        record['final']['nl'] = 'Volunteer text'
        record['facebook']['en'] = 'Edited Facebook'
        newer = bundle_fixture(revision=' revised')
        commit_generation(record, newer, self.profile, settings, [], {}, 'analysis1')
        self.assertEqual(record['final']['nl'], 'Volunteer text')
        self.assertEqual(record['facebook']['en'], 'Edited Facebook')
        self.assertEqual(record['history'][0]['bundle']['website_nl_1']['text'], self.bundle.website_nl_1.text)
        self.assertIn('drafts', record['assessment'])
        self.assertEqual(record['profile_snapshot']['identity']['animal_name'], 'Azra')

    def test_local_reassessment_never_instantiates_client(self):
        self.assertTrue(callable(getattr(service, 'reassess_drafts', None)))
        with patch.object(service, 'OpenAI', side_effect=AssertionError('No API')):
            bundle, issues = service.reassess_drafts(self.bundle, self.profile, 'third_person')
        self.assertEqual(bundle, self.bundle)
        self.assertTrue(issues)

    def test_legacy_diagnostic_accepts_malformed_annotation_metadata(self):
        value = self.bundle.model_dump()
        value['website_nl_1']['claims'] = 'bad claim array'
        value['facebook_en']['claims'] = [None, {'excerpt': 12, 'source_references': 5, 'qualification': False}]
        value['facebook_nl']['review_notes'] = 9
        record = {'pending_response': {'candidate': value}, 'validation_report': []}
        diagnostic = private_diagnostic_data(record, self.profile)
        self.assertEqual(diagnostic['candidate']['website_nl_1']['text'], self.bundle.website_nl_1.text)
        self.assertTrue(diagnostic['candidate']['website_nl_1']['review_notes'])


if __name__ == '__main__':
    unittest.main()
