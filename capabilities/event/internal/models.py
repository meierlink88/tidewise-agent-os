"""Strict contracts for the local Event extraction Workflow."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from capabilities.evidence import EvidenceSemantic
from sematica.analysis.event.contracts import (
    CandidateSet,
    DirectSignalDraft,
    EventAnalysisInput,
    EventClassification,
    SignalProposal,
)
from sematica.ingestion.episcode.event.contracts import HistoricalEvent

EvidenceID = Annotated[
    str,
    Field(pattern=r"^EVD[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"),
]
RawEvidenceID = Annotated[
    str,
    Field(pattern=r"^RAW[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"),
]


class EventEvidenceInput(BaseModel):
    """One locally complete Evidence made available to the Event Extractor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: EvidenceID
    raw_evidence_id: RawEvidenceID
    summary: str = Field(min_length=1, max_length=200)
    keywords: list[str] = Field(min_length=1, max_length=5)
    semantic: EvidenceSemantic


class EventEvidenceQueueItem(BaseModel):
    """One formal Evidence waiting for Event extraction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["event_evidence_queue_item.v1"] = "event_evidence_queue_item.v1"
    evidence_id: EvidenceID
    artifact_manifest_path: str = Field(min_length=1)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("Event Evidence queue created_at must use UTC")
        return value


class FrozenEventExtractionBatch(BaseModel):
    """Immutable semantic input selected before an Event extraction run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["frozen_event_extraction_batch.v1"] = "frozen_event_extraction_batch.v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    evidences: list[EventEvidenceInput] = Field(min_length=1, max_length=50)

    @field_validator("created_at")
    @classmethod
    def require_utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("Event batch created_at must use UTC")
        return value


class EventExtractionBatch(BaseModel):
    """Frozen input plus the exclusive processing lease for one Workflow run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["event_extraction_batch.v1"] = "event_extraction_batch.v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    evidences: list[EventEvidenceInput] = Field(min_length=1, max_length=50)
    needs_analysis: bool
    lease_id: str = Field(min_length=1, max_length=128)
    lease_expires_at: datetime

    @field_validator("created_at")
    @classmethod
    def require_utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("Event batch created_at must use UTC")
        return value

    @field_validator("lease_expires_at")
    @classmethod
    def require_utc_lease_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("Event batch lease_expires_at must use UTC")
        return value


class EventExtractionLease(BaseModel):
    """Exclusive, renewable ownership of one pending local batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["event_extraction_lease.v1"] = "event_extraction_lease.v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    lease_id: str = Field(min_length=1, max_length=128)
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def require_utc_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("Event extraction lease expiry must use UTC")
        return value


class EventExtractionBusy(BaseModel):
    """Safe early stop when another Workflow run owns the pending batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["event_extraction_busy.v1"] = "event_extraction_busy.v1"
    status: Literal["busy"] = "busy"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    retry_after: datetime


class EventSemantic(BaseModel):
    """Normalized semantics used by the Event identity gate."""

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
    """One Candidate and its authoritative Evidence support."""

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


class EventIdentityRequest(BaseModel):
    """One frozen Candidate and only the historical Events it may reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["event_identity_request.v1"] = "event_identity_request.v1"
    candidate_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate: EventCandidateSubmission
    historical_candidates: list[HistoricalEvent] = Field(max_length=30)

    @model_validator(mode="after")
    def historical_ids_are_unique(self) -> "EventIdentityRequest":
        ids = [item.id for item in self.historical_candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("Event identity history IDs must be unique")
        return self


class EventIdentityDecision(BaseModel):
    """Strict Studio Agent judgment for atomicity and historical identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["NEW_EVENT", "SAME_EVENT", "RELATED_BUT_DISTINCT", "IGNORED"]
    atomic: bool
    matched_event_ids: list[str] = Field(max_length=30)
    reason_codes: list[str] = Field(min_length=1, max_length=24)
    summary: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def decision_is_consistent(self) -> "EventIdentityDecision":
        if not self.atomic and self.decision != "IGNORED":
            raise ValueError("a non-atomic Candidate must be ignored")
        if self.decision == "SAME_EVENT" and len(self.matched_event_ids) != 1:
            raise ValueError("SAME_EVENT requires exactly one matched historical Event")
        if self.decision == "NEW_EVENT" and self.matched_event_ids:
            raise ValueError("NEW_EVENT cannot reference a historical Event")
        if len(self.matched_event_ids) != len(set(self.matched_event_ids)):
            raise ValueError("matched historical Event IDs must be unique")
        return self


class EventIdentityRequestJournal(BaseModel):
    """Immutable prepared Agent inputs for one frozen batch."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["event_identity_request_journal.v1"] = "event_identity_request_journal.v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    requests: list[EventIdentityRequest]

    @model_validator(mode="after")
    def request_keys_are_unique(self) -> "EventIdentityRequestJournal":
        keys = [item.candidate_key for item in self.requests]
        if len(keys) != len(set(keys)):
            raise ValueError("Event identity request Candidate keys must be unique")
        return self


class EventResolutionRecord(BaseModel):
    """Immutable validated identity disposition for one Candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["NEW_EVENT", "SAME_EVENT", "RELATED_BUT_DISTINCT", "IGNORED"]
    atomic: bool
    matched_event_ids: list[str]
    reason_codes: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def decision_is_consistent(self) -> "EventResolutionRecord":
        if not self.atomic and self.decision != "IGNORED":
            raise ValueError("a non-atomic frozen Event resolution must be ignored")
        if self.decision == "SAME_EVENT" and len(self.matched_event_ids) != 1:
            raise ValueError("a frozen SAME_EVENT resolution requires exactly one match")
        if self.decision == "NEW_EVENT" and self.matched_event_ids:
            raise ValueError("a frozen NEW_EVENT resolution cannot retain historical matches")
        if len(self.matched_event_ids) != len(set(self.matched_event_ids)):
            raise ValueError("frozen Event resolution matches must be unique")
        return self


class EventResolutionJournal(BaseModel):
    """Immutable Event identity decisions for one frozen batch."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["event_resolution_journal.v1"] = "event_resolution_journal.v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolutions: list[EventResolutionRecord]

    @model_validator(mode="after")
    def resolution_keys_are_unique(self) -> "EventResolutionJournal":
        keys = [item.candidate_key for item in self.resolutions]
        if len(keys) != len(set(keys)):
            raise ValueError("Event resolution Candidate keys must be unique")
        return self


class EventWorkflowProgress(BaseModel):
    """Rename-safe loop completion signal carried as direct predecessor content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: Literal["RESOLVE_EVENTS", "ANALYZE_SIGNALS"]
    processed: int = Field(ge=0)
    total: int = Field(ge=0)
    done: bool


class PublishedEvent(BaseModel):
    """Formal Data Event available to Graphiti and Signal construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^EVT[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    event: EventCandidate


class EventPublicationRecord(BaseModel):
    """Durable outcome of local resolution, Data publication and Graphiti projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["SAME_EVENT", "NEW_EVENT", "RELATED_BUT_DISTINCT", "IGNORED", "FAILED"]
    publication_started: bool = False
    event_id: str | None
    event_created: bool
    evidence_link_result: Literal["CREATED", "IGNORED", "NOT_ATTEMPTED"]
    graph_projection_status: Literal["SUCCEEDED", "IGNORED", "NOT_ATTEMPTED"]
    reason_codes: list[str]
    matched_event_ids: list[str]
    episode_uuid: str | None = None
    published_event: PublishedEvent | None = None


class EventPublicationJournal(BaseModel):
    """Crash-safe journal written after each irreversible Event side effect."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["event_publication_journal.v1"] = "event_publication_journal.v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    publications: list[EventPublicationRecord]

    @model_validator(mode="after")
    def require_unique_candidate_keys(self) -> "EventPublicationJournal":
        keys = [item.candidate_key for item in self.publications]
        if len(keys) != len(set(keys)):
            raise ValueError("Event publication journal Candidate keys must be unique")
        return self


class EventSignalClassificationRequest(BaseModel):
    """Frozen input for the Signal Analyst classification pass."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["event_signal_classification_request.v1"] = "event_signal_classification_request.v1"
    task: Literal["CLASSIFY"] = "CLASSIFY"
    analysis: EventAnalysisInput


class EventSignalAnalysisRequest(BaseModel):
    """Frozen classification and existing graph identities for Signal proposals."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["event_signal_analysis_request.v1"] = "event_signal_analysis_request.v1"
    task: Literal["PROPOSE_SIGNALS"] = "PROPOSE_SIGNALS"
    analysis: EventAnalysisInput
    classification: EventClassification
    candidates: CandidateSet


class EventSignalAnalysisDraft(BaseModel):
    """Strict output shared by both Signal Analyst passes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    classification: EventClassification
    proposals: list[DirectSignalDraft] = Field(max_length=12)
    no_signal_reason: str | None = Field(default=None, max_length=1000)
    reason_codes: list[str] = Field(default_factory=list, max_length=24)


class EventSignalPreparationJournal(BaseModel):
    """Immutable per-Event reference times prepared before any Signal judgment."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["event_signal_preparation_journal.v1"] = "event_signal_preparation_journal.v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    analyses: list[EventAnalysisInput]

    @model_validator(mode="after")
    def event_ids_are_unique(self) -> "EventSignalPreparationJournal":
        ids = [item.event.id for item in self.analyses]
        if len(ids) != len(set(ids)):
            raise ValueError("prepared Signal Event IDs must be unique")
        return self


class EventSignalClassificationRecord(BaseModel):
    """Immutable valid Event classification retained before graph retrieval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    classification: EventClassification


class EventSignalClassificationJournal(BaseModel):
    """Frozen valid classifications retained even when no Signal is supported."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["event_signal_classification_journal.v1"] = "event_signal_classification_journal.v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    classifications: list[EventSignalClassificationRecord]

    @model_validator(mode="after")
    def event_ids_are_unique(self) -> "EventSignalClassificationJournal":
        ids = [item.event_id for item in self.classifications]
        if len(ids) != len(set(ids)):
            raise ValueError("Signal classification Event IDs must be unique")
        return self


class EventSignalCandidateRecord(BaseModel):
    """Immutable bounded existing graph identities prepared after classification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    candidates: CandidateSet


class EventSignalCandidateJournal(BaseModel):
    """Frozen candidate sets so retrieval retries never rerun semantic classification."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["event_signal_candidate_journal.v1"] = "event_signal_candidate_journal.v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidates: list[EventSignalCandidateRecord]

    @model_validator(mode="after")
    def event_ids_are_unique(self) -> "EventSignalCandidateJournal":
        ids = [item.event_id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("Signal candidate-set Event IDs must be unique")
        return self


class EventSignalAnalysisRecord(BaseModel):
    """Immutable validated Signal Agent disposition for one projected Event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    status: Literal["SUCCEEDED", "NO_SIGNAL", "NO_SUPPORTED_ANCHOR", "NONCOMPLIANT"]
    classification: EventClassification | None
    proposals: list[SignalProposal] = Field(max_length=12)
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def classification_and_proposals_match_status(self) -> "EventSignalAnalysisRecord":
        if self.status != "NONCOMPLIANT" and self.classification is None:
            raise ValueError("a compliant Signal analysis must retain its Event classification")
        if self.status == "SUCCEEDED" and not self.proposals:
            raise ValueError("a successful Signal analysis requires at least one proposal")
        if self.status != "SUCCEEDED" and self.proposals:
            raise ValueError("a non-successful Signal analysis cannot retain proposals")
        return self


class EventSignalAnalysisJournal(BaseModel):
    """Frozen terminal Signal analyses before any Graphiti Fact side effect."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["event_signal_analysis_journal.v1"] = "event_signal_analysis_journal.v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    analyses: list[EventSignalAnalysisRecord]

    @model_validator(mode="after")
    def event_ids_are_unique(self) -> "EventSignalAnalysisJournal":
        ids = [item.event_id for item in self.analyses]
        if len(ids) != len(set(ids)):
            raise ValueError("Signal analysis Event IDs must be unique")
        return self


class EventSignalProjectionRecord(BaseModel):
    """One immutable terminal Graphiti Signal projection checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    proposal_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["SUCCEEDED", "REJECTED"] = "SUCCEEDED"
    fact_uuid: str | None = Field(default=None, min_length=1)
    reason_code: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def outcome_is_consistent(self) -> "EventSignalProjectionRecord":
        if self.status == "SUCCEEDED" and (self.fact_uuid is None or self.reason_code is not None):
            raise ValueError("a successful Signal projection requires only its Fact UUID")
        if self.status == "REJECTED" and (self.fact_uuid is not None or self.reason_code is None):
            raise ValueError("a rejected Signal projection requires only its terminal reason")
        return self


class EventSignalProjectionJournal(BaseModel):
    """Per-proposal Graphiti acknowledgements for crash-safe Signal retries."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["event_signal_projection_journal.v1"] = "event_signal_projection_journal.v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    projections: list[EventSignalProjectionRecord]

    @model_validator(mode="after")
    def proposal_keys_are_unique(self) -> "EventSignalProjectionJournal":
        keys = [(item.event_id, item.proposal_key) for item in self.projections]
        if len(keys) != len(set(keys)):
            raise ValueError("Signal projection proposal keys must be unique per Event")
        return self


class EventSignalRecord(BaseModel):
    """Terminal Signal construction outcome for one newly published Event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    status: Literal["SUCCEEDED", "NO_SIGNAL", "NO_SUPPORTED_ANCHOR"]
    signal_fact_uuids: list[str]
    reason_codes: list[str] = Field(default_factory=list)


class EventSignalJournal(BaseModel):
    """Crash-safe Signal outcomes for a frozen Event batch."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["event_signal_journal.v1"] = "event_signal_journal.v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    signals: list[EventSignalRecord]

    @model_validator(mode="after")
    def require_unique_event_ids(self) -> "EventSignalJournal":
        event_ids = [item.event_id for item in self.signals]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("Event signal journal Event IDs must be unique")
        return self


class LegacyEventExtractionResult(BaseModel):
    """Read-only v1 manifest contract retained for already completed batches."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["event_extraction_result.v1"]
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ids: list[EvidenceID] = Field(min_length=1, max_length=50)
    candidate_count: int = Field(ge=0)
    no_event_count: int = Field(ge=0)
    needs_review_count: int = Field(ge=0)
    submission_ids: list[str]

    @field_validator("evidence_ids")
    @classmethod
    def require_unique_evidence_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("completed Event Evidence IDs must be unique")
        return values


class LegacyEventExtractionResultV2(BaseModel):
    """Read-only v2 manifest contract retained for compatibility."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["event_extraction_result.v2"]
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ids: list[EvidenceID] = Field(min_length=1, max_length=50)
    candidate_count: int = Field(ge=0)
    no_event_count: int = Field(ge=0)
    needs_review_count: int = Field(ge=0)
    published_event_ids: list[str]
    duplicate_event_count: int = Field(ge=0)
    review_event_count: int = Field(ge=0)
    signal_fact_uuids: list[str]


class EventExtractionResult(BaseModel):
    """Terminal local result for the complete five-phase Event Workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["event_extraction_result.v4"] = "event_extraction_result.v4"
    batch_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ids: list[EvidenceID] = Field(min_length=1, max_length=50)
    candidate_count: int = Field(ge=0)
    no_event_count: int = Field(ge=0)
    published_event_ids: list[str]
    duplicate_event_count: int = Field(ge=0)
    ignored_candidate_count: int = Field(ge=0)
    ignored_evidence_ids: list[EvidenceID]
    failed_candidate_count: int = Field(ge=0)
    failed_evidence_ids: list[EvidenceID]
    signal_fact_uuids: list[str]

    @field_validator("evidence_ids")
    @classmethod
    def require_unique_evidence_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("completed Event Evidence IDs must be unique")
        return values

    @field_validator("ignored_evidence_ids", "failed_evidence_ids")
    @classmethod
    def require_unique_disposition_evidence_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Event disposition Evidence IDs must be unique")
        return values

    @model_validator(mode="after")
    def failed_evidences_belong_to_batch(self) -> "EventExtractionResult":
        if not set(self.ignored_evidence_ids) <= set(self.evidence_ids):
            raise ValueError("ignored Event Evidence IDs must belong to the completed batch")
        if not set(self.failed_evidence_ids) <= set(self.evidence_ids):
            raise ValueError("failed Event Evidence IDs must belong to the completed batch")
        if set(self.ignored_evidence_ids) & set(self.failed_evidence_ids):
            raise ValueError("ignored and failed Event Evidence IDs must be disjoint")
        return self


class EventExtractionIdle(BaseModel):
    """Terminal Workflow output when no mapped Evidence remains."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["event_extraction_idle.v1"] = "event_extraction_idle.v1"
    status: Literal["no_work"] = "no_work"
