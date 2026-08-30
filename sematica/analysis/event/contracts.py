"""Public contracts for controlled Event classification and direct Signal Facts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sematica.ingestion.episcode.event.contracts import HistoricalEvent
from sematica.ontology.enums import AnalysisAnchorType, VariableGroup


class EventClass(StrEnum):
    GEOPOLITICAL = "GEOPOLITICAL"
    MACRO_ECONOMIC = "MACRO_ECONOMIC"
    INDUSTRY_CHAIN = "INDUSTRY_CHAIN"
    CHAIN_NODE = "CHAIN_NODE"
    COMPANY = "COMPANY"


class ConfidenceLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SignalDirection(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    MIXED = "MIXED"
    STABLE = "STABLE"
    UNKNOWN = "UNKNOWN"


class SignalMagnitude(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class EventAnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event: HistoricalEvent
    episode_uuid: str = Field(min_length=1)
    reference_time: datetime

    @field_validator("reference_time")
    @classmethod
    def reference_time_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("reference_time must be explicit UTC")
        return value


class EventClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_class: EventClass
    confidence: ConfidenceLevel
    anchor_type_hints: list[AnalysisAnchorType] = Field(max_length=6)
    variable_group_hints: list[VariableGroup] = Field(max_length=9)
    retrieval_queries: list[str] = Field(min_length=1, max_length=8)
    rationale: str = Field(min_length=1, max_length=1000)


class AnchorCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uuid: str = Field(min_length=1)
    name: str = Field(min_length=1)
    entity_type: AnalysisAnchorType
    business_id: str = Field(min_length=1)
    summary: str = ""


class VariableCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uuid: str = Field(min_length=1)
    variable_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    name: str = Field(min_length=1)
    variable_group: VariableGroup
    allowed_anchor_types: list[AnalysisAnchorType] = Field(min_length=1)
    definition: str = Field(min_length=1)


class CandidateSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    anchors: list[AnchorCandidate] = Field(max_length=30)
    variables: list[VariableCandidate]


class SignalProposal(BaseModel):
    """One Event-supported Signal before Graphiti Fact resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    anchor_uuid: str = Field(min_length=1)
    variable_uuid: str = Field(min_length=1)
    fact: str = Field(min_length=1, max_length=1000)
    direction: SignalDirection
    magnitude: SignalMagnitude
    derivation_type: Literal["OBSERVED", "DERIVED"]
    assertion_modality: Literal["ACTUAL", "ANTICIPATED", "SOURCE_FORECAST", "ASSUMED"]
    valid_at: datetime
    invalid_at: datetime | None = None
    impact_onset_earliest: datetime | None = None
    impact_onset_latest: datetime | None = None
    impact_peak_earliest: datetime | None = None
    impact_peak_latest: datetime | None = None
    expected_end_earliest: datetime | None = None
    expected_end_latest: datetime | None = None
    horizon_tags: list[Literal["SHORT", "MEDIUM", "LONG"]] = Field(min_length=1)
    mechanism: str = Field(min_length=1, max_length=2000)
    duration_basis: str = Field(min_length=1, max_length=1000)
    assumptions: list[str] = Field(max_length=12)
    invalidation_conditions: list[str] = Field(min_length=1, max_length=12)
    provenance_confidence: ConfidenceLevel
    mechanism_confidence: ConfidenceLevel
    temporal_confidence: ConfidenceLevel

    @field_validator(
        "valid_at",
        "invalid_at",
        "impact_onset_earliest",
        "impact_onset_latest",
        "impact_peak_earliest",
        "impact_peak_latest",
        "expected_end_earliest",
        "expected_end_latest",
    )
    @classmethod
    def times_are_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)):
            raise ValueError("Signal times must be explicit UTC")
        return value

    @model_validator(mode="after")
    def time_ranges_are_ordered(self) -> SignalProposal:
        for earliest, latest, name in (
            (self.impact_onset_earliest, self.impact_onset_latest, "impact onset"),
            (self.impact_peak_earliest, self.impact_peak_latest, "impact peak"),
            (self.expected_end_earliest, self.expected_end_latest, "expected end"),
        ):
            if earliest is not None and latest is not None and earliest > latest:
                raise ValueError(f"{name} earliest must not be after latest")
        if self.invalid_at is not None and self.invalid_at < self.valid_at:
            raise ValueError("invalid_at must not be before valid_at")
        return self


class SignalFactAttributes(BaseModel):
    """Validated Tidewise extensions stored on Graphiti's native EntityEdge Fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_event_ids: list[str] = Field(min_length=1)
    event_class: EventClass
    variable_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    anchor_type: AnalysisAnchorType
    anchor_business_id: str = Field(min_length=1)
    direction: SignalDirection
    magnitude: SignalMagnitude
    derivation_type: Literal["OBSERVED", "DERIVED"]
    assertion_modality: Literal["ACTUAL", "ANTICIPATED", "SOURCE_FORECAST", "ASSUMED"]
    review_status: Literal["REVIEWED"] = "REVIEWED"
    impact_onset_earliest: datetime | None = None
    impact_onset_latest: datetime | None = None
    impact_peak_earliest: datetime | None = None
    impact_peak_latest: datetime | None = None
    expected_end_earliest: datetime | None = None
    expected_end_latest: datetime | None = None
    horizon_tags: list[Literal["SHORT", "MEDIUM", "LONG"]] = Field(min_length=1)
    mechanism: str = Field(min_length=1, max_length=2000)
    duration_basis: str = Field(min_length=1, max_length=1000)
    assumptions: list[str] = Field(max_length=12)
    invalidation_conditions: list[str] = Field(min_length=1, max_length=12)
    provenance_confidence: ConfidenceLevel
    mechanism_confidence: ConfidenceLevel
    temporal_confidence: ConfidenceLevel
    methodology_version: str = Field(min_length=1)


class SignalProposalBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposals: list[SignalProposal] = Field(max_length=12)
    no_signal_reason: str | None = Field(default=None, max_length=1000)
    reason_codes: list[str] = Field(default_factory=list, max_length=24)


class SignalDetailDraft(BaseModel):
    """Small LLM contract for one already-grounded Variable/Anchor pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact: str = Field(min_length=1, max_length=1000)
    direction: SignalDirection
    magnitude: SignalMagnitude
    impact_onset_days: int = Field(ge=0, le=1095)
    impact_peak_days: int = Field(ge=0, le=1095)
    expected_duration_days: int = Field(ge=1, le=1095)
    mechanism: str = Field(min_length=1, max_length=2000)
    duration_basis: str = Field(min_length=1, max_length=1000)
    assumptions: list[str] = Field(max_length=4)
    invalidation_conditions: list[str] = Field(min_length=1, max_length=4)
    provenance_confidence: ConfidenceLevel
    mechanism_confidence: ConfidenceLevel
    temporal_confidence: ConfidenceLevel

    @field_validator("assumptions", "invalidation_conditions", mode="before")
    @classmethod
    def one_text_item_can_be_normalized(cls, value):
        return [value] if isinstance(value, str) else value

    @model_validator(mode="after")
    def peak_must_not_precede_onset(self) -> SignalDetailDraft:
        if self.impact_peak_days < self.impact_onset_days:
            raise ValueError("impact_peak_days must not precede impact_onset_days")
        return self


class DirectSignalDraft(SignalDetailDraft):
    """Grounded detail compiled deterministically into a complete SignalProposal."""

    anchor_uuid: str = Field(min_length=1)
    variable_uuid: str = Field(min_length=1)

    def proposal(
        self,
        *,
        event_time: datetime,
        assertion_modality: Literal["ACTUAL", "ANTICIPATED", "SOURCE_FORECAST", "ASSUMED"],
    ) -> SignalProposal:
        valid_at = event_time + timedelta(days=self.impact_onset_days)
        impact_peak = event_time + timedelta(days=self.impact_peak_days)
        expected_end = valid_at + timedelta(days=self.expected_duration_days)
        horizon = (
            "SHORT" if self.expected_duration_days <= 90 else "MEDIUM" if self.expected_duration_days <= 365 else "LONG"
        )
        return SignalProposal(
            anchor_uuid=self.anchor_uuid,
            variable_uuid=self.variable_uuid,
            fact=self.fact,
            direction=self.direction,
            magnitude=self.magnitude,
            derivation_type="DERIVED",
            assertion_modality=assertion_modality,
            valid_at=valid_at,
            impact_onset_earliest=valid_at,
            impact_onset_latest=valid_at,
            impact_peak_earliest=impact_peak,
            impact_peak_latest=impact_peak,
            expected_end_earliest=expected_end,
            expected_end_latest=expected_end,
            horizon_tags=[horizon],
            mechanism=self.mechanism,
            duration_basis=self.duration_basis,
            assumptions=self.assumptions,
            invalidation_conditions=self.invalidation_conditions,
            provenance_confidence=self.provenance_confidence,
            mechanism_confidence=self.mechanism_confidence,
            temporal_confidence=self.temporal_confidence,
        )


class AnchorSignalSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    has_signal: bool
    variable_key: str | None = Field(default=None, pattern=r"^V[1-9][0-9]*$")
    rationale: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def selected_variable_matches_decision(self) -> AnchorSignalSelection:
        if self.has_signal != (self.variable_key is not None):
            raise ValueError("variable_key must exist exactly when has_signal is true")
        return self


class SignalCritique(BaseModel):
    """Independent semantic gate for directness and evidence support."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: bool
    reason_codes: list[str] = Field(max_length=6)


class EventAnalysisOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["SUCCEEDED", "NO_SIGNAL", "NO_SUPPORTED_ANCHOR"]
    classification: EventClassification
    signal_fact_uuids: list[str]
    reason_codes: list[str]
