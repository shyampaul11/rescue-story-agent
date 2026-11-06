"""One prepared, local contract for eight-draft writing and validation. No I/O."""
from draft_evidence import reference_catalog, UNCERTAIN
from required_content import critical_requirements
from unknown_information import topics, missing_parts, source_missing_parts, MISSING
from required_content import name_in_text
import re
from numeric_evidence import numeric_facts, ADULT, ESTIMATE, DATE


def prepare_writing_context(profile):
    references = reference_catalog(profile)
    required = critical_requirements(profile)
    facts, unknowns, contexts, known, missing_refs = [], [], {}, set(), set()
    permitted_evidence = {item.id: item for item in profile.evidence if item.id in references}

    def selected_context(text, ids=(), category=None):
        if category == 'other_animal' or any(ref not in permitted_evidence for ref in ids):
            return False
        other = re.search(r"\b(?:mother|father|sibling|rescuer|moeder|vader|broer|zus|redder)\b", text, re.I)
        rescuer = profile.source_attribution.rescuer_name if profile.source_attribution else None
        other = other or (rescuer and name_in_text(text, rescuer))
        return not other or (profile.identity.animal_name and name_in_text(text, profile.identity.animal_name))

    def add_unknown(reference, text, parts, scope=None, source_type='PROFILE_UNKNOWN_INFORMATION',
                    verification_status='profile_missing_information', temporal_status='current_profile_gap'):
        for part in parts:
            unknowns.append({'reference': reference, 'text': text, 'topics': sorted(part['topics']),
                             'state': part['state'], 'scope': scope or part['scope'],
                             'subject': 'selected_animal', 'source_type': source_type,
                             'verification_status': verification_status, 'temporal_status': temporal_status})

    for index, text in enumerate(profile.unknown_information):
        if selected_context(text):
            add_unknown(f'unknown:{index}', text, source_missing_parts(text))
        else:
            contexts[f'unknown:{index}'] = set()
    for item in profile.evidence:
        if item.id not in references:
            continue
        parts = missing_parts(item.claim)
        has_missing = bool(MISSING.search(item.claim))
        item_topics = topics(item.claim)
        if item.temporal_status == 'historical' and 'current_age' in item_topics:
            item_topics.remove('current_age')
            item_topics.add('historical_age')
        contexts[item.id] = item_topics.copy()
        if 'historical_age' in item_topics:
            contexts[item.id].update(('current_age', 'birth_date'))
        if has_missing:
            missing_refs.add(item.id)
            scope = None if item.temporal_status == 'current_as_reported' else item.temporal_status
            add_unknown(item.id, item.claim, parts or source_missing_parts(item.claim), scope,
                        item.source_type, item.verification_status, item.temporal_status)
        else:
            if item.temporal_status != 'historical':
                known.update(item_topics)
            elif 'birth_date' in item_topics:
                known.add('birth_date')
            numeric = numeric_facts(item.claim)
            estimated = bool(UNCERTAIN.search(DATE.sub('', item.claim)) or ESTIMATE.search(DATE.sub('', item.claim)) or ADULT.search(item.claim))
            qualifications = ['historical', 'qualified'] if item.temporal_status == 'historical' else (
                ['observed_moment', 'qualified'] if item.source_type in ('PHOTO_OBSERVATION', 'VIDEO_FRAME_OBSERVATION', 'PHOTO_DATE_METADATA') else
                ['qualified', 'historical'] if estimated or item.verification_status == 'interpretation_requires_review' else ['reported', 'qualified'])
            facts.append({'reference': item.id, 'canonical_claim': item.claim,
                'supporting_quote': item.supporting_text_or_description,
                'subject': item.subject, 'source_type': item.source_type, 'source_reference': item.source_reference,
                'verification_status': item.verification_status, 'temporal_status': item.temporal_status,
                'knowledge_state': 'reported' if item.source_type.endswith('_FACT') or item.source_type == 'ANIMAL_LABEL' else 'observed',
                'allowed_qualifications': qualifications,
                'numeric_facts': [{'kind': fact.kind, 'values': fact.values, 'unit': fact.unit,
                                   'adult_prediction': fact.kind == 'weight' and bool(ADULT.search(fact.excerpt)),
                                   'estimated': bool(ESTIMATE.search(fact.excerpt))} for fact in numeric],
                'required': item.id in required,
                'required_reason': required[item.id]['reason'] if item.id in required else None})
    for ref, entry in references.items():
        contexts.setdefault(ref, topics(entry['text']))
        if entry['kind'] == 'warning':
            warning = profile.review_warnings[int(ref.split(':')[1])]
            if selected_context(entry['text'], warning.evidence_ids, warning.category):
                add_unknown(ref, entry['text'], source_missing_parts(entry['text']), source_type='PROFILE_WARNING',
                            verification_status='context_only_not_factual_proof', temporal_status='profile_review_context')
            else:
                contexts[ref] = set()
                entry['allows_selected_context'] = False
    age = profile.age_assessment
    if age and age.status == 'unknown':
        add_unknown('age_assessment', age.limitations, [{'topics': {'current_age'}, 'state': 'unknown', 'scope': 'current'}])
    # Missing states are not factual proof, regardless of their provenance kind.
    missing_refs.update(entry['reference'] for entry in unknowns)
    for ref, entry in references.items():
        evidence_unknown = ref in missing_refs and ref not in {fact['reference'] for fact in facts}
        entry['knowledge_state'] = 'missing_information' if evidence_unknown else ('context' if entry['kind'] in ('warning', 'update') else entry['kind'])
        entry['allows_factual_assertions'] = entry['kind'] in ('evidence', 'age') and not evidence_unknown
    payload = {'version': 'shared-writing-context-1', 'facts': facts,
               'unknown_information': unknowns,
               'context': [{'reference': ref, 'kind': item['kind'], 'text': item['text']} for ref, item in references.items()
                           if item['kind'] in ('warning', 'update') and item.get('allows_selected_context', True)],
               'age_assessment': age.model_dump(mode='json') if age else None,
               'mandatory_evidence': list(required)}
    return {'references': references, 'facts': facts, 'unknowns': unknowns, 'context_topics': contexts,
            'known_current_topics': known, 'requirements': required, 'payload': payload}
