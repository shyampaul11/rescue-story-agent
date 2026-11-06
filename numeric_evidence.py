"""Local English/Dutch numeric evidence checks. No I/O, model calls or repairs.

These conservative checks compare typed values, units and qualifiers. They do
not establish the truth of a rescuer's report or replace human semantic review.
"""
from dataclasses import dataclass
from decimal import Decimal
import re
from types import SimpleNamespace


WORDS = {}
for names in [('zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten',
               'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen'),
              ('nul', 'een', 'twee', 'drie', 'vier', 'vijf', 'zes', 'zeven', 'acht', 'negen', 'tien',
               'elf', 'twaalf', 'dertien', 'veertien', 'vijftien', 'zestien', 'zeventien', 'achttien', 'negentien')]:
    WORDS.update({name: str(index) for index, name in enumerate(names)})
WORDS['één'] = '1'
for value, en, nl in [(20, 'twenty', 'twintig'), (30, 'thirty', 'dertig'), (40, 'forty', 'veertig'),
                       (50, 'fifty', 'vijftig'), (60, 'sixty', 'zestig'), (70, 'seventy', 'zeventig'),
                       (80, 'eighty', 'tachtig'), (90, 'ninety', 'negentig')]:
    WORDS[en] = WORDS[nl] = str(value)
    for unit, en_unit, nl_unit in [(1, 'one', 'een'), (2, 'two', 'twee'), (3, 'three', 'drie'),
                                   (4, 'four', 'vier'), (5, 'five', 'vijf'), (6, 'six', 'zes'),
                                   (7, 'seven', 'zeven'), (8, 'eight', 'acht'), (9, 'nine', 'negen')]:
        WORDS[en + '-' + en_unit] = WORDS[en + ' ' + en_unit] = str(value + unit)
        WORDS[nl_unit + ('ën' if unit in (2, 3) else 'en') + nl] = str(value + unit)

NUMBER = r'(?:\d+(?:[.,]\d+)?|(?:' + '|'.join(re.escape(word) for word in sorted(WORDS, key=len, reverse=True)) + r')\b)'
TOKEN = re.compile(r'(?<!\w)' + NUMBER + r'(?:\s*(?:[–—/\-]|to|tot|and|en)\s*' + NUMBER + r')?', re.I)
WORD_RANGE = re.compile(NUMBER + r'\s+(?:and|en)\s+' + NUMBER, re.I)
UNITS = re.compile(r'^[\s-]*(kg|kilograms?|kilogrammen|kilos?|cm|centimet(?:er|re)s?|mm|mg|ml|bpm|mmhg|'
                   r'°\s*c|%|months?|maand(?:en)?|years?|jaren|jaar|weeks?|weken|days?|dag(?:en)?|hours?|uren|uur|'
                   r'minutes?|minuten|minuut|seconds?|seconden?|dogs?|honden?|cats?|katten?|puppies|puppy|pups?)(?!\w)', re.I)
ESTIMATE = re.compile(r'\b(?:around|about|approximately|approx|estimated|expected|possible|possibly|likely|may|might|'
                      r'roughly|circa|ongeveer|cirka|rond|geschat\w*|verwacht\w*|vermoedelijk|mogelijk|waarschijnlijk)\b', re.I)
ADULT = re.compile(r'\b(?:adult|fully gro[wn]\w*|grow[n]? up|grow up|will weigh|volwassen\w*|uitgegroeid|zal.*wegen)\b', re.I)
HISTORICAL = re.compile(r'\b(?:was|were|earlier|previous\w*|historical|at the time|at (?:that|the) (?:report|date)|'
                        r'at reporting|destijds|toen|eerder\w*|historisch\w*|werd|ten tijde)\b', re.I)
BIRTH = re.compile(r'\b(?:born|birth\w*|birthday|geboren|geboorte\w*)\b', re.I)
UPPER = re.compile(r'\b(?:not more th[ae]n|no more than|at most|up to|maximum|maximaal|niet meer dan|hooguit)\b', re.I)
LOWER = re.compile(r'\b(?:at least|no less than|minimum|minimaal|ten minste|minstens)\b', re.I)
STRICT_UPPER = re.compile(r'\b(?:less than|under|below|minder dan|onder)\b', re.I)
STRICT_LOWER = re.compile(r'\b(?:more than|over|above|meer dan|boven)\b', re.I)
DURATION = re.compile(r'\b(?:last\w*|duration|for|during|treatment|wait\w*|duur\w*|gedurende|behandeling|wacht\w*)\b', re.I)
AGE = re.compile(r'\b(?:age|aged|old|leeftijd|oud|puppy|pup)\b', re.I)
MONTHS = {}
for index, names in enumerate([('january', 'januari'), ('february', 'februari'), ('march', 'maart'),
                               ('april',), ('may', 'mei'), ('june', 'juni'), ('july', 'juli'),
                               ('august', 'augustus'), ('september',), ('october', 'oktober'),
                               ('november',), ('december',)], start=1):
    for name in names:
        MONTHS[name] = MONTHS[name[:3]] = index
MONTH_PATTERN = '(?:' + '|'.join(sorted(MONTHS, key=len, reverse=True)) + ')'
DATE = re.compile(r'\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|'
                  r'(?P<day>\d{1,2})(?:st|nd|rd|th|e)?\s+(?P<month>' + MONTH_PATTERN + r')\s*,?\s*(?P<year>\d{4})|'
                  r'(?P<month_first>' + MONTH_PATTERN + r')\s+(?P<day_first>\d{1,2})(?:st|nd|rd|th|e)?\s*,?\s*(?P<year_first>\d{4}))\b', re.I)
LABEL = re.compile(r'\b(?:draft|version|versie|concept|section|sectie|hoofdstuk)\s+(\d+)\b', re.I)
LIST = re.compile(r'^\s*[#*\s]*(\d+)[.)](?:\s+|$)')
URL = re.compile(r'https?://\S+|www\.\S+', re.I)


@dataclass(frozen=True)
class NumericFact:
    start: int
    end: int
    excerpt: str
    kind: str
    values: tuple[str, ...]
    unit: str


def statements(text):
    """Yield exact sentence/line spans, retaining decimal punctuation and bullets."""
    start = 0
    boundary = r'\n+|[!?;]+|\.(?!\d)(?=\s|$)|\b(?:and|en|but|maar)\b(?=[^.!?;\n]*' + NUMBER + r')'
    ranges = [match.span() for match in WORD_RANGE.finditer(text)]
    for match in re.finditer(boundary, text, re.I):
        if any(begin < match.start() < end for begin, end in ranges):
            continue
        end = match.end() if match[0][0] in '.!?;' else match.start()
        if text[start:end].strip():
            yield start, end, text[start:end]
        start = match.end()
    if text[start:].strip():
        yield start, len(text), text[start:]


def canonical(value):
    return WORDS.get(value.casefold(), str(Decimal(value.replace(',', '.')).normalize()) if value[0].isdigit() else value)


def date_value(match):
    if match['month'] or match['month_first']:
        month = MONTHS[(match['month'] or match['month_first']).casefold()]
        return f"{int(match['year'] or match['year_first']):04}-{month:02}-{int(match['day'] or match['day_first']):02}"
    return match[0]


def estimated(text):
    # The month May is not the uncertainty verb 'may'.
    return bool(ESTIMATE.search(DATE.sub('', text)))


def bound(text):
    # Match inclusive forms first: 'no more than' contains 'more than'.
    if UPPER.search(text):
        return 'upper_inclusive'
    if LOWER.search(text):
        return 'lower_inclusive'
    if STRICT_UPPER.search(text):
        return 'upper_exclusive'
    if STRICT_LOWER.search(text):
        return 'lower_exclusive'
    return None


def unit_kind(unit, context):
    unit = unit.casefold().replace(' ', '')
    if unit.startswith(('kg', 'kilo')):
        return 'weight', 'kg'
    if unit.startswith(('cm', 'centim')):
        return 'height', 'cm'
    if unit in ('mm', 'mg', 'ml', 'bpm', 'mmhg', '°c', '%'):
        return 'medical', unit
    for pattern, normalized in [(r'dog|hond', 'dog'), (r'cat|kat', 'cat'), (r'pupp|pup', 'puppy')]:
        if re.search(pattern, unit):
            return 'count', normalized
    for pattern, normalized in [(r'month|maand', 'month'), (r'year|jaar|jaren', 'year'),
                                 (r'week|weken', 'week'), (r'day|dag', 'day'), (r'hour|uur|uren', 'hour'),
                                 (r'minut', 'minute'), (r'second|seconden', 'second')]:
        if re.search(pattern, unit):
            is_age = bool(AGE.search(context)) or (normalized in ('month', 'year') and not DURATION.search(context))
            return ('age' if is_age else 'duration'), normalized
    return 'number', ''


def numeric_facts(text):
    """Recognize numeric animal statements; exempt only specific formatting spans.

    Number words need a measurement/age context so invitations such as 'one
    loving home' are not animal measurements. Digits default to factual. URL
    tokens are not measurements; the writing service independently forbids URLs.
    """
    result = []
    for offset, _, sentence in statements(text):
        ignored = [match.span() for pattern in (LABEL, LIST, URL) for match in pattern.finditer(sentence)]
        # A label exemption covers its number, never animal facts later on that line.
        dates = list(DATE.finditer(sentence))
        for match in dates:
            if any(start <= match.start() < end for start, end in ignored):
                continue
            kind = 'birth_date' if BIRTH.search(sentence) else 'date'
            result.append(NumericFact(offset + match.start(), offset + match.end(), sentence.strip(), kind, (date_value(match),), 'date'))
        for match in TOKEN.finditer(sentence):
            if any(start <= match.start() < end for start, end in ignored + [item.span() for item in dates]):
                continue
            unit_match = UNITS.match(sentence[match.end():])
            kind, unit = unit_kind(unit_match[1], sentence) if unit_match else ('number', '')
            if kind == 'number' and BIRTH.search(sentence):
                kind = 'birth_date'
            if not unit_match and not match[0][0].isdigit() and not AGE.search(sentence):
                continue
            if match[0].casefold() in ('een', 'one') and not unit_match:
                continue  # An indefinite article before a noun is not an age/count.
            # A range remains a range; endpoint membership alone is insufficient.
            values = tuple(canonical(item[0]) for item in re.finditer(NUMBER, match[0], re.I))
            result.append(NumericFact(offset + match.start(), offset + match.end() + (unit_match.end() if unit_match else 0),
                                      sentence.strip(), kind, values, unit))
    return result


def matching_fact(left, right):
    return (left.kind, left.values, left.unit) == (right.kind, right.values, right.unit)


def fact_problem(fact, source_fact, claim, item):
    target, source = fact.excerpt, source_fact.excerpt
    if fact.kind == 'weight':
        if ADULT.search(source) and not ADULT.search(target):
            return 'EXPECTED_WEIGHT_AS_MEASURED', 'An expected adult weight was presented as measured/current weight. Keep the adult-weight prediction explicit.'
        if not ADULT.search(source) and ADULT.search(target):
            return 'MEASURED_WEIGHT_AS_EXPECTED', 'A measured/reported weight was changed into a prediction without supporting evidence.'
    if fact.kind == 'age' and item.temporal_status in ('historical', 'unclear'):
        if not HISTORICAL.search(target) or claim.qualification not in ('historical', 'qualified'):
            return 'HISTORICAL_AGE_AS_CURRENT', 'An historical or undated reported age does not establish current age. Preserve when the report applied in the prose and annotation.'
    uncertain = (estimated(source) or (fact.kind == 'weight' and bool(ADULT.search(source)))
                 or getattr(item, 'verification_status', '') == 'interpretation_requires_review')
    if uncertain and (not (estimated(target) or (fact.kind == 'weight' and ADULT.search(target)))
                      or claim.qualification not in ('qualified', 'historical', 'observed_moment')):
        return 'UNCERTAINTY_LOST', 'An approximate or estimated numerical report lost its uncertainty in the wording or annotation.'
    if bound(source) != bound(target):
        return 'NUMERIC_BOUND_CHANGED', 'A numerical upper/lower bound changed or disappeared. Preserve the rescuer’s limit as well as the value.'
    return None


def numeric_problem(claim, evidence, catalog, profile, facts=None):
    """A number must match one cited canonical claim, including type and unit.

    Raw source excerpts and ID suffixes are never additional numerical proof.
    Contextual age annotations must still be grounded in actual basis evidence.
    """
    records = [evidence[ref] for ref in claim.source_references if ref in evidence]
    if 'age_assessment' in claim.source_references and profile.age_assessment:
        records += [evidence[ref] for ref in profile.age_assessment.basis_evidence_ids if ref in evidence]
    for fact in numeric_facts(claim.excerpt) if facts is None else facts:
        matches = [(item, source_fact) for item in records for source_fact in numeric_facts(item.claim)
                   if matching_fact(fact, source_fact)]
        if not matches:
            if claim.qualification == 'unknown' and any(catalog[ref]['kind'] == 'unknown' and
                    any(matching_fact(fact, source) for source in numeric_facts(catalog[ref]['text']))
                    for ref in claim.source_references):
                continue
            return 'UNSUPPORTED_NUMBER', 'A draft introduced a date, age, weight or number unsupported by its citations (including its type, range and unit).'
        problems = [fact_problem(fact, source, claim, item) for item, source in matches]
        if all(problems):
            return problems[0]
    return None


def profile_numeric_problem(item):
    """Reject a canonical numeric fact that contradicts its own permitted quote.

    Historical prose is required in drafts, while the profile can retain the
    original age wording with historical/unclear metadata. Never rewrite it.
    """
    source_facts = numeric_facts(item.supporting_text_or_description)
    claim = SimpleNamespace(qualification='qualified')
    source_item = SimpleNamespace(temporal_status='current_as_reported')
    for fact in numeric_facts(item.claim):
        if fact.kind == 'age' and item.source_type == 'HISTORY_FACT' and item.temporal_status == 'current_as_reported' and not DATE.search(item.supporting_text_or_description):
            return 'An undated age in History must remain historical/unclear, not an assertion of current age.'
        matches = [source for source in source_facts if matching_fact(fact, source)]
        if not matches:
            return 'A numerical profile claim differs from its supporting source in value, type, range or unit.'
        problems = [fact_problem(fact, source, claim, source_item) for source in matches]
        if all(problems):
            return problems[0][1]
    return None


def age_assessment_problem(age, evidence):
    """Reported numeric age assessments cannot alter the canonical basis facts."""
    if age.status != 'reported_age' or not age.estimate:
        return None
    source_facts = [fact for ref in age.basis_evidence_ids for fact in numeric_facts(evidence[ref].claim)]
    for fact in numeric_facts(age.estimate):
        matches = [source for source in source_facts if matching_fact(fact, source)]
        if not matches or all(estimated(source.excerpt) and not estimated(fact.excerpt) for source in matches):
            return 'The reported numerical age estimate changes its evidence value, unit or uncertainty. Preserve the original approximate report.'
    return None


def annotation_spans(draft):
    """Assign duplicate excerpts to separate occurrences, never global digit sets."""
    spans, assigned = [], set()
    for index, claim in enumerate(draft.claims, start=1):
        if not claim.excerpt.strip():
            continue
        for match in re.finditer(re.escape(claim.excerpt), draft.text):
            if match.span() not in assigned:
                spans.append((match.start(), match.end(), index, claim))
                assigned.add(match.span())
                break
    return spans


def missing_numeric_annotations(draft):
    spans = annotation_spans(draft)
    missing = []
    for fact in numeric_facts(draft.text):
        covered = False
        for start, end, _, claim in spans:
            if start <= fact.start and fact.end <= end and any(matching_fact(fact, annotated) for annotated in numeric_facts(claim.excerpt)):
                covered = True
                break
        if not covered and fact.excerpt not in missing:
            missing.append(fact.excerpt)
    return missing


def contextual_numeric_problems(draft, evidence, catalog, profile):
    """Recheck the complete clause containing an annotated number occurrence.

    An excerpt such as '16 kg' cannot conceal 'expected adult weight' or other
    qualifiers outside its span. Validate one fact at a time so separate facts
    in the same sentence can cite their own evidence.
    """
    for fact in numeric_facts(draft.text):
        for start, end, index, claim in annotation_spans(draft):
            if start <= fact.start and fact.end <= end and any(matching_fact(fact, annotated) for annotated in numeric_facts(claim.excerpt)):
                contextual = claim.model_copy(update={'excerpt': fact.excerpt})
                problem = numeric_problem(contextual, evidence, catalog, profile, facts=[fact])
                if problem:
                    yield index, contextual, problem
                break


def relevant_records(excerpt, profile, catalog, references=()):
    """Review hints only. Never citations, annotation repairs or acceptance proof."""
    facts = numeric_facts(excerpt)
    cited = [item for item in profile.evidence if item.id in references]
    candidates = [item for item in profile.evidence if item.id in catalog and item.id not in references and
                  any(a.kind == b.kind for a in facts for b in numeric_facts(item.claim))]
    return (cited + candidates)[:8]
