"""Strict local and Reasoning Server contracts for Event extraction."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EvidenceID = Annotated[
    str,
    Field(pattern=r"^EVD[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"),
]


class EventEvidenceInput(BaseModel):
    """One locally complete Evidence made available to the Event Extractor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: EvidenceID
    raw_evidence_id: str
    summary: str = Field(min_length=1, max_length=200)
    semantic: dict[str, str | None]


class EventExtractionBatch(BaseModel):
    """Immutable batch claimed by one Event Extraction run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["event_extraction_batch.v1"] = "event_extraction_batch.v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    needs_analysis: bool
    evidences: list[EventEvidenceInput] = Field(min_length=1, max_length=50)


class EventSemantic(BaseModel):
    """Event identity semantics accepted by Reasoning Server."""

    model_config = ConfigDict(extra="forbid")

    actors: list[str] = Field(min_length=1)
    action: str = Field(min_length=1)
    objects: list[str] = Field(min_length=1)
    stage: Literal[
        "OCCURRED",
        "ANNOUNCED",
        "EFFECTIVE",
        "IMPLEMENTED",
        "UPDATED",
        "SUSPENDED",
        "TERMINATED",
        "EXPECTED",
    ]
    jurisdictions: list[str]
    effective_at: datetime | None
    time_precision: Literal["INSTANT", "DAY", "MONTH", "QUARTER", "YEAR", "UNKNOWN"]

    @field_validator("actors", "objects", "jurisdictions")
    @classmethod
    def normalize_terms(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("Event identity terms must be nonblank and unique")
        return normalized

    @field_validator("action")
    @classmethod
    def normalize_action(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Event action must not be blank")
        return value

    @field_validator("effective_at")
    @classmethod
    def require_utc_effective_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)):
            raise ValueError("Event effective_at must use UTC")
        return value


class EventCandidate(BaseModel):
    """One single-real-world-action Event Candidate."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1)
    semantic: EventSemantic
    modality: Literal["FACT", "PLAN", "SPEC"]
    occurred_at: datetime | None
    announced_at: datetime | None

    @field_validator("title", "summary")
    @classmethod
    def normalize_narrative(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Event narrative must not be blank")
        return value

    @field_validator("occurred_at", "announced_at")
    @classmethod
    def require_utc_event_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)):
            raise ValueError("Event timestamps must use UTC")
        return value

    @model_validator(mode="after")
    def require_time_anchor(self) -> "EventCandidate":
        if self.occurred_at is None and self.announced_at is None and self.semantic.effective_at is None:
            raise ValueError("Event requires an occurrence, announcement, or effective time")
        return self


class EventCandidateSubmission(BaseModel):
    """Exact request body sent to Reasoning Server."""

    model_config = ConfigDict(extra="forbid")

    event: EventCandidate
    evidence_ids: list[EvidenceID] = Field(min_length=1, max_length=50)

    @field_validator("evidence_ids")
    @classmethod
    def require_unique_evidence_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Candidate Evidence IDs must be unique")
        return values


class EventDisposition(BaseModel):
    """Terminal local disposition for an Evidence without a Candidate."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: EvidenceID
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Event disposition reason must not be blank")
        return value


class EventExtractionDraft(BaseModel):
    """Strict structured output returned by the Event Extractor Agent."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[EventCandidateSubmission]
    no_event: list[EventDisposition]
    needs_review: list[EventDisposition]


class EventCandidateAcceptance(BaseModel):
    """Reliable acceptance returned by Reasoning Server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    submission_id: str = Field(min_length=1)
    status: Literal["ACCEPTED"]
    status_url: str = Field(min_length=1)
    replayed: bool


class EventSubmissionRecord(EventCandidateAcceptance):
    """One durably journaled Candidate handoff."""

    candidate_key: str = Field(pattern=r"^[0-9a-f]{64}$")


class EventSubmissionJournal(BaseModel):
    """Crash-safe journal written after each accepted Candidate."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["event_submission_journal.v1"] = "event_submission_journal.v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    submissions: list[EventSubmissionRecord]


class EventExtractionResult(BaseModel):
    """Terminal local handoff result for one frozen batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["event_extraction_result.v1"] = "event_extraction_result.v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ids: list[EvidenceID] = Field(min_length=1, max_length=50)
    candidate_count: int = Field(ge=0)
    no_event_count: int = Field(ge=0)
    needs_review_count: int = Field(ge=0)
    submission_ids: list[str]


class EventExtractionIdle(BaseModel):
    """Terminal Workflow output when no mapped Evidence remains."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["event_extraction_idle.v1"] = "event_extraction_idle.v1"
    status: Literal["no_work"] = "no_work"
