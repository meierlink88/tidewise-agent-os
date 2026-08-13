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


class CollectionQueryPlan(BaseModel):
    """Strict semantic plan returned by the Collector Agent."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=512)
    lookback_hours: int = Field(default=48, ge=1, le=8760)

    @model_validator(mode="after")
    def normalize_query(self) -> "CollectionQueryPlan":
        self.query = self.query.strip()
        if not self.query:
            raise ValueError("query must not be blank")
        return self


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
    requested_after: datetime
    requested_before: datetime
    agent_component_id: str
    agent_config_version: int = Field(ge=1)
    instructions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    collected_at: datetime
    candidates: list[Candidate]

    @model_validator(mode="after")
    def validate_requested_window(self) -> "ToolBatch":
        if self.requested_after.tzinfo is None or self.requested_after.utcoffset() is None:
            raise ValueError("requested_after must include a timezone")
        if self.requested_before.tzinfo is None or self.requested_before.utcoffset() is None:
            raise ValueError("requested_before must include a timezone")
        if self.requested_after >= self.requested_before:
            raise ValueError("requested_after must be earlier than requested_before")
        return self


class ToolBatchReceipt(BaseModel):
    """Small tool response returned to the model."""

    batch_id: str
    connector: str
    query: str
    requested_after: datetime
    requested_before: datetime
    result_count: int
    in_window_result_count: int
    candidate_ids: list[str]


class ChannelFetchReceipt(BaseModel):
    """One channel outcome returned inside a Tool façade receipt."""

    channel_code: str
    outcome: Literal["succeeded", "failed"]
    batch_id: str | None = None
    result_count: int = Field(ge=0)
    in_window_result_count: int = Field(ge=0)
    error_code: str | None = None


class FetchReceipt(BaseModel):
    """Compact aggregate receipt returned by a database-driven Tool façade."""

    tool: Literal["web_fetch", "api_fetch", "rss_fetch"]
    outcome: Literal["succeeded", "partial", "failed", "no_channels"]
    query: str
    requested_after: datetime
    requested_before: datetime
    channels: list[ChannelFetchReceipt]


class AcceptedDocument(BaseModel):
    """One accepted document prepared for publication."""

    candidate_id: str
    relative_path: str
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
