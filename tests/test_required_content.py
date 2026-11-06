"""Required-content versus factual-grounding policy; mocked/offline only."""
import json
import unittest
from unittest.mock import patch

from animal_profile import Evidence
from content_models import DRAFT_KEYS, DraftClaim
import content_service as writing
from test_ai_service import record
from test_content_service import profile_fixture, bundle_fixture
from test_draft_evidence import add_claim


class RequiredContentTests(unittest.TestCase):
    def setUp(self):
        self.profile, self.bundle = profile_fixture(), bundle_fixture()

    def remove_fact(self, ref, keys=DRAFT_KEYS):
        for key in keys:
            draft = getattr(self.bundle, key)
            for claim in draft.claims[:]:
                if ref in claim.source_references:
                    draft.text = draft.text.replace(claim.excerpt, '')
                    draft.claims.remove(claim)

    def evidence(self, evidence_id, text, section=None, **values):
        self.profile.evidence.append(Evidence.model_validate(record(id=evidence_id, quote=text, temporal_status='current_as_reported', **values)))
        if section:
            getattr(self.profile, section).append(evidence_id)

    def rejection(self, code):
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.assertEqual(caught.exception.details['code'], code)
        return caught.exception

    def test_supported_name_in_heading_needs_no_duplicate_identity_annotation(self):
        self.profile.evidence[0].id = 'selected_animal_name'
        self.profile.evidence[0].source_type = 'ANIMAL_LABEL'
        self.profile.evidence[0].source_reference = 'animal_label'
        self.profile.identity.name_evidence_id = 'selected_animal_name'
        for key in DRAFT_KEYS:
            draft = getattr(self.bundle, key)
            draft.claims.pop(0)
            draft.text = '# AZRA\n' + draft.text
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.assertNotIn('selected_animal_name', writing.mandatory_evidence(self.profile))

    def test_missing_name_has_identity_specific_error(self):
        draft = self.bundle.website_nl_1
        draft.text = draft.text.replace('Azra', 'Deze hond')
        for claim in draft.claims:
            claim.excerpt = claim.excerpt.replace('Azra', 'Deze hond')
        failure = self.rejection('MISSING_ANIMAL_NAME')
        self.assertEqual(failure.report[0]['failure_type'], 'missing_name')
        self.assertEqual(failure.report[0]['required_fact'], 'Azra')
        self.assertNotIn('medical', str(failure))

    def test_name_substring_inside_another_name_does_not_pass(self):
        draft = self.bundle.website_nl_1
        draft.text = draft.text.replace('Azra', 'Azrael')
        for claim in draft.claims:
            claim.excerpt = claim.excerpt.replace('Azra', 'Azrael')
        self.rejection('MISSING_ANIMAL_NAME')

    def test_profile_name_must_be_explicitly_supported_in_permitted_quote(self):
        self.profile.evidence[0].supporting_text_or_description = 'An unnamed dog.'
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.validated_profile(self.profile)
        self.assertEqual(caught.exception.details['code'], 'PROFILE_NAME_SUPPORT')

    def test_personality_and_rescue_history_may_vary_without_identical_id_sets(self):
        self.remove_fact('social', ['website_nl_1', 'facebook_en'])
        self.remove_fact('rescue', ['website_en_3'])
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.assertNotIn('social', writing.mandatory_evidence(self.profile))
        self.assertNotIn('rescue', writing.mandatory_evidence(self.profile))

    def test_adult_weight_description_is_optional_adoption_information(self):
        self.evidence('adult', 'Expected adult weight: approximately 16/17 kg.', 'adoption_considerations')
        self.assertNotIn('adult', writing.mandatory_evidence(self.profile))
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_missing_significant_medical_fact_has_private_fact_and_reason(self):
        self.evidence('diabetes', 'Azra has diabetes and needs daily insulin.', 'health_and_special_needs')
        failure = self.rejection('ESSENTIAL_EVIDENCE_OMITTED')
        item = failure.report[0]
        self.assertEqual(item['draft'], 'website_nl_1')
        self.assertEqual(item['required_fact'], 'Azra has diabetes and needs daily insulin.')
        self.assertEqual(item['source_references'], ['diabetes'])
        self.assertEqual(item['failure_type'], 'missing_critical_information')
        self.assertTrue(item['essential_reason'])
        self.assertTrue(item['action'])
        self.assertNotIn('diabetes', str(failure) + json.dumps(failure.details))
        self.assertEqual({item['draft'] for item in failure.report}, set(DRAFT_KEYS))

    def test_citing_critical_id_with_only_name_does_not_establish_coverage(self):
        self.evidence('diabetes', 'Azra has diabetes and needs daily insulin.', 'health_and_special_needs')
        for key in DRAFT_KEYS:
            getattr(self.bundle, key).claims[0].source_references.append('diabetes')
        self.rejection('ESSENTIAL_EVIDENCE_OMITTED')

    def test_condition_alone_does_not_hide_essential_care_requirement(self):
        self.evidence('diabetes', 'Azra has diabetes and needs daily insulin.', 'health_and_special_needs')
        add_claim(self.bundle, 'Azra has diabetes.', ['diabetes'])
        self.rejection('ESSENTIAL_EVIDENCE_OMITTED')

    def test_medical_condition_and_care_paraphrases_in_both_languages_are_accepted(self):
        self.evidence('diabetes', 'Azra has diabetes and needs daily insulin.', 'health_and_special_needs')
        for key in DRAFT_KEYS:
            text = 'Azra heeft diabetes en heeft dagelijks insuline nodig.' if '_nl' in key else 'Azra is diabetic and requires insulin every day.'
            add_claim(self.bundle, text, ['diabetes'], keys=[key])
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_critical_behavior_restriction_cannot_be_omitted(self):
        self.evidence('restriction', 'Azra cannot live with cats.', 'personality_and_behavior')
        self.rejection('ESSENTIAL_EVIDENCE_OMITTED')

    def test_dutch_and_english_restriction_paraphrases_are_accepted(self):
        self.evidence('restriction', 'Azra cannot live with cats.', 'personality_and_behavior')
        for key in DRAFT_KEYS:
            text = 'Een thuis zonder katten is nodig.' if '_nl' in key else 'A home without cats is needed.'
            add_claim(self.bundle, text, ['restriction'], keys=[key])
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_unimportant_photo_update_is_not_mandatory(self):
        self.evidence('photo_update', 'Azra stood on grass in a new photo.', ref='updates')
        from animal_profile import ProfileUpdate
        self.profile.updates.append(ProfileUpdate(original_date=None, normalized_date=None,
            source_excerpt='Azra stood on grass in a new photo.', evidence_ids=['photo_update'],
            change_type='adds_information', superseded_evidence_ids=[], unresolved_contradictions=[]))
        self.assertNotIn('photo_update', writing.mandatory_evidence(self.profile))
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_material_new_update_remains_required_even_outside_factual_sections(self):
        self.evidence('new_need', 'Azra now needs lifelong medication.', ref='updates')
        from animal_profile import ProfileUpdate
        self.profile.updates.append(ProfileUpdate(original_date=None, normalized_date=None,
            source_excerpt='Azra now needs lifelong medication.', evidence_ids=['new_need'],
            change_type='adds_information', superseded_evidence_ids=[], unresolved_contradictions=[]))
        self.rejection('ESSENTIAL_EVIDENCE_OMITTED')

    def test_empty_other_info_and_updates_need_no_invented_references(self):
        self.profile.updates = []
        self.profile.health_and_special_needs = []
        self.profile.review_warnings = []
        self.profile.evidence = [item for item in self.profile.evidence if item.id not in ('old', 'new')]
        self.remove_fact('old')
        self.remove_fact('new')
        result, _ = writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        self.assertEqual(result.model_dump(), self.bundle.model_dump())

    def test_fake_identity_reference_still_blocked(self):
        self.bundle.website_nl_1.claims[0].source_references = ['selected_animal_name']
        self.rejection('DRAFT_RULE')

    def test_unsupported_medical_and_behavioral_claims_are_still_blocked(self):
        for passage in ['Azra is vaccinated.', 'Azra is good with cats.']:
            self.bundle = bundle_fixture()
            add_claim(self.bundle, passage, ['name'])
            self.rejection('UNRELATED_EVIDENCE')

    def test_local_recheck_does_not_mutate_profile_or_candidate_or_call_provider(self):
        self.remove_fact('social', ['website_nl_1'])
        before_profile, before_bundle = self.profile.model_dump(), self.bundle.model_dump()
        with patch.object(writing, 'OpenAI') as provider:
            writing.recheck_drafts(self.bundle, self.profile, 'third_person')
        provider.assert_not_called()
        self.assertEqual(self.profile.model_dump(), before_profile)
        self.assertEqual(self.bundle.model_dump(), before_bundle)

    def test_benign_health_and_healed_minor_history_can_be_optional(self):
        self.evidence('healthy', 'Azra is healthy.', 'health_and_special_needs')
        item = Evidence.model_validate(record(id='scratch', quote='A minor scratch has fully healed.', temporal_status='historical'))
        self.profile.evidence.append(item)
        self.profile.health_and_special_needs.append('scratch')
        self.assertNotIn('healthy', writing.mandatory_evidence(self.profile))
        self.assertNotIn('scratch', writing.mandatory_evidence(self.profile))
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_identity_reference_cannot_hide_substantive_medical_content(self):
        item = self.profile.evidence[0]
        item.claim = item.supporting_text_or_description = 'Azra has diabetes and needs daily insulin.'
        self.profile.health_and_special_needs.append(item.id)
        self.assertIn(item.id, writing.mandatory_evidence(self.profile))
        self.rejection('ESSENTIAL_EVIDENCE_OMITTED')

    def test_routine_word_cannot_hide_an_unfamiliar_medical_condition(self):
        self.evidence('condition', 'Azra has Addison syndrome and is vaccinated.', 'health_and_special_needs')
        self.assertIn('condition', writing.mandatory_evidence(self.profile))
        self.rejection('ESSENTIAL_EVIDENCE_OMITTED')

    def test_rare_diagnosis_must_remain_alongside_its_care_requirement(self):
        self.evidence('condition', 'Azra has Addison disease and needs daily medication.', 'health_and_special_needs')
        add_claim(self.bundle, 'Azra needs daily medication.', ['condition'])
        self.rejection('ESSENTIAL_EVIDENCE_OMITTED')

    def test_essential_requirement_cannot_become_a_mere_preference(self):
        self.remove_fact('home')
        add_claim(self.bundle, 'Azra enjoys a quiet home and training.', ['home'])
        self.rejection('ESSENTIAL_EVIDENCE_OMITTED')

    def test_common_english_and_dutch_compatibility_restrictions_are_required(self):
        for text in ['Azra is not suitable for children.', 'Azra does not tolerate cats.', 'Azra is niet geschikt voor kinderen.']:
            with self.subTest(text=text):
                self.profile, self.bundle = profile_fixture(), bundle_fixture()
                self.evidence('restriction', text, 'personality_and_behavior')
                self.assertIn('restriction', writing.mandatory_evidence(self.profile))
                self.rejection('ESSENTIAL_EVIDENCE_OMITTED')

    def test_common_medical_paraphrases_are_supported_in_dutch(self):
        for source, target in [('Azra has a food allergy.', 'Azra heeft een voedselallergie.'),
                               ('Azra has kidney disease.', 'Azra heeft een nierziekte.'),
                               ('Azra is deaf.', 'Azra is doof.'),
                               ('Azra is blind.', 'Azra is blind.'),
                               ('Azra needs surgery.', 'Azra heeft een operatie nodig.'),
                               ('Azra needs a special diet.', 'Azra heeft een speciaal dieet nodig.')]:
            with self.subTest(source=source):
                self.profile, self.bundle = profile_fixture(), bundle_fixture()
                self.evidence('condition', source, 'health_and_special_needs')
                for key in DRAFT_KEYS:
                    add_claim(self.bundle, target if '_nl' in key else source, ['condition'], keys=[key])
                writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_grouped_update_does_not_make_an_unrelated_description_required(self):
        self.evidence('photo_update', 'Azra stood on grass.', ref='updates')
        self.profile.updates[0].evidence_ids.append('photo_update')
        self.assertNotIn('photo_update', writing.mandatory_evidence(self.profile))
        writing.recheck_drafts(self.bundle, self.profile, 'third_person')

    def test_positive_critical_condition_cannot_be_denied_in_the_draft(self):
        self.evidence('condition', 'Azra has diabetes and needs daily insulin.', 'health_and_special_needs')
        add_claim(self.bundle, 'Azra does not have diabetes and does not need daily insulin.', ['condition'])
        self.rejection('ESSENTIAL_EVIDENCE_OMITTED')

    def test_private_essential_reports_redact_credentials(self):
        secret = 'sk-syntheticprivatecredential123456'
        self.evidence('condition', 'Azra has diabetes. ' + secret, 'health_and_special_needs')
        failure = self.rejection('ESSENTIAL_EVIDENCE_OMITTED')
        self.assertNotIn(secret, json.dumps(failure.report) + json.dumps(failure.details) + str(failure))


class RequiredContentUiTests(unittest.TestCase):
    def setUp(self):
        import analysis_ui
        import content_ui
        from content_state import writing_scope
        from streamlit.testing.v1 import AppTest
        from test_content_ui import APP
        self.ui = content_ui
        self.profile, self.bundle = profile_fixture(), bundle_fixture()
        patches = [patch.object(analysis_ui, 'render_analysis', return_value=None),
                   patch.object(content_ui, 'load_content_settings', return_value=writing.ContentSettings()),
                   patch.object(content_ui, 'load_record', return_value=None), patch.object(content_ui, 'save_record'),
                   patch.object(content_ui, 'generate_drafts'), patch.object(content_ui, 'copy_button')]
        _, _, _, _, self.generate, _ = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        self.app = AppTest.from_file(APP, default_timeout=10).run()
        self.app.session_state['ai_result'] = {'profile': self.profile, 'fingerprint': 'analysis'}
        self.app.session_state['ai_outdated'] = False
        self.app.run()
        self.scope = writing_scope({}, self.profile)

    def test_old_name_citation_rejection_can_be_rechecked_free_without_changing_profile_or_edits(self):
        self.profile.evidence[0].id = 'selected_animal_name'
        self.profile.evidence[0].source_type = 'ANIMAL_LABEL'
        self.profile.evidence[0].source_reference = 'animal_label'
        self.profile.identity.name_evidence_id = 'selected_animal_name'
        for key in DRAFT_KEYS:
            getattr(self.bundle, key).claims.pop(0)
        self.app.session_state['ai_result'] = {'profile': self.profile, 'fingerprint': 'analysis'}
        legacy = writing.ContentValidationError('ESSENTIAL_EVIDENCE_OMITTED', 'Legacy missing identity citation.', 'website_nl_1')
        legacy.candidate = self.bundle.model_dump()
        self.generate.side_effect = legacy
        self.app.run()
        self.app.text_area(key=self.ui.editor_key(self.scope, 'final', 'nl')).input('Keep my website.').run()
        self.app.text_area(key=self.ui.editor_key(self.scope, 'facebook', 'nl')).input('Keep my Facebook.').run()
        self.app.button(key='generate_drafts').click().run()
        self.app.button(key=f'writing_recheck_{self.scope}').click().run()
        self.app.run()
        data = self.app.session_state['writing_workspaces'][self.scope]
        self.assertEqual(data['bundle'], self.bundle)
        self.assertEqual(data['final']['nl'], 'Keep my website.')
        self.assertEqual(data['facebook']['nl'], 'Keep my Facebook.')
        self.assertEqual(self.app.session_state['ai_result']['profile'], self.profile)
        self.generate.assert_called_once()
        self.assertFalse(self.app.exception)

    def test_critical_recheck_retains_candidate_with_findings_previous_bundle_and_human_edits(self):
        from content_state import commit_generation, export_workspace
        data = self.app.session_state['writing_workspaces'][self.scope]
        commit_generation(data, self.bundle, self.profile, writing.ContentSettings(), [], {}, 'analysis')
        self.app.run()
        self.app.text_area(key=self.ui.editor_key(self.scope, 'final', 'nl')).input('Keep my website.').run()
        self.app.text_area(key=self.ui.editor_key(self.scope, 'facebook', 'nl')).input('Keep my Facebook.').run()
        candidate = self.bundle.model_copy(deep=True)
        draft = candidate.website_en_2
        removed = next(item for item in draft.claims if item.source_references == ['home'])
        draft.text = draft.text.replace(removed.excerpt, '')
        draft.claims.remove(removed)
        with self.assertRaises(writing.ContentValidationError) as caught:
            writing.recheck_drafts(candidate, self.profile, 'third_person')
        failure = caught.exception
        failure.candidate = candidate.model_dump()
        self.generate.side_effect = failure
        confirmation = f'writing_regenerate_confirm_{self.scope}_{data["generated_at"]}'
        self.app.checkbox(key=confirmation).check().run()
        self.app.button(key='generate_drafts').click().run()
        self.app.button(key=f'writing_recheck_{self.scope}').click().run()
        data = self.app.session_state['writing_workspaces'][self.scope]
        self.assertEqual(data['bundle'], candidate)
        self.assertEqual(data['history'][-1]['bundle'], self.bundle.model_dump(mode='json'))
        self.assertEqual(data['final']['nl'], 'Keep my website.')
        self.assertEqual(data['facebook']['nl'], 'Keep my Facebook.')
        omissions = [item for item in data['assessment']['drafts']['website_en_2']['findings']
                     if item['code'] == 'ESSENTIAL_EVIDENCE_OMITTED']
        self.assertTrue(omissions)
        self.assertIn('home', omissions[0]['source_references'])
        self.assertEqual(omissions[0]['color'], 'red')
        self.assertEqual(json.loads(export_workspace(data))['final_website_nl'], 'Keep my website.')
        self.generate.assert_called_once()
        self.assertFalse(self.app.exception)


if __name__ == '__main__':
    unittest.main()
