"""Typed contracts for Evidence extraction and Data Service publication."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

_RAW_EVIDENCE_ID_PATTERN = r"^RAW[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_EVIDENCE_ID_PATTERN = r"^EVD[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_EVIDENCE_CATEGORY_ID_PATTERN = r"^EVC[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
RawEvidenceID = Annotated[str, Field(pattern=_RAW_EVIDENCE_ID_PATTERN)]
EvidenceID = Annotated[str, Field(pattern=_EVIDENCE_ID_PATTERN)]
EvidenceCategoryID = Annotated[str, Field(pattern=_EVIDENCE_CATEGORY_ID_PATTERN)]


class EvidenceCheckpoint(BaseModel):
    """Durable cursor into the append-only Raw Collection manifest index."""

    schema_version: Literal["evidence_checkpoint.v1"] = "evidence_checkpoint.v1"
    manifest_offset: int = Field(default=0, ge=0)
    document_index: int = Field(default=0, ge=0)


class PreparedRawDocument(BaseModel):
    """One verified Raw Collection document ready for semantic extraction."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["prepared_raw_document.v2"] = "prepared_raw_document.v2"
    collection_id: str
    manifest_path: str
    manifest_offset: int = Field(ge=0)
    next_manifest_offset: int = Field(ge=0)
    document_index: int = Field(ge=0)
    document_count: int = Field(ge=1)
    document_path: str
    document_url_path: str
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication_key: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=32)
    source_name: str = Field(min_length=1, max_length=100)
    source_level: Literal["L1_OFFICIAL", "L2_WIRE", "L3_MEDIA", "L4_SOCIAL"]
    source_url: str
    title: str | None = Field(default=None, max_length=500)
    raw_text: str
    published_at: datetime | None = None
    collected_at: datetime


class EvidenceCategoryDefinition(BaseModel):
    """LLM-visible Evidence Category semantics without the formal Data identity."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(max_length=50, pattern=r"^[A-Z][A-Z0-9_]*$")
    name: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1)

    @field_validator("code", "name", "description")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Evidence Category text must be non-blank")
        return value


class EvidenceCategory(EvidenceCategoryDefinition):
    """One formal Evidence Category returned by Data Service."""

    id: EvidenceCategoryID


class EvidenceCategoryCatalog(BaseModel):
    """Complete, ordered Evidence Category Catalog snapshot."""

    model_config = ConfigDict(extra="forbid")

    categories: list[EvidenceCategory] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_catalog_identity_and_order(self) -> "EvidenceCategoryCatalog":
        ids = [item.id for item in self.categories]
        codes = [item.code for item in self.categories]
        if len(ids) != len(set(ids)):
            raise ValueError("Evidence Category IDs must be unique")
        if len(codes) != len(set(codes)):
            raise ValueError("Evidence Category codes must be unique")
        if [(item.code, item.id) for item in self.categories] != sorted(
            (item.code, item.id) for item in self.categories
        ):
            raise ValueError("Evidence Categories must be ordered by code and id")
        return self


class EvidenceAnalysisRequest(BaseModel):
    """One Raw document plus the frozen, identity-free category vocabulary for the Agent."""

    model_config = ConfigDict(extra="forbid")

    document: PreparedRawDocument
    categories: list[EvidenceCategoryDefinition] = Field(min_length=1)


class RawEvidenceEnrichment(BaseModel):
    """Semantic fields required to publish the prepared Raw Evidence."""

    model_config = ConfigDict(extra="forbid")

    category_code: str = Field(max_length=50, pattern=r"^[A-Z][A-Z0-9_]*$")
    is_original: bool
    quoted_source_name: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_quotation(self) -> "RawEvidenceEnrichment":
        if self.is_original and self.quoted_source_name is not None:
            raise ValueError("original Raw Evidence cannot declare a quoted source")
        if not self.is_original and not (self.quoted_source_name or "").strip():
            raise ValueError("reposted Raw Evidence requires quoted_source_name")
        if self.quoted_source_name is not None:
            self.quoted_source_name = self.quoted_source_name.strip()
        return self


EvidenceStage = Literal[
    "OCCURRED",
    "ANNOUNCED",
    "EFFECTIVE",
    "IMPLEMENTED",
    "UPDATED",
    "SUSPENDED",
    "TERMINATED",
    "EXPECTED",
]
EvidenceModality = Literal["FACT", "PLAN", "SPEC"]
EvidenceTimePrecision = Literal["INSTANT", "DAY", "RANGE", "MONTH", "QUARTER", "YEAR", "UNKNOWN"]


def _strip_required(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("value must not be blank")
    return stripped


def _strip_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError("optional text must be non-blank; use null when unsupported")
    return stripped


def _validated_string_collection(values: list[str], *, field_name: str) -> list[str]:
    normalized: list[str] = []
    for value in values:
        item = _strip_required(value)
        if item in normalized:
            raise ValueError(f"{field_name} values must be unique")
        normalized.append(item)
    return normalized


def _validated_keywords(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        item = _strip_required(value)
        if item not in normalized:
            normalized.append(item)
    if any(len(item) > 6 for item in normalized):
        raise ValueError("each Evidence keyword must contain at most 6 characters")
    if not 1 <= len(normalized) <= 5:
        raise ValueError("Evidence keywords must contain between 1 and 5 unique values")
    return normalized


class EvidenceMetric(BaseModel):
    """One quantitative observation retained inside a complete business proposition."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    value: str | None = Field(max_length=100)
    unit: str | None = Field(max_length=50)
    change: str | None = Field(max_length=100)
    period: str | None = Field(max_length=100)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return _strip_required(value)

    @field_validator("value", "unit", "change", "period")
    @classmethod
    def strip_optional_metric_text(cls, value: str | None) -> str | None:
        return _strip_optional(value)

    @model_validator(mode="after")
    def require_value_or_change(self) -> "EvidenceMetric":
        if self.value is None and self.change is None:
            raise ValueError("Evidence metric requires value or change")
        return self


class EvidenceAttribution(BaseModel):
    """Source attribution that must not replace the business actor."""

    model_config = ConfigDict(extra="forbid")

    reported_by: str | None = Field(max_length=100)
    claimed_by: str | None = Field(max_length=100)

    @field_validator("reported_by", "claimed_by")
    @classmethod
    def strip_attribution(cls, value: str | None) -> str | None:
        return _strip_optional(value)


class EvidenceTimeDraft(BaseModel):
    """Agent-owned source time; normalized UTC bounds are deliberately unavailable to the model."""

    model_config = ConfigDict(extra="forbid")

    raw: str | None = Field(max_length=200)
    start_at: None
    end_at: None
    precision: EvidenceTimePrecision

    @field_validator("raw")
    @classmethod
    def strip_raw_time(cls, value: str | None) -> str | None:
        return _strip_optional(value)


class EvidenceTime(BaseModel):
    """Deterministically normalized source time used by Data Service and downstream workflows."""

    model_config = ConfigDict(extra="forbid")

    raw: str | None = Field(max_length=200)
    start_at: datetime | None
    end_at: datetime | None
    precision: EvidenceTimePrecision

    @field_validator("raw")
    @classmethod
    def strip_raw_time(cls, value: str | None) -> str | None:
        return _strip_optional(value)

    @model_validator(mode="after")
    def validate_bounds(self) -> "EvidenceTime":
        if (self.start_at is None) != (self.end_at is None):
            raise ValueError("Evidence time bounds must both be present or both be null")
        if self.start_at is not None and self.end_at is not None:
            if self.start_at.utcoffset() is None or self.end_at.utcoffset() is None:
                raise ValueError("Evidence time bounds must be timezone-aware")
            if self.start_at > self.end_at:
                raise ValueError("Evidence time bounds must be ordered")
        return self


class EvidenceSemanticDraft(BaseModel):
    """Agent-owned semantic structure for one minimum complete business proposition."""

    model_config = ConfigDict(extra="forbid")

    actors: list[str] = Field(min_length=1, max_length=20)
    action: str = Field(min_length=1, max_length=200)
    objects: list[str] = Field(min_length=1, max_length=20)
    stage: EvidenceStage
    modality: EvidenceModality
    time: EvidenceTimeDraft
    jurisdictions: list[str] = Field(max_length=20)
    reason: str | None = Field(max_length=500)
    method: str | None = Field(max_length=500)
    metrics: list[EvidenceMetric]
    attribution: EvidenceAttribution

    @field_validator("action")
    @classmethod
    def strip_action(cls, value: str) -> str:
        return _strip_required(value)

    @field_validator("actors", "objects", "jurisdictions")
    @classmethod
    def validate_semantic_collections(cls, values: list[str], info: ValidationInfo) -> list[str]:
        return _validated_string_collection(values, field_name=info.field_name or "semantic collection")

    @field_validator("reason", "method")
    @classmethod
    def strip_optional_semantic_text(cls, value: str | None) -> str | None:
        return _strip_optional(value)


class EvidenceSemantic(BaseModel):
    """Canonical semantic structure published to Data and consumed by Event extraction."""

    model_config = ConfigDict(extra="forbid")

    actors: list[str] = Field(min_length=1, max_length=20)
    action: str = Field(min_length=1, max_length=200)
    objects: list[str] = Field(min_length=1, max_length=20)
    stage: EvidenceStage
    modality: EvidenceModality
    time: EvidenceTime
    jurisdictions: list[str] = Field(max_length=20)
    reason: str | None = Field(max_length=500)
    method: str | None = Field(max_length=500)
    metrics: list[EvidenceMetric]
    attribution: EvidenceAttribution

    @field_validator("action")
    @classmethod
    def strip_action(cls, value: str) -> str:
        return _strip_required(value)

    @field_validator("actors", "objects", "jurisdictions")
    @classmethod
    def validate_semantic_collections(cls, values: list[str], info: ValidationInfo) -> list[str]:
        return _validated_string_collection(values, field_name=info.field_name or "semantic collection")

    @field_validator("reason", "method")
    @classmethod
    def strip_optional_semantic_text(cls, value: str | None) -> str | None:
        return _strip_optional(value)


class AtomicEvidenceDraft(BaseModel):
    """LLM-owned minimum complete business proposition."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=200)
    keywords: list[str]
    semantic: EvidenceSemanticDraft

    @field_validator("summary")
    @classmethod
    def strip_summary(cls, value: str) -> str:
        return _strip_required(value)

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, values: list[str]) -> list[str]:
        return _validated_keywords(values)


class EvidenceExtractionDraft(BaseModel):
    """Strict structured output returned by the Evidence Extractor Agent."""

    model_config = ConfigDict(extra="forbid")

    raw_evidence: RawEvidenceEnrichment
    evidences: list[AtomicEvidenceDraft] = Field(min_length=1)


class RawEvidencePublication(BaseModel):
    """Data Service Raw Evidence request body."""

    model_config = ConfigDict(extra="forbid")

    publication_key: str = Field(min_length=1, max_length=128)
    source_id: str
    source_name: str
    source_level: str
    source_url: str
    is_original: bool
    quoted_source_id: str | None
    quoted_source_name: str | None
    title: str | None
    raw_text: str
    published_at: datetime | None
    collected_at: datetime
    category_ids: list[EvidenceCategoryID] = Field(min_length=1, max_length=1)


class EvidencePublicationItem(BaseModel):
    """One canonical Data Service Evidence publication item."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=200)
    keywords: list[str] = Field(min_length=1, max_length=5)
    semantic: EvidenceSemantic

    @field_validator("summary")
    @classmethod
    def strip_summary(cls, value: str) -> str:
        return _strip_required(value)

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, values: list[str]) -> list[str]:
        return _validated_keywords(values)


class PreparedEvidencePublication(BaseModel):
    """Fully validated deterministic publication set for one Raw document."""

    schema_version: Literal["prepared_evidence_publication.v5"] = "prepared_evidence_publication.v5"
    prepared_raw: PreparedRawDocument
    category_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_category_code: str = Field(max_length=50, pattern=r"^[A-Z][A-Z0-9_]*$")
    raw_evidence: RawEvidencePublication
    evidences: list[EvidencePublicationItem] = Field(min_length=1)


class RawEvidencePublicationResponse(BaseModel):
    """Formal Raw Evidence identity returned by Data Service."""

    model_config = ConfigDict(extra="forbid")

    id: RawEvidenceID


class EvidencePublicationResponseItem(BaseModel):
    """Formal Evidence identity associated with one current request position."""

    model_config = ConfigDict(extra="forbid")

    input_index: int = Field(ge=0)
    id: EvidenceID


class EvidenceSetPublicationResponse(BaseModel):
    """Formal identities returned for one complete Evidence set."""

    model_config = ConfigDict(extra="forbid")

    raw_evidence_id: RawEvidenceID
    ids: list[EvidenceID] = Field(min_length=1)
    items: list[EvidencePublicationResponseItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "EvidenceSetPublicationResponse":
        if len(set(self.ids)) != len(self.ids):
            raise ValueError("Evidence identities must be unique")
        indexes = [item.input_index for item in self.items]
        item_ids = [item.id for item in self.items]
        if indexes != list(range(len(self.items))):
            raise ValueError("Evidence response indexes must cover the request in order")
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("Evidence response item identities must be unique")
        if set(item_ids) != set(self.ids):
            raise ValueError("Evidence response items must match the complete identity set")
        return self


class EvidenceIdentityBindings(BaseModel):
    """Immutable local association between prepared Evidence positions and formal identities."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["evidence_identity_bindings.v1"] = "evidence_identity_bindings.v1"
    publication_key: str = Field(min_length=1, max_length=128)
    raw_evidence_id: RawEvidenceID
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_count: int = Field(ge=1)
    items: list[EvidencePublicationResponseItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_complete_mapping(self) -> "EvidenceIdentityBindings":
        indexes = [item.input_index for item in self.items]
        ids = [item.id for item in self.items]
        if len(self.items) != self.evidence_count or indexes != list(range(self.evidence_count)):
            raise ValueError("Evidence identity bindings must cover every prepared Evidence")
        if len(set(ids)) != len(ids):
            raise ValueError("Evidence identity bindings must contain unique identities")
        return self


class ResolvedEvidence(EvidencePublicationItem):
    """One locally prepared Evidence resolved to its formal Data identity."""

    id: EvidenceID
    raw_evidence_id: RawEvidenceID


class EvidencePublicationResult(BaseModel):
    """Workflow-visible terminal result for one published Raw document."""

    schema_version: Literal["evidence_publication_result.v3"] = "evidence_publication_result.v3"
    raw_evidence_id: RawEvidenceID
    evidence_ids: list[EvidenceID] = Field(min_length=1)
    evidence_count: int = Field(ge=1)
    artifact_manifest_path: str
    checkpoint: EvidenceCheckpoint


class EvidenceExtractionIdle(BaseModel):
    """Terminal output when the manifest index has no unprocessed documents."""

    schema_version: Literal["evidence_extraction_idle.v1"] = "evidence_extraction_idle.v1"
    status: Literal["no_work"] = "no_work"
    checkpoint: EvidenceCheckpoint
