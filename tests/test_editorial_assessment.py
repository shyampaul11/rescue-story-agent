"""Synthetic local editorial review checks; no providers or private sources."""
import unittest
from types import SimpleNamespace

from animal_profile import AnimalProfile, Evidence
from content_models import DRAFT_KEYS
from editorial_assessment import assess_bundle, assess_text


def profile(*records, unknowns=()):
    return AnimalProfile.model_validate({
        'identity': {'animal_name': 'Mika', 'name_evidence_id': 'name',
                     'identification_evidence_ids': [], 'verification_status': 'reported_not_independently_verified'},
        'rescue_background': [], 'personality_and_behavior': [], 'physical_appearance': [],
        'health_and_special_needs': [], 'adoption_considerations': [], 'updates': [],
        'unknown_information': list(unknowns), 'review_warnings': [],
        'evidence': [evidence('name', 'Mika').model_dump()] + [r.model_dump() for r in records]})


def evidence(ref, text, **kwargs):
    return Evidence.model_validate(dict(id=ref, claim=text, supporting_text_or_description=text,
        source_type='OTHER_INFO_FACT', source_reference='other_info', subject='selected_animal',
        verification_status='reported_not_independently_verified', temporal_status='current_as_reported', **kwargs))


def claim(text, refs=(), qualification='reported'):
    return dict(excerpt=text, source_references=list(refs), qualification=qualification)


class AssessmentTests(unittest.TestCase):
    def codes(self, result):
        return {f['code'] for f in result['findings']}

    def test_positive_atomic_source_support_gets_green(self):
        p = profile(evidence('chip', 'Mika is microchipped.'))
        r = assess_text('Mika is microchipped.', p, claims=[claim('Mika is microchipped.', ['chip'])])
        self.assertEqual(r['status'], 'green')
        self.assertEqual(r['passages'][0]['color'], 'green')

    def test_valid_reference_does_not_green_added_medical_clause(self):
        p = profile(evidence('chip', 'Mika is microchipped.'))
        r = assess_text('Mika is microchipped and vaccinated.', p,
            claims=[claim('Mika is microchipped and vaccinated.', ['chip'])])
        self.assertEqual(r['status'], 'red')
        self.assertIn('UNSUPPORTED_FACT', self.codes(r))

    def test_unannotated_medical_and_readiness_are_independent_red_findings(self):
        r = assess_text('Mika is healthy. Mika is ready for travel.', profile())
        self.assertEqual(r['status'], 'red')
        self.assertGreaterEqual(len([f for f in r['findings'] if f['color'] == 'red']), 2)

    def test_supported_edit_without_annotations_is_locally_assessed(self):
        p = profile(evidence('chip', 'Mika is microchipped.'))
        r = assess_text('Mika is microchipped.', p)
        self.assertEqual(r['status'], 'green')
        self.assertEqual(r['passages'][0]['source_references'], ['chip'])

    def test_subjective_unmatched_paraphrase_is_yellow(self):
        p = profile(evidence('happy', 'Mika enjoys walks.'))
        r = assess_text('Mika is a delightful walking companion.', p,
                        claims=[claim('Mika is a delightful walking companion.', ['happy'])])
        self.assertEqual(r['status'], 'yellow')

    def test_truthful_unknown_without_fake_reference_remains_yellow(self):
        p = profile(unknowns=['Vaccination status is unknown.'])
        r = assess_text('Mika\nVaccination status is unknown.', p,
                        claims=[claim('Vaccination status is unknown.', [], 'unknown')])
        self.assertEqual(r['status'], 'yellow')
        self.assertNotIn('MISSING_DRAFT_REFERENCES', self.codes(r))
        self.assertNotIn('UNKNOWN_AS_FACT', self.codes(r))

    def test_unknown_cannot_support_negative_social_assertion(self):
        p = profile(unknowns=['Compatibility with children is unknown.'])
        r = assess_text('Mika is unsuitable for children.', p,
              claims=[claim('Mika is unsuitable for children.', ['unknown:0'], 'unknown')])
        self.assertEqual(r['status'], 'red')

    def test_wrong_citation_is_annotation_yellow_when_text_has_real_support(self):
        p = profile(evidence('chip', 'Mika is microchipped.'))
        r = assess_text('Mika is microchipped.', p,
                        claims=[claim('Mika is microchipped.', ['invented'])])
        self.assertEqual(r['status'], 'yellow')
        self.assertIn('UNKNOWN_EVIDENCE_ID', self.codes(r))

    def test_duplicate_excerpt_assigns_each_occurrence_once(self):
        p = profile(evidence('chip', 'Mika is microchipped.'))
        text = 'Mika is microchipped. Mika is microchipped.'
        r = assess_text(text, p, claims=[claim('Mika is microchipped.', ['chip'])] * 2)
        green = [x for x in r['passages'] if x['color'] == 'green']
        self.assertEqual([(x['start'], x['end']) for x in green], [(0, 21), (22, 43)])

    def test_mismatched_excerpt_does_not_highlight_unrelated_text(self):
        r = assess_text('Mika welcomes visitors.', profile(), claims=[claim('Other text.', ['name'])])
        self.assertIn('DRAFT_EXCERPT_MISMATCH', self.codes(r))
        self.assertFalse(any(p['code'] == 'DRAFT_EXCERPT_MISMATCH' for p in r['passages']))

    def test_estimated_historical_and_media_support_never_get_green(self):
        samples = [evidence('x', 'Mika was around 5 months old.'),
                   evidence('x', 'Mika is approximately 16 kg.'),
                   evidence('x', 'Mika is sitting in the photograph.')]
        samples[0].temporal_status = 'historical'
        samples[2].source_type = 'PHOTO_OBSERVATION'
        for item in samples:
            with self.subTest(item=item.claim):
                r = assess_text(item.claim, profile(item), claims=[claim(item.claim, ['x'], 'qualified')])
                self.assertEqual(r['status'], 'yellow')

    def test_historical_age_and_adult_weight_cannot_become_current(self):
        age = evidence('age', 'Mika was around 5 months old.')
        age.temporal_status = 'historical'
        weight = evidence('weight', 'Expected adult weight is approximately 16 kg.')
        for text, ref in [('Mika is 5 months old.', 'age'), ('Mika weighs 16 kg.', 'weight')]:
            with self.subTest(text=text):
                self.assertEqual(assess_text(text, profile(age, weight), claims=[claim(text, [ref])])['status'], 'red')

    def test_other_animal_cannot_support_fact(self):
        other = evidence('other', 'Mika is vaccinated.')
        other.subject = 'other_animal'
        r = assess_text('Mika is vaccinated.', profile(other), claims=[claim('Mika is vaccinated.', ['other'])])
        self.assertEqual(r['status'], 'red')
        self.assertIn('OTHER_ANIMAL_EVIDENCE', self.codes(r))

    def test_critical_omissions_and_identity_are_findings(self):
        p = profile(evidence('care', 'Mika has diabetes and needs daily insulin.'))
        p.health_and_special_needs = ['care']
        r = assess_text('A loving home awaits.', p)
        self.assertIn('ESSENTIAL_EVIDENCE_OMITTED', self.codes(r))
        self.assertIn('MISSING_ANIMAL_NAME', self.codes(r))

    def test_malformed_annotations_preserve_assessment(self):
        r = assess_text('Mika is healthy.', profile(), claims=[None, {'excerpt': 'Mika is healthy.', 'qualification': {'bad': True}}])
        self.assertEqual(r['status'], 'red')
        self.assertIn('ANNOTATION_INVALID', self.codes(r))

    def test_bundle_collects_all_eight_and_deduplicates_questions(self):
        p = profile(unknowns=['Vaccination status is unknown.'])
        draft = SimpleNamespace(text='Mika is vaccinated.', claims=[claim('Mika is vaccinated.', ['unknown:0'])])
        bundle = SimpleNamespace(**{key: draft for key in DRAFT_KEYS})
        result = assess_bundle(bundle, p)
        self.assertEqual(set(result['drafts']), set(DRAFT_KEYS))
        self.assertTrue(all(r['status'] == 'red' for r in result['drafts'].values()))
        self.assertEqual(len(result['questions']), 1)
        self.assertEqual(result['questions'], assess_bundle(bundle, p)['questions'])

    def test_manual_confirmation_is_not_positive_evidence(self):
        r = assess_text('Mika is vaccinated.', profile(), manual_sources=[{
            'id': 'manual:1', 'topic': 'vaccination', 'answer': 'Possibly vaccinated',
            'respondent': 'Volunteer', 'uncertainty': 'unconfirmed', 'confirmed': True}])
        self.assertNotEqual(r['status'], 'green')

    def test_existing_support_does_not_make_wrong_valid_citation_green(self):
        p = profile(evidence('chip', 'Mika is microchipped.'))
        r = assess_text('Mika is microchipped.', p, claims=[claim('Mika is microchipped.', ['name'])])
        self.assertEqual(r['status'], 'yellow')
        self.assertIn('ANNOTATION_SOURCE_MISMATCH', self.codes(r))
        self.assertFalse(any(p['color'] == 'green' for p in r['passages']))

    def test_historical_context_can_support_truthful_dutch_unknown(self):
        old = evidence('old', 'Mika was around 5 months old.')
        old.temporal_status = 'historical'
        p = profile(old, unknowns=['Current age and birth date are unknown.'])
        line = 'Haar huidige leeftijd en geboortedatum zijn onbekend'
        r = assess_text('Mika\n' + line, p, claims=[claim(line, ['old'], 'unknown')])
        self.assertEqual(r['status'], 'yellow')

    def test_distinct_historical_and_unknown_clauses_in_same_sentence(self):
        old = evidence('old', 'Mika was around 5 months old.')
        old.temporal_status = 'historical'
        p = profile(old, unknowns=['Current age is unknown.'])
        text = 'Mika was around 5 months old and her current age is unknown.'
        r = assess_text(text, p, claims=[claim('Mika was around 5 months old', ['old'], 'historical'),
            claim('her current age is unknown.', [], 'unknown')])
        self.assertEqual(r['status'], 'yellow')

    def test_superseded_unannotated_source_cannot_be_green(self):
        p = profile(evidence('old', 'Mika is vaccinated.'), evidence('new', 'Mika is not vaccinated.'))
        p.updates = [SimpleNamespace(evidence_ids=['new'], superseded_evidence_ids=['old'],
            change_type='supersedes', unresolved_contradictions=[], source_excerpt='Mika is not vaccinated.')]
        r = assess_text('Mika is vaccinated.', p)
        self.assertEqual(r['status'], 'red')
        self.assertIn('SUPERSEDED_AS_CURRENT', self.codes(r))

    def test_explicit_profile_conflict_warning_downgrades_exact_support(self):
        p = profile(evidence('yes', 'Mika is microchipped.'), evidence('no', 'Mika is not microchipped.'))
        p.review_warnings = [SimpleNamespace(category='conflict', evidence_ids=['yes', 'no'], message='Conflicting chip reports.')]
        r = assess_text('Mika is microchipped.', p, claims=[claim('Mika is microchipped.', ['yes'])])
        self.assertEqual(r['status'], 'yellow')
        self.assertIn('SOURCE_CONFLICT', self.codes(r))
        self.assertFalse(any(p['color'] == 'green' for p in r['passages']))

    def test_explicit_manual_response_is_attributed_yellow_support(self):
        source = dict(id='manual:1', animal_name='Mika', topic='vaccination',
            response='Mika is vaccinated.', respondent='Rescuer', response_date='2026-11-05',
            uncertainty='Reported, not independently verified')
        r = assess_text('Mika is vaccinated.', profile(), manual_sources=[source])
        self.assertEqual(r['status'], 'yellow')
        self.assertIn('SOURCE_RESCUER_RESPONSE', self.codes(r))
        self.assertIn('manual:1', r['passages'][0]['source_references'])
        source['animal_name'] = 'Other dog'
        self.assertEqual(assess_text('Mika is vaccinated.', profile(), manual_sources=[source])['status'], 'red')

    def test_uncertain_manual_response_does_not_establish_definite_fact(self):
        source = dict(id='manual:1', animal_name='Mika', topic='vaccination',
            response='Mika is possibly vaccinated.', respondent='Rescuer', response_date='2026-11-05')
        self.assertEqual(assess_text('Mika is vaccinated.', profile(), manual_sources=[source])['status'], 'red')

    def test_cross_language_numeric_conflict_is_collected_but_absence_is_not(self):
        p = profile(evidence('w1', 'Mika weighs 10 kg.'), evidence('w2', 'Mika weegt 12 kg.'))
        bundle = {key: dict(text='Mika', claims=[]) for key in DRAFT_KEYS}
        bundle['website_en_1'] = dict(text='Mika weighs 10 kg.', claims=[claim('Mika weighs 10 kg.', ['w1'])])
        bundle['website_nl_1'] = dict(text='Mika weegt 12 kg.', claims=[claim('Mika weegt 12 kg.', ['w2'])])
        r = assess_bundle(bundle, p)
        self.assertIn('TRANSLATION_CONTRADICTION', self.codes(r['drafts']['website_en_1']))
        self.assertNotIn('TRANSLATION_CONTRADICTION', self.codes(r['drafts']['website_en_2']))

    def test_metadata_issue_note_is_yellow(self):
        draft = dict(text='Mika is microchipped.', claims=[claim('Mika is microchipped.', ['chip'])],
            review_notes=['Annotation metadata issue: missing references'])
        r = assess_bundle(dict.fromkeys(DRAFT_KEYS, draft), profile(evidence('chip', 'Mika is microchipped.')))
        self.assertEqual(r['drafts']['website_en_1']['status'], 'yellow')

    def test_cross_language_social_polarity_conflict_is_reported(self):
        p = profile()
        bundle = {key: dict(text='Mika', claims=[]) for key in DRAFT_KEYS}
        bundle['facebook_en']['text'] = 'Mika is good with cats.'
        bundle['facebook_nl']['text'] = 'Mika is niet geschikt voor katten.'
        r = assess_bundle(bundle, p)
        self.assertIn('TRANSLATION_CONTRADICTION', self.codes(r['drafts']['facebook_nl']))

    def test_cross_language_estimate_qualifier_difference_is_reviewable(self):
        p = profile()
        bundle = {key: dict(text='Mika', claims=[]) for key in DRAFT_KEYS}
        bundle['facebook_en']['text'] = 'Mika weighs approximately 10 kg.'
        bundle['facebook_nl']['text'] = 'Mika weegt 10 kg.'
        r = assess_bundle(bundle, p)
        self.assertIn('TRANSLATION_QUALIFICATION', self.codes(r['drafts']['facebook_nl']))

    def test_optional_current_weight_is_not_adult_weight_translation_conflict(self):
        bundle = {key: dict(text='Mika', claims=[]) for key in DRAFT_KEYS}
        bundle['facebook_nl']['text'] = 'Mika weegt 8 kg. Verwacht volwassen gewicht: ongeveer 16 kg.'
        bundle['facebook_en']['text'] = 'Mika has an expected adult weight of approximately 16 kg.'
        r = assess_bundle(bundle, profile())
        self.assertNotIn('TRANSLATION_CONTRADICTION', self.codes(r['drafts']['facebook_nl']))
        self.assertNotIn('TRANSLATION_QUALIFICATION', self.codes(r['drafts']['facebook_nl']))

    def test_manual_uncertainty_metadata_cannot_be_lost_in_definite_copy(self):
        source = dict(id='manual:1', animal_name='Mika', topic='vaccination', response='Mika is vaccinated.',
            respondent='Rescuer', response_date='2026-11-05', uncertainty='Possibly; records are uncertain.')
        r = assess_text('Mika is vaccinated.', profile(), manual_sources=[source])
        self.assertEqual(r['status'], 'red')

    def test_profile_gap_produces_question_when_optional_gap_is_omitted_from_story(self):
        p = profile(evidence('chip', 'Mika is microchipped.'), unknowns=['Vaccination status is unknown.'])
        draft = dict(text='Mika is microchipped.', claims=[claim('Mika is microchipped.', ['chip'])])
        r = assess_bundle(dict.fromkeys(DRAFT_KEYS, draft), p)
        self.assertEqual(r['drafts']['website_en_1']['status'], 'green')
        self.assertEqual([q['topic'] for q in r['questions']], ['vaccination'])
        self.assertEqual(r['questions'][0]['source_references'], ['unknown:0'])

    def test_ordinary_unmatched_style_is_local_review_not_rescuer_question(self):
        draft = dict(text='Mika is a delightful companion.', claims=[])
        r = assess_bundle(dict.fromkeys(DRAFT_KEYS, draft), profile())
        self.assertEqual(r['questions'], [])

    def test_numeric_range_cannot_become_decimal_via_punctuation_normalization(self):
        for source, edited in [('Mika weighs 16-17 kg.', 'Mika weighs 16.17 kg.'),
                               ('Mika needs 1-2 tablets.', 'Mika needs 1.2 tablets.')]:
            with self.subTest(source=source):
                r = assess_text(edited, profile(evidence('amount', source)))
                self.assertEqual(r['status'], 'red')
                self.assertFalse(any(p['color'] == 'green' for p in r['passages']))

    def test_historical_source_with_current_age_wording_needs_red_even_on_exact_match(self):
        age = evidence('age', 'Mika is 5 months old.')
        age.temporal_status = 'historical'
        r = assess_text('Mika is 5 months old.', profile(age))
        self.assertEqual(r['status'], 'red')
        self.assertIn('HISTORICAL_AGE_AS_CURRENT', self.codes(r))

    def test_public_citation_is_annotation_issue_without_changing_prose(self):
        text = 'Mika is microchipped. [chip]'
        r = assess_text(text, profile(evidence('chip', 'Mika is microchipped.')))
        self.assertEqual(r['status'], 'yellow')
        self.assertIn('DRAFT_PUBLIC_REFERENCE', self.codes(r))
        marked = [p for p in r['passages'] if p['code'] == 'DRAFT_PUBLIC_REFERENCE']
        self.assertEqual([text[p['start']:p['end']] for p in marked], ['[chip]'])

    def test_unannotated_qualified_dutch_adult_weight_paraphrase_is_yellow(self):
        p = profile(evidence('adult', 'Expected adult weight is approximately 16-17 kg.'))
        r = assess_text('Mika\nVerwacht volwassen gewicht: ongeveer 16-17 kg.', p)
        self.assertEqual(r['status'], 'yellow')
        self.assertIn('POSSIBLE_SOURCE_SUPPORT', self.codes(r))
        candidate = next(f for f in r['findings'] if f['code'] == 'POSSIBLE_SOURCE_SUPPORT')
        self.assertEqual(candidate['source_references'], [])
        self.assertEqual(candidate['support_candidates'], ['adult'])

    def test_unannotated_reported_vaccination_paraphrase_is_yellow(self):
        p = profile(evidence('vaccination', 'Mika is vaccinated.'))
        r = assess_text('Mika has received vaccinations.', p, claims=[])
        self.assertEqual(r['status'], 'yellow')
        self.assertIn('POSSIBLE_SOURCE_SUPPORT', self.codes(r))
        self.assertFalse(any(p['color'] == 'green' for p in r['passages']))

    def test_unannotated_source_candidate_does_not_support_extra_medical_assertion(self):
        p = profile(evidence('vaccination', 'Mika is vaccinated.'))
        r = assess_text('Mika has received vaccinations and needs insulin.', p)
        self.assertEqual(r['status'], 'red')
        self.assertTrue(any(f['color'] == 'red' and 'insulin' in f['excerpt'] for f in r['findings']))

    def test_unannotated_numeric_candidate_still_rejects_values_and_lost_qualifiers(self):
        p = profile(evidence('adult', 'Expected adult weight is approximately 16-17 kg.'))
        for text in ('Mika weighs 16-17 kg.', 'Mika has an expected adult weight of approximately 19 kg.'):
            with self.subTest(text=text):
                self.assertEqual(assess_text(text, p)['status'], 'red')

    def test_unsupported_current_location_is_red_in_both_languages(self):
        for text in ('Mika lives in Paris.', 'Mika woont in Parijs.', 'Mika is currently in Paris.',
                     'Mika verblijft in Parijs.', 'Mika\nLocation: Paris', 'Mika\nLocatie: Parijs'):
            for annotated in (False, True):
                with self.subTest(text=text, annotated=annotated):
                    claims = [claim(text, ['name'])] if annotated else None
                    r = assess_text(text, profile(), claims=claims)
                    self.assertEqual(r['status'], 'red')

    def test_matching_location_paraphrase_is_yellow_candidate(self):
        p = profile(evidence('place', 'Mika lives in Paris.'))
        r = assess_text('Mika stays in Paris.', p)
        self.assertEqual(r['status'], 'yellow')
        self.assertIn('POSSIBLE_SOURCE_SUPPORT', self.codes(r))
        self.assertEqual(assess_text('Mika lives in Paris.', p,
            claims=[claim('Mika lives in Paris.', ['place'])])['status'], 'green')

    def test_different_place_is_red_even_with_valid_location_reference(self):
        p = profile(evidence('place', 'Mika lives in Paris.'))
        for claims in (None, [claim('Mika stays in Rome.', ['place'])]):
            with self.subTest(claims=claims):
                r = assess_text('Mika stays in Rome.', p, claims=claims)
                self.assertEqual(r['status'], 'red')
                self.assertIn('LOCATION_CONFLICT', self.codes(r))

    def test_future_home_invitation_does_not_claim_current_location(self):
        for text in ('Mika hopes to find a home in Paris.', 'Mika zoekt een thuis in Parijs.'):
            with self.subTest(text=text):
                r = assess_text(text, profile())
                self.assertFalse(any(f['color'] == 'red' for f in r['findings']))

    def test_unannotated_social_compatibility_verbs_require_support(self):
        for text in ('Mika loves children.', 'Mika is friends with cats.', 'Mika houdt van kinderen.',
                     'Mika gets along with dogs.'):
            with self.subTest(text=text):
                self.assertEqual(assess_text(text, profile())['status'], 'red')

    def test_social_paraphrase_with_relevant_profile_is_yellow_candidate(self):
        p = profile(evidence('dogs', 'Mika is good with dogs.'))
        r = assess_text('Mika gets along with dogs.', p)
        self.assertEqual(r['status'], 'yellow')
        self.assertIn('POSSIBLE_SOURCE_SUPPORT', self.codes(r))

    def test_location_context_suffix_does_not_conflict_with_same_place(self):
        p = profile(evidence('place', 'Mika lives in Paris with a foster family.'))
        r = assess_text('Mika lives in Paris.', p)
        self.assertEqual(r['status'], 'yellow')
        self.assertNotIn('LOCATION_CONFLICT', self.codes(r))
        self.assertIn('POSSIBLE_SOURCE_SUPPORT', self.codes(r))

    def test_common_dutch_place_alias_is_yellow_review_candidate(self):
        p = profile(evidence('place', 'Mika lives in Paris with a foster family.'))
        r = assess_text('Mika woont in Parijs.', p)
        self.assertEqual(r['status'], 'yellow')
        self.assertNotIn('LOCATION_CONFLICT', self.codes(r))
        self.assertFalse(any(p['color'] == 'green' for p in r['passages']))

    def test_ambiguous_location_context_is_yellow_not_proven_conflict(self):
        p = profile(evidence('place', 'Mika lives in Paris at a temporary shelter.'))
        r = assess_text('Mika stays in Paris.', p)
        self.assertEqual(r['status'], 'yellow')
        self.assertNotIn('LOCATION_CONFLICT', self.codes(r))


if __name__ == '__main__':
    unittest.main()
