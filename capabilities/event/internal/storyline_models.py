"""Contracts separating semantic recommendations from durable execution state."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from capabilities.event.internal.models import (
    EventAgentVersions,
    EventIdentityDecision,
    EventIdentityRequest,
    EventPublicationRecord,
    EventResolutionRecord,
)
from sematica.analysis.event.contracts import (
    CandidateSet,
    DirectSignalDraft,
    EventClass,
    EventClassification,
    SignalProposal,
)

EVENT_ASSOCIATION_AGENT_ID = "event-association"
STORYLINE_AGENT_IDS = frozenset(
    {"event-extractor", "event-identity", EVENT_ASSOCIATION_AGENT_ID, "event-signal-analyst"}
)


class StorylineAgentVersions(EventAgentVersions):
    association: int = Field(alias=EVENT_ASSOCIATION_AGENT_ID, ge=1, strict=True)


class StorylineEventClassification(EventClassification):
    """Expose exactly four classes in the LLM JSON schema, not just a post-check."""

    event_class: Literal[
        EventClass.GEOPOLITICAL, EventClass.MACRO_ECONOMIC, EventClass.INDUSTRY_CHAIN, EventClass.COMPANY
    ]


class IdentityClassificationDecision(EventIdentityDecision):
    """The model judges identity and class, never IDs, control flow or publication."""

    classification: StorylineEventClassification | None = None

    @field_validator("classification", mode="before")
    @classmethod
    def normalize_classification_model(cls, value):
        return value.model_dump() if isinstance(value, EventClassification) else value

    @model_validator(mode="after")
    def require_primary_class(self):
        if self.decision in {"NEW_EVENT", "RELATED_BUT_DISTINCT"} and self.classification is None:
            raise ValueError("a publishable Event requires a primary classification")
        return self


class AssociationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uuid: str = Field(min_length=1)
    business_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    entity_type: Literal["GeopoliticRivalry", "MacroEconomic", "IndustryChain", "ChainNode", "Company"]
    profile: dict[str, Any]


class AssociationMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uuid: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=1000)


class AssociationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    matches: list[AssociationMatch] = Field(max_length=64)
    no_match_reason: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def require_unambiguous_result(self):
        if len({item.uuid for item in self.matches}) != len(self.matches):
            raise ValueError("duplicate association UUID")
        return self


class SignalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposals: list[DirectSignalDraft] = Field(max_length=30)
    no_signal_reason: str | None = Field(default=None, min_length=1, max_length=1000)


class StorylineCandidateState(BaseModel):
    """Only Functions mutate this lease-protected journal; it is never Agent output."""

    model_config = ConfigDict(extra="forbid")

    identity_request: EventIdentityRequest
    resolution: EventResolutionRecord | None = None
    classification: EventClassification | None = None
    association_pages: list[list[AssociationProfile]] | None = None
    association_results: list[AssociationDecision] = Field(default_factory=list)
    node_catalog_loaded: bool = False
    signal_pages: list[CandidateSet] | None = None
    signal_results: list[SignalDecision] = Field(default_factory=list)
    proposals: list[SignalProposal] = Field(default_factory=list)
    publication: EventPublicationRecord | None = None
    signal_receipts: dict[str, str] = Field(default_factory=dict)
    done: bool = False


class StorylineJournal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["event_storyline_journal.v1"] = "event_storyline_journal.v1"
    # Missing on pre-fix journals. Never silently trust cached empty judgments
    # produced when a business dict was dropped by Agno's Message parser.
    input_transport_version: Literal[1, 2] = 1
    agent_versions: StorylineAgentVersions
    candidates: dict[str, StorylineCandidateState] = Field(default_factory=dict)
