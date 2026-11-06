"""Structured output: every factual section references an evidence record."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AnimalSources(StrictModel):
    # Deliberately no document bytes, metadata, folder name, or arbitrary dict.
    history: str = ''
    other_info: str = ''
    updates: str = ''


class AnimalContext(StrictModel):
    """Only the user's explicit naming convention; never arbitrary Drive metadata."""
    animal_name: str | None = Field(default=None, max_length=200)
    rescuer_name: str | None = Field(default=None, max_length=200)


class SourceAttribution(StrictModel):
    rescuer_name: str | None
    source_reference: Literal['rescuer_attribution', 'unknown']
    provided_sections: list[Literal['history', 'other_info', 'updates']]
    verification_note: str


class PhotoDateReference(StrictModel):
    source_reference: str
    original_value: str | None
    reference_date: str | None
    date_source: Literal['exif_datetime_original', 'drive_photo_time', 'exif_datetime_digitized', 'exif_datetime', 'unknown']
    verification_status: Literal['metadata_not_independently_verified', 'unknown']
    note: str


class AgeAssessment(StrictModel):
    estimate: str | None
    reference_date: str | None
    photo_reference: str | None
    basis_evidence_ids: list[str]
    status: Literal['unknown', 'reported_age', 'qualified_visual_estimate']
    limitations: str


SourceType = Literal['HISTORY_FACT', 'OTHER_INFO_FACT', 'UPDATE_FACT',
                     'PHOTO_OBSERVATION', 'VIDEO_FRAME_OBSERVATION', 'ANIMAL_LABEL', 'PHOTO_DATE_METADATA']


class Evidence(StrictModel):
    id: str
    claim: str
    source_type: SourceType
    source_reference: str
    supporting_text_or_description: str
    subject: Literal['selected_animal', 'other_animal', 'unclear']
    verification_status: Literal['reported_not_independently_verified', 'direct_visual_observation',
                                 'interpretation_requires_review', 'metadata_not_independently_verified']
    temporal_status: Literal['historical', 'current_as_reported', 'at_sampled_moment', 'unclear']
    reported_by: str | None = None


class Identity(StrictModel):
    animal_name: str | None
    name_evidence_id: str | None
    identification_evidence_ids: list[str]
    verification_status: Literal['reported_not_independently_verified', 'unknown', 'requires_review']


class ProfileUpdate(StrictModel):
    original_date: str | None
    normalized_date: str | None = Field(description='ISO date only for an unambiguous original date; otherwise null.')
    source_excerpt: str
    evidence_ids: list[str]
    change_type: Literal['adds_information', 'supersedes', 'contradiction', 'unclear']
    superseded_evidence_ids: list[str]
    unresolved_contradictions: list[str]


class ReviewWarning(StrictModel):
    category: Literal['conflict', 'ambiguous_date', 'uncertain_medical', 'other_animal',
                      'unsupported_conclusion', 'missing_adoption_details', 'media_limitations',
                      'photo_date', 'age_uncertainty']
    message: str
    evidence_ids: list[str]


class AnimalProfile(StrictModel):
    identity: Identity
    rescue_background: list[str] = Field(description='Evidence IDs for rescue circumstances, prior living conditions and developments.')
    personality_and_behavior: list[str] = Field(description='Evidence IDs for reported temperament, social behavior, activities, training and behavioral needs.')
    physical_appearance: list[str] = Field(description='Evidence IDs for visible characteristics, markings and sampled activities.')
    health_and_special_needs: list[str]
    adoption_considerations: list[str]
    updates: list[ProfileUpdate]
    unknown_information: list[str]
    evidence: list[Evidence]
    review_warnings: list[ReviewWarning]
    source_attribution: SourceAttribution | None = Field(default=None, description='Application-managed rescuer attribution; return null.')
    photo_date_references: list[PhotoDateReference] = Field(default_factory=list, description='Application-managed selected photo dates; return an empty list.')
    age_assessment: AgeAssessment | None = Field(default=None, description='A cautious age or life-stage assessment with evidence, or null when unknown. Photo dates are not birth dates.')
    review_only_evidence_ids: list[str] = Field(default_factory=list, description='Application-managed quarantine of unapproved evidence; return an empty list.')
