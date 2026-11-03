"""Check the interface without a browser or any paid services."""

from pathlib import Path
import unittest

from streamlit.testing.v1 import AppTest


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


class RescueStoryAppTests(unittest.TestCase):
    def setUp(self):
        self.app = AppTest.from_file(str(APP_PATH)).run()

    def assert_no_errors(self):
        self.assertEqual(len(self.app.exception), 0)

    def test_initial_interface(self):
        self.assert_no_errors()
        self.assertEqual(self.app.title[0].value, "Rescue Story Agent")
        self.assertEqual(
            self.app.subheader[0].value,
            "AI-powered animal adoption content creator",
        )
        self.assertEqual(self.app.text_area[0].label, "Animal History")
        self.assertEqual(
            [button.label for button in self.app.button],
            ["Generate Website Text", "Generate Facebook Text"],
        )
        self.assertNotIn("website_text", self.app.session_state)
        self.assertNotIn("facebook_text", self.app.session_state)

    def test_website_button_shows_dutch_placeholder(self):
        self.app.text_area[0].input("Luna is in foster care.").run()
        self.app.button[0].click().run()
        self.assert_no_errors()
        self.assertIn("Website Text", [item.value for item in self.app.subheader])
        self.assertTrue(
            any("Voorbeeldtekst voor de website" in item.value for item in self.app.markdown)
        )
        self.assertNotIn("facebook_text", self.app.session_state)

    def test_facebook_button_shows_dutch_placeholder(self):
        self.app.text_area[0].input("Luna is in foster care.").run()
        self.app.button[1].click().run()
        self.assert_no_errors()
        self.assertIn("Facebook Text", [item.value for item in self.app.subheader])
        self.assertTrue(
            any("Voorbeeldtekst voor Facebook" in item.value for item in self.app.markdown)
        )
        self.assertNotIn("website_text", self.app.session_state)

    def test_both_previews_remain_visible(self):
        self.app.text_area[0].input("Luna is in foster care.").run()
        self.app.button[0].click().run()
        self.app.button[1].click().run()
        self.assert_no_errors()
        self.assertEqual(
            [item.value for item in self.app.subheader][1:],
            ["Website Text", "Facebook Text"],
        )

    def test_empty_or_whitespace_history_shows_warning(self):
        for history in ("", "   \n "):
            for button_index in (0, 1):
                with self.subTest(history=history, button=button_index):
                    self.app.text_area[0].input(history).run()
                    self.app.button[button_index].click().run()
                    self.assert_no_errors()
                    self.assertEqual(
                        self.app.warning[0].value, "Please enter an animal history first."
                    )
                    self.assertNotIn("website_text", self.app.session_state)
                    self.assertNotIn("facebook_text", self.app.session_state)

    def test_editing_history_clears_previous_previews(self):
        self.app.text_area[0].input("Luna is in foster care.").run()
        self.app.button[0].click().run()
        self.app.button[1].click().run()
        self.app.text_area[0].input("Milo has a different rescue history.").run()
        self.assert_no_errors()
        self.assertNotIn("website_text", self.app.session_state)
        self.assertNotIn("facebook_text", self.app.session_state)


if __name__ == "__main__":
    unittest.main()
