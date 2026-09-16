"""Document-level extraction contracts; no source-content or length heuristics."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SemanticItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str | None = None
    action: str
    target: str | None = None
    announced_time: str | None = None
    effective_time: str | None = None
    planned_execution_time: str | None = None
    executed_time: str | None = None
    statement_type: Literal["POLICY", "GENERAL"]
    action_status: Literal["PLANNED", "OCCURRED"]
    assertion_status: Literal["CONFIRMED", "UNCONFIRMED"]


class DocumentEventDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    summary: str
    semantic: list[SemanticItem]
    keywords: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="0到5条量化事实短语，每条包含指标含义和数值；不含单独主体名、主题词、日期或编号。无量化事实为空数组。",
    )


class DuplicateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duplicate: bool
    matched_id: str | None = None
    reason: str


class DocumentEventAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: DocumentEventDraft
    deduplication: DuplicateDecision


class EventRecallCandidate(BaseModel):
    candidate_id: str
    title: str
    summary: str
    semantic: list[SemanticItem] = Field(default_factory=list)
    score: float


class StagedDocumentEvent(BaseModel):
    candidate_id: str
    article_key: str
    raw_path: str
    collected_at: datetime
    published_at: datetime | None
    event: DocumentEventDraft
