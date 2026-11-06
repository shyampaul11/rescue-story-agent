"""User-authorized identity convention and narrowly allowed photo dates."""
from datetime import date, datetime
import re

from animal_profile import AnimalContext, PhotoDateReference


def parse_animal_label(label):
    """Azra (Rescuer) or Azra [Rescuer]. Never interpret rescuer as a dog trait."""
    value = str(label or '').strip()
    match = re.fullmatch(r'([^()\[\]]+?)\s*(?:\(([^()\[\]]+)\)|\[([^()\[\]]+)\])', value)
    if match:
        name, rescuer = match[1].strip(), (match[2] or match[3]).strip()
        if name and rescuer and max(len(name), len(rescuer)) <= 200:
            return AnimalContext(animal_name=name, rescuer_name=rescuer)
    if value and len(value) <= 200 and not any(char in value for char in '()[]'):
        return AnimalContext(animal_name=value)
    return AnimalContext()  # Ambiguous/multiple brackets are not guessed.


def metadata_date(value):
    """Parse EXIF or ISO date/time without guessing numeric date conventions."""
    if not isinstance(value, str):
        return None
    value = value.strip().rstrip('\x00')
    try:
        if re.fullmatch(r'\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}', value):
            return datetime.strptime(value, '%Y:%m:%d %H:%M:%S').date()
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?', value):
            return datetime.fromisoformat(value.replace('Z', '+00:00')).date()
    except ValueError:
        pass
    return None


def photo_date_reference(reference, candidates, today=None):
    """Candidates are capture-related fields only, ordered by preference."""
    today = today or date.today()
    usable, notes = [], []
    supplied = [(kind, value[:200]) for kind, value in candidates if isinstance(value, str) and value.strip()]
    for kind, value in supplied:
        parsed = metadata_date(value)
        if parsed is None:
            notes.append('A capture-date field was invalid or ambiguous and was ignored.')
        elif parsed > today:
            notes.append('A future capture date was ignored; confirm the camera clock.')
        else:
            usable.append((kind, value, parsed))
    if not usable:
        return PhotoDateReference(source_reference=reference, original_value=supplied[0][1] if supplied else None,
            reference_date=None, date_source=supplied[0][0] if supplied else 'unknown', verification_status='unknown',
            note=' '.join(notes + ['No usable capture date. Upload/modified dates and filenames are not substituted. Age cannot be calculated from this photo date.']))
    kind, original, parsed = usable[0]
    if kind == 'exif_datetime':
        notes.append('Generic EXIF DateTime may reflect an edit rather than capture; confirm it with the rescuer.')
    elif kind == 'exif_datetime_digitized':
        notes.append('Digitization date may differ from capture; confirm it with the rescuer.')
    if len({entry[2] for entry in usable}) > 1:
        notes.append('Capture-date fields disagree; the preferred date is provisional and needs confirmation.')
    return PhotoDateReference(source_reference=reference, original_value=original,
        reference_date=parsed.isoformat(), date_source=kind, verification_status='metadata_not_independently_verified',
        note=' '.join(notes + ['Photo reference date only; editable metadata is not a birth date or proof of age.']))
