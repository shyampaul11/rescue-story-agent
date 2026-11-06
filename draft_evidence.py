"""Writing reference contract and private rejection reports. No I/O, logging or AI."""
import re


class ContentError(ValueError):
    """Safe writing error; never contains raw provider bodies or credentials."""


def private_text(value):
    """Defence in depth for user-supplied passages/IDs displayed in private review."""
    text = re.sub(r'''(?ix)\b(?:access[_\ -]?token|refresh[_\ -]?token|id[_\ -]?token|
        (?:openai[_\ -]?)?api[_\ -]?key|client[_\ -]?secret|authorization|password)\b
        ["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|(?:Basic|Bearer)\s+[^\s,;&]+|[^\s,;&]+)''',
                  '[credential removed]', str(value))
    return re.sub(r'(?i)\b(?:Bearer\s+\S+|(?:sk|sess)-[\w-]{8,}|ya29\.[\w.-]+|AIza[\w-]{20,})',
                  '[credential removed]', text)


def rejection(code, reason, draft=None, claim_number=None, excerpt='', references=(), origin='generated_text', action=None):
    return {'code': code, 'draft': draft, 'language': ('Dutch' if '_nl' in draft else 'English') if draft else None,
            'claim_number': claim_number, 'excerpt': private_text(excerpt),
            'source_references': [private_text(ref) for ref in references], 'reason': reason, 'origin': origin,
            'action': action or ('Correct the source/profile and reanalyze the selected animal before writing.'
                if origin == 'animal_profile' else
                'Keep this draft blocked. Review the cited source; omit unsupported optional facts or explicitly generate a corrected batch. Do not substitute unrelated evidence IDs.')}


class ContentValidationError(ContentError):
    def __init__(self, code, explanation, draft=None, claim_number=None, report=None, origin='generated_text', **checks):
        # Shareable diagnostic contains no passages or evidence IDs. Those stay in report.
        self.details = {'code': code, 'origin': origin}
        self.explanation = explanation
        if draft is not None:
            self.details.update(draft=draft, language='Dutch' if '_nl' in draft else 'English')
        if claim_number is not None:
            self.details['claim_number'] = claim_number
        self.report = report or []
        if report:
            self.details.update(rejection_count=len(report), rejection_codes=sorted({item['code'] for item in report}))
        self.details.update(checks)
        self.candidate, self.usage = None, {}
        location = (f' ({draft}, claim {claim_number})' if claim_number is not None else f' ({draft})') if draft else ''
        super().__init__(f'[{code}]{location} {explanation} No automatic retry was made.')


UNKNOWN = re.compile(r'\b(?:unknown|not (?:yet )?(?:known|tested|assessed|confirmed)|unclear|'
                     r'needs? confirmation|requires? confirmation|to be confirmed|onbekend|onduidelijk|'
                     r'(?:nog )?niet (?:bekend|getest|beoordeeld|bevestigd)|moet.*bevestigd)\b', re.I)
UNCERTAIN = re.compile(r'\b(?:possible|possibly|may|might|perhaps|likely|appears?|suspected|uncertain|'
                       r'estimated|approximately|expected|possibly|mogelijk|vermoedelijk|lijkt|'
                       r'waarschijnlijk|geschat|ongeveer|verwacht|onzeker)\b', re.I)
NEGATIVE = re.compile(r"\b(?:not|no|never|cannot|can't|without|niet|geen|nooit|zonder)\b", re.I)
TOPICS = {
    'vaccination': r'vaccin\w*|ingeënt\w*|geënt\w*',
    'sterilization': r'sterili[sz]\w*|neuter\w*|castrat\w*|spayed|gesteriliseerd\w*',
    'microchip': r'microchip\w*|gechipt\w*',
    'injury': r'injur\w*|blessure\w*|fractur\w*|broken|gebroken|wound\w*|wond\w*',
    'recovery': r'recover\w*|herstel\w*',
    'treatment': r'treat\w*|behandel\w*|medicat\w*|medicijn\w*',
    'dogs': r'(?:with|around|meeting|other|met|andere)\s+(?:(?:other|andere)\s+)?(?:dogs|honden)|dog[- ]friendly|hondvriendelijk|(?:dogs|honden)\s*:',
    'cats': r'cats?|katten', 'children': r'children|child|kids|kinderen',
    'weight': r'weigh\w*|gewicht\w*|kg|kilo\w*',
}


def reference_catalog(profile):
    """One exact namespace shared by prompt, constrained schema and validator."""
    excluded = set(profile.review_only_evidence_ids)
    catalog = {item.id: {'kind': 'evidence', 'text': item.claim + '\n' + item.supporting_text_or_description,
                         'evidence_ids': [item.id]}
               for item in profile.evidence if item.subject == 'selected_animal' and item.id not in excluded}
    selected = set(catalog)
    catalog.update({f'unknown:{i}': {'kind': 'unknown', 'text': text, 'evidence_ids': []}
                    for i, text in enumerate(profile.unknown_information)})
    catalog.update({f'warning:{i}': {'kind': 'warning', 'text': item.message, 'evidence_ids': []}
                    for i, item in enumerate(profile.review_warnings)})
    for i, item in enumerate(profile.updates):
        refs = item.evidence_ids + item.superseded_evidence_ids
        if refs and set(refs) <= selected:
            catalog[f'update:{i}'] = {'kind': 'update', 'text': item.source_excerpt, 'evidence_ids': refs}
    age = profile.age_assessment
    if age:
        catalog['age_assessment'] = {'kind': 'unknown' if age.status == 'unknown' else 'age',
            'text': age.model_dump_json(), 'evidence_ids': age.basis_evidence_ids}
    return catalog


def public_reference(text, catalog):
    """Recognize explicit citation markers without banning ordinary ID words."""
    for ref in catalog:
        token = re.escape(ref)
        if re.search(r'\[\s*' + token + r'\s*\]|\{\s*' + token + r'\s*\}|'
                     r'\b(?:evidence|source|bron|reference)\s*:\s*' + token + r'(?!\w)', text, re.I):
            return ref
        if re.search(r'[A-Za-z].*\d|[_:]', ref) and re.search(r'(?<!\w)' + token + r'(?!\w)', text):
            return ref
    return None


def reference_rejections(bundle, profile, catalog):
    """Collect all invalid ID/subject references across all eight drafts at once."""
    from content_models import DRAFT_KEYS
    evidence = {item.id: item for item in profile.evidence}
    report = []
    for key in DRAFT_KEYS:
        for index, claim in enumerate(getattr(bundle, key).claims, start=1):
            for ref in claim.source_references:
                if ref in catalog:
                    continue
                item = evidence.get(ref)
                if item and item.subject != 'selected_animal':
                    code, reason = 'OTHER_ANIMAL_EVIDENCE', 'This evidence belongs to another animal or has an unclear subject.'
                elif item:
                    code, reason = 'REVIEW_ONLY_EVIDENCE', 'This evidence is quarantined for review and cannot support adoption drafts.'
                else:
                    code, reason = 'UNKNOWN_EVIDENCE_ID', 'This exact reference is absent from the permitted reference catalog. IDs must not be invented, renumbered or replaced with source categories.'
                report.append(rejection(code, reason, key, index, claim.excerpt, [ref]))
    return report


def semantic_problem(claim, evidence, catalog, context=None):
    """Conservative English/Dutch consistency signals, not a semantic truth guarantee."""
    refs = claim.source_references
    records = [evidence[ref] for ref in refs if ref in evidence]
    from unknown_information import MISSING, unknown_problem
    unknown_refs = [ref for ref in refs if catalog[ref]['kind'] == 'unknown']
    unknown_records = [item for item in records if MISSING.search(item.claim)]
    if claim.qualification == 'unknown':
        if context is None:
            return 'UNSUPPORTED_UNKNOWN', 'Unknown statements require the shared prepared Animal Profile context.'
        return unknown_problem(claim, context)
    else:
        if unknown_refs or unknown_records:
            return 'UNKNOWN_AS_FACT', 'Missing information cannot support either an affirmative or a negative animal assertion.'
        if not records and not any(catalog[ref]['kind'] == 'age' for ref in refs):
            return 'NONFACTUAL_REFERENCE', 'A factual claim needs selected-animal evidence, not only a warning, update or unknown.'
        source = ' '.join(item.claim for item in records)
        source += ' ' + ' '.join(catalog[ref]['text'] for ref in refs if catalog[ref]['kind'] == 'age')
    for topic, pattern in TOPICS.items():
        if re.search(r'\b(?:' + pattern + r')\b', claim.excerpt, re.I) and not re.search(r'\b(?:' + pattern + r')\b', source, re.I):
            return 'UNRELATED_EVIDENCE', f'This {topic} statement is unsupported by the cited evidence. A valid ID alone is insufficient.'
    # Wording must retain uncertainty, not merely set a metadata qualification.
    from numeric_evidence import numeric_facts, matching_fact
    facts = numeric_facts(claim.excerpt)
    numeric = bool(facts)
    # Numeric checks already preserve qualifiers of matching numerical source
    # clauses. Other cited facts still need the ordinary uncertainty check.
    # A year must never exempt a separate possible medical/social statement.
    uncertain = any((UNCERTAIN.search(item.claim) or item.verification_status == 'interpretation_requires_review')
                    and not any(matching_fact(fact, source) for fact in facts for source in numeric_facts(item.claim))
                    for item in records)
    uncertain = uncertain or any(catalog[ref]['kind'] == 'age' and profile_age_uncertain(catalog[ref]['text']) for ref in refs)
    if uncertain and (claim.qualification not in ('qualified', 'historical', 'observed_moment') or not UNCERTAIN.search(claim.excerpt)):
        return 'UNCERTAINTY_LOST', 'A possible, estimated or review-required interpretation became definite in the draft.'
    # Reject changed polarity for critical topics using canonical, atomic profile claims.
    critical = any(re.search(r'\b(?:' + pattern + r')\b', claim.excerpt, re.I) for topic, pattern in TOPICS.items()
                   if topic != 'weight' or not numeric)
    if critical and records and any(NEGATIVE.search(item.claim) for item in records) and not NEGATIVE.search(claim.excerpt):
        return 'NEGATION_LOST', 'A negative medical/social statement lost its negation. Confirm the wording against the cited evidence.'
    return None


def profile_age_uncertain(text):
    return 'qualified_visual_estimate' in text or bool(UNCERTAIN.search(text))
