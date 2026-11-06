"""Scoped missing-information language shared by catalog, prompt and validation.

Missing knowledge is not a positive/negative animal fact. These bounded English
and Dutch constructions deliberately require review for unrecognized wording.
"""
import re

TOPIC_PATTERNS = {
    'birth_date': r'birth\w*|birthday|born|geboorte\w*|geboren',
    'current_age': r'age|aged|leeftijd|months? old|maanden? oud',
    'vaccination': r'vaccin\w*|ingeënt\w*|geënt\w*',
    'sterilization': r'sterili[sz]\w*|neuter\w*|spayed|castrat\w*|gesteriliseerd|gecastreerd',
    'microchip': r'microchip\w*|gechipt\w*',
    'health': r'health|healthy|medical conditions?|gezond\w*|medische toestand',
    'treatment': r'treat\w*|medicat\w*|medicijn\w*|behandel\w*',
    'children': r'children|child|kids|kinderen|kind',
    'cats': r'cats?|katten|kat', 'dogs': r'dogs|honden',
    'current_weight': r'weight|weigh\w*|gewicht|kg|kilo\w*',
    'height': r'height|shoulder height|hoogte|schofthoogte|centimet\w*|cm',
    'breed': r'breed|ras|rassen', 'sex': r'sex|gender|geslacht',
    'location': r'location|whereabouts|locatie|verblijf\w*',
    'home_requirements': r'household|home requirements?|home needs?|household requirements?|wooneisen|thuisvereisten',
    'exercise': r'exercise|beweging\w*', 'training': r'training|trainingsbehoefte\w*',
    'travel_readiness': r'travel|reizen|reis\w*', 'adoption_readiness': r'adoption|adoptie\w*',
    'photo_date': r'photo (?:capture |reference )?dates?|capture dates?|fotodat\w*|opnamedat\w*',
}
MISSING = re.compile(r'\b(?:unknown|unclear|unreported|unconfirmed|undetermined|'
    r'(?:not|never)(?:\s+\w+){0,4}\s+(?:known|tested|assessed|confirmed|established|reported|provided|verified)|'
    r'(?:cannot|does not) establish|no (?:usable|information|details)|needs? confirmation|requires? confirmation|'
    r'to be confirmed|onbekend|onduidelijk|niet(?:\s+\w+){0,3}\s+(?:bekend|duidelijk|getest|beoordeeld|bevestigd|vastgesteld|onderzocht|geverifieerd)|'
    r'niets bekend|geen (?:informatie|gegevens)|moet(?:\s+\w+){0,3}\s+bevestigd)\b', re.I)
ASSESSED = re.compile(r'\b(?:not|never|niet|nooit)(?:\s+\w+){0,4}\s+(?:tested|assessed|examined|getest|onderzocht|beoordeeld)\b', re.I)
VERIFICATION = re.compile(r'\b(?:not independently verified|niet onafhankelijk (?:geverifieerd|bevestigd))\b', re.I)
HISTORICAL = re.compile(r'\b(?:earlier|previous|historical|at (?:the|that) time|as of (?:the|that) report|'
                        r'destijds|toen|eerder\w*|historisch\w*|ten tijde)\b', re.I)
ASSERTION = re.compile(r'\b(?:suitable|unsuitable|compatible|incompatible|healthy|unhealthy|vaccinated|unvaccinated|'
    r'good with|bad with|not good with|geschikt|ongeschikt|gezond|ongezond|gevaccineerd|goed met|'
    r'weighs|weegt|has diabetes|heeft diabetes)\b', re.I)
WHETHER = re.compile(r'^\s*(?:whether|of\b|it is (?:unknown|unclear) whether|het is (?:onbekend|onduidelijk) of)', re.I)


def topics(text):
    result = {topic for topic, pattern in TOPIC_PATTERNS.items()
              if re.search(r'\b(?:' + pattern + r')\b', text, re.I)}
    if 'current_weight' in result and re.search(r'\b(?:adult|volwassen|fully grown)\b', text, re.I):
        result.remove('current_weight')
        result.add('adult_weight')
    if VERIFICATION.search(text):
        result = {'verification:' + topic for topic in result}
    return result


def missing_parts(text):
    """Return (topic set, knowledge state, time scope) for each pure clause.

    Reject mixed assertions rather than letting one 'unknown' token qualify an
    entire paragraph. No numerical animal value is established by this policy.
    """
    from numeric_evidence import numeric_facts
    if numeric_facts(text):
        return None
    parts = []
    for clause in re.split(r'[.!?;\n]+|\b(?:but|maar|however|echter|although|hoewel)\b', text, flags=re.I):
        clause = clause.strip(' #▪️💙💖💛:-,')
        if not clause:
            continue
        if not MISSING.search(clause) or not topics(clause):
            return None
        # Once a missing-information predicate has closed, a later clause must
        # describe a gap of its own. A leading 'whether' cannot scope a later
        # 'and she is vaccinated' or an unrecognized factual predicate.
        marker = MISSING.search(clause)
        connector = next((match for match in re.finditer(r',|\b(?:and|en|while|terwijl)\b', clause, re.I)
                          if match.start() >= marker.end()), None)
        if connector:
            before, after = missing_parts(clause[:connector.start()]), missing_parts(clause[connector.end():])
            if not before or not after:
                return None
            parts.extend(before + after)
            continue
        if ASSERTION.search(clause) and not WHETHER.search(clause):
            return None
        state = 'not_verified' if VERIFICATION.search(clause) else 'not_assessed' if ASSESSED.search(clause) else 'unknown'
        report_scoped = re.search(r'\b(?:in|from) the (?:provided|supplied|undated) report\b|\bin het (?:aangeleverde|ongedateerde) verslag\b', clause, re.I)
        parts.append({'topics': topics(clause), 'state': state,
                      'scope': 'report' if report_scoped else 'historical' if HISTORICAL.search(clause) else 'current'})
    return parts or None


def source_missing_parts(text):
    """Extract explicit source gaps independently from explanatory source prose."""
    result = []
    for clause in re.split(r'[.!?;\n]+', text):
        parsed = missing_parts(clause)
        if parsed:
            result.extend(parsed)
    return result


def unknown_problem(claim, context):
    parts = missing_parts(claim.excerpt)
    if not parts:
        return 'UNKNOWN_AS_FACT', 'An unknown annotation contains an affirmative/negative animal assertion, a numeric value, or an unrecognized mixed clause. Separate supported facts from missing information.'
    requested = set().union(*(part['topics'] for part in parts))
    for ref in claim.source_references:
        if not requested.intersection(context['context_topics'].get(ref, set())):
            return 'UNRELATED_EVIDENCE', 'A supplied context reference concerns a different topic. Unknown wording must match the Animal Profile; a valid ID alone is insufficient.'
    for part in parts:
        for topic in part['topics']:
            candidates = [entry for entry in context['unknowns'] if topic in entry['topics']]
            if not candidates:
                return 'UNSUPPORTED_UNKNOWN', 'This missing-information topic is not established by the prepared Animal Profile unknown catalog.'
            scoped = [entry for entry in candidates if entry['scope'] == part['scope'] or
                      (part['scope'] == 'report' and entry['scope'] in ('historical', 'unclear', 'at_sampled_moment'))]
            if not scoped:
                return 'UNKNOWN_TEMPORAL_SCOPE', 'A historical missing-information report cannot establish current knowledge/testing status. Preserve its reporting time.'
            if part['state'] == 'not_assessed' and not any(entry['state'] == 'not_assessed' for entry in scoped):
                return 'UNSUPPORTED_UNKNOWN', 'Unknown or unreported information does not establish that testing/assessment never occurred.'
            if part['state'] == 'not_verified' and not any(entry['state'] == 'not_verified' for entry in scoped):
                return 'UNSUPPORTED_UNKNOWN', 'The profile does not establish this claim about missing independent verification.'
            if topic in context['known_current_topics'] and part['scope'] == 'current':
                return 'UNKNOWN_PROFILE_CONFLICT', 'The profile contains a selected-animal report for this topic. Do not replace a known positive/negative report with a blanket unknown.'
    return None
