"""Shared fact/unknown contract regressions; no live providers."""
import json
import unittest
from unittest.mock import patch

from animal_profile import Evidence, ReviewWarning
from content_models import DRAFT_KEYS
import content_service as writing
from test_ai_service import record
from test_content_service import profile_fixture, bundle_fixture
from test_draft_evidence import add_claim


def azra_unknown_profile():
    profile = profile_fixture()
    profile.evidence.append(Evidence.model_validate(record(id='E12', quote='Around 5 months old', temporal_status='historical')))
    profile.evidence[-1].claim = 'Azra was reported as around 5 months old at an unspecified earlier time.'
    profile.unknown_information = ['Current age and date of birth are unknown; the reported approximate age is undated.',
        'Compatibility with children has not been reported.', 'Vaccination status is unknown.']
    profile.review_warnings = [ReviewWarning(category='age_uncertainty',
        message='The undated reported age is approximate and cannot establish current age or birth date. No usable photo capture dates were supplied.', evidence_ids=['E12'])]
    return profile


class SharedWritingContextTests(unittest.TestCase):
    def setUp(self):
        self.profile, self.bundle = azra_unknown_profile(), bundle_fixture()

    def reject(self, code=None):
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        if code:
            self.assertIn(code, [entry['code'] for entry in caught.exception.report])
        return caught.exception

    def test_recovered_claim_eight_with_historical_context_is_valid_without_reassignment(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='playful', quote='Azra is reported as playful.', temporal_status='current_as_reported')))
        add_claim(self.bundle, 'Azra is reported as playful.', ['playful'])
        text = 'Haar huidige leeftijd en geboortedatum zijn onbekend'
        add_claim(self.bundle, text, ['E12', 'warning:0'], 'unknown')
        self.assertEqual(self.bundle.website_nl_1.claims[7].excerpt, text)
        before = self.bundle.model_dump()
        accepted, _ = writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.assertEqual(accepted.model_dump(), before)

    def test_truthful_unknown_needs_no_fabricated_factual_reference(self):
        add_claim(self.bundle, 'Compatibility with children has not been established.', [], 'unknown')
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_unknown_paraphrases_in_both_languages(self):
        for text in ['Compatibility with children remains unknown.', 'Whether Azra can live with children is unknown.',
                     'No information about compatibility with children has been provided.',
                     'Er is niets bekend over haar omgang met kinderen.',
                     'Of Azra met kinderen kan samenleven is nog niet duidelijk.',
                     'Haar omgang met kinderen is niet vastgesteld.', 'Kinderen: onbekend.']:
            with self.subTest(text=text):
                self.bundle = bundle_fixture()
                add_claim(self.bundle, text, ['unknown:1'], 'unknown')
                writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_unknown_source_paraphrases_match_the_same_catalog(self):
        for source in ['No information about compatibility with children has been provided.',
                       'Omgang met kinderen is niet onderzocht.', 'Compatibility with children has not been tested.']:
            self.profile.unknown_information[1] = source
            self.bundle = bundle_fixture()
            add_claim(self.bundle, 'Compatibility with children is unknown.', ['unknown:1'], 'unknown')
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_unknown_cannot_support_positive_or_negative_suitability(self):
        for text in ['Azra is suitable for children.', 'Azra is unsuitable for children.',
                     'Azra is good with children.', 'Azra is not good with children.',
                     'Azra is geschikt voor kinderen.', 'Azra is ongeschikt voor kinderen.']:
            with self.subTest(text=text):
                self.bundle = bundle_fixture()
                add_claim(self.bundle, text, ['unknown:1'], 'unknown')
                self.reject('UNKNOWN_AS_FACT')

    def test_unknown_cannot_conceal_an_assertion_in_the_same_excerpt(self):
        for text in ['Azra is good with children but vaccination status is unknown.',
                     'Azra is good with children and her vaccination status is unknown.',
                     'Azra is geschikt voor kinderen, maar haar vaccinatiestatus is onbekend.',
                     'Azra is unsuitable for children although her current age is unknown.']:
            self.bundle = bundle_fixture()
            add_claim(self.bundle, text, ['unknown:0', 'unknown:1', 'unknown:2'], 'unknown')
            self.reject('UNKNOWN_AS_FACT')

    def test_not_tested_evidence_can_support_broader_unknown_without_negation_loss(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='cats', quote='Compatibility with cats has not been tested.', temporal_status='current_as_reported')))
        add_claim(self.bundle, 'Compatibility with cats remains unknown.', ['cats'], 'unknown')
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_unknown_does_not_establish_that_a_test_never_happened(self):
        add_claim(self.bundle, 'Azra has never been tested with children.', ['unknown:1'], 'unknown')
        self.reject('UNSUPPORTED_UNKNOWN')

    def test_unknown_breed_cannot_cite_a_vaccination_unknown(self):
        self.profile.unknown_information.append('Breed is unknown.')
        add_claim(self.bundle, 'Her breed is unknown.', ['unknown:2'], 'unknown')
        self.reject('UNRELATED_EVIDENCE')

    def test_unknown_factual_context_must_be_relevant(self):
        add_claim(self.bundle, 'Her current age is unknown.', ['home'], 'unknown')
        self.reject('UNRELATED_EVIDENCE')

    def test_reported_unverified_compatibility_is_not_missing_compatibility(self):
        self.profile.unknown_information.append('Reported sociability with dogs is not independently verified.')
        add_claim(self.bundle, 'Compatibility with dogs is unknown.', [], 'unknown')
        self.reject('UNSUPPORTED_UNKNOWN')

    def test_explicit_known_negative_cannot_be_replaced_by_unknown(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='children', quote='Azra cannot live with children.', temporal_status='current_as_reported')))
        self.profile.personality_and_behavior.append('children')
        add_claim(self.bundle, 'Compatibility with children is unknown.', ['unknown:1'], 'unknown')
        self.reject('UNKNOWN_PROFILE_CONFLICT')

    def test_historical_untested_report_cannot_be_asserted_as_current_testing_status(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='cats', quote='Compatibility with cats was not tested in the earlier report.', temporal_status='historical')))
        add_claim(self.bundle, 'Compatibility with cats has not been tested.', ['cats'], 'unknown')
        self.reject('UNKNOWN_TEMPORAL_SCOPE')

    def test_historical_missing_information_keeps_its_time_scope(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='cats', quote='Compatibility with cats was not tested in the earlier report.', temporal_status='historical')))
        add_claim(self.bundle, 'In the earlier report, compatibility with cats was unknown.', ['cats'], 'unknown')
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_fact_without_references_is_still_blocked(self):
        add_claim(self.bundle, 'Azra is vaccinated.', [], 'reported')
        self.reject('MISSING_DRAFT_REFERENCES')

    def test_unknown_information_can_be_omitted_from_all_eight_drafts(self):
        before = self.bundle.model_dump()
        accepted, _ = writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.assertEqual(accepted.model_dump(), before)

    def test_all_independent_claim_errors_across_eight_are_reported_at_once(self):
        add_claim(self.bundle, 'Azra is suitable for children.', ['unknown:1'], 'unknown')
        add_claim(self.bundle, 'Azra is vaccinated.', ['name'])
        failure = self.reject()
        self.assertEqual({entry['draft'] for entry in failure.report}, set(DRAFT_KEYS))
        self.assertEqual(len(failure.report), 16)
        self.assertTrue(all(entry['classification'] == 'genuine_unsupported_claim' for entry in failure.report))

    def test_private_unknown_report_contains_matching_source_and_qualification(self):
        add_claim(self.bundle, 'Her breed is unknown.', ['unknown:2'], 'unknown', ['website_nl_1'])
        failure = self.reject()
        entry = failure.report[0]
        self.assertEqual(entry['claim_qualification'], 'unknown')
        self.assertTrue(entry['relevant_unknowns'])
        self.assertEqual(entry['relevant_unknowns'][0]['reference'], 'unknown:2')
        self.assertNotIn('unknown:2', json.dumps(failure.details))

    def test_prepared_catalog_has_the_writer_and_validator_contract(self):
        payload = json.loads(writing.build_content_payload(self.profile, 'third_person')[1]['content'])
        catalog = payload['writing_catalog']
        age = next(item for item in catalog['facts'] if item['reference'] == 'E12')
        self.assertEqual(age['subject'], 'selected_animal')
        self.assertEqual(age['temporal_status'], 'historical')
        self.assertEqual(age['verification_status'], 'reported_not_independently_verified')
        self.assertNotEqual(age['canonical_claim'], age['supporting_quote'])
        self.assertTrue(age['allowed_qualifications'])
        self.assertTrue(catalog['unknown_information'])
        self.assertNotIn('warning:0', {item['reference'] for item in catalog['facts']})

    def test_local_recheck_is_nonmutating_and_never_creates_a_client(self):
        add_claim(self.bundle, 'Haar huidige leeftijd en geboortedatum zijn onbekend', ['E12', 'warning:0'], 'unknown')
        profile_before, bundle_before = self.profile.model_dump(), self.bundle.model_dump()
        with patch.object(writing, 'OpenAI') as provider:
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        provider.assert_not_called()
        self.assertEqual(self.profile.model_dump(), profile_before)
        self.assertEqual(self.bundle.model_dump(), bundle_before)

    def test_required_missing_compatibility_can_use_unknown_stance_without_factual_id(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='cats', quote='Compatibility with cats is not tested.', temporal_status='current_as_reported')))
        self.profile.personality_and_behavior.append('cats')
        self.assertIn('cats', writing.mandatory_evidence(self.profile))
        add_claim(self.bundle, 'Compatibility with cats is unknown.', [], 'unknown')
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_reported_unverified_statement_can_be_kept_as_verification_uncertainty(self):
        self.profile.unknown_information.append('Reported sociability with dogs is not independently verified.')
        add_claim(self.bundle, 'Reported sociability with dogs is not independently verified.', [], 'unknown')
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_missing_states_are_not_in_the_affirmative_fact_catalog(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='cats', quote='Compatibility with cats has not been tested.', temporal_status='current_as_reported')))
        payload = json.loads(writing.build_content_payload(self.profile, 'third_person')[1]['content'])
        self.assertNotIn('cats', {entry['reference'] for entry in payload['writing_catalog']['facts']})
        self.assertFalse(payload['reference_catalog']['cats']['allows_factual_assertions'])

    def test_mocked_failure_retains_complete_batch_and_makes_one_request(self):
        from types import SimpleNamespace
        add_claim(self.bundle, 'Azra is suitable for children.', ['unknown:1'], 'unknown')
        before = self.profile.model_dump()
        with patch.object(writing, 'require_credentials', return_value='synthetic'), patch.object(writing, 'OpenAI') as provider:
            client = provider.return_value.__enter__.return_value
            client.responses.parse.return_value = SimpleNamespace(status='completed', output_parsed=self.bundle, usage=None)
            bundle, issues, _ = writing.generate_drafts(self.profile, writing.ContentSettings())
            client.responses.parse.assert_called_once()
            self.assertEqual(provider.call_args.kwargs['max_retries'], 0)
        self.assertEqual(bundle.model_dump(), self.bundle.model_dump())
        self.assertTrue(any(issue.draft == 'website_nl_1' for issue in issues))
        self.assertEqual(self.profile.model_dump(), before)

    def test_whether_scope_cannot_hide_a_following_affirmative_claim(self):
        for text in ['Whether Azra is suitable for children is unknown, and she is vaccinated.',
                     'Of Azra geschikt is voor kinderen is onbekend en zij is gevaccineerd.',
                     'Whether Azra is suitable for children is unknown; she is vaccinated.']:
            self.bundle = bundle_fixture()
            add_claim(self.bundle, text, ['unknown:1', 'unknown:2'], 'unknown')
            self.reject('UNKNOWN_AS_FACT')

    def test_unknown_cannot_hide_an_unrecognized_factual_predicate(self):
        for text in ['Compatibility with children is unknown and she lives in Paris.',
                     'Omgang met kinderen is onbekend en Azra woont in Parijs.']:
            self.bundle = bundle_fixture()
            add_claim(self.bundle, text, ['unknown:1'], 'unknown')
            self.reject('UNKNOWN_AS_FACT')

    def test_mixed_source_missing_status_does_not_support_an_assertion(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='mixed',
            quote='Vaccination status is unknown; Azra is playful.', temporal_status='current_as_reported')))
        add_claim(self.bundle, 'Azra is vaccinated.', ['mixed'])
        self.reject('UNKNOWN_AS_FACT')

    def test_mixed_source_is_separated_from_publishable_fact_catalog(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='mixed',
            quote='Vaccination status is unknown; Azra is playful.', temporal_status='current_as_reported')))
        payload = json.loads(writing.build_content_payload(self.profile, 'third_person')[1]['content'])
        self.assertFalse(payload['reference_catalog']['mixed']['allows_factual_assertions'])

    def test_other_animal_warning_cannot_supply_selected_animal_unknowns(self):
        self.profile.unknown_information = []
        self.profile.evidence.append(Evidence.model_validate(record(id='mother', quote='Mother has no vaccination record.', subject='other_animal')))
        self.profile.review_warnings = [ReviewWarning(category='other_animal',
            message="Mother's vaccination status is unknown.", evidence_ids=['mother'])]
        add_claim(self.bundle, "Azra's vaccination status is unknown.", [], 'unknown')
        self.reject('UNSUPPORTED_UNKNOWN')

    def test_undated_untested_evidence_does_not_establish_current_testing_status(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='cats', quote='Compatibility with cats has not been tested.', temporal_status='unclear')))
        add_claim(self.bundle, 'Compatibility with cats has never been tested.', ['cats'], 'unknown')
        self.reject('UNKNOWN_TEMPORAL_SCOPE')

    def test_historical_unknown_cannot_cover_a_current_critical_gap(self):
        self.profile.evidence.extend([
            Evidence.model_validate(record(id='cats_current', quote='Compatibility with cats is not tested.', temporal_status='current_as_reported')),
            Evidence.model_validate(record(id='cats_old', quote='Compatibility with cats was not tested in the earlier report.', temporal_status='historical'))])
        self.profile.personality_and_behavior.append('cats_current')
        add_claim(self.bundle, 'In the earlier report, compatibility with cats was unknown.', ['cats_old'], 'unknown')
        self.reject('ESSENTIAL_EVIDENCE_OMITTED')

    def test_unknown_registry_preserves_attribution_and_original_status(self):
        payload = json.loads(writing.build_content_payload(self.profile, 'third_person')[1]['content'])
        for entry in payload['writing_catalog']['unknown_information']:
            self.assertEqual(entry['subject'], 'selected_animal')
            self.assertIn('verification_status', entry)
            self.assertIn('temporal_status', entry)
            self.assertIn('source_type', entry)

    def test_known_dutch_sterilization_cannot_be_hidden_by_blanket_unknown(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='sterile', quote='Azra is gecastreerd.', temporal_status='current_as_reported')))
        self.profile.unknown_information.append('Sterilization status is unknown.')
        add_claim(self.bundle, 'Sterilization status is unknown.', ['unknown:3'], 'unknown')
        self.reject('UNKNOWN_PROFILE_CONFLICT')

    def test_writer_does_not_receive_other_animal_warning_passages_as_factual_context(self):
        self.profile.evidence.append(Evidence.model_validate(record(id='mother', quote='Mother has no vaccination record.', subject='other_animal')))
        self.profile.review_warnings.append(ReviewWarning(category='other_animal',
            message="PRIVATE MOTHER ONLY: Mother's vaccination status is unknown.", evidence_ids=['mother']))
        payload = writing.build_content_payload(self.profile, 'third_person')
        self.assertNotIn('PRIVATE MOTHER ONLY', json.dumps(payload))


if __name__ == '__main__':
    unittest.main()
