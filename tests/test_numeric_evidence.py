"""Numeric writing regressions. Synthetic profiles and mocked providers only."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from animal_profile import Evidence
from content_models import DRAFT_KEYS, DraftClaim
import content_service as writing
from test_ai_service import record
from test_content_service import profile_fixture, bundle_fixture
from test_draft_evidence import add_claim


class NumericEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.profile, self.bundle = profile_fixture(), bundle_fixture()

    def evidence(self, ref, text, **values):
        self.profile.evidence.append(Evidence.model_validate(record(id=ref, quote=text, **values)))

    def rejected(self, code=None):
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        if code:
            self.assertIn(code, [item['code'] for item in caught.exception.report])
        return caught.exception

    def test_missing_numeric_annotation_reports_exact_passage_and_relevant_evidence(self):
        self.evidence('adult', 'Expected adult weight: approximately 16/17 kg.')
        passage = 'Expected adult weight: approximately 16–17 kg.'
        self.bundle.website_en_2.text += '\n' + passage
        failure = self.rejected('UNANNOTATED_NUMBER')
        item = failure.report[0]
        self.assertEqual(item['draft'], 'website_en_2')
        self.assertEqual(item['language'], 'English')
        self.assertEqual(item['excerpt'], passage)
        self.assertEqual(item['annotation_status'], 'missing')
        self.assertEqual(item['source_references'], [])
        self.assertEqual(item['relevant_evidence'][0]['id'], 'adult')
        self.assertIn('16/17', item['relevant_evidence'][0]['claim'])
        self.assertTrue(item['action'])
        self.assertNotIn(passage, str(failure) + json.dumps(failure.details))

    def test_missing_annotations_are_collected_across_all_eight_drafts(self):
        for key in DRAFT_KEYS:
            getattr(self.bundle, key).text += '\nAge: 5 months.'
        failure = self.rejected('UNANNOTATED_NUMBER')
        self.assertEqual({item['draft'] for item in failure.report}, set(DRAFT_KEYS))

    def test_same_digits_elsewhere_do_not_cover_an_unannotated_occurrence(self):
        self.bundle.website_en_1.text += '\nAzra weighs 2025 kg.'
        self.rejected('UNANNOTATED_NUMBER')

    def test_repeated_sentence_needs_separate_annotation(self):
        self.bundle.website_en_1.text += '\nA leg injury was reported in 2025.'
        self.rejected('UNANNOTATED_NUMBER')

    def test_number_only_annotation_does_not_establish_animal_statement(self):
        self.bundle.website_en_1.text += '\nAzra weighs 2025 kg.'
        self.bundle.website_en_1.claims.append(DraftClaim(excerpt='2025', source_references=['old'], qualification='historical'))
        self.rejected()

    def test_draft_section_and_list_numbering_are_not_animal_facts(self):
        for key in DRAFT_KEYS:
            draft = getattr(self.bundle, key)
            draft.text = '### Draft 1\nSection 2\n3. ' + draft.text
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_numbered_headings_and_bullets_do_not_exempt_animal_facts(self):
        for passage in ['### Age: 5 months', '1. Weight: 16 kg', 'Draft 2 — Age: five months', '▪️ Leeftijd: vijf maanden']:
            with self.subTest(passage=passage):
                self.bundle = bundle_fixture()
                self.bundle.facebook_en.text += '\n' + passage
                self.rejected('UNANNOTATED_NUMBER')

    def test_estimated_adult_range_from_azras_history_is_supported_in_both_languages(self):
        self.evidence('adult', 'Will be medium size when will fully grow up (not more then 16/17 kg)', temporal_status='unclear')
        for key in DRAFT_KEYS:
            passage = ('Naar verwachting als volwassen hond niet meer dan 16–17 kg.' if '_nl' in key else
                       'Expected adult weight: no more than 16–17 kg.')
            add_claim(self.bundle, passage, ['adult'], 'qualified', [key])
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_expected_weight_cannot_become_current_even_with_qualified_metadata(self):
        self.evidence('adult', 'Will be medium size when will fully grow up (not more then 16/17 kg)', temporal_status='unclear')
        add_claim(self.bundle, 'Current weight: approximately 16–17 kg.', ['adult'], 'qualified')
        self.rejected('EXPECTED_WEIGHT_AS_MEASURED')

    def test_adult_range_cannot_become_single_exact_value(self):
        self.evidence('adult', 'Expected adult weight: approximately 16/17 kg.', temporal_status='unclear')
        add_claim(self.bundle, 'Expected adult weight: approximately 17 kg.', ['adult'], 'qualified')
        self.rejected('UNSUPPORTED_NUMBER')

    def test_historical_approximate_age_preserved_with_number_words(self):
        self.evidence('age', 'Around 5 months old', temporal_status='historical')
        for key in DRAFT_KEYS:
            passage = ('In de eerdere beschrijving was Azra ongeveer vijf maanden oud.' if '_nl' in key else
                       'In the earlier report, Azra was around five months old.')
            add_claim(self.bundle, passage, ['age'], 'historical', [key])
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_undated_reported_age_does_not_establish_current_age(self):
        self.evidence('age', 'Around 5 months old', temporal_status='unclear')
        add_claim(self.bundle, 'Azra is around five months old.', ['age'], 'qualified')
        self.rejected('HISTORICAL_AGE_AS_CURRENT')

    def test_metadata_alone_cannot_preserve_uncertainty(self):
        self.evidence('age', 'Around 5 months old', temporal_status='historical')
        add_claim(self.bundle, 'Azra was five months old in the earlier report.', ['age'], 'historical')
        self.rejected('UNCERTAINTY_LOST')

    def test_exact_birthday_cannot_be_inferred_from_reported_age(self):
        self.evidence('age', 'Around 5 months old', temporal_status='unclear')
        add_claim(self.bundle, 'Born on 2026-05-05.', ['age'], 'qualified')
        self.rejected()

    def test_date_components_cannot_be_reordered_or_repurposed_as_birthday(self):
        self.evidence('date', 'Rescue date: 2026-05-04.', temporal_status='historical')
        for passage in ['Rescue date: 2026-04-05.', 'Birth date: 2026-05-04.']:
            with self.subTest(passage=passage):
                self.bundle = bundle_fixture()
                add_claim(self.bundle, passage, ['date'], 'historical')
                self.rejected()

    def test_same_number_in_unrelated_claim_or_source_excerpt_is_not_support(self):
        self.evidence('age', 'Around 5 months old', temporal_status='unclear')
        self.profile.evidence[-1].supporting_text_or_description += '; weight 16 kg'
        for passage in ['Reported height: 5 cm.', 'Reported weight: 16 kg.']:
            with self.subTest(passage=passage):
                self.bundle = bundle_fixture()
                add_claim(self.bundle, passage, ['age'], 'qualified')
                self.rejected()

    def test_dutch_decimal_weight_and_english_decimal_are_equivalent(self):
        self.evidence('measured', 'Measured weight: 8.5 kg.', temporal_status='current_as_reported')
        add_claim(self.bundle, 'Gemeten gewicht: 8,5 kg.', ['measured'])
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_spelled_out_unsupported_measurements_medical_values_and_durations_blocked(self):
        for passage in ['Weight: ninety-nine kg.', 'Gewicht: negenennegentig kilo.', 'Height: fifty cm.',
                        'Medication: five mg.', 'Temperature: 39 °C.', 'Treatment lasted five weeks.', 'Behandeling duurde vijf weken.']:
            with self.subTest(passage=passage):
                self.bundle = bundle_fixture()
                self.bundle.facebook_nl.text += '\n' + passage
                self.rejected('UNANNOTATED_NUMBER')

    def test_other_animal_and_fake_numeric_references_remain_blocked(self):
        self.evidence('mother', 'Mother weighs 16 kg.', subject='other_animal')
        for ref in ['mother', 'invented']:
            with self.subTest(ref=ref):
                self.bundle = bundle_fixture()
                add_claim(self.bundle, 'Reported weight: 16 kg.', [ref])
                self.rejected()

    def test_numeric_facebook_facts_need_their_own_annotation(self):
        self.evidence('adult', 'Expected adult weight: approximately 16/17 kg.', temporal_status='unclear')
        add_claim(self.bundle, '▪️ Expected adult weight: approximately 16–17 kg.', ['adult'], 'qualified')
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.bundle.facebook_en.claims.pop()
        self.rejected('UNANNOTATED_NUMBER')

    def test_local_validation_does_not_mutate_profile_candidate_or_infer_annotations(self):
        self.bundle.website_en_1.text += '\nAge: 5 months.'
        before_profile, before_bundle = self.profile.model_dump(), self.bundle.model_dump()
        with patch.object(writing, 'OpenAI') as factory:
            self.rejected('UNANNOTATED_NUMBER')
        factory.assert_not_called()
        self.assertEqual(self.profile.model_dump(), before_profile)
        self.assertEqual(self.bundle.model_dump(), before_bundle)

    def test_mocked_numeric_failure_retains_response_and_never_retries(self):
        self.bundle.website_en_2.text += '\nAge: 5 months.'
        with patch.object(writing, 'require_credentials', return_value='synthetic'), patch.object(writing, 'OpenAI') as factory:
            client = factory.return_value.__enter__.return_value
            client.responses.parse.return_value = SimpleNamespace(status='completed', output_parsed=self.bundle, usage=None)
            bundle, issues, _ = writing.generate_drafts(self.profile, writing.ContentSettings())
            client.responses.parse.assert_called_once()
            self.assertEqual(factory.call_args.kwargs['max_retries'], 0)
        self.assertEqual(bundle.model_dump(), self.bundle.model_dump())
        self.assertTrue(any(issue.draft == 'website_en_2' for issue in issues))

    def test_profile_cannot_misrepresent_adult_weight_as_current(self):
        self.evidence('adult', 'Current weight: 16/17 kg.', temporal_status='unclear')
        self.profile.evidence[-1].supporting_text_or_description = 'Will be medium size when will fully grow up (not more then 16/17 kg)'
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.validated_profile(self.profile)
        self.assertEqual(caught.exception.details['origin'], 'animal_profile')

    def test_profile_cannot_remove_reported_age_approximation(self):
        self.evidence('age', '5 months old', temporal_status='historical')
        self.profile.evidence[-1].supporting_text_or_description = 'Around 5 months old'
        with self.assertRaises(writing.ContentValidationError):
            writing.validated_profile(self.profile)

    def test_profile_with_original_azra_numeric_records_needs_no_migration(self):
        self.evidence('adult', 'Will be medium size when will fully grow up (not more then 16/17 kg)', temporal_status='unclear')
        self.evidence('age', 'Around 5 months old', temporal_status='unclear')
        snapshot = self.profile.model_dump()
        self.assertEqual(writing.validated_profile(self.profile).model_dump(), snapshot)

    def test_sparse_profile_accepts_a_100_to_150_word_website_without_length_warning(self):
        for key in DRAFT_KEYS:
            if key.startswith('website'):
                draft = getattr(self.bundle, key)
                # Generic invitation copy has no extra animal facts.
                draft.text += ' A loving home could begin with a thoughtful introduction.' * 8
        _, issues = writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.assertFalse([issue for issue in issues if issue.code == 'length'])

    def test_measurement_without_space_before_unit_is_still_factual(self):
        self.bundle.facebook_en.text += '\nWeight: 16kg.'
        self.rejected('UNANNOTATED_NUMBER')

    def test_supported_measurement_without_space_before_unit(self):
        self.evidence('weight', 'Measured weight: 8.5 kg.', temporal_status='current_as_reported')
        add_claim(self.bundle, 'Measured weight: 8.5kg.', ['weight'])
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_multiple_draft_labels_in_one_line_are_formatting(self):
        self.bundle.website_en_2.text += '\nDraft 1 / Draft 2 / Draft 3'
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_mixed_current_and_adult_weight_source_preserves_each_meaning(self):
        self.evidence('mixed', 'Current weight: 8 kg and expected adult weight: 18 kg.', temporal_status='current_as_reported')
        add_claim(self.bundle, 'Reported current weight: 8 kg.', ['mixed'])
        add_claim(self.bundle, 'Expected adult weight: 18 kg.', ['mixed'], 'qualified')
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_history_age_cannot_be_mislabeled_current_in_profile(self):
        self.evidence('age', 'Around 5 months old', temporal_status='current_as_reported')
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.validated_profile(self.profile)
        self.assertEqual(caught.exception.details['origin'], 'animal_profile')

    def test_age_assessment_cannot_change_reported_number_or_uncertainty(self):
        from animal_profile import AgeAssessment
        self.evidence('age', 'Around 5 months old', temporal_status='historical')
        for estimate in ['Around 6 months old', '5 months old']:
            self.profile.age_assessment = AgeAssessment(estimate=estimate, reference_date=None, photo_reference=None,
                basis_evidence_ids=['age'], status='reported_age', limitations='Historical report, not current age.')
            with self.subTest(estimate=estimate), self.assertRaises(writing.ContentValidationError):
                writing.validated_profile(self.profile)

    def test_invalid_numeric_reference_report_contains_annotation_and_cited_record(self):
        self.evidence('mother', 'Mother weighs 16 kg.', subject='other_animal')
        add_claim(self.bundle, 'Reported weight: 16 kg.', ['mother'])
        failure = self.rejected()
        self.assertEqual(failure.report[0]['annotation_status'], 'invalid')
        self.assertEqual(failure.report[0]['relevant_evidence'][0]['subject'], 'other_animal')

    def test_url_identifier_is_not_a_measurement_but_contact_policy_still_blocks_urls(self):
        from numeric_evidence import numeric_facts
        self.assertEqual(numeric_facts('https://approved.example/animals/12345'), [])
        self.bundle.website_en_2.text += '\nhttps://approved.example/animals/12345'
        self.rejected('DRAFT_CONTACT')

    def test_private_evidence_records_redact_credentials(self):
        secret = 'sk-syntheticprivatecredential123456'
        self.evidence('adult', 'Expected adult weight: approximately 16/17 kg. ' + secret)
        self.bundle.website_en_2.text += '\nExpected adult weight: approximately 16–17 kg.'
        failure = self.rejected()
        self.assertNotIn(secret, json.dumps(failure.report))

    def test_short_annotation_cannot_hide_changed_weight_meaning_in_surrounding_text(self):
        self.evidence('measured', 'Measured weight: 16 kg.', temporal_status='current_as_reported')
        self.bundle.website_en_2.text += '\nExpected adult weight: 16 kg.'
        self.bundle.website_en_2.claims.append(DraftClaim(excerpt='16 kg', source_references=['measured'], qualification='qualified'))
        self.rejected('MEASURED_WEIGHT_AS_EXPECTED')

    def test_date_in_url_identifier_is_formatting(self):
        from numeric_evidence import numeric_facts
        self.assertEqual(numeric_facts('https://approved.example/2026-05-04'), [])

    def test_written_month_date_cannot_change_month(self):
        self.evidence('birth', 'Born on 5 May 2026.', temporal_status='historical')
        add_claim(self.bundle, 'Born on 5 April 2026.', ['birth'], 'historical')
        self.rejected('UNSUPPORTED_NUMBER')

    def test_written_month_date_can_be_translated_to_dutch(self):
        self.evidence('birth', 'Born on 5 May 2026.', temporal_status='historical')
        add_claim(self.bundle, 'Geboren op 5 mei 2026.', ['birth'], 'historical')
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_word_ranges_cannot_collapse_to_one_endpoint(self):
        for source, target in [('Weight: between 16 and 17 kg.', 'Weight: 17 kg.'),
                               ('Gewicht: tussen zestien en zeventien kilo.', 'Gewicht: zeventien kilo.')]:
            with self.subTest(source=source):
                self.profile, self.bundle = profile_fixture(), bundle_fixture()
                self.evidence('range', source, temporal_status='current_as_reported')
                add_claim(self.bundle, target, ['range'])
                self.rejected('UNSUPPORTED_NUMBER')

    def test_supported_word_range_can_be_translated(self):
        self.evidence('range', 'Weight: between 16 and 17 kg.', temporal_status='current_as_reported')
        add_claim(self.bundle, 'Gewicht: tussen zestien en zeventien kilo.', ['range'])
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_strict_numeric_limits_cannot_disappear_or_become_inclusive(self):
        for source, target in [('Weight: less than 16 kg.', 'Weight: 16 kg.'),
                               ('Weight: under 16 kg.', 'Weight: no more than 16 kg.'),
                               ('Gewicht: minder dan 16 kg.', 'Gewicht: 16 kg.'),
                               ('Weight: more than 16 kg.', 'Weight: at least 16 kg.')]:
            with self.subTest(source=source):
                self.profile, self.bundle = profile_fixture(), bundle_fixture()
                self.evidence('limit', source, temporal_status='current_as_reported')
                add_claim(self.bundle, target, ['limit'])
                self.rejected('NUMERIC_BOUND_CHANGED')

    def test_dutch_indefinite_article_is_not_an_additional_age_value(self):
        self.evidence('age', 'Around 5 months old', temporal_status='historical')
        add_claim(self.bundle, 'In het eerdere verslag was Azra een hond van ongeveer vijf maanden oud.', ['age'], 'historical')
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_factual_animal_counts_in_words_also_need_annotations(self):
        for passage in ['Azra had five puppies.', 'Azra had vijf pups.']:
            self.bundle = bundle_fixture()
            self.bundle.facebook_en.text += '\n' + passage
            self.rejected('UNANNOTATED_NUMBER')

    def test_number_in_clause_cannot_disable_compatibility_uncertainty_check(self):
        self.evidence('cats', 'Azra may be comfortable with cats.', temporal_status='historical')
        add_claim(self.bundle, 'In 2025, Azra was comfortable with cats.', ['old', 'cats'], 'historical')
        self.rejected('UNCERTAINTY_LOST')

    def test_numeric_draft_without_any_claims_reports_the_rejected_clause(self):
        self.bundle.website_en_2.text = 'Azra\nAge: 5 months.'
        self.bundle.website_en_2.claims = []
        failure = self.rejected('UNANNOTATED_NUMBER')
        self.assertEqual(failure.report[0]['excerpt'], 'Age: 5 months.')

    def test_private_reference_markers_cannot_be_in_copyable_text(self):
        self.bundle.website_en_2.text += '\n[rescue]'
        self.rejected('DRAFT_PUBLIC_REFERENCE')


class NumericPrivateUiTests(unittest.TestCase):
    def test_numeric_private_report_no_retry_and_human_text_preserved(self):
        import analysis_ui
        import content_ui as ui
        from content_state import writing_scope, export_workspace
        from streamlit.testing.v1 import AppTest
        from test_content_ui import APP

        profile, bundle = profile_fixture(), bundle_fixture()
        bundle.website_en_2.text += '\nExpected adult weight: approximately 16–17 kg.'
        profile.evidence.append(Evidence.model_validate(record(id='adult', quote='Expected adult weight: approximately 16/17 kg.')))
        before_profile = profile.model_dump()
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.recheck_drafts(bundle, profile, 'third_person')
        failure = caught.exception
        failure.candidate = bundle.model_dump()
        scope = writing_scope({}, profile)
        with (patch.object(analysis_ui, 'render_analysis', return_value=None),
              patch.object(ui, 'load_content_settings', return_value=writing.ContentSettings()),
              patch.object(ui, 'load_record', return_value=None), patch.object(ui, 'save_record'),
              patch.object(ui, 'generate_drafts', side_effect=failure) as generate,
              patch.object(ui, 'copy_button'), patch.object(writing, 'OpenAI') as provider):
            app = AppTest.from_file(APP, default_timeout=10).run()
            app.session_state['ai_result'] = {'profile': profile, 'fingerprint': 'analysis'}
            app.session_state['ai_outdated'] = False
            app.run()
            app.text_area(key=ui.editor_key(scope, 'final', 'nl')).input('Keep my website edit.').run()
            app.text_area(key=ui.editor_key(scope, 'facebook', 'nl')).input('Keep my Facebook edit.').run()
            app.button(key='generate_drafts').click().run()
            app.button(key=f'writing_recheck_{scope}').click().run()
            app.run()
            self.assertFalse(app.exception)
            record_data = app.session_state['writing_workspaces'][scope]
            self.assertEqual(record_data['bundle'].website_en_2.text, bundle.website_en_2.text)
            findings = record_data['assessment']['drafts']['website_en_2']['findings']
            numeric = [item for item in findings if item['code'] in ('REVIEW_REQUIRED', 'POSSIBLE_SOURCE_SUPPORT')
                       and '16–17 kg' in item['excerpt']]
            self.assertTrue(numeric)
            self.assertIn('16–17 kg', numeric[0]['excerpt'])
            self.assertEqual(numeric[0]['color'], 'yellow')
            if numeric[0]['code'] == 'POSSIBLE_SOURCE_SUPPORT':
                self.assertEqual(numeric[0]['source_references'], [])
                self.assertIn('adult', numeric[0]['support_candidates'])
            else:
                self.assertIn('adult', numeric[0]['source_references'])
            self.assertEqual(record_data['final']['nl'], 'Keep my website edit.')
            self.assertEqual(record_data['facebook']['nl'], 'Keep my Facebook edit.')
            self.assertEqual(app.session_state['ai_result']['profile'].model_dump(), before_profile)
            self.assertNotIn('relevant_evidence', export_workspace(record_data).decode())
            generate.assert_called_once()
            provider.assert_not_called()


if __name__ == '__main__':
    unittest.main()
