"""Synthetic/local OCR and guarded source-import regressions. No paid API requests."""
from io import BytesIO
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from editorial_test_support import isolated_editorial_storage
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont
from docx import Document
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.errors import AppTestError
import streamlit as st

import screenshot_ocr as ocr
import message_import_ui as ui
import analysis_ui
import drive_client as drive
import drive_media
from ai_config import AnalysisSettings
from animal_profile import AnimalSources
from ai_service import build_payload
from test_ai_service import empty_profile


APP = str(Path(__file__).resolve().parents[1] / 'app.py')


def screenshot(size=(1000, 220), format='PNG', with_text=True):
    image = Image.new('RGB', size, 'white')
    if with_text:
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 34)
        except OSError:
            font = ImageFont.load_default(size=34)
        draw.text((30, 25), 'Azra is in foster care.', font=font, fill='black')
        draw.text((30, 90), '04/11/2026: She is eating well.', font=font, fill='black')
    buffer = BytesIO()
    image.save(buffer, format=format)
    return buffer.getvalue()


class OcrServiceTests(unittest.TestCase):
    def test_real_local_ocr_reads_synthetic_message_and_date(self):
        result = ocr.extract_screenshot_text(screenshot())
        self.assertIn('Azra is in foster care.', result.text)
        self.assertIn('04/11/2026', result.text)
        self.assertIn('She is eating well.', result.text)
        self.assertGreaterEqual(result.line_count, 2)

    def test_prepare_image_bounds_and_strips_metadata(self):
        data = ocr.prepare_screenshot(screenshot((3000, 1500), with_text=False))
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.size, (2400, 1200))
            self.assertEqual(image.format, 'PNG')
            self.assertFalse(image.getexif())

    def test_damaged_unsupported_and_over_limit_images(self):
        for data, message in [(b'broken', 'damaged'), (screenshot(format='GIF'), 'PNG, JPEG or WebP'),
                              (b'x' * (ocr.MAX_BYTES + 1), '8 MB'), (b'', 'non-empty'),
                              (screenshot((4001, 4000), with_text=False), '16-megapixel')]:
            with self.subTest(message=message), self.assertRaisesRegex(ocr.OcrError, message):
                ocr.prepare_screenshot(data)

    def test_line_order_dates_negations_and_uncertain_lines_preserved(self):
        result = ocr.decode_result([{'text': '04/11/2026', 'confidence': 0.95},
                                    {'text': 'She is not vaccinated.', 'confidence': 0.4}])
        self.assertEqual(result.text, '04/11/2026\nShe is not vaccinated.')
        self.assertEqual(result.low_confidence_lines, 1)

    def test_empty_malformed_and_oversized_ocr_output_fail_safely(self):
        for value in [[], {}, [{'text': 'SECRET', 'confidence': float('nan')}],
                      [{'text': 'x' * 30001, 'confidence': 0.9}]]:
            with self.subTest(value_type=type(value)), self.assertRaises(ocr.OcrError) as caught:
                ocr.decode_result(value)
            self.assertNotIn('SECRET', str(caught.exception))

    @patch.object(ocr.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ocr', 45))
    def test_hung_engine_terminated_and_temporary_files_cleaned(self, run):
        with self.assertRaisesRegex(ocr.OcrError, '45-second'):
            ocr.extract_screenshot_text(screenshot())
        self.assertEqual(run.call_args.kwargs['timeout'], 45)
        self.assertFalse(Path(run.call_args.args[0][2]).exists())

    @patch.object(ocr.subprocess, 'run', return_value=SimpleNamespace(returncode=2))
    def test_missing_engine_has_install_instructions(self, run):
        with self.assertRaisesRegex(ocr.OcrError, 'Install requirements.txt'):
            ocr.extract_screenshot_text(screenshot())

    @patch.object(ocr.subprocess, 'run', return_value=SimpleNamespace(returncode=1))
    def test_engine_failure_is_safe(self, run):
        with self.assertRaisesRegex(ocr.OcrError, 'Local OCR could not read'):
            ocr.extract_screenshot_text(screenshot())

    def test_append_replace_and_verbatim_optional_date(self):
        self.assertEqual(ocr.merge_reviewed_text('Existing.', 'New note.', 'Append'), 'Existing.\n\nNew note.')
        self.assertEqual(ocr.merge_reviewed_text('Existing.', 'New note.', 'Replace'), 'New note.')
        self.assertEqual(ocr.merge_reviewed_text('', 'New note.', 'Append', '04/11/26'), '04/11/26\nNew note.')
        self.assertEqual(ocr.merge_reviewed_text('', '04/11/26: New note.', 'Append', '04/11/26'), '04/11/26: New note.')

    def test_empty_or_invalid_import_cannot_destroy_destination(self):
        for reviewed, mode, date in [('', 'Replace', ''), ('New.', 'Unexpected', ''), ('New.', 'Append', 'x\ny')]:
            with self.assertRaises(ocr.OcrError):
                ocr.merge_reviewed_text('Existing.', reviewed, mode, date)


@isolated_editorial_storage
class OcrImportUiTests(unittest.TestCase):
    def setUp(self):
        self.ocr = patch.object(ui, 'extract_screenshot_text', return_value=ocr.OcrResult(
            '04/11/2026: She is not vaccinated.', 0.75, 1, 1))
        self.extract = self.ocr.start()
        self.addCleanup(self.ocr.stop)
        self.ai = patch.object(analysis_ui.ai_service, 'analyze_animal', return_value=empty_profile())
        self.analyze = self.ai.start()
        self.addCleanup(self.ai.stop)
        self.app = AppTest.from_file(APP, default_timeout=10).run()

    def upload(self):
        self.app.get('file_uploader')[1].upload('PRIVATE_CHAT_NAME.png', screenshot()).run()
        self.assertFalse(self.app.exception)

    def extract_once(self):
        self.upload()
        self.app.button(key='ocr_extract').click().run()
        self.assertFalse(self.app.exception)

    def apply(self, target='History', mode='Append'):
        self.app.selectbox(key='ocr_target').select(target).run()
        self.app.radio(key='ocr_mode').set_value(mode).run()
        self.app.checkbox(key='ocr_reviewed').check().run()
        self.app.button(key='ocr_apply').click().run()
        self.assertFalse(self.app.exception)

    def test_upload_alone_never_extracts_or_calls_ai_or_changes_sources(self):
        self.app.text_area(key='animal_history').input('Existing history.').run()
        self.upload()
        self.app.run()
        self.extract.assert_not_called()
        self.analyze.assert_not_called()
        self.assertEqual(self.app.text_area(key='animal_history').value, 'Existing history.')

    def test_extraction_is_explicit_and_draft_does_not_change_sources(self):
        self.extract_once()
        self.app.run()
        self.extract.assert_called_once()
        self.analyze.assert_not_called()
        self.assertEqual(self.app.text_area(key='animal_history').value, '')
        self.assertEqual(self.app.text_area(key='ocr_reviewed_text').value, '04/11/2026: She is not vaccinated.')
        self.assertTrue(self.app.button(key='ocr_apply').disabled)
        with self.assertRaises(AppTestError):
            self.app.button(key='ocr_apply').click()

    def test_reviewed_text_can_be_routed_to_each_permitted_section(self):
        self.extract_once()
        for target, key in ui.TARGETS.items():
            self.apply(target)
            self.assertEqual(self.app.text_area(key=key).value, '04/11/2026: She is not vaccinated.')
        self.analyze.assert_not_called()

    def test_append_keeps_existing_text_and_replace_changes_only_chosen_field(self):
        self.app.text_area(key='animal_history').input('Existing history.').run()
        self.app.text_area(key='animal_other_info').input('Existing other info.').run()
        self.extract_once()
        self.apply()
        self.assertEqual(self.app.text_area(key='animal_history').value,
                         'Existing history.\n\n04/11/2026: She is not vaccinated.')
        self.apply('Other Info', 'Replace')
        self.assertEqual(self.app.text_area(key='animal_other_info').value, '04/11/2026: She is not vaccinated.')
        self.assertTrue(self.app.text_area(key='animal_history').value.startswith('Existing history.'))

    def test_editing_review_text_resets_confirmation(self):
        self.extract_once()
        self.app.checkbox(key='ocr_reviewed').check().run()
        self.app.text_area(key='ocr_reviewed_text').input('Corrected text.').run()
        self.assertFalse(self.app.checkbox(key='ocr_reviewed').value)
        self.assertTrue(self.app.button(key='ocr_apply').disabled)
        self.apply('Updates')
        self.assertEqual(self.app.text_area(key='animal_updates').value, 'Corrected text.')

    def test_message_date_preserved_without_using_upload_date(self):
        self.extract_once()
        self.app.text_area(key='ocr_reviewed_text').input('Recovery reported.').run()
        self.app.text_input(key='ocr_message_date').input('05/06/26').run()
        self.apply('Updates')
        self.assertEqual(self.app.text_area(key='animal_updates').value, '05/06/26\nRecovery reported.')

    def test_reruns_do_not_repeat_ocr_or_append_and_duplicate_action_is_blocked(self):
        self.extract_once()
        self.apply()
        before = self.app.text_area(key='animal_history').value
        self.app.run()
        self.app.run()
        self.extract.assert_called_once()
        self.assertEqual(self.app.text_area(key='animal_history').value, before)
        self.apply()
        self.assertEqual(self.app.text_area(key='animal_history').value, before)
        self.assertTrue(any('already imported' in error.value for error in self.app.error))

    def test_failed_ocr_preserves_existing_sources(self):
        self.app.text_area(key='animal_history').input('Existing.').run()
        self.extract.side_effect = ocr.OcrError('No readable text found.')
        self.extract_once()
        self.assertEqual(self.app.text_area(key='animal_history').value, 'Existing.')
        self.assertNotIn('ocr_result', self.app.session_state)
        self.analyze.assert_not_called()

    def test_result_finishing_after_context_change_is_discarded(self):
        result = self.extract.return_value
        def context_changed_during_ocr(data):
            st.session_state.ocr_upload_version = 1
            return result
        self.extract.side_effect = context_changed_during_ocr
        self.extract_once()
        self.assertNotIn('ocr_result', self.app.session_state)
        self.assertEqual(self.app.text_area(key='animal_history').value, '')
        self.analyze.assert_not_called()

    def test_document_change_clears_ocr_draft_and_rotates_upload(self):
        self.extract_once()
        document = Document()
        document.add_paragraph('History: A different animal.')
        output = BytesIO()
        document.save(output)
        self.app.get('file_uploader')[0].upload('new.docx', output.getvalue()).run()
        self.assertFalse(self.app.exception)
        self.assertNotIn('ocr_result', self.app.session_state)
        self.assertEqual(self.app.text_area(key='animal_history').value, 'A different animal.')
        self.assertEqual(self.app.get('file_uploader')[1].key, 'ocr_screenshot_1')

    def test_animal_change_clears_screenshot_draft(self):
        self.extract_once()
        self.app.session_state['drive_folders'] = [{'id': 'azra', 'name': 'Azra (Sophie)'}]
        self.app.session_state['drive_service'] = object()
        self.app.run()
        with (patch.object(drive, 'load_animal', return_value={'history': 'Azra story.', 'animal': 'Azra', 'document': 'new.docx'}),
              patch.object(drive_media, 'discover_media', return_value={'files': [], 'folders': []})):
            self.app.selectbox(key='drive_animal').select('azra').run()
        self.assertFalse(self.app.exception)
        self.assertNotIn('ocr_result', self.app.session_state)
        self.assertEqual(self.app.text_area(key='animal_history').value, 'Azra story.')

    def test_import_marks_ai_result_outdated_without_another_ai_call(self):
        with (patch.object(analysis_ui, 'load_settings', return_value=AnalysisSettings(model='synthetic')),
              patch.object(analysis_ui, 'require_credentials', return_value='synthetic'),
              patch.object(analysis_ui, 'prepare_selected_media', return_value=([], {}))):
            self.app.text_area(key='animal_history').input('Existing.').run()
            self.app.checkbox(key='ai_consent').check().run()
            self.app.button(key='analyze_animal').click().run()
            self.extract_once()
            self.apply('Updates')
            self.assertTrue(any('Outdated analysis' in warning.value for warning in self.app.warning))
            self.analyze.assert_called_once()

    def test_only_reviewed_section_text_enters_ai_payload(self):
        self.extract_once()
        self.apply('Updates')
        sources = AnimalSources(history=self.app.text_area(key='animal_history').value,
                                other_info=self.app.text_area(key='animal_other_info').value,
                                updates=self.app.text_area(key='animal_updates').value)
        payload = json.dumps(build_payload(sources, [], AnalysisSettings()))
        self.assertIn('She is not vaccinated.', payload)
        self.assertNotIn('PRIVATE_CHAT_NAME', payload)
        self.assertNotIn('data:image', payload)
        self.assertNotIn('media_selected', self.app.session_state)


if __name__ == '__main__':
    unittest.main()
