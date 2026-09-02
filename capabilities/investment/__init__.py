"""Stable public contracts for investment reasoning."""

from capabilities.investment.internal.models import (
    AcceptedCrossLayerTransmission,
    AcceptedImpactClaim,
    AcceptedTransmission,
    AnalysisAnchorSnapshot,
    AnalysisDraft,
    CandidateCrossLayerMechanism,
    ChainNodeSnapshot,
    ChainTrendView,
    Confidence,
    CrossLayerAnalysisResult,
    CrossLayerTransmissionBatch,
    CrossLayerTransmissionProposal,
    Direction,
    EventSnapshot,
    FactSnapshot,
    GeopoliticalAnalysisState,
    Horizon,
    ImpactClaimProposal,
    ImpactLayer,
    IndustryAnalysisState,
    IndustryChainSnapshot,
    InvestmentAnalysisContext,
    InvestmentAnalysisRequest,
    InvestmentAnalysisResult,
    InvestmentAssessment,
    InvestmentConclusionArtifact,
    InvestmentReasoningInput,
    InvestmentReportWorkflowOutput,
    LayerAnalysisContext,
    LayerAnalysisResult,
    LayerImpactBatch,
    MacroAnalysisState,
    NodeAnalysisBatch,
    NodeTrendView,
    PreparedInvestmentContext,
    ReasoningTraceNode,
    ReviewedInvestmentState,
    ReviewResult,
    TopologyEdgeSnapshot,
    TransmissionBatch,
    TransmissionProposal,
    Trend,
)


def create_local_investment_workflow_runtime(*args, **kwargs):
    """Compose the private runtime lazily so Graphiti can import stable contracts."""

    from capabilities.investment.internal.local_runtime import create_local_investment_workflow_runtime as create

    return create(*args, **kwargs)


def configure_investment_workflow_runtime(runtime) -> None:
    """Install the app-owned runtime without exposing internal modules to composition code."""

    from capabilities.investment.internal.runtime import configure_investment_workflow_runtime as configure

    configure(runtime)


__all__ = [
    "AcceptedCrossLayerTransmission",
    "AcceptedImpactClaim",
    "AcceptedTransmission",
    "AnalysisAnchorSnapshot",
    "AnalysisDraft",
    "CandidateCrossLayerMechanism",
    "ChainNodeSnapshot",
    "ChainTrendView",
    "Confidence",
    "CrossLayerAnalysisResult",
    "CrossLayerTransmissionBatch",
    "CrossLayerTransmissionProposal",
    "Direction",
    "EventSnapshot",
    "FactSnapshot",
    "GeopoliticalAnalysisState",
    "Horizon",
    "ImpactClaimProposal",
    "ImpactLayer",
    "IndustryAnalysisState",
    "IndustryChainSnapshot",
    "InvestmentAnalysisContext",
    "InvestmentAnalysisRequest",
    "InvestmentAnalysisResult",
    "InvestmentConclusionArtifact",
    "InvestmentAssessment",
    "InvestmentReasoningInput",
    "InvestmentReportWorkflowOutput",
    "LayerAnalysisContext",
    "LayerAnalysisResult",
    "LayerImpactBatch",
    "MacroAnalysisState",
    "NodeAnalysisBatch",
    "NodeTrendView",
    "PreparedInvestmentContext",
    "ReasoningTraceNode",
    "ReviewResult",
    "ReviewedInvestmentState",
    "TopologyEdgeSnapshot",
    "TransmissionBatch",
    "TransmissionProposal",
    "Trend",
    "configure_investment_workflow_runtime",
    "create_local_investment_workflow_runtime",
]
