"""Safe diagnostics and lossless formatting repairs before strict evidence checks."""
from datetime import date
import re

from pydantic import ValidationError

from ai_config import AnalysisError
from animal_profile import AnimalContext, AnimalProfile, Evidence, ReviewWarning, AgeAssessment


class ProfileRuleError(ValueError):
    def __init__(self, code, explanation, **location):
        super().__init__(explanation)
        self.code, self.explanation, self.location = code, explanation, location


class ProfileOutputError(AnalysisError):
    def __init__(self, stage, code, explanation, details=None, candidate=None):
        self.details = {'stage': stage, 'code': code, **(details or {})}
        self.candidate = candidate
        super().__init__(f'AI output failed {stage} validation [{code}]. {explanation} '
                         'It was not saved as a valid profile. No automatic retry was made.')


RULES = {
    'Duplicate evidence IDs': ('DUPLICATE_IDS', 'Two evidence records use the same ID.'),
    'Empty evidence': ('EMPTY_EVIDENCE', 'An evidence record has no claim or supporting excerpt.'),
    'Invalid animal label evidence': ('ANIMAL_LABEL', 'An identity citation does not match the selected dog-name context.'),
    'Invalid photo date evidence': ('PHOTO_DATE', 'A photo-date citation does not match the supplied metadata.'),
    'Invalid visual provenance': ('VISUAL_SOURCE', 'A visual observation has an invalid source, status or temporal label.'),
    'Unknown source reference': ('UNKNOWN_SOURCE', 'A claim cites a source that was not provided.'),
    'Unknown evidence ID': ('UNKNOWN_EVIDENCE_ID', 'A section or update refers to an evidence ID missing from the response.'),
    'Other-animal attribution': ('OTHER_ANIMAL', 'A claim about another animal or an unclear subject was assigned to this dog.'),
    'Visual evidence cannot establish reported animal facts': ('VISUAL_FACT', 'An image/frame observation was used as a reported rescue, behavior, health or adoption fact.'),
    'Identification must be text-supported': ('IDENTIFICATION_SOURCE', 'Identification details were supported only by visual information.'),
    'Unsupported identity': ('IDENTITY_SUPPORT', 'The animal name is not supported by its cited text/context.'),
    'Identity differs from selected animal': ('WRONG_ANIMAL', 'The response names a different animal than the selected dog.'),
    'Name citation without name': ('NAME_REFERENCE', 'The response supplies a name citation without an animal name.'),
    'Update excerpt must be exact': ('UPDATE_QUOTE', 'An update excerpt is not present in the provided Updates text.'),
    'Update evidence must match its excerpt': ('UPDATE_EVIDENCE', 'Update evidence does not match the associated update excerpt.'),
    'Changed update date': ('UPDATE_DATE', 'An update date does not occur in its source excerpt.'),
    'Ambiguous normalized date': ('DATE_NORMALIZATION', 'A date was normalized without an unambiguous ISO source date.'),
    'Changed normalized date': ('DATE_NORMALIZATION', 'A derived date differs from the original source date.'),
    'Age assessment needs evidence': ('AGE_EVIDENCE', 'An age assessment lacks supporting evidence.'),
    'Age reference date differs from photo metadata': ('AGE_PHOTO_DATE', 'An age assessment uses a date not supported by its selected photo.'),
    'Visual age impression needs a dated photo and qualified evidence': ('AGE_VISUAL', 'A visual age impression lacks a dated selected photo or qualified visual evidence.'),
    'Reported age must come from permitted text': ('AGE_SOURCE', 'A reported age was not supported by the permitted text sections.'),
    'Reported age date must be explicitly supported': ('AGE_DATE', 'A reported age date does not occur in the supporting excerpt.'),
}


def rule_error(error):
    if isinstance(error, ProfileRuleError):
        return error
    code, explanation = RULES.get(str(error), ('PROFILE_RULE', 'The profile failed an evidence consistency check.'))
    return ProfileRuleError(code, explanation)


def schema_details(error):
    """Expose field names/positions only, never Pydantic input values or raw messages."""
    schema = AnimalProfile.model_json_schema()
    allowed = set(schema.get('properties', {}))
    for definition in schema.get('$defs', {}).values():
        allowed.update(definition.get('properties', {}))
    errors = error.errors(include_input=False, include_context=False, include_url=False)
    fields = []
    for issue in errors[:4]:
        path = '.'.join(str(part) if isinstance(part, int) or part in allowed else '<unexpected_field>'
                        for part in issue.get('loc', ()))
        fields.append({'field': path, 'type': issue['type']})
    return {'fields': fields, 'error_count': len(errors)}


def source_excerpt(source, quote):
    """Permit whitespace differences only, returning the original verbatim source span."""
    if not quote.strip():
        return None
    if quote in source:
        return quote
    pattern = r'\s+'.join(re.escape(token) for token in quote.strip().split())
    match = re.search(pattern, source)
    return match.group(0) if match else None


def normalize_candidate(candidate, sources, context=None):
    profile = candidate.model_copy(deep=True)
    context = AnimalContext.model_validate(context or AnimalContext())
    for item in profile.evidence:
        source = sources.model_dump().get(item.source_reference)
        if source is not None:
            exact = source_excerpt(source, item.supporting_text_or_description)
            if exact is not None:
                item.supporting_text_or_description = exact
    for update in profile.updates:
        exact = source_excerpt(sources.updates, update.source_excerpt)
        if exact is not None:
            update.source_excerpt = exact
        # Normalized dates are derived locally. Never trust a model's day/month guess.
        if update.original_date and update.original_date in update.source_excerpt and exact is not None:
            normalized = None
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', update.original_date):
                try:
                    normalized = date.fromisoformat(update.original_date).isoformat()
                except ValueError:
                    pass  # Strict validation below still rejects an invalid ISO date.
            if update.normalized_date is not None and update.normalized_date != normalized:
                profile.review_warnings.append(ReviewWarning(category='ambiguous_date' if normalized is None else 'unsupported_conclusion',
                    message='The model-suggested normalized date was discarded. The source date is preserved; date normalization is managed locally.',
                    evidence_ids=list(update.evidence_ids)))
            update.normalized_date = normalized
    name = profile.identity.animal_name
    matching_name = context.animal_name and (name is None or name.strip().casefold() == context.animal_name.strip().casefold())
    ids = {item.id for item in profile.evidence}
    if matching_name:
        profile.identity.animal_name = context.animal_name
        if profile.identity.name_evidence_id not in ids:
            old_ref = profile.identity.name_evidence_id
            ref = 'selected_animal_name'
            while ref in ids:
                ref += '_'
            profile.evidence.append(Evidence(id=ref, claim='Selected animal name: ' + context.animal_name,
                source_type='ANIMAL_LABEL', source_reference='animal_label', supporting_text_or_description=context.animal_name,
                subject='selected_animal', verification_status='reported_not_independently_verified', temporal_status='unclear'))
            profile.identity.name_evidence_id = ref
            profile.identity.identification_evidence_ids = [ref if old_ref is not None and item == old_ref else item
                                                           for item in profile.identity.identification_evidence_ids]
            profile.identity.verification_status = 'reported_not_independently_verified'
    quarantine_non_text_summaries(profile, context, bool(matching_name))
    return profile


def quarantine_non_text_summaries(profile, context, matching_name):
    """Repair category placement without accepting an unsupported visual claim as a fact."""
    text_refs = {'history', 'other_info', 'updates'}
    by_id = {item.id: item for item in profile.evidence}
    # Preserve prior app quarantine on rechecking, but never let model flags hide reported text facts.
    review_only = {ref for ref in profile.review_only_evidence_ids if ref in by_id
                   and by_id[ref].source_reference not in text_refs}
    reported = ('rescue_background', 'personality_and_behavior', 'health_and_special_needs', 'adoption_considerations')
    for field in reported:
        review_only.update(ref for ref in getattr(profile, field) if ref in by_id
                           and by_id[ref].source_reference not in text_refs)
    if not review_only:
        profile.review_only_evidence_ids = []
        return
    for field in (*reported, 'physical_appearance'):
        setattr(profile, field, [ref for ref in getattr(profile, field) if ref not in review_only])
    # The dog name may still be proven independently by the explicit selected-name context.
    if profile.identity.name_evidence_id in review_only and matching_name and context.animal_name:
        ref = 'selected_animal_name'
        while ref in by_id:
            ref += '_'
        item = Evidence(id=ref, claim='Selected animal name: ' + context.animal_name,
            source_type='ANIMAL_LABEL', source_reference='animal_label', supporting_text_or_description=context.animal_name,
            subject='selected_animal', verification_status='reported_not_independently_verified', temporal_status='unclear')
        profile.evidence.append(item)
        by_id[ref] = item
        profile.identity.name_evidence_id = ref
        profile.identity.verification_status = 'reported_not_independently_verified'
    profile.identity.identification_evidence_ids = [ref for ref in profile.identity.identification_evidence_ids if ref not in review_only]
    profile.review_only_evidence_ids = [item.id for item in profile.evidence if item.id in review_only]
    if profile.age_assessment and review_only.intersection(profile.age_assessment.basis_evidence_ids):
        profile.age_assessment = AgeAssessment(estimate=None, reference_date=None, photo_reference=None,
            basis_evidence_ids=[], status='unknown', limitations='The age estimate depended on review-only evidence; confirm age with the rescuer.')
    message = ('Review-only non-text evidence: removed from reported-fact summaries and kept separately for human review. '
               'These observations do not establish rescue circumstances, lasting behavior, health or adoption requirements. '
               'Confirm any such interpretation with the rescuer before using it in adoption drafts.')
    if not any(item.category == 'unsupported_conclusion' and item.message == message
               and set(item.evidence_ids) == review_only for item in profile.review_warnings):
        profile.review_warnings.append(ReviewWarning(category='unsupported_conclusion', message=message,
                                                     evidence_ids=profile.review_only_evidence_ids))
    question = 'Confirm any review-only visual/metadata interpretations with the rescuer; they are not established animal facts.'
    if question not in profile.unknown_information:
        profile.unknown_information.append(question)
