"""Typed contracts for the fixed multi-stage investment reasoning DAG."""

from __future__ import annotations

from datetime import UTC, datetime
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


class InvestmentAnalysisRequest(FrozenModel):
    question: str = Field(min_length=1, max_length=2000)
    decision_at: datetime
    event_window_hours: int = Field(default=48, ge=1, le=720)
    forward_horizon_days: int = Field(default=1095, ge=1, le=3650)
    min_anchor_matches: int = Field(default=2, ge=1, le=10)
    max_chains: int = Field(default=10, ge=1, le=10)
    max_hops: int = Field(default=3, ge=1, le=3)

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
    variable_id: str | None = None
    variable_role: str | None = None
    variable_group: str | None = None
    variable_definition: str | None = None
    variable_measurement_basis: str | None = None
    direction: Direction | None = None
    magnitude: str | None = None
    horizons: list[Horizon] = Field(default_factory=list)
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    expected_end_at: datetime | None = None
    assertion_modality: str | None = None
    mechanism: str | None = None


class ChainNodeSnapshot(FrozenModel):
    uuid: str
    business_id: str
    name: str
    stage: str | None = None
    position: int | None = None


class TopologyEdgeSnapshot(FrozenModel):
    uuid: str
    business_id: str
    name: Literal[
        "ChainNodeInputTo",
        "ChainNodeIsComponentOf",
        "ChainNodeDependsOn",
    ]
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
    nodes: list[ChainNodeSnapshot] = Field(min_length=1, max_length=10)
    edges: list[TopologyEdgeSnapshot] = Field(max_length=20)


class InvestmentAnalysisContext(FrozenModel):
    context_version: Literal["investment-reasoning-context/v1"] = "investment-reasoning-context/v1"
    request: InvestmentAnalysisRequest
    events: list[EventSnapshot] = Field(min_length=1, max_length=100)
    facts: list[FactSnapshot] = Field(max_length=500)
    chains: list[IndustryChainSnapshot] = Field(min_length=1, max_length=10)
    retrieval_strategy: Literal["GRAPHITI_NATIVE_HYBRID_PLUS_EXACT_TEMPORAL_SCOPE"] = (
        "GRAPHITI_NATIVE_HYBRID_PLUS_EXACT_TEMPORAL_SCOPE"
    )
    native_retrieved_fact_ids: list[str] = Field(default_factory=list, max_length=100)
    validation_issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def identities_are_unique_and_scoped(self) -> InvestmentAnalysisContext:
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
    parent_transmission_ids: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=8)


class AcceptedTransmission(TransmissionProposal):
    transmission_id: str
    hop: int = Field(ge=1, le=3)


class TransmissionBatch(FrozenModel):
    proposals: list[TransmissionProposal] = Field(default_factory=list, max_length=80)
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
    supporting_transmission_ids: list[str] = Field(default_factory=list, max_length=30)
    risks: list[str] = Field(default_factory=list, max_length=10)


class NodeAnalysisBatch(FrozenModel):
    nodes: list[NodeTrendView] = Field(default_factory=list, max_length=10)


class ChainTrendView(FrozenModel):
    chain_id: str
    chain_name: str
    short: Trend
    medium: Trend
    long: Trend
    confidence: Confidence
    summary: str = Field(min_length=1, max_length=1600)
    nodes: list[NodeTrendView] = Field(min_length=1, max_length=10)


class AnalysisDraft(FrozenModel):
    one_sentence_conclusion: str = Field(min_length=1, max_length=2000)
    chains: list[ChainTrendView] = Field(min_length=1, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class ReviewResult(FrozenModel):
    accepted: bool
    confidence: Confidence
    issue_codes: list[str] = Field(default_factory=list, max_length=30)
    review_summary: str = Field(min_length=1, max_length=2000)


class InvestmentAnalysisResult(FrozenModel):
    result_version: Literal["investment-reasoning-result/v1"] = "investment-reasoning-result/v1"
    executor: str
    status: Literal["SUCCEEDED", "NEEDS_REVIEW"]
    context_fingerprint: str
    transmissions: list[AcceptedTransmission]
    draft: AnalysisDraft
    review: ReviewResult
    stage_metrics: dict[str, int]
    execution_issues: list[str] = Field(default_factory=list, max_length=100)


class RecordedReasoningPayload(FrozenModel):
    payload_version: Literal["recorded-investment-reasoner/v1"] = "recorded-investment-reasoner/v1"
    executor_name: str = "codex-recorded-reasoner"
    rounds: dict[int, TransmissionBatch]
    draft: AnalysisDraft
    review: ReviewResult
    execution_issues: list[str] = Field(default_factory=list, max_length=100)


class ComparisonDifference(FrozenModel):
    chain_id: str
    node_id: str
    horizon: Horizon
    left: Trend
    right: Trend
    severity: Literal["COMPATIBLE", "MATERIAL"]


class ComparisonReport(FrozenModel):
    comparison_version: Literal["investment-reasoning-comparison/v1"] = "investment-reasoning-comparison/v1"
    same_context: bool
    total_node_horizons: int
    exact_matches: int
    compatible_matches: int
    material_contradictions: int
    weighted_similarity: float = Field(ge=0, le=1)
    basically_consistent: bool
    differences: list[ComparisonDifference]
