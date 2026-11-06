"""Evidence contract regressions: synthetic profiles/responses, never live API calls."""
import json
from io import StringIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pydantic import ValidationError

from animal_profile import Evidence, AgeAssessment
from content_models import DRAFT_KEYS, DraftClaim
import content_service as writing
from test_ai_service import record
from test_content_service import profile_fixture, bundle_fixture


def add_claim(bundle, excerpt, refs, qualification='reported', keys=DRAFT_KEYS):
    for key in keys:
        draft = getattr(bundle, key)
        draft.text += '\n' + excerpt
        draft.claims.append(DraftClaim(excerpt=excerpt, source_references=refs, qualification=qualification))


class DraftEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.profile, self.bundle = profile_fixture(), bundle_fixture()

    def test_wrong_ids_report_every_affected_draft_and_private_passage(self):
        for key in ('website_nl_2', 'facebook_en'):
            getattr(self.bundle, key).claims[0].source_references = ['invented-id']
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        report = caught.exception.report
        self.assertEqual({item['draft'] for item in report}, {'website_nl_2', 'facebook_en'})
        self.assertEqual({item['language'] for item in report}, {'Dutch', 'English'})
        self.assertEqual(report[0]['excerpt'], 'Azra')
        self.assertEqual(report[0]['source_references'], ['invented-id'])
        self.assertEqual(report[0]['code'], 'UNKNOWN_EVIDENCE_ID')
        self.assertEqual(report[0]['origin'], 'generated_text')
        self.assertTrue(report[0]['action'])
        self.assertNotIn('invented-id', json.dumps(caught.exception.details))

    def test_other_animal_records_are_excluded_from_writer(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='mother', quote='Mother weighs 18 kg.', subject='other_animal')))
        payload = json.loads(writing.build_content_payload(self.profile, 'third_person')[1]['content'])
        self.assertNotIn('mother', {item['id'] for item in payload['animal_profile']['evidence']})
        self.assertNotIn('mother', payload['reference_catalog'])

    def test_unknown_cannot_support_affirmative_claim_even_with_qualified_label(self):
        add_claim(self.bundle, 'Azra is vaccinated.', ['name', 'unknown:0'], 'qualified')
        with self.assertRaises(writing.ContentValidationError):
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_expected_weight_cannot_be_presented_as_measured(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='weight', quote='Expected adult weight is 18 kg.', temporal_status='current_as_reported')))
        add_claim(self.bundle, 'Azra weighs 18 kg.', ['weight'])
        with self.assertRaises(writing.ContentValidationError):
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_other_animal_age_basis_requires_profile_correction(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='mother', quote='Mother is two years old.', subject='other_animal')))
        self.profile.age_assessment = AgeAssessment(estimate='two years', reference_date=None, photo_reference=None,
            basis_evidence_ids=['mother'], status='reported_age', limitations='Reported age.')
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.validated_profile(self.profile)
        self.assertEqual(caught.exception.details['origin'], 'animal_profile')

    def test_correct_evidence_preserves_six_website_and_two_facebook_drafts(self):
        original = self.bundle.model_dump()
        result, _ = writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.assertEqual(result.model_dump(), original)
        self.assertEqual(sum(key.startswith('website_') for key in DRAFT_KEYS), 6)
        self.assertEqual(sum(key.startswith('facebook_') for key in DRAFT_KEYS), 2)

    def test_per_profile_schema_restricts_refs_and_parses_all_eight(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='mother', quote='Mother is shy.', subject='other_animal')))
        schema = writing.grounded_draft_schema(writing.reference_catalog(self.profile))
        parsed = schema.model_validate(self.bundle.model_dump())
        self.assertEqual(parsed.model_dump(), self.bundle.model_dump())
        data = self.bundle.model_dump()
        for ref in ['made-up', 'mother', 'HISTORY_FACT', 'UNKNOWN', 'unknown:99']:
            data['facebook_en']['claims'][0]['source_references'] = [ref]
            with self.subTest(ref=ref), self.assertRaises(ValidationError):
                schema.model_validate(data)

    def test_existing_ids_are_used_exactly_without_renumbering_or_reassignment(self):
        ref = 'Azra:H-002'
        self.profile.evidence[1].id = ref
        self.profile.rescue_background = [ref]
        for key in DRAFT_KEYS:
            getattr(self.bundle, key).claims[1].source_references = [ref]
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.bundle.website_en_2.claims[1].source_references = ['azra:h-002']
        with self.assertRaises(writing.ContentValidationError):
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_evidence_id_looking_like_unknown_is_valid_when_no_alias_collision(self):
        self.profile.evidence[1].id = 'unknown:123'
        self.profile.rescue_background = ['unknown:123']
        for key in DRAFT_KEYS:
            getattr(self.bundle, key).claims[1].source_references = ['unknown:123']
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_age_basis_id_numbers_are_not_age_measurements(self):
        self.profile.evidence[1].id = 'history:1'
        self.profile.rescue_background = ['history:1']
        for key in DRAFT_KEYS:
            getattr(self.bundle, key).claims[1].source_references = ['history:1']
        self.profile.age_assessment = AgeAssessment(estimate='Possibly a young adult', reference_date=None,
            photo_reference=None, basis_evidence_ids=['history:1'], status='reported_age', limitations='Uncertain report.')
        add_claim(self.bundle, 'Possibly 1 year old.', ['age_assessment'], 'qualified')
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.assertEqual(caught.exception.details['code'], 'UNSUPPORTED_NUMBER')

    def test_explicit_unknown_stays_unknown(self):
        add_claim(self.bundle, 'Vaccination status remains unknown.', ['unknown:0'], 'unknown')
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_unknown_about_one_topic_cannot_support_different_unknown(self):
        add_claim(self.bundle, 'Compatibility with cats is unknown.', ['unknown:0'], 'unknown')
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.assertEqual(caught.exception.details['code'], 'UNRELATED_EVIDENCE')

    def test_valid_id_cannot_support_unrelated_medical_claim(self):
        add_claim(self.bundle, 'Azra is vaccinated.', ['name'])
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.assertEqual(caught.exception.details['code'], 'UNRELATED_EVIDENCE')

    def test_uncertain_history_must_be_qualified_in_actual_wording(self):
        self.profile.evidence[1].claim = 'Azra may have been found in a shelter.'
        for key in DRAFT_KEYS:
            getattr(self.bundle, key).claims[1].qualification = 'qualified'
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.assertEqual(caught.exception.details['code'], 'UNCERTAINTY_LOST')
        for key in DRAFT_KEYS:
            draft = getattr(self.bundle, key)
            claim = draft.claims[1]
            replacement = 'Azra may have been found in a shelter.'
            draft.text = draft.text.replace(claim.excerpt, replacement)
            claim.excerpt = replacement
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_valid_historical_medical_and_current_social_information_remain(self):
        result, _ = writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        for key in DRAFT_KEYS:
            claims = getattr(result, key).claims
            self.assertTrue(any('old' in claim.source_references and claim.qualification == 'historical' for claim in claims))
            self.assertTrue(any('social' in claim.source_references for claim in claims))

    def test_untested_social_compatibility_cannot_become_positive(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='cats', quote='Compatibility with cats is not tested.', temporal_status='current_as_reported')))
        add_claim(self.bundle, 'Azra is good with cats.', ['cats'])
        with self.assertRaises(writing.ContentValidationError):
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_negative_social_claim_cannot_lose_its_negation(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='cats', quote='Azra is not good with cats.', temporal_status='current_as_reported')))
        add_claim(self.bundle, 'Azra is good with cats.', ['cats'])
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.assertEqual(caught.exception.details['code'], 'NEGATION_LOST')

    def test_qualified_expected_adult_weight_is_valid(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='weight', quote='Expected adult weight is 18 kg.', temporal_status='current_as_reported')))
        add_claim(self.bundle, 'Expected adult weight: approximately 18 kg.', ['weight'], 'qualified')
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_current_weight_and_expected_adult_weight_remain_distinct(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='measured', quote='Current reported weight is 8 kg.', temporal_status='current_as_reported')))
        self.profile.evidence.append(Evidence.model_validate(record(id='expected', quote='Expected adult weight is 18 kg.', temporal_status='current_as_reported')))
        add_claim(self.bundle, 'Current reported weight: 8 kg.', ['measured'])
        add_claim(self.bundle, 'Expected adult weight: 18 kg.', ['expected'], 'qualified')
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_dutch_expected_weight_remains_prediction(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='weight', quote='Verwacht volwassen gewicht: 18 kg.', temporal_status='current_as_reported')))
        add_claim(self.bundle, 'Verwacht volwassen gewicht: ongeveer 18 kg.', ['weight'], 'qualified')
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_other_animal_and_unclear_subject_are_separately_identified(self):
        for subject in ('other_animal', 'unclear'):
            profile = self.profile.model_copy(deep=True)
            profile.evidence.append(Evidence.model_validate(record(id='other', quote='Mother is shy.', subject=subject)))
            bundle = self.bundle.model_copy(deep=True)
            bundle.facebook_en.claims[0].source_references = ['other']
            with self.assertRaises(writing.ContentValidationError) as caught:
                writing.recheck_drafts(bundle, profile, 'third_person')
            self.assertEqual(caught.exception.report[0]['code'], 'OTHER_ANIMAL_EVIDENCE')

    def test_other_animal_in_factual_profile_section_is_profile_error(self):
        self.profile.evidence[4].subject = 'other_animal'
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.validated_profile(self.profile)
        self.assertEqual(caught.exception.details['code'], 'PROFILE_OTHER_ANIMAL')
        self.assertEqual(caught.exception.report[0]['origin'], 'animal_profile')

    def test_invalid_profile_is_rejected_before_openai_client_is_created(self):
        self.profile.rescue_background = ['missing']
        with patch.object(writing, 'OpenAI') as factory:
            with self.assertRaises(writing.ContentValidationError):
                writing.generate_drafts(self.profile, writing.ContentSettings())
        factory.assert_not_called()

    def test_reserved_alias_collision_requires_profile_correction(self):
        self.profile.evidence[1].id = 'unknown:0'
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.validated_profile(self.profile)
        self.assertEqual(caught.exception.details['code'], 'PROFILE_ID_NAMESPACE')

    def test_failed_generation_makes_one_call_preserves_profile_and_never_logs_passage(self):
        snapshot = self.profile.model_dump()
        self.bundle.website_en_2.claims[0].source_references = ['PRIVATE_INVALID_REFERENCE']
        stdout, stderr = StringIO(), StringIO()
        with (patch.object(writing, 'require_credentials', return_value='synthetic-key'),
              patch.object(writing, 'OpenAI') as factory, patch('sys.stdout', stdout), patch('sys.stderr', stderr)):
            client = factory.return_value.__enter__.return_value
            client.responses.parse.return_value = SimpleNamespace(status='completed', output_parsed=self.bundle, usage=None)
            bundle, issues, _ = writing.generate_drafts(self.profile, writing.ContentSettings())
            client.responses.parse.assert_called_once()
            self.assertEqual(factory.call_args.kwargs['max_retries'], 0)
        self.assertEqual(bundle.website_en_2.text, self.bundle.website_en_2.text)
        self.assertTrue(issues)
        self.assertNotIn('PRIVATE', stdout.getvalue() + stderr.getvalue())
        self.assertEqual(self.profile.model_dump(), snapshot)

    def test_credentials_are_redacted_even_inside_invalid_model_references(self):
        secret = 'sk-syntheticprivatecredential123456'
        self.bundle.website_en_1.claims[0].source_references = [secret]
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.assertNotIn(secret, json.dumps(caught.exception.report) + json.dumps(caught.exception.details) + str(caught.exception))

    def test_empty_evidence_requires_profile_correction(self):
        self.profile.evidence[1].supporting_text_or_description = ''
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.validated_profile(self.profile)
        self.assertEqual(caught.exception.details['code'], 'PROFILE_EMPTY_EVIDENCE')


class DraftEvidenceUiTests(unittest.TestCase):
    def setUp(self):
        import analysis_ui
        import content_ui
        from content_state import writing_scope
        from streamlit.testing.v1 import AppTest
        from test_content_ui import APP

        self.ui = content_ui
        self.profile, self.bundle = profile_fixture(), bundle_fixture()
        bad = self.bundle.model_copy(deep=True)
        bad.website_en_2.claims[0].source_references = ['private-invalid-id']
        try:
            writing.recheck_drafts(bad, self.profile, 'third_person')
        except writing.ContentValidationError as error:
            self.failure = error
        self.failure.candidate = bad.model_dump(mode='json')
        self.failure.usage = {'total_tokens': 100}
        patches = [patch.object(analysis_ui, 'render_analysis', return_value=None),
            patch.object(content_ui, 'load_content_settings', return_value=writing.ContentSettings(model='synthetic')),
            patch.object(content_ui, 'generate_drafts', side_effect=self.failure), patch.object(content_ui, 'copy_button')]
        _, _, self.generate, self.copy = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        for item in (patch.object(content_ui, 'load_record', return_value=None),
                     patch.object(content_ui, 'save_record')):
            item.start()
            self.addCleanup(item.stop)
        self.app = AppTest.from_file(APP, default_timeout=10).run()
        self.app.session_state['ai_result'] = {'profile': self.profile, 'fingerprint': 'analysis-v1'}
        self.app.session_state['ai_outdated'] = False
        self.app.run()
        self.scope = writing_scope({}, self.profile)

    def record(self):
        return self.app.session_state['writing_workspaces'][self.scope]

    def test_private_report_and_local_recheck_retain_bad_drafts_with_findings_without_api_retry(self):
        self.app.button(key='generate_drafts').click().run()
        for _ in range(2):
            self.app.run()
        self.assertIsNone(self.record()['bundle'])
        self.assertTrue(any('Rejected claims — private review' == item.label for item in self.app.expander))
        self.assertTrue(any('private-invalid-id' in item.value for item in self.app.text))
        self.app.button(key=f'writing_recheck_{self.scope}').click().run()
        self.generate.assert_called_once()
        self.assertIsNotNone(self.record()['bundle'])
        self.assertTrue(any(item['code'] == 'UNKNOWN_EVIDENCE_ID'
            for item in self.record()['assessment']['drafts']['website_en_2']['findings']))
        self.assertEqual(self.app.session_state['ai_result']['profile'], self.profile)
        self.assertFalse(self.app.exception)

    def test_failed_regeneration_preserves_previous_drafts_and_both_human_editors(self):
        self.generate.side_effect = None
        self.generate.return_value = (self.bundle, writing.validate_drafts(self.bundle, self.profile, 'third_person'), {})
        self.app.button(key='generate_drafts').click().run()
        self.app.text_area(key=self.ui.editor_key(self.scope, 'final', 'nl')).input('Keep my website edit.').run()
        self.app.text_area(key=self.ui.editor_key(self.scope, 'facebook', 'nl')).input('Keep my Facebook edit.').run()
        confirmation = f'writing_regenerate_confirm_{self.scope}_{self.record()["generated_at"]}'
        self.app.checkbox(key=confirmation).check().run()
        self.generate.side_effect = self.failure
        self.app.button(key='generate_drafts').click().run()
        self.assertEqual(self.record()['bundle'], self.bundle)
        self.assertEqual(self.record()['final']['nl'], 'Keep my website edit.')
        self.assertEqual(self.record()['facebook']['nl'], 'Keep my Facebook edit.')
        self.assertEqual(self.generate.call_count, 2)
        from content_state import export_workspace
        exported = export_workspace(self.record()).decode()
        self.assertNotIn('private-invalid-id', exported)
        self.assertNotIn('validation_report', exported)
        self.assertFalse(self.app.exception)

    def test_legacy_generic_failure_gets_detailed_local_report_without_regeneration(self):
        legacy = writing.ContentValidationError('DRAFT_RULE', 'Drafts cite unknown or other-animal evidence.')
        legacy.candidate = self.failure.candidate
        self.generate.side_effect = legacy
        self.app.button(key='generate_drafts').click().run()
        self.assertFalse(self.record()['validation_report'])
        self.app.button(key=f'writing_recheck_{self.scope}').click().run()
        self.assertTrue(any(item['code'] == 'UNKNOWN_EVIDENCE_ID'
            for item in self.record()['assessment']['drafts']['website_en_2']['findings']))
        self.generate.assert_called_once()
        self.assertFalse(self.app.exception)

    def test_profile_correction_flag_keeps_existing_manual_scope_and_edits(self):
        self.app.text_area(key=self.ui.editor_key(self.scope, 'final', 'nl')).input('Keep this manual edit.').run()
        invalid = self.profile.model_copy(deep=True)
        invalid.evidence[4].subject = 'other_animal'
        self.app.session_state['ai_result'] = {'profile': invalid, 'fingerprint': 'analysis-v1'}
        self.app.run()
        self.assertEqual(self.app.text_area(key=self.ui.editor_key(self.scope, 'final', 'nl')).value, 'Keep this manual edit.')
        self.assertEqual(self.record()['validation_details']['origin'], 'animal_profile')
        self.app.button(key='generate_drafts').click().run()
        self.generate.assert_not_called()
        self.assertFalse(self.app.exception)


if __name__ == '__main__':
    unittest.main()
