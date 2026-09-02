"""AgentOS-owned immutable investment Report Artifact contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReportResult(ReportModel):
    code: Literal["warming", "cooling", "diverging", "pending"]
    label: Literal["升温", "降温", "分化", "待验证"]


class ReportNature(ReportModel):
    code: Literal["direct_evidence", "reasoning_hypothesis", "pending_validation"]
    label: Literal["直接证据", "推理假设", "待验证"]


class ReportConfidence(ReportModel):
    label: str = Field(min_length=1, max_length=100)
    score: float | None = Field(default=None, ge=0, le=1)


class ReportEvidenceReference(ReportModel):
    evidence_id: str = Field(pattern=r"^EVD[0-9a-f-]{36}$")
    role: str = Field(min_length=1, max_length=200)
    display_order: int = Field(ge=1)


class ReportTargetReference(ReportModel):
    type: Literal["layer", "anchor", "industry_chain", "industry_chain_node"]
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")


class ReportImpactItem(ReportModel):
    ref: ReportTargetReference
    name: str
    result: ReportResult
    confidence: ReportConfidence
    time_window: str


class ReportCard(ReportModel):
    key: str
    kind: Literal["geopolitics", "macroeconomics", "industry_chain"]
    display_order: int
    detail_ref: ReportTargetReference
    title: str
    subtitle: str
    conclusion: str
    result: ReportResult
    confidence: ReportConfidence
    time_window: str
    impact_items: list[ReportImpactItem]
    evidence_refs: list[ReportEvidenceReference]


class ReportStatistics(ReportModel):
    event_count: int = Field(ge=0)
    ordinary_fact_count: int = Field(ge=0)
    signal_fact_count: int = Field(ge=0)
    transmission_hypothesis_count: int = Field(ge=0)
    remaining_topology_pending_count: int = Field(ge=0)
    adaptive_inclusion_threshold: float = Field(ge=0, le=1)
    adaptive_continuation_threshold: float = Field(ge=0, le=1)
    adaptive_hard_max_hops: int = Field(ge=0)
    adaptive_observed_max_hops: int = Field(ge=0)
    adaptive_stopped_by_confidence: int = Field(ge=0)
    adaptive_stopped_by_no_unvisited_neighbor: int = Field(ge=0)
    adaptive_rejected_below_inclusion: int = Field(ge=0)
    geopolitic_anchor_count: int = Field(ge=0)
    macroeconomic_anchor_count: int = Field(ge=0)
    signaled_chain_node_count: int = Field(ge=0)
    industry_chain_count: int = Field(ge=0)
    unmapped_chain_node_count: int = Field(ge=0)


class ReportAnchor(ReportModel):
    key: str
    display_order: int
    name: str
    current_state: str
    result: ReportResult
    nature: ReportNature
    reasoning: str
    time_window: str
    confidence: ReportConfidence
    evidence_refs: list[ReportEvidenceReference]


class ReportReasoningStep(ReportModel):
    key: str
    display_order: int
    input: str
    mechanism: str
    output: str
    type: str
    confidence: ReportConfidence
    evidence_refs: list[ReportEvidenceReference]


class ReportTransmissionTarget(ReportModel):
    ref: ReportTargetReference
    label: str
    result: ReportResult


class ReportTransmissionPath(ReportModel):
    key: str
    display_order: int
    source_conclusion: str
    target_refs: list[ReportTransmissionTarget] = Field(min_length=1)
    logic: str
    relation_nature: str
    evidence_role: str
    confidence: ReportConfidence
    status: str
    evidence_refs: list[ReportEvidenceReference]


class ReportCandidateMechanism(ReportModel):
    key: str
    display_order: int
    mechanism: str
    evidence_gap: str | None
    confidence: ReportConfidence
    evidence_refs: list[ReportEvidenceReference]


class ReportCheckpoint(ReportModel):
    key: str
    display_order: int
    summary: str


class ReportDownwardTransmission(ReportModel):
    summary: str
    published_paths: list[ReportTransmissionPath]
    candidate_mechanisms: list[ReportCandidateMechanism]
    boundary_notes: list[str]


class ReportLayerUncertainty(ReportModel):
    counterevidence: str | None
    evidence_gap: str | None
    boundary: str | None
    reversal_condition: str | None
    checkpoints: list[ReportCheckpoint]


class ReportLayer(ReportModel):
    key: Literal["geopolitics", "macroeconomics"]
    display_order: Literal[1, 2]
    title: str
    conclusion: str
    result: ReportResult
    confidence: ReportConfidence
    time_window: str
    anchors: list[ReportAnchor]
    reasoning_steps: list[ReportReasoningStep]
    related_anchor_keys: list[str]
    related_chain_keys: list[str]
    downward_transmission: ReportDownwardTransmission
    uncertainty: ReportLayerUncertainty
    evidence_refs: list[ReportEvidenceReference]


class ReportIndustryChainNode(ReportModel):
    key: str
    display_order: int
    name: str
    impact: str
    result: ReportResult
    nature: ReportNature
    reasoning: str
    time_window: str
    confidence: ReportConfidence
    evidence_refs: list[ReportEvidenceReference]


class ReportIndustryChainEdge(ReportModel):
    key: str
    display_order: int
    from_node_key: str
    to_node_key: str
    relation_label: str


class ReportChainUncertainty(ReportModel):
    counterevidence_and_gap: str | None
    stop_condition: str | None
    checkpoints: list[ReportCheckpoint]


class ReportIndustryChain(ReportModel):
    key: str
    claim_key: str
    display_order: int
    name: str
    conclusion: str
    status: str
    result: ReportResult
    confidence: ReportConfidence
    time_window: str
    path_summary: str | None
    accepted_hypothesis_summary: str | None
    evidence_refs: list[ReportEvidenceReference]
    nodes: list[ReportIndustryChainNode]
    edges: list[ReportIndustryChainEdge]
    uncertainty: ReportChainUncertainty


class ReportCompanyBoundary(ReportModel):
    key: Literal["company"] = "company"
    display_order: Literal[4] = 4
    title: str = "公司层面"
    included: Literal[False] = False
    boundary: str = "公司层面尚未纳入本期推理与报告范围。"


class ReportContent(ReportModel):
    report_type: Literal["investment_reasoning"] = "investment_reasoning"
    title: str
    status: Literal["generated"] = "generated"
    simulation: bool = False
    generated_at: datetime
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    included_layers: list[Literal["geopolitics", "macroeconomics", "industry_chain"]]
    statistics: ReportStatistics
    report_cards: list[ReportCard]
    geopolitics: ReportLayer
    macroeconomics: ReportLayer
    industry_chains: list[ReportIndustryChain]
    company: ReportCompanyBoundary


class InvestmentReportArtifact(ReportModel):
    schema_version: Literal["investment-report-artifact/v1"] = "investment-report-artifact/v1"
    source_report_id: str = Field(min_length=1, max_length=200)
    content: ReportContent
