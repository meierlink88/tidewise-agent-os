"""Frozen 24-hour research inputs and durable per-story execution receipts."""

from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

PRESET = "geopolitical_war_room"
MAX_STORIES = 1_000


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cutoff_at: AwareDatetime | None = None
    market: str = Field(default="A股市场", min_length=1, max_length=200)


class ResearchStory(BaseModel):
    story_id: str = Field(pattern=r"^GPR[A-Za-z0-9_-]+$", max_length=100)
    name: str = Field(min_length=1)
    core_proposition: str = ""
    events: list[dict[str, Any]] = Field(min_length=1)


class ResearchPlan(BaseModel):
    schema_version: Literal["geopolitical-research-plan/v1"] = "geopolitical-research-plan/v1"
    workflow_run_id: str
    start_at: AwareDatetime
    cutoff_at: AwareDatetime
    market: str
    stories: list[ResearchStory]


class ResearchReceipt(BaseModel):
    schema_version: Literal["geopolitical-research-receipt/v1"] = "geopolitical-research-receipt/v1"
    job_id: str
    story_id: str
    source_workflow_run_id: str
    research_base_url: str
    user_vars: dict[str, str]
    status: Literal["submitting", "running", "completed", "failed", "cancelled", "unknown"]
    research_run_id: str | None = None
    report_path: str | None = None
    report_sha256: str | None = None
    report_bytes: int | None = None
    error_code: str | None = None
    updated_at: datetime


class ResearchRunDetail(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    preset_name: Literal["geopolitical_war_room"]
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    user_vars: dict[str, Any]
    final_report: str | None = None
