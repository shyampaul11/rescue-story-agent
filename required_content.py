"""Local required-content policy, separate from factual grounding. No I/O or AI.

Field placement alone does not make personality/history/size information essential.
Medical records are conservative: unfamiliar conditions in the health section
stay required. This is a focused language check, not unrestricted semantic proof.
"""
import re
import unicodedata

from analysis_validation import source_excerpt
from draft_evidence import private_text, rejection, NEGATIVE


def name_in_text(text, name):
    name = unicodedata.normalize('NFC', name).strip()
    text = unicodedata.normalize('NFC', text)
    pattern = r'(?<!\w)' + r'\s+'.join(re.escape(word) for word in name.split()) + r'(?!\w)'
    return bool(name and re.search(pattern, text, re.I))


def matches(pattern, text):
    return bool(re.search(r'\b(?:' + pattern + r')\b', text, re.I))


MEDICAL = r'diabet\w*|insulin\w*|epilep\w*|seizur\w*|aanval\w*|disease\w*|illness\w*|ziek\w*|'
MEDICAL += r'injur\w*|blessur\w*|fractur\w*|wound\w*|wond\w*|pain\w*|pijn\w*|'
MEDICAL += r'medicat\w*|medicijn\w*|treatment\w*|behandel\w*|infect\w*|blind\w*|deaf\w*|doof|'
MEDICAL += r'allerg\w*|chronic\w*|chronisch\w*|kidney\w*|nier\w*|surgery|operatie\w*'
RESTRICTION = r'cannot|can.t|must|only|avoid\w*|not (?:good|compatible|safe|tested|suitable|comfortable|friendly)|does not (?:like|tolerate)|without|'
RESTRICTION += r'no (?:cats|dogs|children)|niet (?:met|goed|geschikt|getest)|geen|zonder|uitsluitend|alleen|moet\w*|vermijd\w*'
BEHAVIOR_RISK = r'aggress\w*|reactiv\w*|reactief|bijt\w*|bites?|biting|anxi\w*|angst\w*|panic\w*|'
BEHAVIOR_RISK += r'resource.guard\w*|voedselnijd|escape\w*|ontsnap\w*'
SOCIAL = r'cats?|kat\w*|dogs?|hond\w*|children|child|kids|kind\w*|adults?[- ]only|volwassenen'
NEED = r'need\w*|requir\w*|must|only|cannot|can.t|nodig|moet\w*|uitsluitend|alleen|zonder'
HOME = r'quiet|rustig\w*|training|trainen|omhein\w*|fenc\w*|garden|tuin|exercise|beweging|'
HOME += r'experienced|ervaren|supervis\w*|toezicht|special\w*|speciaal|lifelong|levenslang|care|zorg'
READINESS = r'(?:not )?ready for (?:adoption|travel)|(?:niet )?klaar voor (?:adoptie|reizen)|'
READINESS += r'adoption (?:hold|suspended)|adoptiestop|reisverbod'
BENIGN = r'healthy|gezond\w*|vaccinat\w*|ingeënt\w*|microchip\w*|gechipt|sterili[sz]\w*|neutered|spayed|gecastreerd'
CHANGE = r'recover\w*|herstel\w*|improv\w*|better|verbeter\w*|resolved|genezen|no longer|niet langer'
RESOLVED = r'healed|fully recovered|resolved|genezen|volledig hersteld'
MINOR = r'minor|small scratch|kleine (?:wond|kras)|lichte blessure'

# Bilingual meaning signals prevent a name-only annotation or a partial
# condition mention from satisfying a care/restriction requirement.
FACETS = {
    'diabetes': r'diabet\w*', 'insulin': r'insulin\w*',
    'seizures': r'epilep\w*|seizur\w*|aanval\w*',
    'medication': r'medicat\w*|medicijn\w*',
    'injury': r'injur\w*|\w*blessur\w*|fractur\w*|wound\w*|wond\w*|gebroken|broken',
    'recovery': r'recover\w*|herstel\w*|genezen',
    'daily': r'daily|every day|dagelijks|elke dag|iedere dag',
    'lifelong': r'lifelong|levenslang', 'quiet': r'quiet|rustig\w*',
    'training': r'train\w*', 'fence': r'fenc\w*|omhein\w*',
    'cats': r'cats?|kat\w*', 'dogs': r'dogs?|hond\w*',
    'children': r'children|child|kids|kind\w*|adults?[- ]only|volwassenen',
    'anxiety': r'anxi\w*|angst\w*|panic\w*',
    'aggression': r'aggress\w*|bijt\w*|biting|bites?',
    'treatment': r'treat\w*|behandel\w*', 'care': r'care|zorg',
    'allergy': r'\w*allerg\w*', 'food': r'food|voedsel\w*',
    'kidney': r'kidney\w*|nier\w*', 'heart': r'heart|cardiac\w*|hart\w*',
    'condition': r'disease\w*|illness\w*|condition\w*|syndrome\w*|\w*ziekte\w*|aandoening\w*|syndroom\w*',
    'deaf': r'deaf\w*|doof\w*|dove', 'blind': r'blind\w*',
    'surgery': r'surgery|operat\w*|geopereerd',
    'diet': r'diet\w*|dieet\w*', 'special': r'special\w*|speciaal|aangepast\w*',
    'leg': r'legs?|poot\w*|been', 'experienced': r'experienced|ervaren',
    'exercise': r'exercise|beweging', 'garden': r'garden|tuin',
    'supervision': r'supervis\w*|toezicht',
    'checkups': r'check[- ]?ups?|controles?|controle', 'regular': r'regular|regelmatig',
    'readiness': r'ready|klaar', 'adoption': r'adoption|adopti\w*', 'travel': r'travel|reizen|reis\w*',
}

NOISE = set(('a an the is was were has have had been be to as of for with and or in on at '
             'she her he his him this that dog animal reported report reports according '
             'currently current ongoing now earlier previous historical unspecified time '
             'needs need needed requires require required must cannot without only not no '
             'may might possible possibly suspected approximately around about estimated '
             'home household live living continued further every day receives receiving given '
             'een de het is was heeft had hebben wordt volgens beschreven gemeld met en of '
             'voor in op bij aan deze hond dier haar hij zij hem moet nodig thuis wonen '
             'adopter owner owners rescuer reporter treatment').split())


def anchors(text, name=None, strip_facets=True):
    """Retain unfamiliar substantive terms instead of dropping an unknown diagnosis."""
    if name:
        text = re.sub(re.escape(name), ' ', text, flags=re.I)
    if strip_facets:
        for pattern in FACETS.values():
            text = re.sub(r'\b(?:' + pattern + r')\b', ' ', text, flags=re.I)
    text = re.sub(r'\b(?:' + BENIGN + r')\b', ' ', text, flags=re.I)
    return {word for word in re.findall(r'[^\W\d_]+', text.casefold()) if len(word) > 2 and word not in NOISE}


def routine_health(text, name):
    # The entire record must be routine/benign. A vaccination token must not
    # hide a different, unfamiliar condition or a negative medical status.
    return matches(BENIGN, text) and not NEGATIVE.search(text) and not anchors(text, name, strip_facets=False)


def critical_requirements(profile):
    excluded = set(profile.review_only_evidence_ids)
    health = set(profile.health_and_special_needs)
    selected = {item.id: item for item in profile.evidence if item.subject == 'selected_animal'
                and item.id not in excluded
                and item.source_type in ('HISTORY_FACT', 'OTHER_INFO_FACT', 'UPDATE_FACT')}
    required = {}
    for ref, item in selected.items():
        text = item.claim
        # Clear, resolved minor history need not be repeated in every version.
        resolved_minor = (item.temporal_status == 'historical' and matches(MINOR, text)
                          and matches(RESOLVED, text) and not matches(r'needs?|requires?|nodig', text))
        medical = matches(MEDICAL, text) or (ref in health and not routine_health(text, profile.identity.animal_name))
        if medical and not resolved_minor:
            required[ref] = ('critical_medical', 'A reported medical condition or care need may affect responsible adoption; preserve significant history and uncertainty.')
        elif matches(BEHAVIOR_RISK, text) or (matches(SOCIAL, text) and matches(RESTRICTION, text)):
            required[ref] = ('critical_behavior', 'A behavioral risk or compatibility restriction affects the safety of the animal and household.')
        elif (matches(NEED, text) and matches(HOME, text)) or matches(READINESS, text):
            required[ref] = ('critical_adoption', 'An explicit home, care, training or adoption/travel requirement affects adoption suitability.')
    for update in profile.updates:
        # Material changes inherit criticality from the condition they update,
        # even when a newer recovery report is stored outside a factual array.
        material = bool(set(update.superseded_evidence_ids) & set(required))
        for ref in update.evidence_ids:
            related = ref in selected and matches(MEDICAL + '|' + BEHAVIOR_RISK + '|' + CHANGE + '|' + READINESS, selected[ref].claim)
            if ref in selected and (ref in required or (material and related)):
                required[ref] = ('material_update', 'This update changes a critical condition, care requirement or adoption restriction. Preserve the change and any unresolved uncertainty.')
    return {ref: {'evidence': selected[ref], 'category': category, 'reason': reason, 'animal_name': profile.identity.animal_name}
            for ref, (category, reason) in sorted(required.items())}


def conveys_required_fact(text, item, name=None):
    from unknown_information import missing_parts
    source_gaps, target_gaps = missing_parts(item.claim), missing_parts(text)
    if source_gaps:
        if not target_gaps:
            return False
        for source in source_gaps:
            scope = source['scope'] if item.temporal_status == 'current_as_reported' else (
                'report' if item.temporal_status in ('unclear', 'at_sampled_moment') else item.temporal_status)
            covering = [target for target in target_gaps if target['scope'] == scope or
                        (scope == 'report' and target['scope'] == 'historical')]
            if not source['topics'] <= set().union(*(target['topics'] for target in covering)):
                return False
        return True
    facets = [pattern for pattern in FACETS.values() if matches(pattern, item.claim)]
    if facets and not all(matches(pattern, text) for pattern in facets):
        return False
    if matches(SOCIAL, item.claim) and matches(RESTRICTION, item.claim) and not matches(RESTRICTION, text):
        return False
    if matches(NEED, item.claim) and not matches(NEED + '|' + RESTRICTION + r'|receiv\w*|gets|given|krijgt|ontvangt', text):
        return False
    if NEGATIVE.search(item.claim) and not NEGATIVE.search(text):
        return False
    denial = r'does not (?:have|need|require)|doesn.t (?:have|need|require)|no longer (?:has|needs?|requires?)|heeft geen|niet.*nodig'
    if matches(denial, text) and not matches(denial, item.claim):
        return False
    remaining = anchors(item.claim, name)
    if any(not matches(re.escape(word), text) for word in remaining):
        return False
    return bool(facets or remaining or source_excerpt(text, item.claim) is not None)


def missing_requirements(draft, requirements, evidence):
    missing = []
    for ref, requirement in requirements.items():
        item = requirement['evidence']
        excerpts = []
        for claim in draft.claims:
            # Accept a real equivalent canonical record, not inferred/new IDs.
            equivalent = any(cited in evidence and evidence[cited].claim.strip().casefold() == item.claim.strip().casefold()
                             for cited in claim.source_references)
            from unknown_information import missing_parts
            missing_knowledge = (claim.qualification == 'unknown' and missing_parts(item.claim)
                                 and conveys_required_fact(claim.excerpt, item, requirement['animal_name']))
            if ref in claim.source_references or equivalent or missing_knowledge:
                excerpts.append(claim.excerpt)
        if not conveys_required_fact(' '.join(excerpts), item, requirement['animal_name']):
            missing.append((ref, requirement, bool(excerpts)))
    return missing


def omission_report(key, draft, requirements, evidence):
    report = []
    for ref, requirement, cited in missing_requirements(draft, requirements, evidence):
        item = requirement['evidence']
        present = conveys_required_fact(draft.text, item, requirement['animal_name'])
        reason = 'A critical fact is not covered by an annotation that conveys its required condition, restriction and care information.'
        entry = rejection('ESSENTIAL_EVIDENCE_OMITTED', reason, key, references=[ref],
            action='Review the required fact and its permitted source below. Retain the critical meaning and cite an existing supporting record. If the text is already present, correct its annotation rather than inventing IDs or changing the profile. Recheck saved drafts locally first; request a new paid batch only if needed.')
        entry.update(required_fact=private_text(item.claim), essential_reason=requirement['reason'],
                     requirement_category=requirement['category'],
                     failure_type='missing_critical_annotation' if present else 'missing_critical_information',
                     annotation_status='incomplete' if cited else 'missing',
                     text_presence='Relevant wording found; review its annotation.' if present else 'Required meaning is absent or could not be confirmed locally.',
                     relevant_evidence=[{field: private_text(value) for field, value in item.model_dump(mode='json').items()}])
        report.append(entry)
    return report
