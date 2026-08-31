"""Strict Agent OS and Pipeline contracts for Event Candidate resolution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from capabilities.evidence import EvidenceMetric

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
TimePrecision = Literal["INSTANT", "DAY", "RANGE", "MONTH", "QUARTER", "YEAR", "UNKNOWN"]


class EventTimeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    occurred_at: datetime | None
    announced_at: datetime | None
    effective_at: datetime | None
    observed_at: datetime | None = None
    precision: TimePrecision

    @field_validator("occurred_at", "announced_at", "effective_at", "observed_at")
    @classmethod
    def event_times_are_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)):
            raise ValueError("Event time values must be explicit UTC")
        return value


def event_time_anchor(value: EventTimeDTO) -> datetime | None:
    """Return the ordered formal Event time anchor."""

    return value.occurred_at or value.announced_at or value.effective_at or value.observed_at


def _metric_key(metric: EvidenceMetric) -> tuple[str, str, str, str, str]:
    return (
        metric.name,
        metric.value or "",
        metric.unit or "",
        metric.change or "",
        metric.period or "",
    )


class EventSemanticDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actors: list[str] = Field(min_length=1)
    action: str = Field(min_length=1)
    objects: list[str] = Field(min_length=1)
    stage: EventStage
    modality: Literal["FACT", "PLAN", "SPEC"]
    time: EventTimeDTO
    jurisdictions: list[str]
    reason: str | None = Field(max_length=500)
    method: str | None = Field(max_length=500)
    metrics: list[EvidenceMetric]

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

    @field_validator("reason", "method")
    @classmethod
    def support_text_is_nonblank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("reason and method must be nonblank or null")
        return value

    @field_validator("metrics")
    @classmethod
    def metrics_are_deterministically_unique(cls, values: list[EvidenceMetric]) -> list[EvidenceMetric]:
        return sorted({_metric_key(metric): metric for metric in values}.values(), key=_metric_key)


class EventCandidateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1)
    semantic: EventSemanticDTO

    @field_validator("title", "summary")
    @classmethod
    def narrative_text_is_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Event title and summary must not be blank")
        return value


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


class HistoricalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    event: EventCandidateDTO

    @model_validator(mode="after")
    def require_formal_time_anchor(self) -> HistoricalEvent:
        if event_time_anchor(self.event.semantic.time) is None:
            raise ValueError("formal Event time requires a business or observed time anchor")
        return self
