"""Private retained-response inspection never authorizes or publishes a draft."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import analysis_ui
import content_state
import content_ui as ui
from content_models import DRAFT_KEYS
from content_service import ContentSettings, ContentValidationError, profile_fingerprint
from content_state import export_workspace, workspace, writing_scope
from test_content_service import profile_fixture, bundle_fixture


APP = str(Path(__file__).resolve().parents[1] / 'app.py')
SECRET = 'sk-privatecredential123456'


def registry_unknown():
    return {'reference': 'unknown:0', 'text': 'Compatibility with cats has not been assessed.',
            'topics': ['cats'], 'state': 'not_assessed', 'scope': 'current',
            'subject': 'selected_animal', 'source_type': 'OTHER_INFO_FACT',
            'verification_status': 'reported_not_independently_verified',
            'temporal_status': 'current_as_reported'}


def retained_record(profile):
    record = workspace({}, 'private')
    candidate = bundle_fixture().model_dump(mode='json')
    for key in DRAFT_KEYS:
        candidate[key]['text'] = 'PRIVATE REJECTED ' + key + ' <script>alert("review")</script>'
        candidate[key]['claims'] = [{'excerpt': 'PRIVATE annotation ' + key,
                                    'qualification': 'qualified',
                                    'source_references': ['exact-ref:' + key]}]
        candidate[key]['review_notes'] = ['PRIVATE note ' + key]
    record['pending_response'] = {'candidate': candidate,
        'usage': {'input_tokens': 101, 'output_tokens': 202, 'total_tokens': 303},
        'profile_fingerprint': profile_fingerprint(profile), 'analysis_fingerprint': 'analysis',
        'perspective': 'third_person', 'settings': {'model': 'synthetic', 'output_tokens': 16000},
        'received_at': '2026-11-05T00:00:00+00:00'}
    record['validation_report'] = [{'code': 'UNSUPPORTED_UNKNOWN', 'draft': 'website_en_1',
        'language': 'English', 'claim_number': 1, 'excerpt': 'PRIVATE annotation website_en_1',
        'origin': 'generated_text', 'source_references': ['unknown:0'],
        'reason': 'Review the unknown wording.', 'action': 'Keep this draft blocked.',
        'failure_type': 'claim_support', 'classification': 'annotation_mismatch',
        'claim_qualification': 'qualified',
        'relevant_unknowns': [{'reference': 'unknown:0', 'text': 'Compatibility with cats is unknown.',
                               'topics': ['cats'], 'status': 'unknown'}],
        'relevant_evidence': [{'id': 'name', 'subject': 'selected_animal', 'temporal_status': 'current',
            'claim': 'Name is reported.', 'source_reference': 'source:1',
            'supporting_text_or_description': 'The selected animal name.',
            'verification_status': 'reported_source_fact'}]}]
    record['final'] = {'nl': 'Keep my Dutch final.', 'en': 'Keep my English final.'}
    record['facebook'] = {'nl': 'Keep my Dutch post.', 'en': 'Keep my English post.'}
    return record


class PrivateDiagnosticExportTests(unittest.TestCase):
    def setUp(self):
        self.profile = profile_fixture()
        self.record = retained_record(self.profile)

    def export(self):
        exporter = getattr(content_state, 'export_private_diagnostic', None)
        self.assertTrue(callable(exporter), 'A separate explicit private diagnostic export is required.')
        return json.loads(exporter(self.record, self.profile))

    def test_private_export_contains_all_eight_candidates_and_only_allowlisted_context(self):
        self.record['oauth_token'] = 'DO NOT EXPORT SESSION'
        self.record['pending_response']['settings']['api_key'] = 'DO NOT EXPORT SETTINGS'
        self.record['pending_response']['candidate']['oauth'] = 'DO NOT EXPORT CANDIDATE EXTRAS'
        self.record['pending_response']['usage']['provider_body'] = 'DO NOT EXPORT USAGE EXTRAS'
        data = self.export()
        self.assertFalse(data['approved'])
        self.assertIn('Private', data['status'])
        self.assertIn('not approved', data['status'])
        self.assertEqual(data['profile']['identity']['animal_name'], 'Azra')
        self.assertEqual(data['token_usage'], {'input_tokens': 101, 'output_tokens': 202, 'total_tokens': 303})
        self.assertEqual(data['validation_report'][0]['classification'], 'annotation_mismatch')
        for key in DRAFT_KEYS:
            self.assertIn('PRIVATE REJECTED ' + key, data['candidate'][key]['text'])
            self.assertEqual(data['candidate'][key]['claims'][0]['source_references'], ['exact-ref:' + key])
        self.assertNotIn('DO NOT EXPORT', json.dumps(data))

    def test_private_export_redacts_credentials_recursively_without_mutating_originals(self):
        self.profile.unknown_information.append('PRIVATE profile ' + SECRET)
        self.profile.evidence[0].source_reference = 'Bearer oauth-secret-reference'
        candidate = self.record['pending_response']['candidate']
        candidate['website_en_1']['text'] += ' ' + SECRET
        candidate['website_nl_2']['claims'][0]['source_references'].append('ya29.private-oauth-token')
        candidate['facebook_en']['review_notes'].append('sess-privatecredential1234')
        self.record['validation_report'][0]['relevant_unknowns'][0]['text'] += ' ' + SECRET
        self.record['validation_report'][0]['reason'] += ' Bearer private-report-token'
        before_record, before_profile = copy.deepcopy(self.record), self.profile.model_dump(mode='json')
        serialized = json.dumps(self.export())
        for secret in (SECRET, 'oauth-secret-reference', 'ya29.private-oauth-token',
                       'sess-privatecredential1234', 'private-report-token'):
            self.assertNotIn(secret, serialized)
        self.assertIn('[credential removed]', serialized)
        self.assertEqual(self.record, before_record)
        self.assertEqual(self.profile.model_dump(mode='json'), before_profile)

    def test_normal_export_excludes_rejected_candidate_and_private_report(self):
        data = json.loads(export_workspace(self.record))
        self.assertEqual(data['generated_drafts'], {})
        self.assertEqual(data['evidence_annotations'], {})
        self.assertEqual(data['final_website_nl'], 'Keep my Dutch final.')
        self.assertNotIn('PRIVATE', json.dumps(data))

    def test_token_usage_rejects_non_counts(self):
        self.record['pending_response']['usage'] = {'input_tokens': SECRET, 'output_tokens': -1,
                                                   'total_tokens': True, 'oauth': 'irrelevant'}
        self.assertEqual(self.export()['token_usage'], {})

    def test_registry_unknown_metadata_survives_private_export_without_extra_context(self):
        entry = registry_unknown()
        self.record['validation_report'][0]['relevant_unknowns'] = [dict(entry, oauth='DO NOT EXPORT')]
        exported = self.export()['validation_report'][0]['relevant_unknowns'][0]
        self.assertEqual(exported, entry)


class PrivateDiagnosticUiTests(unittest.TestCase):
    def setUp(self):
        self.profile = profile_fixture()
        patches = [patch.object(analysis_ui, 'render_analysis', return_value=None),
                   patch.object(ui, 'load_content_settings', return_value=ContentSettings(model='synthetic')),
                   patch.object(ui, 'generate_drafts', side_effect=AssertionError('Unexpected provider call')),
                   patch.object(ui, 'copy_button')]
        self.analysis, self.settings, self.generate, self.copy_button = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        for item in (patch.object(ui, 'load_record', return_value=None), patch.object(ui, 'save_record')):
            item.start()
            self.addCleanup(item.stop)
        self.app = AppTest.from_file(APP, default_timeout=10).run()
        self.scope = writing_scope({}, self.profile)
        self.app.session_state['ai_result'] = {'profile': self.profile, 'fingerprint': 'analysis'}
        self.app.session_state['ai_outdated'] = False
        record = retained_record(self.profile)
        record['error_analysis_id'] = 'analysis'
        self.app.session_state['writing_workspaces'] = {self.scope: record}
        self.app.run()

    def record(self):
        return self.app.session_state['writing_workspaces'][self.scope]

    def test_all_eight_texts_and_annotations_are_private_without_copy_or_approval(self):
        self.assertFalse(self.app.exception)
        panels = [item for item in self.app.expander if 'retained response' in item.label.casefold()]
        self.assertEqual(len(panels), 1, 'All eight rejected drafts need a clearly private inspection expander.')
        self.assertIn('private', panels[0].label.casefold())
        text = '\n'.join(item.value for item in panels[0].text)
        for key in DRAFT_KEYS:
            self.assertIn('PRIVATE REJECTED ' + key, text)
            self.assertIn('PRIVATE annotation ' + key, text)
            self.assertIn('exact-ref:' + key, text)
        self.assertIn('Dutch', text)
        self.assertIn('English', text)
        self.assertIn('Website draft 1', text)
        self.assertIn('Facebook draft', text)
        self.assertIn('qualified', text)
        self.assertIn('<script>alert("review")</script>', text)
        self.assertFalse(panels[0].button)
        self.assertFalse(panels[0].text_area)
        self.assertFalse(any('PRIVATE' in str(call.args) for call in self.copy_button.call_args_list))
        self.assertIsNone(self.record()['bundle'])

    def test_report_shows_classification_qualification_unknowns_and_source_status(self):
        values = '\n'.join(item.value for item in self.app.text)
        for text in ('annotation_mismatch', 'qualified', 'unknown:0',
                     'Compatibility with cats is unknown.', 'reported_source_fact'):
            self.assertIn(text, values)

    def test_report_displays_registry_state_scope_and_source_verification(self):
        self.record()['validation_report'][0]['relevant_unknowns'] = [registry_unknown()]
        self.app.run()
        panel = next(item for item in self.app.expander if item.label == 'Rejected claims — private review')
        values = '\n'.join(item.value for item in panel.text)
        self.assertIn('Status: not_assessed', values)
        self.assertIn('Scope: current', values)
        self.assertIn('Source verification status: reported_not_independently_verified', values)
        self.assertIn('Compatibility with cats has not been assessed.', values)
        self.assertFalse(self.app.exception)

    def test_inspection_download_and_rerun_preserve_blocked_response_and_edits(self):
        before, profile_before = copy.deepcopy(self.record()), self.profile.model_dump(mode='json')
        with patch.object(ui.st, 'download_button', wraps=ui.st.download_button) as download:
            self.app.run()
        calls = [call for call in download.call_args_list
                 if call.args and call.args[0] == 'Download private rejection diagnostic (JSON)']
        self.assertEqual(len(calls), 1, 'Private diagnostics need their own explicit download control.')
        self.assertEqual(calls[0].kwargs['on_click'], 'ignore')
        self.assertFalse(json.loads(calls[0].kwargs['data'])['approved'])
        self.assertEqual(self.record(), before)
        self.assertEqual(self.profile.model_dump(mode='json'), profile_before)
        self.generate.assert_not_called()
        self.assertEqual(self.app.text_area(key=ui.editor_key(self.scope, 'final', 'nl')).value, 'Keep my Dutch final.')
        self.assertEqual(self.app.text_area(key=ui.editor_key(self.scope, 'facebook', 'nl')).value, 'Keep my Dutch post.')

    def test_private_panel_redacts_candidate_and_report_and_does_not_use_html(self):
        self.record()['pending_response']['candidate']['website_en_1']['text'] += ' ' + SECRET
        self.record()['validation_report'][0]['relevant_evidence'][0]['source_reference'] += ' ' + SECRET
        self.app.run()
        values = '\n'.join(item.value for item in self.app.text)
        self.assertNotIn(SECRET, values)
        self.assertIn('[credential removed]', values)
        self.assertFalse(any('PRIVATE REJECTED' in item.value for item in self.app.markdown))
        self.assertFalse(self.app.exception)


if __name__ == '__main__':
    unittest.main()
