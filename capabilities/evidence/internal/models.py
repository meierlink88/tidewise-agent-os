"""Typed contracts for Evidence extraction and Data Service publication."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator

_DATETIME_ADAPTER = TypeAdapter(datetime)
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
    keywords: list[str] = Field(min_length=1, max_length=5)
    is_original: bool
    quoted_source_name: str | None = Field(default=None, max_length=100)

    @field_validator("keywords", mode="before")
    @classmethod
    def validate_keywords(cls, values: Any) -> Any:
        if not isinstance(values, list):
            return values
        normalized: list[str] = []
        for value in values:
            if not isinstance(value, str):
                raise ValueError("each keyword must be a string")
            item = value.strip()
            if not item or len(item) > 5:
                continue
            if item in normalized:
                continue
            normalized.append(item)
            if len(normalized) == 5:
                break
        if not normalized:
            raise ValueError("at least one keyword must contain 1 to 5 characters")
        return normalized

    @model_validator(mode="after")
    def validate_quotation(self) -> "RawEvidenceEnrichment":
        if self.is_original and self.quoted_source_name is not None:
            raise ValueError("original Raw Evidence cannot declare a quoted source")
        if not self.is_original and not (self.quoted_source_name or "").strip():
            raise ValueError("reposted Raw Evidence requires quoted_source_name")
        if self.quoted_source_name is not None:
            self.quoted_source_name = self.quoted_source_name.strip()
        return self


class AtomicEvidenceDraft(BaseModel):
    """LLM-owned semantic draft; deterministic publication metadata is added later."""

    model_config = ConfigDict(extra="forbid")

    layer_type: Literal["SINGLE", "DOUBLE"]
    source_who: str | None = None
    source_what: str = Field(min_length=1)
    source_when: datetime | None = None
    source_when_raw: str | None = None
    source_where: str | None = None
    source_why: str | None = None
    source_how: str | None = None
    source_who_core: str | None = None
    source_what_core: str | None = None
    source_when_core: datetime | None = None
    source_when_raw_core: str | None = None
    source_where_core: str | None = None
    source_why_core: str | None = None
    source_how_core: str | None = None
    expression_fingerprint: str = Field(min_length=1, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def preserve_fuzzy_fact_times(cls, value: Any) -> Any:
        """Move non-ISO fact-time expressions into their lossless raw fields."""
        if not isinstance(value, dict):
            return value
        normalized = value.copy()
        for timestamp_field, raw_field in (
            ("source_when", "source_when_raw"),
            ("source_when_core", "source_when_raw_core"),
        ):
            candidate = normalized.get(timestamp_field)
            if not isinstance(candidate, str):
                continue
            try:
                _DATETIME_ADAPTER.validate_python(candidate)
            except ValidationError:
                if not normalized.get(raw_field):
                    normalized[raw_field] = candidate
                normalized[timestamp_field] = None
        return normalized

    @field_validator("source_what", "expression_fingerprint")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("required Evidence text must not be blank")
        return stripped

    @field_validator(
        "source_who",
        "source_when_raw",
        "source_where",
        "source_why",
        "source_how",
        "source_who_core",
        "source_what_core",
        "source_when_raw_core",
        "source_where_core",
        "source_why_core",
        "source_how_core",
    )
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def validate_layers(self) -> "AtomicEvidenceDraft":
        core_values = (
            self.source_who_core,
            self.source_what_core,
            self.source_when_core,
            self.source_when_raw_core,
            self.source_where_core,
            self.source_why_core,
            self.source_how_core,
        )
        if self.layer_type == "SINGLE" and any(value is not None for value in core_values):
            raise ValueError("SINGLE Evidence cannot contain core fields")
        if self.layer_type == "DOUBLE" and not self.source_what_core:
            raise ValueError("DOUBLE Evidence requires source_what_core")
        return self


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
    keywords: list[str]
    category_ids: list[EvidenceCategoryID] = Field(min_length=1, max_length=1)


class EvidencePublicationItem(BaseModel):
    """One complete Data Service Evidence publication item."""

    model_config = ConfigDict(extra="forbid")

    split_order: int = Field(ge=0)
    layer_type: Literal["SINGLE", "DOUBLE"]
    source_who: str | None
    source_what: str
    source_when: datetime | None
    source_when_raw: str | None
    source_where: str | None
    source_why: str | None
    source_how: str | None
    source_who_core: str | None
    source_what_core: str | None
    source_when_core: datetime | None
    source_when_raw_core: str | None
    source_where_core: str | None
    source_why_core: str | None
    source_how_core: str | None
    expression_fingerprint: str
    expression_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    fingerprint_version: Literal["evidence-expression.v1"] = "evidence-expression.v1"


class PreparedEvidencePublication(BaseModel):
    """Fully validated deterministic publication set for one Raw document."""

    schema_version: Literal["prepared_evidence_publication.v3"] = "prepared_evidence_publication.v3"
    prepared_raw: PreparedRawDocument
    category_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_category_code: str = Field(max_length=50, pattern=r"^[A-Z][A-Z0-9_]*$")
    raw_evidence: RawEvidencePublication
    evidences: list[EvidencePublicationItem] = Field(min_length=1)


class RawEvidencePublicationResponse(BaseModel):
    """Formal Raw Evidence identity returned by Data Service."""

    model_config = ConfigDict(extra="forbid")

    raw_evidence_id: RawEvidenceID


class EvidenceSetPublicationResponse(BaseModel):
    """Formal Evidence identities returned by Data Service in split order."""

    model_config = ConfigDict(extra="forbid")

    raw_evidence_id: RawEvidenceID
    evidence_ids: list[EvidenceID] = Field(min_length=1)


class EvidencePublicationResult(BaseModel):
    """Workflow-visible terminal result for one published Raw document."""

    schema_version: Literal["evidence_publication_result.v2"] = "evidence_publication_result.v2"
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
