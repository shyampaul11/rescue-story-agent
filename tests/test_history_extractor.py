"""Generate real DOCX samples in memory; no private rescue records are used."""

from io import BytesIO
import unittest

from docx import Document

from history_extractor import HistoryError, extract_history, extract_animal_info


def docx_bytes(document):
    output = BytesIO()
    document.save(output)
    return output.getvalue()


class HistoryExtractionTests(unittest.TestCase):
    def test_requested_optional_rows_only_and_dates_preserved(self):
        document = Document()
        table = document.add_table(rows=6, cols=2)
        for row, (label, value) in zip(table.rows, [
            ('History', 'A rescued dog.'), (' OTHER INFO: ', 'A short extra note'),
            ('Updates (please note the date of the update)', '04/11/2026: First update\n05/11/2026: Second update'),
            ('Social with dogs', 'PRIVATE SOCIAL'), ('Text for website', 'PRIVATE WEBSITE'),
            ('Text for Facebook', 'PRIVATE FACEBOOK'),
        ]):
            row.cells[0].text = label
            row.cells[1].text = value
        result = extract_animal_info(docx_bytes(document))
        self.assertEqual(result, {'history': 'A rescued dog.', 'other_info': 'A short extra note',
                                  'updates': '04/11/2026: First update\n05/11/2026: Second update'})

    def test_optional_missing_empty_or_undated(self):
        self.assertEqual(extract_animal_info(self.paragraphs('History: A rescued dog.')),
                         {'history': 'A rescued dog.', 'other_info': '', 'updates': ''})
        document = Document()
        table = document.add_table(rows=3, cols=2)
        for row, (label, value) in zip(table.rows, [('History', 'A rescued dog.'),
                                                  ('Other info', ''), ('UPDATES:', 'An undated note')]):
            row.cells[0].text = label
            row.cells[1].text = value
        result = extract_animal_info(docx_bytes(document))
        self.assertEqual(result['updates'], 'An undated note')
        self.assertEqual(result['other_info'], '')

    def test_optional_duplicate_ambiguous_and_nested_rows_rejected(self):
        for layout in ('duplicate', 'three columns', 'nested'):
            document = Document()
            document.add_paragraph('History: A rescued dog.')
            table = document.add_table(rows=2 if layout == 'duplicate' else 1,
                                       cols=3 if layout == 'three columns' else 2)
            for row in table.rows:
                row.cells[0].text = 'Other info'
                row.cells[1].text = 'A note'
            if layout == 'nested':
                table.cell(0, 1).add_table(rows=1, cols=1)
            with self.assertRaises(HistoryError):
                extract_animal_info(docx_bytes(document))

    def paragraphs(self, *texts):
        document = Document()
        for text in texts:
            document.add_paragraph(text)
        return docx_bytes(document)

    def test_paragraphs_preserve_breaks(self):
        data = self.paragraphs('Name: Luna', 'History', 'Luna was rescued.',
                               'She now lives in foster care.', 'Social with other dogs', 'PRIVATE')
        self.assertEqual(extract_history(data), 'Luna was rescued.\n\nShe now lives in foster care.')

    def test_capitalization_and_inline_text(self):
        for heading in ('HISTORY', 'history', 'HiStOrY:'):
            self.assertEqual(extract_history(self.paragraphs(heading, 'A rescued dog.')), 'A rescued dog.')
        self.assertEqual(extract_history(self.paragraphs('History: A rescued dog.')), 'A rescued dog.')

    def test_two_column_table(self):
        document = Document()
        table = document.add_table(rows=3, cols=2)
        table.cell(0, 0).text = 'Name'
        table.cell(0, 1).text = 'PRIVATE NAME'
        table.cell(1, 0).text = 'hIsToRy:'
        table.cell(1, 1).text = 'Luna was rescued.'
        table.cell(1, 1).add_paragraph('She is in foster care.')
        table.cell(2, 0).text = 'Social with other dogs'
        table.cell(2, 1).text = 'PRIVATE SOCIAL'
        self.assertEqual(extract_history(docx_bytes(document)), 'Luna was rescued.\n\nShe is in foster care.')

    def test_history_value_cell_uses_cell_boundary_not_sentence_heuristics(self):
        document = Document()
        table = document.add_table(rows=4, cols=2)
        table.cell(0, 0).text = 'History'
        paragraphs = ['A rescued dog without ending punctuation', 'A short reply',
                      'She is around 18 kg', 'Follow-up notes',
                      'Care plan: Discussed with the foster family.',
                      'Name mentioned in the history', 'A final paragraph.']
        cell = table.cell(0, 1)
        cell.text = paragraphs[0]
        for text in paragraphs[1:]:
            cell.add_paragraph(text)
        cell.paragraphs[3].style = 'Heading 2'
        for row, label in zip(table.rows[1:], ['Social with other dogs', 'Text for website', 'Text for Facebook']):
            row.cells[0].text = label
            row.cells[1].text = 'PRIVATE OTHER SECTION'
        self.assertEqual(extract_history(docx_bytes(document)), '\n\n'.join(paragraphs))

    def test_history_value_cell_rejects_nested_tables_and_duplicate_history(self):
        for nested in (True, False):
            document = Document()
            table = document.add_table(rows=1, cols=2)
            table.cell(0, 0).text = 'History'
            cell = table.cell(0, 1)
            cell.text = 'A rescued dog.'
            if nested:
                cell.add_table(rows=1, cols=1)
            else:
                cell.add_paragraph('History')
                cell.add_paragraph('Another story.')
            with self.assertRaises(HistoryError):
                extract_history(docx_bytes(document))

    def test_history_and_sections_in_single_cell(self):
        document = Document()
        cell = document.add_table(rows=1, cols=1).cell(0, 0)
        cell.text = 'History'
        cell.add_paragraph('Luna was rescued.')
        cell.add_paragraph('Text for Facebook/WhatsApp')
        cell.add_paragraph('PRIVATE')
        self.assertEqual(extract_history(docx_bytes(document)), 'Luna was rescued.')

    def test_empty_history(self):
        for texts in [('History', 'Social with other dogs', 'PRIVATE'), ('History:', '')]:
            with self.assertRaisesRegex(HistoryError, 'empty'):
                extract_history(self.paragraphs(*texts))
        document = Document()
        row = document.add_table(rows=1, cols=2).rows[0]
        row.cells[0].text = 'History'
        with self.assertRaisesRegex(HistoryError, 'empty'):
            extract_history(docx_bytes(document))

    def test_excluded_sections_before_and_after(self):
        for heading in ('Text for website', 'Text for Facebook', 'Text for Facebook/WhatsApp',
                        'YouTube text', 'Social with other dogs', 'Age: 4', 'Unknown field:'):
            with self.subTest(heading=heading):
                data = self.paragraphs('Text for website', 'PRIVATE OLD COPY', 'History',
                                       'Luna was rescued.', heading, 'PRIVATE NEXT FIELD')
                self.assertEqual(extract_history(data), 'Luna was rescued.')

    def test_word_heading_stops_extraction(self):
        document = Document()
        document.add_paragraph('History')
        document.add_paragraph('Luna was rescued.')
        document.add_heading('Unusual profile field', level=2)
        document.add_paragraph('PRIVATE')
        self.assertEqual(extract_history(docx_bytes(document)), 'Luna was rescued.')

    def test_missing_history_does_not_match_prose(self):
        with self.assertRaisesRegex(HistoryError, 'not found'):
            extract_history(self.paragraphs('Her history is unknown.', 'Text for website', 'PRIVATE'))

    def test_duplicate_history_is_ambiguous(self):
        with self.assertRaisesRegex(HistoryError, 'multiple History'):
            extract_history(self.paragraphs('History', 'First story.', 'History', 'Second story.'))

    def test_unclear_table_is_rejected(self):
        document = Document()
        cells = document.add_table(rows=1, cols=3).rows[0].cells
        cells[0].text = 'History'
        cells[1].text = 'Story.'
        cells[2].text = 'PRIVATE'
        with self.assertRaisesRegex(HistoryError, 'Parsing warning'):
            extract_history(docx_bytes(document))

    def test_unknown_title_is_rejected(self):
        with self.assertRaisesRegex(HistoryError, 'Parsing warning'):
            extract_history(self.paragraphs('History', 'Luna was rescued.', 'Special Needs', 'PRIVATE'))

    def test_invalid_document(self):
        with self.assertRaisesRegex(HistoryError, 'valid .docx'):
            extract_history(b'not a Word document')

    def test_sections_on_line_breaks_do_not_leak(self):
        self.assertEqual(extract_history(self.paragraphs('History: Luna was rescued.\nText for website\nPRIVATE')), 'Luna was rescued.')
        self.assertEqual(extract_history(self.paragraphs('History: Luna was rescued.\nText for website', 'Private website story.')), 'Luna was rescued.')
        with self.assertRaisesRegex(HistoryError, 'Parsing warning'):
            extract_history(self.paragraphs('History', 'Luna was rescued.\nText for Facebook\nPRIVATE'))
