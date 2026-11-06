"""Misclassified visual evidence stays review-only, never a reported/public fact."""
import unittest
from editorial_test_support import isolated_editorial_storage
import json
from unittest.mock import patch

from animal_profile import AnimalProfile, AnimalSources, AnimalContext
from analysis_media import PreparedImage
from analysis_validation import ProfileOutputError, ProfileRuleError
import ai_service
import analysis_ui as ui
from ai_config import AnalysisSettings
from content_service import build_content_payload, mandatory_evidence, validate_drafts, ContentError
from streamlit.testing.v1 import AppTest
from test_ai_service import empty_profile, record
from test_content_service import profile_fixture, bundle_fixture
from test_analysis_ui import APP


class VisualRoutingTests(unittest.TestCase):
    def setup_candidate(self):
        data = empty_profile().model_dump()
        data['evidence'] = [record(id='medical', quote='Treatment was reported.'),
            record(id='photo', ref='photo:1', quote='The dog appears healthy.', source_type='PHOTO_OBSERVATION',
                   verification_status='interpretation_requires_review', temporal_status='at_sampled_moment')]
        data['health_and_special_needs'] = ['medical', 'photo']
        return AnimalProfile.model_validate(data)

    def test_visual_health_claim_is_quarantined_without_losing_reported_health(self):
        profile = ai_service.validate_candidate(self.setup_candidate(),
            AnimalSources(history='Treatment was reported.'), [PreparedImage('photo:1', 'PHOTO_OBSERVATION', b'jpeg')])
        self.assertEqual(profile.health_and_special_needs, ['medical'])
        self.assertEqual(profile.review_only_evidence_ids, ['photo'])
        self.assertEqual(len(profile.evidence), 2)

    def test_strict_validator_still_blocks_unrouted_visual_facts_with_section_details(self):
        with self.assertRaises(ProfileRuleError) as caught:
            ai_service.validate_profile(self.setup_candidate(), AnimalSources(history='Treatment was reported.'),
                [PreparedImage('photo:1', 'PHOTO_OBSERVATION', b'jpeg')])
        self.assertEqual(caught.exception.code, 'VISUAL_FACT')
        self.assertEqual(caught.exception.location['section'], 'health_and_special_needs')
        self.assertEqual(caught.exception.location['source_types'], ['PHOTO_OBSERVATION'])

    def test_correct_visual_observations_stay_visual_and_quarantined_claims_do_not(self):
        candidate = self.setup_candidate()
        candidate.evidence.append(type(candidate.evidence[0]).model_validate(record(id='coat', ref='photo:1',
            quote='Brown coat is visible.', source_type='PHOTO_OBSERVATION',
            verification_status='direct_visual_observation', temporal_status='at_sampled_moment')))
        candidate.physical_appearance = ['coat', 'photo']
        profile = ai_service.validate_candidate(candidate, AnimalSources(history='Treatment was reported.'),
                                              [PreparedImage('photo:1', 'PHOTO_OBSERVATION', b'jpeg')])
        self.assertEqual(profile.physical_appearance, ['coat'])
        self.assertEqual(profile.review_only_evidence_ids, ['photo'])

    def test_unknown_media_sources_are_not_accepted_by_quarantining_them(self):
        with self.assertRaises(ProfileRuleError) as caught:
            ai_service.validate_candidate(self.setup_candidate(), AnimalSources(history='Treatment was reported.'), [])
        self.assertEqual(caught.exception.code, 'UNKNOWN_SOURCE')

    def test_review_only_flags_cannot_hide_reported_medical_facts_and_are_idempotent(self):
        candidate = self.setup_candidate()
        candidate.review_only_evidence_ids = ['medical']  # Model must not suppress a supported medical report.
        sources = AnimalSources(history='Treatment was reported.')
        images = [PreparedImage('photo:1', 'PHOTO_OBSERVATION', b'jpeg')]
        profile = ai_service.validate_candidate(candidate, sources, images)
        again = ai_service.validate_candidate(profile, sources, images)
        self.assertEqual(again.health_and_special_needs, ['medical'])
        self.assertEqual(again.review_only_evidence_ids, ['photo'])
        self.assertEqual(sum(item.category == 'unsupported_conclusion' for item in again.review_warnings), 1)

    def test_age_based_on_review_only_evidence_stays_unknown(self):
        candidate = self.setup_candidate()
        from animal_profile import AgeAssessment
        candidate.age_assessment = AgeAssessment(estimate='An adult.', reference_date=None, photo_reference='photo:1',
            basis_evidence_ids=['photo'], status='qualified_visual_estimate', limitations='Unconfirmed.')
        profile = ai_service.validate_candidate(candidate, AnimalSources(history='Treatment was reported.'),
                                              [PreparedImage('photo:1', 'PHOTO_OBSERVATION', b'jpeg')])
        self.assertEqual(profile.age_assessment.status, 'unknown')

    def test_identity_metadata_misplacement_keeps_an_independent_selected_name_citation(self):
        data = empty_profile().model_dump()
        data['identity'].update(animal_name='Azra', name_evidence_id='label')
        data['evidence'] = [record(id='label', ref='animal_label', quote='Azra', source_type='ANIMAL_LABEL', temporal_status='unclear')]
        data['rescue_background'] = ['label']
        profile = ai_service.validate_candidate(AnimalProfile.model_validate(data), AnimalSources(), [], AnimalContext(animal_name='Azra'))
        self.assertEqual(profile.review_only_evidence_ids, ['label'])
        self.assertEqual(profile.rescue_background, [])
        self.assertEqual(profile.identity.animal_name, 'Azra')
        self.assertNotIn(profile.identity.name_evidence_id, profile.review_only_evidence_ids)

    def test_quarantined_claim_wording_is_not_sent_to_draft_model_or_accepted_as_a_citation(self):
        candidate = profile_fixture()
        candidate.evidence.append(type(candidate.evidence[0]).model_validate(record(id='photo', ref='photo:1',
            quote='UNSUPPORTED_VISUAL_SENTINEL', source_type='PHOTO_OBSERVATION',
            verification_status='interpretation_requires_review', temporal_status='at_sampled_moment')))
        candidate.adoption_considerations.append('photo')
        sources = AnimalSources(history='\n'.join(item.supporting_text_or_description for item in candidate.evidence if item.source_reference == 'history'),
                                updates='Recovery was reported in 2026.')
        profile = ai_service.validate_candidate(candidate, sources, [PreparedImage('photo:1', 'PHOTO_OBSERVATION', b'jpeg')])
        self.assertNotIn('UNSUPPORTED_VISUAL_SENTINEL', json.dumps(build_content_payload(profile, 'third_person')))
        self.assertNotIn('photo', mandatory_evidence(profile))
        validate_drafts(bundle_fixture(), profile, 'third_person')
        bad = bundle_fixture()
        bad.website_nl_1.claims[0].source_references.append('photo')
        with self.assertRaises(ContentError):
            validate_drafts(bad, profile, 'third_person')


@isolated_editorial_storage
class SavedVisualResponseTests(unittest.TestCase):
    def test_existing_rejected_response_can_be_rechecked_without_another_ai_call(self):
        candidate = VisualRoutingTests().setup_candidate()
        failure = ProfileOutputError('evidence', 'VISUAL_FACT', 'Visual claim in a reported section.', candidate=candidate)
        images = [PreparedImage('photo:1', 'PHOTO_OBSERVATION', b'jpeg')]
        with (patch.object(ui, 'load_settings', return_value=AnalysisSettings(model='synthetic')),
              patch.object(ui, 'require_credentials', return_value='synthetic'),
              patch.object(ui, 'prepare_selected_media', return_value=(images, {'photo:1': 'synthetic.jpg'})) as prepare,
              patch.object(ai_service, 'analyze_animal', side_effect=failure) as analyze):
            app = AppTest.from_file(APP, default_timeout=10).run()
            app.text_area(key='animal_history').input('Treatment was reported.').run()
            app.checkbox(key='ai_consent').check().run()
            app.button(key='analyze_animal').click().run()
            self.assertIn('ai_pending_validation', app.session_state)
            pending = app.session_state['ai_pending_validation']
            pending['candidate'].pop('review_only_evidence_ids', None)  # Saved by the previous schema/version.
            app.session_state['ai_pending_validation'] = pending
            app.button(key='recheck_analysis').click().run()
            self.assertFalse(app.exception)
            profile = app.session_state['ai_result']['profile']
            self.assertEqual(profile.health_and_special_needs, ['medical'])
            self.assertEqual(profile.review_only_evidence_ids, ['photo'])
            self.assertTrue(any('Review-only observations' in item.label for item in app.expander))
            analyze.assert_called_once()
            prepare.assert_called_once()


if __name__ == '__main__':
    unittest.main()
