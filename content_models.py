"""Milestone 6 structured writing output; plain copyable text plus review provenance."""
from typing import Literal

from pydantic import Field, create_model, BaseModel, model_validator
import json

from animal_profile import StrictModel

Perspective = Literal['third_person', 'first_person']
DRAFT_KEYS = ('website_nl_1', 'website_nl_2', 'website_nl_3',
              'website_en_1', 'website_en_2', 'website_en_3', 'facebook_nl', 'facebook_en')
STYLES = {'1': 'Warm and personal', '2': 'Clear and informative', '3': 'Engaging storytelling'}


class DraftClaim(StrictModel):
    excerpt: str = Field(description='Exact contiguous factual clause from this draft. For each numerical occurrence include its subject, value, unit and uncertainty/time qualifiers, never only the digits. Include numbers in headings and Facebook facts.')
    source_references: list[str] = Field(description='Exact existing references from reference_catalog. Factual assertions require supporting fact IDs. An unknown annotation may use [] or relevant existing context references; its missing-information wording must match writing_catalog. Unknowns never support positive or negative animal assertions.')
    qualification: Literal['reported', 'observed_moment', 'qualified', 'historical', 'unknown']


class ContentDraft(StrictModel):
    text: str = Field(description='Standalone adoption copy with the supported identity name and all mandatory critical facts. Optional descriptive facts may vary by style.')
    claims: list[DraftClaim] = Field(description='Evidence annotations for chosen facts and all mandatory critical information. A separate name-only annotation is optional because identity is checked directly.')
    review_notes: list[str]


class GeneratedDrafts(StrictModel):
    perspective: Perspective
    website_nl_1: ContentDraft
    website_nl_2: ContentDraft
    website_nl_3: ContentDraft
    website_en_1: ContentDraft
    website_en_2: ContentDraft
    website_en_3: ContentDraft
    facebook_nl: ContentDraft
    facebook_en: ContentDraft


def grounded_draft_schema(references):
    """Constrain citations for this profile; keep the persisted eight-draft format."""
    choices = tuple(sorted(references))
    if not choices or len(choices) > 900 or (len(choices) > 250 and sum(map(len, choices)) > 14000):
        raise ValueError('Reference catalog exceeds the supported structured-output budget.')
    reference_type = Literal[choices]
    claim = create_model('GroundedDraftClaim', __base__=DraftClaim,
                         source_references=(list[reference_type], Field(description='Copy exact permitted references; never invent IDs. Factual assertions require nonempty supporting fact references. Unknown annotations may use [] or relevant existing context; match the prepared unknown catalog and never turn missing information into a positive/negative assertion.')))
    draft = create_model('GroundedContentDraft', __base__=ContentDraft, claims=(list[claim], ...))
    return create_model('GroundedGeneratedDrafts', __base__=GeneratedDrafts,
                        **{key: (draft, ...) for key in DRAFT_KEYS})


class ContentReviewIssue(StrictModel):
    code: str
    message: str
    draft: str | None = None
    source_references: list[str] = Field(default_factory=list)


class ReviewableClaim(StrictModel):
    """Wire metadata is reviewable data, rather than an acceptance certificate."""
    excerpt: str | None
    source_references: list[str] | None
    qualification: str | None


class ReviewableContentDraft(StrictModel):
    text: str
    claims: list[ReviewableClaim] | None
    review_notes: list[str] | None

    @model_validator(mode='before')
    @classmethod
    def retain_annotation_faults(cls, value):
        # Runs inside the SDK parse boundary, before nested annotation types.
        # The eight text fields still undergo normal strict model validation.
        return _retained_draft(value)


def editorial_draft_schema(references):
    # Keep the catalog budget guard. IDs remain explicit in the prompt; a bad
    # model citation must reach editorial assessment instead of losing prose.
    grounded_draft_schema(references)
    return create_model('ReviewableGeneratedDrafts', __base__=GeneratedDrafts,
                        **{key: (ReviewableContentDraft, ...) for key in DRAFT_KEYS})


def retained_drafts(value):
    """Validate the eight-text envelope; quarantine malformed metadata explicitly.

    No prose, evidence ID or qualification is repaired. Valid annotations survive
    unchanged; malformed ones are retained as private review notes for diagnosis.
    """
    from draft_evidence import private_text
    raw = value.model_dump(mode='json') if isinstance(value, BaseModel) else value
    if not isinstance(raw, dict):
        raise ValueError('Invalid eight-draft envelope')
    clean = {'perspective': raw.get('perspective')}
    for key in DRAFT_KEYS:
        clean[key] = _retained_draft(raw.get(key))
    return GeneratedDrafts.model_validate(clean)


def _retained_draft(draft):
    from draft_evidence import private_text
    if not isinstance(draft, dict) or not isinstance(draft.get('text'), str) or not draft['text'].strip():
        raise ValueError('Incomplete eight-draft text')
    notes = draft.get('review_notes')
    clean_notes = list(notes) if isinstance(notes, list) and all(isinstance(note, str) for note in notes) else []
    if notes is not None and not clean_notes and notes != []:
        clean_notes.append('Annotation metadata issue: malformed review notes.')
    claims, clean_claims = draft.get('claims'), []
    if not isinstance(claims, list):
        clean_notes.append('Annotation metadata issue: missing or malformed claim list; factual support needs review.')
    else:
        for index, claim in enumerate(claims, 1):
            if isinstance(claim, BaseModel):
                claim = claim.model_dump(mode='json')
            try:
                clean_claims.append(DraftClaim.model_validate(claim).model_dump(mode='json'))
            except (ValueError, TypeError):
                clean_notes.append('Annotation metadata issue: claim ' + str(index) + ' was quarantined: ' +
                                   private_text(json.dumps(claim, ensure_ascii=False, default=str)))
    return {'text': draft['text'], 'claims': clean_claims, 'review_notes': clean_notes}
