"""Extract only explicitly approved animal fields; never return a whole document."""

import re
from io import BytesIO

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph


class HistoryError(ValueError):
    """Missing, empty, invalid, or ambiguous History."""


FIELDS = re.compile(
    r"^(?:social\b|text for\b|youtube\b|name\b|age\b|breed\b|sex\b|gender\b|"
    r"weight\b|height\b|size\b|health\b|medical\b|character\b|personality\b|"
    r"adoption\b|location\b|children\b|cats\b|dogs\b|vaccinat\w*\b|"
    r"neuter\w*\b|sterili\w*\b|date of birth\b)", re.I
)
HISTORY = re.compile(r"^history\s*(?::\s*(.*))?$", re.I | re.S)


def _paragraphs(container):
    """Yield paragraphs, refusing nested table layouts inside a History cell."""
    for child in container._element:
        if child.tag.endswith('}tbl'):
            raise HistoryError("Parsing warning: nested tables are ambiguous. Enter History manually.")
    return container.paragraphs


def _boundary(paragraph, text):
    return bool(FIELDS.match(text) or text.endswith(':') or
                paragraph.style.name.lower().startswith(('heading', 'title')))


def _history_cell(cell):
    """A labelled two-column row provides the boundary: keep its entire value cell."""
    paragraphs = _paragraphs(cell)
    if any(HISTORY.fullmatch(line.strip())
           for paragraph in paragraphs for line in paragraph.text.splitlines()):
        raise HistoryError("Parsing warning: multiple History sections. Enter History manually.")
    return '\n\n'.join(paragraph.text for paragraph in paragraphs).strip()


def _collect(paragraphs, start, inline):
    lines = []
    if inline:
        for line in inline.splitlines():
            if FIELDS.match(line.strip()) or re.match(r'^[^:]{1,80}:', line.strip()):
                return '\n\n'.join(lines).strip()
            if HISTORY.fullmatch(line.strip()):
                raise HistoryError("Parsing warning: multiple History sections. Enter History manually.")
            lines.append(line)
    for paragraph in paragraphs[start:]:
        text = paragraph.text.strip()
        if HISTORY.fullmatch(text):
            raise HistoryError("Parsing warning: multiple History sections. Enter History manually.")
        if text and (_boundary(paragraph, text) or re.match(r'^[^:]{1,80}:', text)):
            break
        # Unlabelled short title-like lines cannot safely be treated as story text.
        if text and len(text.split()) <= 7 and not text.endswith(('.', '!', '?', ',', ';')):
            raise HistoryError("Parsing warning: an unclear section heading follows History. Enter History manually.")
        if any(FIELDS.match(line.strip()) or re.match(r'^[^:]{1,80}:', line.strip())
               for line in paragraph.text.splitlines()[1:]):
            raise HistoryError("Parsing warning: History and other fields share a paragraph. Enter History manually.")
        lines.append(paragraph.text)
    return '\n\n'.join(lines).strip()


def extract_history(data: bytes) -> str:
    """Read DOCX bytes. Fail closed on ambiguous layouts or duplicate fields."""
    try:
        document = Document(BytesIO(data))
        candidates = []
        body = list(document.element.body)
        for index, child in enumerate(body):
            if child.tag.endswith('}p'):
                paragraph = Paragraph(child, document)
                match = HISTORY.fullmatch(paragraph.text.strip())
                if match:
                    following = []
                    for node in body[index + 1:]:
                        if not node.tag.endswith('}p'):
                            break
                        following.append(Paragraph(node, document))
                    candidates.append(_collect(following, 0, match.group(1)))
            elif child.tag.endswith('}tbl'):
                table = Table(child, document)
                for row in table.rows:
                    cells = []
                    for cell in row.cells:
                        if not any(cell._tc is old._tc for old in cells):
                            cells.append(cell)
                    for ci, cell in enumerate(cells):
                        paragraphs = cell.paragraphs
                        for pi, paragraph in enumerate(paragraphs):
                            match = HISTORY.fullmatch(paragraph.text.strip())
                            if not match:
                                continue
                            if len(cells) == 1:
                                candidates.append(_collect(_paragraphs(cell), pi + 1, match.group(1)))
                            elif ci == 0 and len(cells) == 2 and not match.group(1) and not any(p.text.strip() for p in paragraphs[pi + 1:]):
                                candidates.append(_history_cell(cells[1]))
                            else:
                                raise HistoryError("Parsing warning: unclear History table layout. Enter History manually.")
        if not candidates:
            raise HistoryError("History was not found. Use a clear History heading or enter it manually.")
        if len(candidates) != 1:
            raise HistoryError("Parsing warning: multiple History fields found. Enter History manually.")
        if not candidates[0]:
            raise HistoryError("The History section is empty. Add a history or enter it manually.")
        return candidates[0]
    except HistoryError:
        raise
    except Exception as exc:
        raise HistoryError("This document could not be read as a DOCX file. Upload a valid .docx document.") from exc


def extract_animal_info(data: bytes) -> dict:
    """Import only History and the two explicitly requested optional table rows."""
    history = extract_history(data)
    fields = {'history': history, 'other_info': '', 'updates': ''}
    labels = {'other info': 'other_info', 'updates': 'updates',
              'updates (please note the date of the update)': 'updates'}
    found = set()
    document = Document(BytesIO(data))
    for table in document.tables:
        for row in table.rows:
            cells = []
            for cell in row.cells:
                if not any(cell._tc is old._tc for old in cells):
                    cells.append(cell)
            if not cells:
                continue
            label = ' '.join(cells[0].text.split()).casefold().rstrip(':').strip()
            field = labels.get(label)
            if field is None:
                continue
            if len(cells) != 2:
                raise HistoryError(f'Parsing warning: unclear {field.replace("_", " ")} table row. Enter this information manually.')
            if field in found:
                raise HistoryError(f'Parsing warning: multiple {field.replace("_", " ")} rows found. Enter this information manually.')
            found.add(field)
            # The neighbouring cell is the boundary. Preserve dates and paragraphs verbatim.
            fields[field] = '\n\n'.join(paragraph.text for paragraph in _paragraphs(cells[1])).strip()
    return fields
