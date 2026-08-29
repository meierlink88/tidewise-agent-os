"""Strict Agent OS and Pipeline contracts for Event Candidate resolution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EventStage = Literal[
    "OCCURRED",
    "ANNOUNCED",
    "EFFECTIVE",
    "IMPLEMENTED",
    "UPDATED",
    "SUSPENDED",
    "TERMINATED",
    "EXPECTED",
]
TimePrecision = Literal["INSTANT", "DAY", "MONTH", "QUARTER", "YEAR", "UNKNOWN"]
Decision = Literal[
    "SAME_EVENT",
    "NEW_EVENT",
    "RELATED_BUT_DISTINCT",
    "FAILED",
    "SAME_EVENT_REVISION",
]


class EventSemanticDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actors: list[str] = Field(min_length=1)
    action: str = Field(min_length=1)
    objects: list[str] = Field(min_length=1)
    stage: EventStage
    jurisdictions: list[str]
    effective_at: datetime | None
    time_precision: TimePrecision

    @field_validator("actors", "objects", "jurisdictions")
    @classmethod
    def identity_terms_are_nonblank_and_unique(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("semantic identity terms must be nonblank and unique")
        return normalized

    @field_validator("action")
    @classmethod
    def action_is_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("action must not be blank")
        return value

    @field_validator("effective_at")
    @classmethod
    def effective_time_is_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)):
            raise ValueError("effective_at must be explicit UTC")
        return value


class EventCandidateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1)
    semantic: EventSemanticDTO
    modality: Literal["FACT", "PLAN", "SPEC"]
    occurred_at: datetime | None
    announced_at: datetime | None

    @field_validator("title", "summary")
    @classmethod
    def narrative_text_is_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Event title and summary must not be blank")
        return value

    @field_validator("occurred_at", "announced_at")
    @classmethod
    def event_times_are_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)):
            raise ValueError("Event timestamps must be explicit UTC")
        return value

    @model_validator(mode="after")
    def occurrence_has_a_time_anchor(self) -> EventCandidateDTO:
        if self.occurred_at is None and self.announced_at is None and self.semantic.effective_at is None:
            raise ValueError("Event requires an occurrence, announcement, or effective time")
        return self


class EventCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event: EventCandidateDTO
    evidence_ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("evidence_ids")
    @classmethod
    def evidence_ids_are_formal_and_unique(cls, values: list[str]) -> list[str]:
        import re

        pattern = re.compile(r"^EVD[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        if any(pattern.fullmatch(value) is None for value in values) or len(set(values)) != len(values):
            raise ValueError("evidence_ids must be unique formal Data identities")
        return values


class EventResolutionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    decision: Decision
    event_id: str | None
    event_created: bool
    evidence_link_result: Literal["CREATED", "IGNORED", "NOT_ATTEMPTED"]
    graph_projection_status: Literal["SUCCEEDED", "IGNORED", "NOT_ATTEMPTED"]
    reason_codes: list[str]
    matched_event_ids: list[str]


class HistoricalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    event: EventCandidateDTO


class PairComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    decision: Literal["SAME_EVENT", "RELATED_BUT_DISTINCT", "NEEDS_REVIEW", "SAME_EVENT_REVISION"]
    same_actor: bool
    same_action: bool
    same_object: bool
    same_stage: bool
    same_occurrence_time: bool
    material_conflicts: list[str]
    reason_codes: list[str]
    summary: str = Field(min_length=1, max_length=500)


class AtomicityAssessment(BaseModel):
    """Constrained model result for the one-real-world-action invariant."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    atomic: bool
    reason_codes: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=500)
