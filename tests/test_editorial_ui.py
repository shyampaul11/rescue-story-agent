"""Safe review markup and explicit, local volunteer review controls."""
from html.parser import HTMLParser
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import editorial_ui as ui


class Markup(HTMLParser):
    def __init__(self, value):
        super().__init__()
        self.tags, self.content, self.spans = [], [], []
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, value):
        self.content.append(value)


class HighlightTests(unittest.TestCase):
    def test_review_status_is_visible_without_certifying_publication(self):
        def app():
            from editorial_ui import render_review
            render_review('sample', 'A claim needing review.', {'status': 'red', 'passages': [], 'findings': []})
        rendered = AppTest.from_function(app).run()
        self.assertFalse(rendered.exception)
        captions = ' '.join(item.value for item in rendered.caption)
        self.assertIn('Review status: Contains unsupported', captions)
        self.assertNotIn('factually verified', captions.lower())

    def test_model_html_is_escaped_and_never_becomes_markup(self):
        text = '<img src=x onerror="steal()"> & paragraph\n\nNext <script>x</script>'
        result = ui.highlighted_html(text, [{'start': 0, 'end': 5, 'color': 'red',
            'label': '<svg/onload=x>', 'reason': '" onmouseover="x'}])
        parsed = Markup(result)
        self.assertTrue(all(tag in ('div', 'p', 'span', 'mark') for tag, _ in parsed.tags))
        self.assertIn('&lt;img', result)
        self.assertNotIn('onmouseover=', result)
        self.assertNotIn('<svg', result)
        self.assertIn('Next &lt;script&gt;x&lt;/script&gt;', result)

    def test_overlap_uses_red_then_yellow_then_green_without_repeating_prose(self):
        result = ui.highlighted_html('ABCDEF', [
            {'start': 0, 'end': 6, 'color': 'green'},
            {'start': 1, 'end': 5, 'color': 'yellow'},
            {'start': 2, 'end': 4, 'color': 'red'}])
        parsed = Markup(result)
        marks = [attrs for tag, attrs in parsed.tags if tag == 'mark']
        self.assertEqual([item['data-review'] for item in marks], ['green', 'yellow', 'red', 'yellow', 'green'])
        self.assertEqual(''.join(parsed.content), 'ABCDEF')
        self.assertTrue(all('aria-label' in item for item in marks))

    def test_paragraphs_and_newlines_survive_and_invalid_spans_are_ignored(self):
        result = ui.highlighted_html('First\nline\n\nSecond & last', [
            {'start': -2, 'end': 4, 'color': 'red'},
            {'start': 2, 'end': 900, 'color': 'yellow'},
            {'start': 0, 'end': 3, 'color': 'purple'},
            {'start': 0, 'end': 3, 'color': ['red']},
            {'start': 'zero', 'end': 2, 'color': 'red'}])
        self.assertEqual(result.count('<p '), 2)
        self.assertIn('First\nline', result)
        self.assertIn('Second &amp; last', result)
        self.assertNotIn('<mark', result)


class PublicationCopyTests(unittest.TestCase):
    def setUp(self):
        from test_content_service import profile_fixture
        self.profile = profile_fixture()

    def test_only_known_explicit_markers_are_removed_without_factual_rewriting(self):
        original = 'Azra is healthy [home].\n\nNew care {old}; Source: new. [not_a_known_id]'
        copied = ui.publication_copy_text(original, self.profile)
        self.assertEqual(copied, 'Azra is healthy .\n\nNew care ; . [not_a_known_id]')
        self.assertIn('[home]', original)

    def test_common_words_remain_but_distinctive_ids_and_known_aliases_are_removed(self):
        self.profile.evidence[1].id = 'rescue_12'
        self.profile.rescue_background = ['rescue_12']
        text = 'An old story has a new home. rescue_12 unknown:0 [warning:0] [update:0]'
        self.assertEqual(ui.publication_copy_text(text, self.profile), 'An old story has a new home.    ')

    def test_id_substrings_and_unrelated_brackets_remain_unchanged(self):
        self.profile.evidence[1].id = 'ev12'
        text = 'ev123 and preev12 and ev12_suffix stay. [a note] Draft 1 has 12 kg.'
        self.assertEqual(ui.publication_copy_text(text, self.profile), text)
        self.assertEqual(ui.publication_copy_text(text, None), text)


def question_app():
    import streamlit as st
    from editorial_ui import render_questions
    record = st.session_state.setdefault('record', {
        'scope': 'a' * 20, 'animal_name': 'Azra',
        'assessment': {'questions': [{'id': 'medical', 'topic': 'health', 'priority': 'high',
            'question': 'What care is needed now?', 'reason': 'Care information is unknown.', 'source_references': []}],
            'drafts': {'website_nl_1': {'findings': [{'color': 'red', 'reason': 'Unsupported health claim'}]}}}})
    render_questions(record, 'a' * 20, None)


def review_app():
    from editorial_ui import render_review
    from test_content_service import profile_fixture
    profile = profile_fixture()
    render_review('website_en_1', 'A quiet home is needed.', {'passages': [
        {'start': 0, 'end': 23, 'color': 'green', 'reason': 'Supported adoption needs.', 'source_references': ['home']}],
        'findings': []}, profile)


def candidate_app():
    from editorial_ui import render_review
    from test_content_service import profile_fixture
    render_review('editor', 'A home needs consideration.', {'passages': [], 'findings': [
        {'id': 'candidate-one', 'color': 'yellow', 'excerpt': 'A home needs consideration.',
         'reason': 'Compare the possible source.', 'source_references': [], 'support_candidates': ['home']}]}, profile_fixture())


class SourcePanelTests(unittest.TestCase):
    def test_supported_passage_shows_source_wording_and_visible_label(self):
        app = AppTest.from_function(review_app).run()
        self.assertFalse(app.exception)
        self.assertTrue(any('Needs a quiet home and continued training.' in item.value for item in app.text))
        self.assertTrue(any('source supported' in item.value.lower() and 'not independently verified' in item.value
                            and 'A quiet home is needed.' in item.value for item in app.text))

    def test_possible_support_is_shown_separately_from_assigned_citations(self):
        app = AppTest.from_function(candidate_app).run()
        self.assertFalse(app.exception)
        self.assertTrue(any('Possible source records for volunteer comparison' in item.value and 'not assigned citations' in item.value
                            for item in app.text))
        self.assertTrue(any('Needs a quiet home and continued training.' in item.value for item in app.text))
        self.assertTrue(any('Sources: No supporting reference' == item.value for item in app.text))


class QuestionUiTests(unittest.TestCase):
    def setUp(self):
        self.copy_patch = patch.object(ui, 'copy_button')
        self.copy = self.copy_patch.start()
        self.addCleanup(self.copy_patch.stop)
        self.app = AppTest.from_function(question_app).run()
        self.prefix = 'writing_question_' + 'a' * 20 + '_' + ui.text_fingerprint('medical')[:16]

    def test_response_is_audited_and_only_explicit_source_action_changes_evidence(self):
        self.app.selectbox(key=self.prefix + '_status').set_value('confirmed_by_rescuer')
        self.app.text_area(key=self.prefix + '_response').input('She currently needs daily medication.')
        self.app.text_area(key=self.prefix + '_note').input('Ask for next check-up date.')
        self.app.text_input(key=self.prefix + '_attribution').input('Mira, rescuer')
        self.app.button(key=self.prefix + '_save').click().run()
        self.assertFalse(self.app.exception)
        record = self.app.session_state['record']
        self.assertEqual(record['confirmations'][0]['rescuer_response'], 'She currently needs daily medication.')
        self.assertEqual(record['confirmations'][0]['volunteer_note'], 'Ask for next check-up date.')
        self.assertTrue(any('Mira, rescuer' in item.value and 'confirmed by rescuer' in item.value for item in self.app.text))
        self.assertFalse(record.get('manual_sources'))
        self.assertEqual(record['assessment']['drafts']['website_nl_1']['findings'][0]['color'], 'red')
        self.app.button(key=self.prefix + '_source').click().run()
        record = self.app.session_state['record']
        self.assertEqual(record['manual_sources'][0]['attribution'], 'Mira, rescuer')
        self.assertEqual(record['manual_sources'][0]['verification_status'], 'reported_not_independently_verified')
        self.assertTrue(record['assessment_stale'])
        self.assertEqual(record['assessment']['drafts']['website_nl_1']['findings'][0]['color'], 'red')
        self.assertTrue(any(call.args[1] == 'What care is needed now?' for call in self.copy.call_args_list))

    def test_source_action_requires_attributed_response_and_preserves_draft(self):
        self.app.button(key=self.prefix + '_source').click().run()
        self.assertFalse(self.app.exception)
        self.assertFalse(self.app.session_state['record'].get('manual_sources'))
        self.assertTrue(any('required' in warning.value for warning in self.app.warning))

    def test_invalid_confirmation_date_does_not_crash_or_save_audit(self):
        self.app.text_input(key=self.prefix + '_date').input('2026-02-31')
        self.app.button(key=self.prefix + '_save').click().run()
        self.assertFalse(self.app.exception)
        self.assertFalse(self.app.session_state['record'].get('confirmations'))
        self.assertTrue(self.app.warning)

    def test_numeric_priorities_put_urgent_questions_first_in_copy_list(self):
        record = self.app.session_state['record']
        record['assessment']['questions'] = [
            {'id': 'coat', 'topic': 'appearance', 'priority': 30, 'question': 'What coat color?'},
            {'id': 'care', 'topic': 'health', 'priority': 10, 'question': 'What care is urgent?'}]
        self.app.run()
        self.assertEqual(self.copy.call_args.args[1], 'What care is urgent?\nWhat coat color?')

    def test_audit_remains_accessible_when_reassessment_removes_questions(self):
        record = self.app.session_state['record']
        record['confirmations'] = [{'confirmation_date': '2026-11-05', 'status': 'corrected',
            'attribution': 'Mira, rescuer', 'rescuer_response': 'Earlier reply retained.'}]
        record['assessment']['questions'] = []
        self.app.run()
        self.assertTrue(any('Earlier reply retained.' in item.value for item in self.app.text))

    def test_annotation_finding_without_question_accepts_status_and_note_without_recoloring(self):
        record = self.app.session_state['record']
        record['assessment']['questions'] = []
        finding = {'id': 'annotation-one', 'code': 'DRAFT_EXCERPT_MISMATCH', 'color': 'yellow',
                   'excerpt': 'Unmatched annotation.', 'reason': 'Annotation does not match the retained draft.'}
        record['assessment']['drafts'] = {'website_nl_1': {'findings': [finding]},
                                         'website_en_1': {'findings': [dict(finding)]}}
        record['final'] = {'nl': 'My unchanged final wording.', 'en': ''}
        self.app.run()
        selector = self.app.selectbox(key='writing_finding_' + 'a' * 20)
        self.assertEqual(len(selector.options), 1)
        prefix = 'writing_finding_response_' + 'a' * 20 + '_' + ui.text_fingerprint('annotation-one')[:16]
        self.app.selectbox(key=prefix + '_status').set_value('corrected')
        self.app.text_area(key=prefix + '_note').input('Fixed the annotation in my manual review.')
        self.app.button(key=prefix + '_save').click().run()
        self.assertFalse(self.app.exception)
        record = self.app.session_state['record']
        self.assertEqual(record['confirmations'][0]['issue_id'], 'annotation-one')
        self.assertEqual(record['confirmations'][0]['status'], 'corrected')
        self.assertEqual(record['confirmations'][0]['volunteer_note'], 'Fixed the annotation in my manual review.')
        self.assertEqual(record['assessment']['drafts']['website_nl_1']['findings'][0]['color'], 'yellow')
        self.assertEqual(record['final']['nl'], 'My unchanged final wording.')
        self.assertFalse(record.get('manual_sources'))


if __name__ == '__main__':
    unittest.main()
