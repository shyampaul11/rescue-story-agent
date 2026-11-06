"""Conservative, deterministic local evidence review. No I/O or prose rewriting.

Green means an exact canonical selected-animal statement was positively matched.
The bounded bilingual detectors identify concrete problems; they are deliberately
not used as proof that an arbitrary paraphrase is supported.
"""
from hashlib import sha256
import re
from types import SimpleNamespace

from content_models import DRAFT_KEYS
from draft_evidence import NEGATIVE, UNCERTAIN, semantic_problem, public_reference
from numeric_evidence import (numeric_facts, numeric_problem, statements, ADULT, HISTORICAL,
                              WORD_RANGE, estimated)
from required_content import (BENIGN, MEDICAL, READINESS, SOCIAL, BEHAVIOR_RISK,
                              FACETS, conveys_required_fact, matches, name_in_text)
from unknown_information import MISSING, missing_parts, topics, unknown_problem
from writing_context import prepare_writing_context

VERSION = 'editorial-assessment-1'
QUALIFICATIONS = {'reported', 'observed_moment', 'qualified', 'historical', 'unknown'}
_HIGH_RISK = MEDICAL + '|' + BENIGN + '|' + READINESS + '|' + BEHAVIOR_RISK
_SUBJECTIVE = re.compile(r'\b(?:sweet|lovely|delightful|wonderful|adorable|perfect|gentle|friendly|'
                          r'beautiful|lief|lieve|prachtig|geweldig|zachtaardig|vriendelijk)\b', re.I)
_SOCIAL_ASSERTION = re.compile(r'\b(?:good with|bad with|suitable|unsuitable|compatible|'
    r'incompatible|friendly|cannot live|can live|not safe|safe with|without|'
    r'likes?|loves?|enjoys?|gets? along|friends? with|'
    r'goed met|gaat goed|houdt van|houden van|geschikt|ongeschikt|zonder|kan met|kan niet met|samenleven)\b', re.I)
_LOCATION = re.compile(r'\b(?:(?:currently\s+)?(?:lives|stays)|(?:is\s+)?located|(?:is\s+)?currently|'
    r'woont|verblijft|bevindt\s+zich|is\s+momenteel)\s+(?:in|at|near|te|bij)\s*:?\s*([^.!?;\n]+)|'
    r'\b(?:current\s+location|location|locatie|verblijfplaats)\s*:\s*([^.!?;\n]+)', re.I)
_PLACE_ALIASES = {'parijs': 'paris', 'londen': 'london', 'brussel': 'brussels',
    'antwerpen': 'antwerp', 'gent': 'ghent', 'wenen': 'vienna', 'keulen': 'cologne',
    'nederland': 'the netherlands', 'belgië': 'belgium', 'frankrijk': 'france',
    'duitsland': 'germany', 'spanje': 'spain', 'italië': 'italy'}
_ANNOTATION_CODES = {'ANNOTATION_INVALID', 'ANNOTATION_MISSING', 'DRAFT_EXCERPT_MISMATCH',
    'EMPTY_DRAFT_EXCERPT', 'MISSING_DRAFT_REFERENCES', 'UNKNOWN_EVIDENCE_ID',
    'REVIEW_ONLY_EVIDENCE', 'MEDIA_QUALIFICATION', 'HISTORICAL_QUALIFICATION'}
_ANNOTATION_CODES.add('ANNOTATION_SOURCE_MISMATCH')
_ANNOTATION_CODES.add('DRAFT_PUBLIC_REFERENCE')


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _stable(*parts):
    return sha256('\0'.join(str(part) for part in parts).encode('utf-8')).hexdigest()[:16]


def _normalized(text):
    # Internal punctuation is evidence-bearing: 16-17 is not 16.17. Only
    # whitespace, letter case and terminal sentence punctuation are immaterial.
    return re.sub(r'\s+', ' ', text.casefold()).strip().rstrip('.!?')


def _literal_support(text, records):
    """Match entire canonical assertions, never token overlap or a valid ID alone."""
    target = _normalized(text)
    return [item for item in records if target and target == _normalized(item.claim)]


def _risk(text):
    return bool(numeric_facts(text) or matches(_HIGH_RISK, text) or
                _location_values(text) or (matches(SOCIAL, text) and _SOCIAL_ASSERTION.search(text)))


def _location_values(text):
    result = []
    for match in _LOCATION.finditer(text):
        # Household/context wording is not part of the place name. Preserve
        # unfamiliar location forms for human review instead of inventing a city.
        core = re.split(r',|\s+(?:with|met|where|waar|together with|samen met)\b',
                        match[1] or match[2], maxsplit=1, flags=re.I)[0]
        value = _normalized(core)
        result.append(_PLACE_ALIASES.get(value, value))
    return result


def _review_topics(text):
    return topics(text) | ({'location'} if _location_values(text) else set())


def _confident_place(value):
    # Unrecognized multiword tails may contain context or translated wording.
    # They can establish a review candidate, never a proven place conflict.
    return value in set(_PLACE_ALIASES.values()) or bool(
        re.fullmatch(r'[^\W\d_]+(?:[-\u2019\'][^\W\d_]+)*', value) and
        value not in {'shelter', 'opvang', 'home', 'thuis', 'unknown', 'onbekend'})


def _potential_sources(text, records, profile):
    """Review hints for an unannotated paraphrase, never assigned citations."""
    wanted_topics = _review_topics(text)
    numeric_kinds = {fact.kind for fact in numeric_facts(text)}
    facets = [pattern for pattern in FACETS.values() if matches(pattern, text)]
    superseded = {ref for update in profile.updates for ref in update.superseded_evidence_ids}
    return [item for item in records if item.id not in superseded and not MISSING.search(item.claim) and (
        wanted_topics.intersection(_review_topics(item.claim)) or
        numeric_kinds.intersection(fact.kind for fact in numeric_facts(item.claim)) or
        any(matches(pattern, item.claim) for pattern in facets))]


def _clauses(text):
    """Exact spans for review; split joined clauses to expose added assertions."""
    for start, end, sentence in statements(text):
        # Keep numeric ranges and pure missing-knowledge scopes intact.
        if missing_parts(sentence):
            ranges = [(0, len(sentence))]
        else:
            ranges, cursor = [], 0
            for connector in re.finditer(r'\b(?:and|but|en|maar)\b|,', sentence, re.I):
                if any(a <= connector.start() < b for a, b in
                       (match.span() for match in WORD_RANGE.finditer(sentence))):
                    continue
                if connector[0] == ',' and connector.start() and connector.end() < len(sentence) and sentence[connector.start() - 1].isdigit() and sentence[connector.end()].isdigit():
                    continue
                ranges.append((cursor, connector.start()))
                cursor = connector.end()
            ranges.append((cursor, len(sentence)))
        for begin, finish in ranges:
            part = sentence[begin:finish]
            left, right = len(part) - len(part.lstrip()), len(part.rstrip())
            if right > left:
                yield start + begin + left, start + begin + right, part[left:right]


def _uncertain_support(text, records, qualification):
    return (qualification in ('unknown', 'historical', 'observed_moment') or
        bool(UNCERTAIN.search(text) or MISSING.search(text) or _SUBJECTIVE.search(text)) or
        any(item.temporal_status != 'current_as_reported' or
            item.verification_status == 'interpretation_requires_review' or
            item.source_type in ('PHOTO_OBSERVATION', 'VIDEO_FRAME_OBSERVATION', 'PHOTO_DATE_METADATA') or
            UNCERTAIN.search(item.claim) or ADULT.search(item.claim) for item in records))


def _new_finding(key, code, color, excerpt, reason, refs=(), kind=None, **extra):
    refs = sorted(set(refs))
    entry = dict(id=_stable(key, code, excerpt, '|'.join(refs)), code=code,
        kind=kind or ('annotation' if code in _ANNOTATION_CODES else 'evidence'),
        color=color, excerpt=excerpt, reason=reason, source_references=refs,
        action=('Review the wording and its source before copying this passage.' if color == 'red' else
                'Check the source and preserve any uncertainty; edit the text or annotation as needed.'))
    entry.update(extra)
    return entry


def _passage(start, end, color, reason, refs, code):
    return dict(start=start, end=end, color=color,
        label={'green': 'Supported', 'yellow': 'Review needed', 'red': 'Unsupported or conflicting'}[color],
        reason=reason, source_references=sorted(set(refs)), code=code)


def _annotation_records(text, claims, key, context, all_evidence):
    """Validate metadata separately; duplicate annotations consume distinct spans."""
    findings, assigned, annotations = [], set(), []
    if claims is not None and not isinstance(claims, (list, tuple)):
        return [], [_new_finding(key, 'ANNOTATION_INVALID', 'yellow', '', 'Annotation metadata is malformed.')]
    for raw in claims or ():
        excerpt, refs, qualification = (_get(raw, 'excerpt'), _get(raw, 'source_references'),
                                         _get(raw, 'qualification'))
        if (not isinstance(excerpt, str) or not isinstance(refs, (list, tuple)) or
            any(not isinstance(ref, str) for ref in refs) or
            not isinstance(qualification, str) or qualification not in QUALIFICATIONS):
            findings.append(_new_finding(key, 'ANNOTATION_INVALID', 'yellow',
                excerpt if isinstance(excerpt, str) else '', 'Annotation metadata is malformed; readable prose was retained.'))
            continue
        claim = SimpleNamespace(excerpt=excerpt, source_references=list(refs), qualification=qualification)
        if not excerpt.strip():
            findings.append(_new_finding(key, 'EMPTY_DRAFT_EXCERPT', 'yellow', '', 'An annotation has no excerpt.', refs))
            continue
        span = next((match.span() for match in re.finditer(re.escape(excerpt), text)
                     if match.span() not in assigned), None)
        if span is None:
            findings.append(_new_finding(key, 'DRAFT_EXCERPT_MISMATCH', 'yellow', excerpt,
                'This annotation does not match an unused exact occurrence in its own draft.', refs))
            continue
        assigned.add(span)
        problems = []
        for ref in refs:
            if ref in context['references']:
                continue
            item = all_evidence.get(ref)
            code = ('OTHER_ANIMAL_EVIDENCE' if item and item.subject != 'selected_animal' else
                    'REVIEW_ONLY_EVIDENCE' if item else 'UNKNOWN_EVIDENCE_ID')
            color = 'red' if code == 'OTHER_ANIMAL_EVIDENCE' else 'yellow'
            problems.append(_new_finding(key, code, color, excerpt,
                'This reference belongs to another animal or has an unclear subject.' if color == 'red' else
                'This reference is not in the permitted selected-animal evidence catalog.', [ref]))
        if not refs and qualification != 'unknown':
            problems.append(_new_finding(key, 'MISSING_DRAFT_REFERENCES', 'yellow', excerpt,
                'A factual annotation has no source references.'))
        findings.extend(problems)
        annotations.append((span[0], span[1], claim, problems))
    return annotations, findings


def _concrete_problems(claim, records, context, profile):
    """Collect independently bounded problems instead of stopping at the first."""
    catalog = context['references']
    evidence = {item.id: item for item in profile.evidence if item.id in catalog}
    safe = SimpleNamespace(excerpt=claim.excerpt,
        source_references=[ref for ref in claim.source_references if ref in catalog], qualification=claim.qualification)
    result = []
    problem = semantic_problem(safe, evidence, catalog, context)
    if problem:
        result.append(problem)
    if numeric_facts(claim.excerpt):
        problem = numeric_problem(safe, evidence, catalog, profile)
        if problem:
            result.append(problem)
    # Detectors' topic vocabulary deliberately also covers healthy/readiness and
    # unfamiliar diagnoses. A vaccination token cannot license another condition.
    if not missing_parts(claim.excerpt):
        source = ' '.join(item.claim for item in records)
        target_topics = _review_topics(claim.excerpt)
        absent = target_topics - _review_topics(source)
        if _risk(claim.excerpt) and (not records or absent):
            result.append(('UNSUPPORTED_FACT', 'This concrete assertion is not supported by the cited selected-animal facts.'))
        locations = _location_values(claim.excerpt)
        source_locations = [place for item in records for place in _location_values(item.claim)]
        if (locations and source_locations and all(_confident_place(place) for place in locations + source_locations)
                and any(place not in source_locations for place in locations)):
            result.append(('LOCATION_CONFLICT', 'The stated current place differs from the cited selected-animal location. A location topic match does not support a different place.'))
        for pattern in (MEDICAL, BENIGN, READINESS, BEHAVIOR_RISK):
            for match in re.finditer(r'\b(?:' + pattern + r')\b', claim.excerpt, re.I):
                token = match[0]
                # A bounded topic can have translated spellings; exact term absence
                # alone is only proof of absence when its semantic topic is absent.
                if not source or (not re.search(r'\b' + re.escape(token) + r'\b', source, re.I)
                                  and not topics(token).intersection(topics(source))):
                    result.append(('UNSUPPORTED_FACT', 'An additional medical, behavioral or readiness assertion lacks matching source support.'))
        if records and _risk(claim.excerpt) and not missing_parts(source):
            related = [item for item in records if _review_topics(item.claim).intersection(target_topics)]
            if related and all(bool(NEGATIVE.search(item.claim)) != bool(NEGATIVE.search(claim.excerpt)) for item in related):
                result.append(('POLARITY_CONFLICT', 'The assertion reverses the source’s positive/negative meaning.'))
    return list(dict.fromkeys(result))


def assess_text(text, profile, draft_key='editor', claims=None, manual_sources=()):
    """Review unchanged text locally; evidence concerns are values, not errors."""
    context = prepare_writing_context(profile)
    catalog = context['references']
    evidence = {item.id: item for item in profile.evidence}
    permitted = [item for item in profile.evidence if item.id in catalog]
    annotations, findings = _annotation_records(text, claims, draft_key, context, evidence)
    passages = []
    leaked = public_reference(text, catalog)
    if leaked:
        token = re.escape(leaked)
        marker = re.search(r'\[\s*' + token + r'\s*\]|\{\s*' + token + r'\s*\}|\b(?:evidence|source|bron|reference)\s*:\s*' + token + r'(?!\w)', text, re.I)
        if marker is None:
            marker = re.search(r'(?<!\w)' + token + r'(?!\w)', text)
        reason = 'A technical evidence reference appears in the public draft text. Review the copy before sharing.'
        findings.append(_new_finding(draft_key, 'DRAFT_PUBLIC_REFERENCE', 'yellow', marker[0] if marker else '', reason, [leaked]))
        if marker:
            passages.append(_passage(marker.start(), marker.end(), 'yellow', reason, [leaked], 'DRAFT_PUBLIC_REFERENCE'))

    # First review the exact annotations. An atomic source match is positive
    # evidence; passing the weaker detectors alone never turns a passage green.
    reviewed = []
    for start, end, claim, annotation_findings in annotations:
        valid_refs = [ref for ref in claim.source_references if ref in catalog]
        records = [evidence[ref] for ref in valid_refs if ref in evidence]
        direct = _literal_support(claim.excerpt, permitted)
        if direct and claim.qualification != 'unknown' and not any(item.id in valid_refs for item in direct):
            mismatch = _new_finding(draft_key, 'ANNOTATION_SOURCE_MISMATCH', 'yellow', claim.excerpt,
                'The cited record does not support this statement. Separate matching source candidates are available for review.',
                claim.source_references, support_candidates=[item.id for item in direct])
            annotation_findings.append(mismatch)
            findings.append(mismatch)
        local_problems = _concrete_problems(claim, records, context, profile)
        if claim.qualification == 'unknown':
            unknown = unknown_problem(claim, context)
            local_problems = [unknown] if unknown else []
        # Wrong/missing citations are yellow metadata issues if the *whole*
        # assertion is independently an exact permitted source statement.
        if direct and not any(f['code'] == 'OTHER_ANIMAL_EVIDENCE' for f in annotation_findings):
            local_problems = _concrete_problems(SimpleNamespace(excerpt=claim.excerpt,
                source_references=[item.id for item in direct], qualification=claim.qualification), direct, context, profile)
        color = 'yellow'
        for code, reason in local_problems:
            # Missing/unrelated citation alone does not establish false prose.
            annotation_only = code in ('NONFACTUAL_REFERENCE', 'UNRELATED_EVIDENCE') and not _risk(claim.excerpt)
            severity = 'yellow' if annotation_only else 'red'
            findings.append(_new_finding(draft_key, code, severity, claim.excerpt, reason, claim.source_references))
            if severity == 'red':
                color = 'red'
        if any(f['color'] == 'red' for f in annotation_findings):
            color = 'red'
        if not local_problems and not annotation_findings and direct and not _uncertain_support(claim.excerpt, direct, claim.qualification):
            color = 'green'
        reason = ('An exact selected-animal canonical statement supports this passage.' if color == 'green' else
                  'A concrete assertion conflicts with or exceeds the available evidence.' if color == 'red' else
                  'Preserve uncertainty and review the source; exact positive support is not established.')
        code = 'SUPPORTED' if color == 'green' else 'UNSUPPORTED_FACT' if color == 'red' else 'REVIEW_REQUIRED'
        passages.append(_passage(start, end, color, reason, claim.source_references, code))
        if color == 'yellow' and not local_problems and not annotation_findings:
            findings.append(_new_finding(draft_key, 'REVIEW_REQUIRED', 'yellow', claim.excerpt, reason, claim.source_references))
        reviewed.append((start, end))

    # Examine every clause, including facts hidden inside broad annotations and
    # completely unannotated assertions. Never mark an unmatched clause green.
    for start, end, clause in _clauses(text):
        support_candidates = []
        covering = [(a, b, c, p) for a, b, c, p in annotations if a <= start and end <= b]
        if covering:
            parent = covering[0][2]
            if clause == parent.excerpt:
                continue
            records = [evidence[ref] for ref in parent.source_references if ref in catalog and ref in evidence]
            probe = SimpleNamespace(excerpt=clause, source_references=parent.source_references, qualification=parent.qualification)
            problems = _concrete_problems(probe, records, context, profile)
            for code, reason in problems:
                if _risk(clause):
                    findings.append(_new_finding(draft_key, code, 'red', clause, reason, parent.source_references))
                    passages.append(_passage(start, end, 'red', reason, parent.source_references, code))
            continue
        direct = _literal_support(clause, permitted)
        if direct:
            color = 'yellow' if _uncertain_support(clause, direct, 'reported') else 'green'
            reason = 'An exact selected-animal canonical statement supports this passage.'
            if color == 'yellow':
                reason = 'This source is estimated, historical, subjective or limited to a sampled moment.'
            refs = [item.id for item in direct]
            code = 'SUPPORTED' if color == 'green' else 'REVIEW_REQUIRED'
            if numeric_facts(clause):
                qualification = 'historical' if HISTORICAL.search(clause) else 'qualified' if estimated(clause) or ADULT.search(clause) else 'reported'
                problem = numeric_problem(SimpleNamespace(excerpt=clause, source_references=refs, qualification=qualification),
                    {item.id: item for item in permitted}, catalog, profile)
                if problem:
                    color, code, reason = 'red', problem[0], problem[1]
            if claims is not None and color == 'green':
                color, code, reason = 'yellow', 'ANNOTATION_MISSING', 'This exact source match is a review candidate; the draft has no matching annotation for this occurrence.'
        elif missing_parts(clause):
            probe = SimpleNamespace(excerpt=clause, qualification='unknown', source_references=[])
            problem = unknown_problem(probe, context)
            color, code = ('yellow', 'REVIEW_REQUIRED') if not problem else ('red', problem[0])
            reason, refs = ('Missing information remains uncertain.' if not problem else problem[1]), []
        elif _risk(clause):
            candidates = _potential_sources(clause, permitted, profile)
            qualification = 'historical' if HISTORICAL.search(clause) else 'qualified' if estimated(clause) or UNCERTAIN.search(clause) or ADULT.search(clause) else 'reported'
            probe = SimpleNamespace(excerpt=clause, source_references=[item.id for item in candidates], qualification=qualification)
            problems = _concrete_problems(probe, candidates, context, profile) if candidates else []
            refs = []
            if candidates and not problems:
                color, code, reason = 'yellow', 'POSSIBLE_SOURCE_SUPPORT', 'Relevant source records may support this unannotated paraphrase. Compare them manually; these are review candidates, not assigned citations.'
                support_candidates = [item.id for item in candidates]
            else:
                color, code, reason = 'red', 'UNSUPPORTED_FACT', 'This unannotated concrete assertion lacks matching selected-animal source support.'
                for problem_code, problem_reason in problems:
                    findings.append(_new_finding(draft_key, problem_code, 'red', clause, problem_reason, [],
                        support_candidates=[item.id for item in candidates]))
        else:
            # Identity alone is directly checked; generic invitations/formatting
            # are not factual evidence claims and need no colored passage.
            if _normalized(clause) == _normalized(profile.identity.animal_name or '') or not (
                _SUBJECTIVE.search(clause) or re.search(r'\b(?:is|has|was|enjoys|heeft|houdt|kan|werd)\b', clause, re.I)):
                continue
            color, code, reason, refs = 'yellow', 'REVIEW_REQUIRED', 'This wording needs a human source comparison; positive semantic support was not established.', []
        passages.append(_passage(start, end, color, reason, refs, code))
        if support_candidates:
            passages[-1]['support_candidates'] = support_candidates
        if color != 'green':
            findings.append(_new_finding(draft_key, code, color, clause, reason, refs,
                **({'support_candidates': support_candidates} if support_candidates else {})))

    name = profile.identity.animal_name
    if name and not name_in_text(text, name):
        findings.append(_new_finding(draft_key, 'MISSING_ANIMAL_NAME', 'red', '',
            'The supported animal name is missing from this standalone draft.', [profile.identity.name_evidence_id] if profile.identity.name_evidence_id else [], required_fact=name))
    for ref, requirement in context['requirements'].items():
        item = requirement['evidence']
        if not conveys_required_fact(text, item, name):
            findings.append(_new_finding(draft_key, 'ESSENTIAL_EVIDENCE_OMITTED', 'red', '',
                requirement['reason'], [ref], kind='omission', required_fact=item.claim,
                requirement_category=requirement['category']))
    for update in profile.updates:
        affected = set(update.evidence_ids + update.superseded_evidence_ids)
        if update.change_type in ('contradiction', 'unclear') or update.unresolved_contradictions:
            findings.append(_new_finding(draft_key, 'SOURCE_CONFLICT', 'yellow', '',
                'Selected-animal updates contain an unresolved conflict; retain both reports and clarify the current situation.', affected,
                kind='source_conflict', required_fact=update.source_excerpt))
            for passage in passages:
                if passage['color'] == 'green' and affected.intersection(passage['source_references']):
                    passage.update(color='yellow', label='Review needed', code='SOURCE_CONFLICT')
        for start, end, claim, _ in annotations:
            stale = set(claim.source_references).intersection(update.superseded_evidence_ids)
            if stale and claim.qualification not in ('historical', 'qualified'):
                reason = 'A superseded report is presented without its historical/update qualification.'
                findings.append(_new_finding(draft_key, 'SUPERSEDED_AS_CURRENT', 'red', claim.excerpt, reason, stale))
                passages.append(_passage(start, end, 'red', reason, stale, 'SUPERSEDED_AS_CURRENT'))
        # Locally matched edited text must obey the same supersession rule.
        for passage in list(passages):
            stale = set(passage['source_references']).intersection(update.superseded_evidence_ids)
            excerpt = text[passage['start']:passage['end']]
            if stale and not HISTORICAL.search(excerpt):
                reason = 'A superseded report is presented as current; review the newer source and preserve the report dates.'
                findings.append(_new_finding(draft_key, 'SUPERSEDED_AS_CURRENT', 'red', excerpt, reason, stale))
                passage.update(color='red', label='Unsupported or conflicting', reason=reason, code='SUPERSEDED_AS_CURRENT')
    conflicts = [(warning.evidence_ids, warning.message) for warning in profile.review_warnings if warning.category == 'conflict']
    superseded = {ref for update in profile.updates for ref in update.superseded_evidence_ids}
    active = [item for item in permitted if item.id not in superseded and not MISSING.search(item.claim)]
    for i, left in enumerate(active):
        for right in active[i + 1:]:
            shared = topics(left.claim).intersection(topics(right.claim))
            if shared and left.temporal_status == right.temporal_status and bool(NEGATIVE.search(left.claim)) != bool(NEGATIVE.search(right.claim)):
                conflicts.append(([left.id, right.id], 'Selected-animal reports give conflicting positive/negative information for the same topic.'))
    for refs, reason in conflicts:
        findings.append(_new_finding(draft_key, 'SOURCE_CONFLICT', 'yellow', '', reason, refs, kind='source_conflict'))
        for passage in passages:
            if passage['color'] == 'green' and set(refs).intersection(passage['source_references']):
                passage.update(color='yellow', label='Review needed', reason=reason, code='SOURCE_CONFLICT')
    hints = []
    for source in manual_sources or ():
        if isinstance(source, dict):
            hints.append(dict(source))
    _apply_manual_sources(text, profile, draft_key, hints, passages, findings, permitted)
    # IDs are stable and repeated detector paths do not duplicate a concern.
    findings = list({item['id']: item for item in findings}.values())
    colors = {item['color'] for item in findings} | {item['color'] for item in passages}
    status = 'red' if 'red' in colors else 'yellow' if 'yellow' in colors else 'green' if passages else 'yellow'
    return dict(status=status, passages=sorted(passages, key=lambda p: (p['start'], p['end'], p['color'])),
                findings=findings, manual_source_hints=hints)


def _apply_manual_sources(text, profile, key, sources, passages, findings, permitted):
    """Explicit, attributed supplementary reports may establish yellow support."""
    for source in sources:
        response = source.get('text') or source.get('response') or source.get('rescuer_response')
        attribution = source.get('attribution') or source.get('respondent')
        date = source.get('confirmation_date') or source.get('response_date')
        animal = source.get('animal_name')
        ref = source.get('id')
        if (not isinstance(response, str) or not isinstance(attribution, str) or not attribution.strip() or
            not isinstance(date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date) or
            not isinstance(animal, str) or _normalized(animal) != _normalized(profile.identity.animal_name or '') or
            not isinstance(ref, str) or not ref.startswith('manual:') or
            source.get('subject', 'selected_animal') != 'selected_animal' or missing_parts(response)):
            continue
        for passage in passages:
            excerpt = text[passage['start']:passage['end']]
            if _normalized(excerpt) != _normalized(response):
                continue
            if UNCERTAIN.search(str(source.get('uncertainty', ''))) and not UNCERTAIN.search(excerpt):
                continue
            related = [item for item in permitted if topics(item.claim).intersection(topics(response)) and not MISSING.search(item.claim)]
            conflicts = [item for item in related if bool(NEGATIVE.search(item.claim)) != bool(NEGATIVE.search(response)) or
                         (numeric_facts(item.claim) and numeric_facts(response) and
                          {(f.kind, f.values, f.unit) for f in numeric_facts(item.claim)} !=
                          {(f.kind, f.values, f.unit) for f in numeric_facts(response)})]
            if conflicts:
                findings.append(_new_finding(key, 'MANUAL_SOURCE_CONFLICT', 'red', excerpt,
                    'This attributed supplementary response conflicts with the original Profile. Preserve both reports and clarify the discrepancy.',
                    [ref] + [item.id for item in conflicts], kind='source_conflict'))
                continue
            reason = 'Supplementary response attributed to ' + attribution + ' on ' + date + '; reported, not independently verified. Preserve its uncertainty.'
            # A matching response resolves only unsupported-evidence concerns for
            # this same assertion, never annotation, identity or conflict findings.
            removable = {'UNSUPPORTED_FACT', 'NONFACTUAL_REFERENCE', 'UNRELATED_EVIDENCE', 'NUMERIC_MISMATCH', 'UNKNOWN_AS_FACT', 'MISSING_NUMERIC_EVIDENCE'}
            findings[:] = [f for f in findings if not (f['excerpt'] == excerpt and f['code'] in removable)]
            passage.update(color='yellow', label='Review needed', code='SOURCE_RESCUER_RESPONSE', reason=reason,
                           source_references=sorted(set(passage['source_references'] + [ref])))
            findings.append(_new_finding(key, 'SOURCE_RESCUER_RESPONSE', 'yellow', excerpt, reason, [ref],
                kind='supplementary_source', attribution=attribution, confirmation_date=date,
                uncertainty=source.get('uncertainty', 'Reported, not independently verified')))


def _questions(drafts, context):
    questions = {}

    def add(topic, refs, reason):
        refs = sorted(set(refs))
        key = (topic, tuple(refs))
        priority = 10 if topic in ('health', 'vaccination', 'sterilization', 'microchip', 'treatment',
            'medical_care', 'travel_readiness', 'adoption_readiness') else 20 if topic in ('cats', 'dogs', 'children', 'home_requirements') else 30
        questions.setdefault(key, dict(id=_stable(topic, '|'.join(refs)), topic=topic,
            question='What is currently known about ' + topic.replace('_', ' ') + ', and what source, date and uncertainty support that report?',
            reason=reason, priority=priority, source_references=refs))

    for gap in context['unknowns']:
        for topic in gap['topics']:
            add(topic, [gap['reference']], 'The Animal Profile records missing or uncertain information about this topic: ' + gap['text'])
    for result in drafts.values():
        for finding in result['findings']:
            if finding['color'] == 'green' or finding['kind'] == 'annotation' or finding['code'] == 'MISSING_ANIMAL_NAME':
                continue
            text = finding.get('required_fact', '') + ' ' + finding['excerpt']
            if finding['kind'] == 'source_conflict':
                text += ' ' + ' '.join(context['references'].get(ref, {}).get('text', '') for ref in finding['source_references'])
            chosen = topics(text)
            if not chosen:
                if matches(MEDICAL, text):
                    chosen = {'medical_care'}
                elif finding['kind'] in ('source_conflict', 'translation'):
                    chosen = {'source_review'}
                else:
                    continue
            for topic in sorted(chosen):
                refs = finding['source_references']
                if not refs:
                    refs = [gap['reference'] for gap in context['unknowns'] if topic in gap['topics']]
                add(topic, refs, finding['reason'])
    return sorted(questions.values(), key=lambda item: (item['priority'], item['topic'], item['id']))


def _translation_checks(bundle, drafts):
    """Compare only shared concrete dimensions, not optional coverage or style."""
    pairs = [(f'website_nl_{i}', f'website_en_{i}') for i in range(1, 4)] + [('facebook_nl', 'facebook_en')]
    polarity_topics = {'vaccination', 'sterilization', 'microchip', 'health', 'children', 'cats', 'dogs',
                       'travel_readiness', 'adoption_readiness'}
    for nl, en in pairs:
        left_text, right_text = _get(_get(bundle, nl), 'text', ''), _get(_get(bundle, en), 'text', '')
        left_facts, right_facts = numeric_facts(left_text), numeric_facts(right_text)
        issues = []
        for kind in ('age', 'weight', 'height', 'medical'):
            left = [fact for fact in left_facts if fact.kind == kind]
            right = [fact for fact in right_facts if fact.kind == kind]
            if not left or not right:
                continue
            def by_role(facts):
                groups = {}
                for fact in facts:
                    role = (bool(HISTORICAL.search(fact.excerpt)), bool(ADULT.search(fact.excerpt)))
                    groups.setdefault(role, []).append(fact)
                return groups
            left_roles, right_roles = by_role(left), by_role(right)
            shared = set(left_roles).intersection(right_roles)
            # Current size and adult prediction are different optional facts.
            # Compare their values only when both drafts actually state that role.
            for role in shared:
                a, b = left_roles[role], right_roles[role]
                if {(f.values, f.unit) for f in a} != {(f.values, f.unit) for f in b}:
                    issues.append(('TRANSLATION_CONTRADICTION', 'red',
                        'The paired Dutch and English drafts give different numerical values or units for ' + kind + '.'))
                elif {estimated(f.excerpt) for f in a} != {estimated(f.excerpt) for f in b}:
                    issues.append(('TRANSLATION_QUALIFICATION', 'yellow',
                        'The paired Dutch and English numerical statements differ in estimate, historical or adult-prediction wording.'))
            if not shared and {(f.values, f.unit) for f in left} == {(f.values, f.unit) for f in right}:
                issues.append(('TRANSLATION_QUALIFICATION', 'yellow',
                    'The paired Dutch and English numerical statements differ in historical or adult-prediction wording.'))
        def polarities(text):
            result = {}
            for _, _, clause in _clauses(text):
                if MISSING.search(clause) or UNCERTAIN.search(clause):
                    continue
                for topic in topics(clause).intersection(polarity_topics):
                    if topic in ('dogs', 'cats', 'children') and not _SOCIAL_ASSERTION.search(clause):
                        continue
                    result.setdefault(topic, set()).add(bool(NEGATIVE.search(clause)))
            return result
        left_polarity, right_polarity = polarities(left_text), polarities(right_text)
        for topic in sorted(set(left_polarity).intersection(right_polarity)):
            if left_polarity[topic] != right_polarity[topic]:
                issues.append(('TRANSLATION_CONTRADICTION', 'red',
                    'The paired Dutch and English drafts reverse the positive/negative meaning for ' + topic.replace('_', ' ') + '.'))
        for code, color, reason in issues:
            for key, other in ((nl, en), (en, nl)):
                result = drafts[key]
                refs = sorted({ref for passage in result['passages'] for ref in passage['source_references']})
                result['findings'].append(_new_finding(key, code, color, '', reason, refs,
                    kind='translation', paired_draft=other))
                if color == 'red' or result['status'] == 'green':
                    result['status'] = color


def assess_bundle(bundle, profile, manual_sources=()):
    """Collect all eight independent reviews and stable, deduplicated questions."""
    drafts = {}
    for key in DRAFT_KEYS:
        draft = _get(bundle, key)
        drafts[key] = assess_text(_get(draft, 'text', ''), profile, key,
                                 _get(draft, 'claims', []), manual_sources)
        for note in _get(draft, 'review_notes', []) or []:
            if isinstance(note, str) and note.startswith('Annotation metadata issue:'):
                drafts[key]['findings'].append(_new_finding(key, 'ANNOTATION_INVALID', 'yellow', '',
                    'Annotation metadata needs review; all readable draft text was retained.'))
                if drafts[key]['status'] == 'green':
                    drafts[key]['status'] = 'yellow'
    _translation_checks(bundle, drafts)
    return dict(version=VERSION, drafts=drafts, questions=_questions(drafts, prepare_writing_context(profile)))
