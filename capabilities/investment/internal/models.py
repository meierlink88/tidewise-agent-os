"""Typed contracts for Schedule-driven investment reasoning."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Confidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Direction(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    MIXED = "MIXED"
    STABLE = "STABLE"
    UNKNOWN = "UNKNOWN"


class Horizon(StrEnum):
    SHORT = "SHORT"
    MEDIUM = "MEDIUM"
    LONG = "LONG"


class Trend(StrEnum):
    WARMING = "WARMING"
    COOLING = "COOLING"
    DIVERGENT = "DIVERGENT"
    NO_MATERIAL_CHANGE = "NO_MATERIAL_CHANGE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class InvestmentAssessment(StrEnum):
    OPPORTUNITY_CANDIDATE = "OPPORTUNITY_CANDIDATE"
    RISK_POINT = "RISK_POINT"
    MIXED = "MIXED"
    NO_CLEAR_EDGE = "NO_CLEAR_EDGE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class InvestmentReasoningInput(FrozenModel):
    """Schedule-owned proposition accepted directly by the Workflow."""

    question: str = Field(min_length=1, max_length=2000)
    event_window_hours: int = Field(default=48, ge=1, le=720)
    include_company: Literal[False] = False

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_schedule_message(cls, value: object) -> object:
        """Normalize existing natural-language Schedule rows before field validation."""

        if isinstance(value, dict) and set(value) == {"message"}:
            value = value["message"]
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            return value
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            match = re.search(r"(?:最近|近)\s*(\d+)\s*小时", stripped)
            hours = int(match.group(1)) if match else 48
            return {
                "question": stripped,
                "event_window_hours": hours,
                "include_company": False,
            }
        if isinstance(decoded, dict) and set(decoded) == {"message"}:
            decoded = decoded["message"]
        if isinstance(decoded, str):
            match = re.search(r"(?:最近|近)\s*(\d+)\s*小时", decoded)
            return {
                "question": decoded,
                "event_window_hours": int(match.group(1)) if match else 48,
                "include_company": False,
            }
        return decoded


class InvestmentAnalysisRequest(InvestmentReasoningInput):
    """Runtime-owned bounds plus the Schedule's semantic scope."""

    forward_horizon_days: int = Field(default=1095, ge=1, le=3650)
    min_anchor_matches: int = Field(default=1, ge=1, le=10)
    max_chains: int = Field(default=10, ge=1, le=10)
    max_hops: int = Field(default=3, ge=1, le=3)
    decision_at: datetime

    @field_validator("decision_at")
    @classmethod
    def decision_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("decision_at must be explicit UTC")
        return value


class EventSnapshot(FrozenModel):
    episode_uuid: str
    event_id: str
    title: str
    summary: str
    modality: Literal["FACT", "PLAN", "SPEC"]
    occurred_at: datetime
    effective_at: datetime | None = None


class FactSnapshot(FrozenModel):
    uuid: str
    kind: Literal["ORDINARY", "SIGNAL"]
    name: str
    fact: str
    source_uuid: str
    source_name: str
    source_business_id: str | None = None
    source_labels: list[str] = Field(default_factory=list)
    target_uuid: str
    target_name: str
    target_business_id: str | None = None
    target_labels: list[str] = Field(default_factory=list)
    source_event_ids: list[str] = Field(default_factory=list)
    event_class: str | None = None
    anchor_type: str | None = None
    variable_id: str | None = None
    variable_role: str | None = None
    variable_group: str | None = None
    variable_definition: str | None = None
    variable_measurement_basis: str | None = None
    direction: Direction | None = None
    magnitude: str | None = None
    horizons: list[Horizon] = Field(default_factory=list)
    confidence: Confidence | None = None
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    expected_end_at: datetime | None = None
    assertion_modality: str | None = None
    mechanism: str | None = None

    def is_active_signal(self, decision_at: datetime) -> bool:
        return (
            self.kind == "SIGNAL"
            and self.direction not in {None, Direction.UNKNOWN}
            and bool(self.horizons)
            and self.confidence is not None
            and (self.valid_at is None or self.valid_at <= decision_at)
            and (self.invalid_at is None or self.invalid_at > decision_at)
            and (self.expected_end_at is None or self.expected_end_at > decision_at)
        )


class ImpactLayer(StrEnum):
    GEOPOLITICAL = "GEOPOLITICAL"
    MACRO_ECONOMIC = "MACRO_ECONOMIC"
    INDUSTRY = "INDUSTRY"


class AnalysisAnchorSnapshot(FrozenModel):
    uuid: str
    business_id: str
    name: str
    entity_type: Literal["GeopoliticRivalry", "MacroEconomic", "IndustryChain", "ChainNode"]
    summary: str = ""
    source_event_ids: list[str] = Field(default_factory=list, max_length=500)


class ImpactClaimProposal(FrozenModel):
    """One model-proposed layer conclusion before deterministic lineage gates."""

    anchor_id: str = Field(min_length=1)
    variable_id: str = Field(min_length=1, max_length=100)
    direction: Direction
    horizons: list[Horizon] = Field(min_length=1, max_length=3)
    confidence: Confidence
    summary: str = Field(min_length=1, max_length=1200)
    mechanism: str = Field(min_length=1, max_length=1600)
    source_fact_ids: list[str] = Field(default_factory=list, max_length=20)
    mechanism_fact_ids: list[str] = Field(default_factory=list, max_length=20)
    parent_claim_ids: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    risks: list[str] = Field(default_factory=list, max_length=8)


class AcceptedImpactClaim(ImpactClaimProposal):
    claim_id: str
    layer: ImpactLayer
    anchor_name: str
    anchor_type: Literal["GeopoliticRivalry", "MacroEconomic", "IndustryChain", "ChainNode"]
    derivation: Literal["DIRECT_SIGNAL", "CROSS_LAYER"]
    root_event_ids: list[str] = Field(min_length=1, max_length=500)
    root_signal_fact_ids: list[str] = Field(min_length=1, max_length=40)


class LayerImpactBatch(FrozenModel):
    proposals: list[ImpactClaimProposal] = Field(default_factory=list, max_length=50)
    supplemental_queries: list[str] = Field(default_factory=list, max_length=4)
    summary: str = Field(min_length=1, max_length=1600)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class LayerAnalysisContext(FrozenModel):
    layer: ImpactLayer
    decision_at: datetime
    question: str = Field(min_length=1, max_length=2000)
    events: list[EventSnapshot] = Field(min_length=1, max_length=500)
    anchors: list[AnalysisAnchorSnapshot] = Field(default_factory=list, max_length=100)
    facts: list[FactSnapshot] = Field(default_factory=list, max_length=1200)
    parent_claims: list[AcceptedImpactClaim] = Field(default_factory=list, max_length=100)
    direct_signal_fact_ids: list[str] = Field(default_factory=list, max_length=500)
    retrieval_round: int = Field(default=1, ge=1, le=2)


class LayerAnalysisResult(FrozenModel):
    layer: ImpactLayer
    claims: list[AcceptedImpactClaim] = Field(default_factory=list, max_length=100)
    supporting_facts: list[FactSnapshot] = Field(default_factory=list, max_length=1200)
    summary: str = Field(min_length=1, max_length=1600)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    retrieval_rounds: int = Field(default=1, ge=1, le=2)


class ChainNodeSnapshot(FrozenModel):
    uuid: str
    business_id: str
    name: str
    stage: str | None = None
    position: int | None = None


class TopologyEdgeSnapshot(FrozenModel):
    uuid: str
    business_id: str
    name: Literal["ChainNodeInputTo", "ChainNodeIsComponentOf", "ChainNodeDependsOn"]
    source_node_id: str
    source_name: str
    target_node_id: str
    target_name: str
    fact: str


class IndustryChainSnapshot(FrozenModel):
    uuid: str
    business_id: str
    name: str
    anchor_match_count: int = Field(ge=1)
    matched_node_ids: list[str]
    signal_root_fact_ids: list[str] = Field(default_factory=list)
    signal_root_node_ids: list[str] = Field(default_factory=list)
    nodes: list[ChainNodeSnapshot] = Field(min_length=1, max_length=200)
    edges: list[TopologyEdgeSnapshot] = Field(default_factory=list, max_length=500)


class InvestmentAnalysisContext(FrozenModel):
    context_version: Literal["investment-reasoning-context/v3"] = "investment-reasoning-context/v3"
    request: InvestmentAnalysisRequest
    events: list[EventSnapshot] = Field(min_length=1, max_length=500)
    facts: list[FactSnapshot] = Field(default_factory=list, max_length=2000)
    anchors: list[AnalysisAnchorSnapshot] = Field(default_factory=list, max_length=500)
    chains: list[IndustryChainSnapshot] = Field(default_factory=list, max_length=10)
    retrieval_strategy: Literal["GRAPHITI_NATIVE_SEARCH_PLUS_EXACT_TEMPORAL_SCOPE"] = (
        "GRAPHITI_NATIVE_SEARCH_PLUS_EXACT_TEMPORAL_SCOPE"
    )
    native_retrieved_fact_ids: list[str] = Field(default_factory=list, max_length=1000)
    validation_issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def identities_are_unique_and_scoped(self) -> InvestmentAnalysisContext:
        anchor_uuids = [item.uuid for item in self.anchors]
        if len(anchor_uuids) != len(set(anchor_uuids)):
            raise ValueError("analysis-anchor identities must be unique")
        chain_ids = [item.business_id for item in self.chains]
        if len(chain_ids) != len(set(chain_ids)):
            raise ValueError("industry-chain identities must be unique")
        for chain in self.chains:
            node_ids = {item.business_id for item in chain.nodes}
            if len(node_ids) != len(chain.nodes):
                raise ValueError(f"duplicate nodes in chain {chain.business_id}")
            for edge in chain.edges:
                if edge.source_node_id not in node_ids or edge.target_node_id not in node_ids:
                    raise ValueError(f"topology edge {edge.business_id} escapes chain {chain.business_id}")
        return self

    @property
    def eligible_signal_fact_ids(self) -> set[str]:
        window_start = self.request.decision_at - timedelta(hours=self.request.event_window_hours)
        scoped_event_ids = {
            event.event_id for event in self.events if window_start <= event.occurred_at <= self.request.decision_at
        }
        return {
            fact.uuid
            for fact in self.facts
            if fact.is_active_signal(self.request.decision_at)
            and bool(scoped_event_ids.intersection(fact.source_event_ids))
        }


class TransmissionProposal(FrozenModel):
    chain_id: str
    topology_edge_id: str
    source_node_id: str
    target_node_id: str
    flow: Literal["ALONG_EDGE", "AGAINST_EDGE"]
    target_variable: str = Field(min_length=1, max_length=100)
    direction: Direction
    horizon: Horizon
    confidence: Confidence
    mechanism: str = Field(min_length=1, max_length=1200)
    source_fact_ids: list[str] = Field(default_factory=list, max_length=20)
    source_claim_ids: list[str] = Field(default_factory=list, max_length=20)
    parent_transmission_ids: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=8)


class AcceptedTransmission(TransmissionProposal):
    transmission_id: str
    hop: int = Field(ge=1, le=3)
    root_signal_fact_ids: list[str] = Field(min_length=1, max_length=20)


class TransmissionBatch(FrozenModel):
    proposals: list[TransmissionProposal] = Field(default_factory=list, max_length=100)
    stopped_reason: str | None = Field(default=None, max_length=500)


class NodeTrendView(FrozenModel):
    chain_id: str
    node_id: str
    node_name: str
    short: Trend
    medium: Trend
    long: Trend
    confidence: Confidence
    investment_assessment: InvestmentAssessment
    rationale: str = Field(min_length=1, max_length=1600)
    supporting_fact_ids: list[str] = Field(default_factory=list, max_length=30)
    supporting_claim_ids: list[str] = Field(default_factory=list, max_length=30)
    supporting_transmission_ids: list[str] = Field(default_factory=list, max_length=30)
    risks: list[str] = Field(default_factory=list, max_length=10)


class NodeAnalysisBatch(FrozenModel):
    nodes: list[NodeTrendView] = Field(default_factory=list, max_length=200)


class ChainTrendView(FrozenModel):
    chain_id: str
    chain_name: str
    short: Trend
    medium: Trend
    long: Trend
    confidence: Confidence
    summary: str = Field(min_length=1, max_length=1600)
    nodes: list[NodeTrendView] = Field(min_length=1, max_length=200)


class AnalysisDraft(FrozenModel):
    one_sentence_conclusion: str = Field(min_length=1, max_length=2000)
    chains: list[ChainTrendView] = Field(default_factory=list, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class ReviewResult(FrozenModel):
    accepted: bool
    confidence: Confidence
    issue_codes: list[str] = Field(default_factory=list, max_length=30)
    review_summary: str = Field(min_length=1, max_length=2000)


class ReasoningTraceNode(FrozenModel):
    node_id: str
    node_type: Literal["EVENT", "FACT", "SIGNAL", "LAYER_CLAIM", "TRANSMISSION", "NODE_CONCLUSION"]
    label: str = Field(min_length=1, max_length=1600)
    parent_ids: list[str] = Field(default_factory=list, max_length=50)


class InvestmentAnalysisResult(FrozenModel):
    result_version: Literal["investment-reasoning-result/v3"] = "investment-reasoning-result/v3"
    executor: str
    status: Literal["SUCCEEDED", "NEEDS_REVIEW"]
    context_fingerprint: str
    geopolitical: LayerAnalysisResult
    macro: LayerAnalysisResult
    industry: LayerAnalysisResult
    transmissions: list[AcceptedTransmission]
    draft: AnalysisDraft
    review: ReviewResult
    reasoning_tree: list[ReasoningTraceNode] = Field(default_factory=list, max_length=5000)
    stage_metrics: dict[str, int]
    execution_issues: list[str] = Field(default_factory=list, max_length=100)


class PreparedInvestmentContext(FrozenModel):
    context: InvestmentAnalysisContext
    context_fingerprint: str


class GeopoliticalAnalysisState(FrozenModel):
    prepared: PreparedInvestmentContext
    geopolitical: LayerAnalysisResult


class MacroAnalysisState(FrozenModel):
    prepared: PreparedInvestmentContext
    geopolitical: LayerAnalysisResult
    macro: LayerAnalysisResult


class IndustryAnalysisState(FrozenModel):
    prepared: PreparedInvestmentContext
    geopolitical: LayerAnalysisResult
    macro: LayerAnalysisResult
    industry: LayerAnalysisResult
    industry_context: InvestmentAnalysisContext
    transmissions: list[AcceptedTransmission]
    rounds_executed: int = Field(ge=0, le=3)
    draft: AnalysisDraft
    execution_issues: list[str] = Field(default_factory=list)
