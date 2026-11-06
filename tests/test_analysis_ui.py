from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.errors import AppTestError

from ai_config import AnalysisError, AnalysisSettings
import analysis_ui as ui
from animal_profile import AnimalSources
from test_ai_service import empty_profile
from editorial_test_support import isolated_editorial_storage


APP = str(Path(__file__).resolve().parents[1] / 'app.py')


@isolated_editorial_storage
class AnalysisUiTests(unittest.TestCase):
    def setUp(self):
        patches = [patch.object(ui, 'load_settings', return_value=AnalysisSettings(model='test-model')),
                   patch.object(ui, 'require_credentials', return_value='synthetic-key'),
                   patch.object(ui, 'prepare_selected_media', return_value=([], {})),
                   patch.object(ui.ai_service, 'analyze_animal', return_value=empty_profile())]
        self.settings, self.credentials, self.prepare, self.analyze = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        self.app = AppTest.from_file(APP, default_timeout=10).run()

    def analyze_once(self):
        self.app.text_area(key='animal_history').input('Azra was rescued.').run()
        self.app.checkbox(key='ai_consent').check().run()
        self.app.button(key='analyze_animal').click().run()
        self.assertFalse(self.app.exception)

    def test_no_call_on_startup_editing_or_consent_alone(self):
        self.assertTrue(self.app.button(key='analyze_animal').disabled)
        self.app.text_area[0].input('History.').run()
        self.app.checkbox(key='ai_consent').check().run()
        self.app.run()
        self.analyze.assert_not_called()
        self.prepare.assert_not_called()

    def test_button_without_consent_never_calls_ai(self):
        with self.assertRaises(AppTestError):
            self.app.button(key='analyze_animal').click()
        self.app.run()
        self.analyze.assert_not_called()

    def test_explicit_action_calls_once_and_reruns_do_not_repeat(self):
        self.analyze_once()
        self.analyze.assert_called_once()
        self.app.run()
        self.app.button(key='generate_drafts').click().run()
        self.app.number_input(key='limit_photo_count').set_value(0).run()
        self.analyze.assert_called_once()
        self.assertIn('ai_result', self.app.session_state)
        self.assertFalse(any('Outdated analysis' in warning.value for warning in self.app.warning))

    def test_each_new_analysis_requires_another_button_click(self):
        self.analyze_once()
        self.app.button(key='analyze_animal').click().run()
        self.assertEqual(self.analyze.call_count, 2)

    def test_three_fields_are_only_ai_text_input(self):
        self.app.text_area(key='animal_history').input('History.').run()
        self.app.text_area(key='animal_other_info').input('Other note.').run()
        self.app.text_area(key='animal_updates').input('04/11/26: Update.').run()
        self.app.session_state['website_text'] = 'EXCLUDED WEBSITE'
        self.app.session_state['facebook_text'] = 'EXCLUDED FB'
        self.app.checkbox(key='ai_consent').check().run()
        self.app.button(key='analyze_animal').click().run()
        sources = self.analyze.call_args.args[0]
        self.assertEqual(sources.model_dump(), {'history': 'History.', 'other_info': 'Other note.', 'updates': '04/11/26: Update.'})

    def test_text_change_marks_saved_result_outdated_without_call(self):
        self.analyze_once()
        for key in ['animal_history', 'animal_other_info', 'animal_updates']:
            self.app.text_area(key=key).input('Changed source.').run()
            self.assertTrue(any('Outdated analysis' in warning.value for warning in self.app.warning))
        self.analyze.assert_called_once()
        self.assertIn('ai_result', self.app.session_state)

    def test_reverting_edits_still_requires_explicit_new_analysis(self):
        self.analyze_once()
        self.app.text_area(key='animal_history').input('Different source.').run()
        self.app.text_area(key='animal_history').input('Azra was rescued.').run()
        self.assertTrue(any('Outdated analysis' in warning.value for warning in self.app.warning))
        self.app.button(key='analyze_animal').click().run()
        self.assertFalse(any('Outdated analysis' in warning.value for warning in self.app.warning))

    def test_animal_switch_marks_result_outdated(self):
        self.analyze_once()
        self.app.session_state['media_animal'] = {'id': 'different', 'name': 'Different animal'}
        self.app.run()
        self.assertFalse(self.app.exception)
        self.assertTrue(any('Outdated analysis' in warning.value for warning in self.app.warning))
        self.analyze.assert_called_once()

    def test_media_change_and_settings_change_fingerprint(self):
        sources = AnimalSources(history='History.')
        settings = AnalysisSettings(model='test-model')
        file = {'id': 'photo', 'name': 'local.jpg', 'kind': 'image', 'size': '10', 'modifiedTime': 'v1'}
        original = ui.input_fingerprint(sources, [file], 'azra', settings)
        for files, context, config in [([], 'azra', settings), ([dict(file, modifiedTime='v2')], 'azra', settings),
                                       ([file], 'luna', settings), ([file], 'azra', replace(settings, image_side=768))]:
            self.assertNotEqual(original, ui.input_fingerprint(sources, files, context, config))

    def test_invalid_selection_marks_previous_result_outdated(self):
        self.analyze_once()
        self.app.session_state['media_selected'] = ['no-longer-listed']
        self.app.run()
        self.assertTrue(self.app.button(key='analyze_animal').disabled)
        self.assertTrue(any('Outdated analysis' in warning.value for warning in self.app.warning))
        self.analyze.assert_called_once()

    def test_missing_key_stops_before_media_download(self):
        self.credentials.side_effect = AnalysisError('OPENAI_API_KEY is missing.')
        self.analyze_once()
        self.prepare.assert_not_called()
        self.analyze.assert_not_called()
        self.assertTrue(any('OPENAI_API_KEY is missing' in error.value for error in self.app.error))

    def test_api_failure_keeps_previous_result_and_does_not_retry(self):
        self.analyze_once()
        self.analyze.side_effect = AnalysisError('Safe API failure.')
        self.app.button(key='analyze_animal').click().run()
        self.app.run()
        self.assertEqual(self.analyze.call_count, 2)
        self.assertIn('ai_result', self.app.session_state)
        self.assertTrue(any('Safe API failure' in error.value for error in self.app.error))

    def test_video_failure_prevents_billable_request(self):
        self.prepare.side_effect = AnalysisError('Video processing timed out.')
        self.analyze_once()
        self.analyze.assert_not_called()
        self.assertTrue(any('timed out' in error.value for error in self.app.error))

    def test_selected_photo_transport_failure_prevents_ai_call_and_preserves_result(self):
        import drive_client as drive
        import drive_media
        from analysis_media import prepare_selected_media

        self.analyze_once()
        self.analyze.reset_mock()
        self.app.session_state['media_catalog'] = {'folders': [], 'files': [
            {'id': 'photo', 'name': 'photo.jpg', 'kind': 'image', 'mimeType': 'image/jpeg',
             'size': '100', 'folder': 'Photos'}]}
        self.app.session_state['media_selected'] = ['photo']
        self.app.session_state['media_animal'] = {'id': 'synthetic', 'name': 'Azra'}
        self.app.session_state['limit_photo_count'] = 0
        self.prepare.side_effect = prepare_selected_media
        with patch.object(drive_media, 'download_media', side_effect=drive.DriveError(drive.INTERRUPTED)):
            self.app.button(key='analyze_animal').click().run()
        self.analyze.assert_not_called()
        self.assertIn('ai_result', self.app.session_state)
        self.assertTrue(any('interrupted' in item.value for item in self.app.error),
                        [item.value for item in self.app.error])
        self.assertFalse(self.app.exception)

    def test_section_views_and_json_display(self):
        self.analyze_once()
        labels = [item.label for item in self.app.expander]
        for label in ['Rescue background', 'Personality and behavior', 'Visual appearance',
                      'Health and special needs', 'Adoption considerations', 'Updates',
                      'Unknown information', 'Review warnings', 'Animal Profile JSON (debugging and evaluation)']:
            self.assertIn(label, labels)
        self.assertTrue(self.app.json)


if __name__ == '__main__':
    unittest.main()
