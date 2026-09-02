"""Typed contracts for Schedule-driven investment reasoning."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_EVENT_WINDOW_HOURS = 24 * 365
MAX_RUN_ANCHORS = 2000
MAX_RUN_CHAINS = 2000
MAX_RUN_PROPOSALS = 2000
TRANSMISSION_INCLUSION_THRESHOLD = 0.4
TRANSMISSION_CONTINUATION_THRESHOLD = 0.65


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
    event_window_hours: int = Field(default=48, ge=1, le=MAX_EVENT_WINDOW_HOURS)
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
    max_hops: int = Field(default=5, ge=1, le=5)
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
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)


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
            and self.anchor_type != "IndustryChain"
            and "IndustryChain" not in self.target_labels
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


class ReasoningOntologyContext(FrozenModel):
    """Compact ontology semantics supplied beside graph instances to every reasoning step."""

    ontology_version: Literal["investment-reasoning-ontology/v1"] = "investment-reasoning-ontology/v1"
    entity_types: dict[str, str]
    fact_types: dict[str, str]
    relationship_types: dict[str, str]
    usage_rules: list[str] = Field(min_length=1, max_length=30)


class RetrievalReceipt(FrozenModel):
    """Auditable proof that one layer executed its required Graphiti retrieval actions."""

    stage: Literal["PREPARE", "GEOPOLITICAL", "MACRO_ECONOMIC", "INDUSTRY"]
    layer: ImpactLayer | None = None
    retrieval_round: int = Field(default=1, ge=1, le=2)
    required_actions: list[str] = Field(min_length=1, max_length=20)
    completed_actions: list[str] = Field(min_length=1, max_length=20)
    queries: list[str] = Field(default_factory=list, max_length=25)
    event_ids: list[str] = Field(default_factory=list, max_length=500)
    anchor_ids: list[str] = Field(default_factory=list, max_length=MAX_RUN_ANCHORS)
    fact_ids: list[str] = Field(default_factory=list, max_length=2000)
    direct_signal_fact_ids: list[str] = Field(default_factory=list, max_length=2000)


class LayerAssessmentProposal(FrozenModel):
    """One Agent-produced interpretation of a retrieved, Signal-backed graph anchor."""

    anchor_id: str = Field(min_length=1)
    result: Trend
    confidence: Confidence
    summary: str = Field(min_length=1, max_length=1200)
    reasoning: str = Field(min_length=1, max_length=1600)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    risks: list[str] = Field(default_factory=list, max_length=8)


class LayerAssessment(LayerAssessmentProposal):
    """Reviewed layer result; direct graph evidence remains referenced as Signal Facts."""

    assessment_id: str
    layer: ImpactLayer
    direct_signal_fact_ids: list[str] = Field(min_length=1, max_length=500)
    anchor_name: str
    anchor_type: Literal["GeopoliticRivalry", "MacroEconomic", "IndustryChain", "ChainNode"]
    horizons: list[Horizon] = Field(min_length=1, max_length=3)
    root_event_ids: list[str] = Field(min_length=1, max_length=500)


class LayerAssessmentBatch(FrozenModel):
    proposals: list[LayerAssessmentProposal] = Field(default_factory=list, max_length=MAX_RUN_PROPOSALS)
    supplemental_queries: list[str] = Field(default_factory=list, max_length=4)
    summary: str = Field(min_length=1, max_length=1600)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class LayerAnalysisContext(FrozenModel):
    layer: ImpactLayer
    decision_at: datetime
    question: str = Field(min_length=1, max_length=2000)
    events: list[EventSnapshot] = Field(min_length=1, max_length=500)
    anchors: list[AnalysisAnchorSnapshot] = Field(default_factory=list, max_length=MAX_RUN_ANCHORS)
    facts: list[FactSnapshot] = Field(default_factory=list, max_length=2000)
    parent_assessments: list[LayerAssessment] = Field(default_factory=list, max_length=MAX_RUN_ANCHORS)
    direct_signal_fact_ids: list[str] = Field(default_factory=list, max_length=2000)
    ontology: ReasoningOntologyContext
    retrieval_receipt: RetrievalReceipt
    retrieval_round: int = Field(default=1, ge=1, le=2)


class LayerAnalysisResult(FrozenModel):
    layer: ImpactLayer
    assessments: list[LayerAssessment] = Field(default_factory=list, max_length=MAX_RUN_ANCHORS)
    supporting_facts: list[FactSnapshot] = Field(default_factory=list, max_length=2000)
    summary: str = Field(min_length=1, max_length=1600)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    retrieval_receipts: list[RetrievalReceipt] = Field(default_factory=list, max_length=2)
    retrieval_rounds: int = Field(default=1, ge=1, le=2)


class CrossLayerTransmissionProposal(FrozenModel):
    """One explicit hypothesis explaining how an upper assessment informs a lower assessment."""

    source_assessment_id: str = Field(min_length=1)
    target_assessment_id: str = Field(min_length=1)
    mechanism_fact_ids: list[str] = Field(default_factory=list, max_length=20)
    logic: str = Field(min_length=1, max_length=1600)
    confidence: Confidence
    status: str = Field(min_length=1, max_length=500)


class CrossLayerTransmissionBatch(FrozenModel):
    proposals: list[CrossLayerTransmissionProposal] = Field(default_factory=list, max_length=MAX_RUN_PROPOSALS)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class AcceptedCrossLayerTransmission(CrossLayerTransmissionProposal):
    transmission_id: str
    source_layer: ImpactLayer
    target_layer: ImpactLayer
    relation_type: Literal["CROSS_LAYER", "SAME_SOURCE_SIGNAL"]


class CandidateCrossLayerMechanism(CrossLayerTransmissionProposal):
    reason: str = Field(min_length=1, max_length=500)


class CrossLayerAnalysisResult(FrozenModel):
    target_layer: ImpactLayer
    accepted: list[AcceptedCrossLayerTransmission] = Field(default_factory=list, max_length=MAX_RUN_PROPOSALS)
    candidates: list[CandidateCrossLayerMechanism] = Field(default_factory=list, max_length=MAX_RUN_PROPOSALS)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class ChainNodeSnapshot(FrozenModel):
    uuid: str
    business_id: str
    name: str
    stage: str | None = None
    position: int | None = None


class TopologyEdgeSnapshot(FrozenModel):
    uuid: str
    business_id: str
    name: str = Field(min_length=1, max_length=120)
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
    context_version: Literal["investment-reasoning-context/v5"] = "investment-reasoning-context/v5"
    request: InvestmentAnalysisRequest
    events: list[EventSnapshot] = Field(min_length=1, max_length=500)
    facts: list[FactSnapshot] = Field(default_factory=list, max_length=2000)
    anchors: list[AnalysisAnchorSnapshot] = Field(default_factory=list, max_length=MAX_RUN_ANCHORS)
    chains: list[IndustryChainSnapshot] = Field(default_factory=list, max_length=MAX_RUN_CHAINS)
    ontology: ReasoningOntologyContext
    retrieval_strategy: Literal["GRAPHITI_NATIVE_SEARCH_PLUS_EXACT_TEMPORAL_SCOPE"] = (
        "GRAPHITI_NATIVE_SEARCH_PLUS_EXACT_TEMPORAL_SCOPE"
    )
    native_retrieved_fact_ids: list[str] = Field(default_factory=list, max_length=1000)
    retrieval_receipts: list[RetrievalReceipt] = Field(default_factory=list, max_length=10)
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


class TransmissionCandidate(FrozenModel):
    """One real topology move that the Agent must evaluate without changing IDs."""

    candidate_id: str
    chain_id: str
    topology_edge_id: str
    source_node_id: str
    target_node_id: str
    flow: Literal["ALONG_EDGE", "AGAINST_EDGE"]
    horizon: Horizon
    source_fact_ids: list[str] = Field(default_factory=list, max_length=20)
    source_assessment_ids: list[str] = Field(default_factory=list, max_length=20)
    parent_transmission_ids: list[str] = Field(default_factory=list, max_length=20)


class TransmissionProposal(FrozenModel):
    candidate_id: str | None = None
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
    source_assessment_ids: list[str] = Field(default_factory=list, max_length=20)
    parent_transmission_ids: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=8)


class AcceptedTransmission(TransmissionProposal):
    transmission_id: str
    hop: int = Field(ge=1, le=5)
    root_signal_fact_ids: list[str] = Field(min_length=1, max_length=20)
    path_score: float = Field(default=0.4, ge=0, le=1)


class TransmissionBatch(FrozenModel):
    # One Agent call remains bounded per chain, while the deterministic runtime
    # merges results from as many as 100 real chains into this run-level batch.
    proposals: list[TransmissionProposal] = Field(default_factory=list, max_length=2000)
    stopped_reason: str | None = Field(default=None, max_length=500)


class TransmissionSemanticIssue(FrozenModel):
    """One concrete semantic defect attached to an otherwise valid transmission."""

    transmission_id: str = Field(min_length=1, max_length=100)
    issue_code: Literal[
        "VARIABLE_TRANSITION_INCONSISTENT",
        "MECHANISM_DIRECTION_INCONSISTENT",
        "UNJUSTIFIED_EVIDENCE_REUSE",
        "OTHER",
    ]
    critique: str = Field(min_length=1, max_length=1200)
    repair_instruction: str = Field(min_length=1, max_length=1200)


class TransmissionSemanticReview(FrozenModel):
    """Sparse local review: only defective transmissions are returned."""

    issues: list[TransmissionSemanticIssue] = Field(default_factory=list, max_length=MAX_RUN_PROPOSALS)


class TransmissionExecutionMetrics(FrozenModel):
    inclusion_threshold: float = Field(default=TRANSMISSION_INCLUSION_THRESHOLD, ge=0, le=1)
    continuation_threshold: float = Field(default=TRANSMISSION_CONTINUATION_THRESHOLD, ge=0, le=1)
    candidates_enumerated: int = Field(default=0, ge=0)
    candidates_evaluated: int = Field(default=0, ge=0)
    accepted: int = Field(default=0, ge=0)
    rejected_below_inclusion: int = Field(default=0, ge=0)
    stopped_by_confidence: int = Field(default=0, ge=0)
    stopped_by_no_unvisited_neighbor: int = Field(default=0, ge=0)
    semantic_review_issues: int = Field(default=0, ge=0)
    semantic_repaired: int = Field(default=0, ge=0)
    semantic_dropped: int = Field(default=0, ge=0)


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
    supporting_assessment_ids: list[str] = Field(default_factory=list, max_length=30)
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
    chains: list[ChainTrendView] = Field(default_factory=list, max_length=MAX_RUN_CHAINS)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class ReviewResult(FrozenModel):
    accepted: bool
    confidence: Confidence
    issue_codes: list[str] = Field(default_factory=list, max_length=30)
    review_summary: str = Field(min_length=1, max_length=2000)


class ReasoningTraceNode(FrozenModel):
    node_id: str
    node_type: Literal["EVENT", "FACT", "SIGNAL", "LAYER_ASSESSMENT", "TRANSMISSION", "NODE_CONCLUSION"]
    label: str = Field(min_length=1, max_length=1600)
    parent_ids: list[str] = Field(default_factory=list, max_length=50)


class InvestmentAnalysisResult(FrozenModel):
    result_version: Literal["investment-reasoning-result/v5"] = "investment-reasoning-result/v5"
    executor: str
    status: Literal["SUCCEEDED", "NEEDS_REVIEW"]
    context_fingerprint: str
    geopolitical: LayerAnalysisResult
    macro: LayerAnalysisResult
    industry: LayerAnalysisResult
    cross_layer_transmissions: list[AcceptedCrossLayerTransmission] = Field(
        default_factory=list, max_length=MAX_RUN_PROPOSALS
    )
    cross_layer_candidates: list[CandidateCrossLayerMechanism] = Field(
        default_factory=list, max_length=MAX_RUN_PROPOSALS
    )
    transmissions: list[AcceptedTransmission]
    draft: AnalysisDraft
    review: ReviewResult
    reasoning_tree: list[ReasoningTraceNode] = Field(default_factory=list, max_length=5000)
    stage_metrics: dict[str, int | float]
    execution_issues: list[str] = Field(default_factory=list, max_length=100)


class InvestmentConclusionArtifact(InvestmentAnalysisResult):
    """Durable product result emitted by one completed reasoning Workflow run."""

    schema_version: Literal["investment-conclusion-artifact/v1"] = "investment-conclusion-artifact/v1"
    workflow_run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    artifact_path: str = Field(min_length=1, max_length=4096)
    decision_at: datetime
    question: str = Field(min_length=1, max_length=2000)
    event_window_hours: int = Field(ge=1, le=MAX_EVENT_WINDOW_HOURS)
    conclusion_status: Literal["SUPPORTED", "INSUFFICIENT_EVIDENCE"]

    @field_validator("decision_at")
    @classmethod
    def artifact_decision_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("decision_at must be explicit UTC")
        return value


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
    macro_transmission: CrossLayerAnalysisResult = Field(
        default_factory=lambda: CrossLayerAnalysisResult(target_layer=ImpactLayer.MACRO_ECONOMIC)
    )


class IndustryAnalysisState(FrozenModel):
    prepared: PreparedInvestmentContext
    geopolitical: LayerAnalysisResult
    macro: LayerAnalysisResult
    industry: LayerAnalysisResult
    macro_transmission: CrossLayerAnalysisResult = Field(
        default_factory=lambda: CrossLayerAnalysisResult(target_layer=ImpactLayer.MACRO_ECONOMIC)
    )
    industry_transmission: CrossLayerAnalysisResult = Field(
        default_factory=lambda: CrossLayerAnalysisResult(target_layer=ImpactLayer.INDUSTRY)
    )
    industry_context: InvestmentAnalysisContext
    transmissions: list[AcceptedTransmission]
    rounds_executed: int = Field(ge=0, le=5)
    transmission_metrics: TransmissionExecutionMetrics = Field(default_factory=TransmissionExecutionMetrics)
    draft: AnalysisDraft
    execution_issues: list[str] = Field(default_factory=list)


class ReviewedInvestmentState(FrozenModel):
    """Rename-safe handoff from review/audit to deterministic Report generation."""

    analysis: InvestmentConclusionArtifact
    context: InvestmentAnalysisContext


class InvestmentReportWorkflowOutput(FrozenModel):
    """Small final Workflow product, separate from the full reasoning audit."""

    schema_version: Literal["investment-report-workflow-output/v2"] = "investment-report-workflow-output/v2"
    source_report_id: str
    report_artifact_path: str
    audit_artifact_path: str
    generation_status: Literal["GENERATED", "SKIPPED"]
    reason: str | None = None
