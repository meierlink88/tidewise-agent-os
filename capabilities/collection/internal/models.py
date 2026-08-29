"""Typed contracts for the raw information collection capability."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class SourceLevel(StrEnum):
    """Trust level assigned to the original publishing source."""

    L1_OFFICIAL = "L1_OFFICIAL"
    L2_WIRE = "L2_WIRE"
    L3_MEDIA = "L3_MEDIA"
    L4_SOCIAL = "L4_SOCIAL"


class CollectionRequest(BaseModel):
    """User-facing Workflow input."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=65_536)

    @model_validator(mode="before")
    @classmethod
    def accept_plain_objective(cls, value: Any) -> Any:
        """Allow AgentOS chat clients to send a plain string."""
        if isinstance(value, str):
            return {"objective": value}
        return value

    @model_validator(mode="after")
    def reject_blank_objective(self) -> "CollectionRequest":
        self.objective = self.objective.strip()
        if not self.objective:
            raise ValueError("objective must not be blank")
        return self


class TitleCurationItem(BaseModel):
    """Bounded material context visible to the Raw Evidence Filter Agent."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=1_024)
    source_name: str = Field(min_length=1, max_length=200)
    published_at: datetime | None = None
    content_excerpt: str = Field(min_length=1, max_length=2_000)


class TitleCurationRequest(BaseModel):
    """Complete candidate set requiring one title decision each."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[TitleCurationItem]


class TitleCurationDecision(BaseModel):
    """One strict binary title-only relevance decision."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    is_relevant: bool = Field(strict=True)


class TitleCurationDraft(BaseModel):
    """Strict Title Curator output validated before Artifact construction."""

    model_config = ConfigDict(extra="forbid")

    decisions: list[TitleCurationDecision]


class Candidate(BaseModel):
    """One direct result returned by a channel tool."""

    candidate_id: str
    connector: str
    query: str
    title: str
    url: HttpUrl
    content: str
    source_name: str
    source_level: SourceLevel = SourceLevel.L3_MEDIA
    source_external_id: str | None = None
    published_at: datetime | None = None
    collected_at: datetime


class ToolBatch(BaseModel):
    """Complete persisted result of one channel-tool call."""

    schema_version: Literal["collection_tool_batch.v1"] = "collection_tool_batch.v1"
    batch_id: str
    collection_id: str
    connector: str
    query: str
    collected_at: datetime
    candidates: list[Candidate]


class ToolBatchReceipt(BaseModel):
    """Small tool response returned to the model."""

    batch_id: str
    connector: str
    query: str
    result_count: int
    candidate_ids: list[str]


class ChannelFetchReceipt(BaseModel):
    """One channel outcome returned inside an acquisition-group receipt."""

    channel_code: str
    outcome: Literal["succeeded", "failed"]
    batch_id: str | None = None
    result_count: int = Field(ge=0)
    error_code: str | None = None


class FetchReceipt(BaseModel):
    """Compact aggregate receipt returned by one deterministic acquisition group."""

    channel_group: Literal["web_search", "api", "rss"]
    outcome: Literal["succeeded", "partial", "failed", "no_channels"]
    query: str
    channels: list[ChannelFetchReceipt]


class AcceptedDocument(BaseModel):
    """One accepted document prepared for publication."""

    candidate_id: str
    relative_path: str
    url_path: str
    sha256: str


class PreparedArtifactSet(BaseModel):
    """Output of deterministic Artifact construction."""

    schema_version: Literal["prepared_collection_artifacts.v1"] = "prepared_collection_artifacts.v1"
    collection_id: str
    outcome: Literal["changed", "no_change"]
    staging_root: str
    results_terminal: int
    results_pending: Literal[0] = 0
    candidate_counts: dict[str, int]
    accepted_documents: list[AcceptedDocument]
    publication_items: list[str]


class CollectionResult(BaseModel):
    """Final Workflow result returned after manifest publication."""

    schema_version: Literal["collection_result.v1"] = "collection_result.v1"
    collection_id: str
    outcome: Literal["changed", "no_change"]
    accepted_documents: int
    candidate_counts: dict[str, int]
    manifest_path: str
    completed_at: datetime
