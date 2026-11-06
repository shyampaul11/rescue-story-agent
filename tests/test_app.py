"""Document/manual-source regressions and the Milestone 6 replacement for placeholders."""
from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import patch

from docx import Document
from streamlit.testing.v1 import AppTest

from content_state import writing_scope
from content_ui import editor_key

APP_PATH = Path(__file__).resolve().parents[1] / 'app.py'


class RescueStoryAppTests(unittest.TestCase):
    def setUp(self):
        for item in (patch('content_ui.load_record', return_value=None), patch('content_ui.save_record')):
            item.start()
            self.addCleanup(item.stop)
        self.app = AppTest.from_file(str(APP_PATH), default_timeout=10).run()
        self.scope = writing_scope({})

    def upload_document(self, *paragraphs):
        document = Document()
        for text in paragraphs:
            document.add_paragraph(text)
        output = BytesIO()
        document.save(output)
        self.app.get('file_uploader')[0].upload('sample.docx', output.getvalue()).run()

    def assert_no_errors(self):
        self.assertFalse(self.app.exception)

    def test_upload_populates_editable_history_and_preserves_edits(self):
        self.upload_document('History', 'Luna was rescued.', 'Text for website', 'PRIVATE')
        self.assert_no_errors()
        self.assertEqual(self.app.text_area[0].value, 'Luna was rescued.')
        self.app.text_area[0].input('Edited history.').run()
        self.app.button(key='generate_drafts').click().run()
        self.assert_no_errors()
        self.assertEqual(self.app.text_area[0].value, 'Edited history.')
        self.assertTrue(any('Profile' in error.value for error in self.app.error))

    def test_bad_upload_clears_stale_history_and_previews(self):
        self.app.text_area[0].input('Old story.').run()
        self.app.button(key='generate_drafts').click().run()
        self.upload_document('Text for website', 'PRIVATE')
        self.assert_no_errors()
        self.assertEqual(self.app.text_area[0].value, '')
        self.assertIn('not found', self.app.error[0].value)
        self.assertNotIn('website_text', self.app.session_state)

    def test_replacing_upload_loads_new_history(self):
        self.upload_document('History', 'First story.')
        self.app.button(key='generate_drafts').click().run()
        self.upload_document('History', 'Second story.')
        self.assert_no_errors()
        self.assertEqual(self.app.text_area[0].value, 'Second story.')
        self.assertNotIn('website_text', self.app.session_state)

    def test_initial_interface(self):
        self.assert_no_errors()
        self.assertEqual(self.app.title[0].value, 'Rescue Story Agent')
        self.assertEqual(self.app.subheader[0].value, 'AI-powered animal adoption content creator')
        self.assertEqual(self.app.text_area[0].label, 'Animal History')
        for key in ['connect_drive', 'load_drive_folders', 'analyze_animal', 'generate_drafts']:
            self.assertTrue(self.app.button(key=key))
        self.assertIn('Generate eight drafts', self.app.button(key='generate_drafts').label)
        self.assertIn('paid API request', self.app.button(key='generate_drafts').label)
        self.assertEqual(self.app.segmented_control[0].value, 'Nederlands')
        self.assertNotIn('website_text', self.app.session_state)
        self.assertNotIn('facebook_text', self.app.session_state)

    def test_generation_requires_animal_profile_instead_of_placeholder(self):
        self.app.text_area[0].input('Luna is in foster care.').run()
        self.app.button(key='generate_drafts').click().run()
        self.assert_no_errors()
        self.assertTrue(any('Profile' in error.value for error in self.app.error))
        self.assertNotIn('website_text', self.app.session_state)

    def test_facebook_editor_is_manual_and_independent(self):
        key = editor_key(self.scope, 'facebook', 'nl')
        self.app.text_area(key=key).input('Mijn eigen Facebook-concept.').run()
        self.assert_no_errors()
        self.assertEqual(self.app.text_area(key=key).value, 'Mijn eigen Facebook-concept.')
        self.assertEqual(self.app.text_area(key=editor_key(self.scope, 'final', 'nl')).value, '')

    def test_two_editors_preserve_text_on_reruns(self):
        for target, text in [('final', 'Website-concept.'), ('facebook', 'Facebook-concept.')]:
            self.app.text_area(key=editor_key(self.scope, target, 'nl')).input(text).run()
        self.app.run()
        self.assert_no_errors()
        self.assertEqual(self.app.text_area(key=editor_key(self.scope, 'final', 'nl')).value, 'Website-concept.')
        self.assertEqual(self.app.text_area(key=editor_key(self.scope, 'facebook', 'nl')).value, 'Facebook-concept.')

    def test_empty_or_whitespace_history_still_requires_valid_profile(self):
        for history in ('', '   \n '):
            self.app.text_area[0].input(history).run()
            self.app.button(key='generate_drafts').click().run()
            self.assert_no_errors()
            self.assertTrue(any('Profile' in error.value for error in self.app.error))
            self.assertNotIn('website_text', self.app.session_state)
            self.assertNotIn('facebook_text', self.app.session_state)

    def test_editing_history_keeps_human_writing_for_review(self):
        key = editor_key(self.scope, 'final', 'nl')
        self.app.text_area[0].input('First source.').run()
        self.app.text_area(key=key).input('Keep this human edit.').run()
        self.app.text_area[0].input('Changed source.').run()
        self.assert_no_errors()
        self.assertEqual(self.app.text_area(key=key).value, 'Keep this human edit.')


if __name__ == '__main__':
    unittest.main()
