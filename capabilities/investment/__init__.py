"""Stable public contracts for investment reasoning."""

from capabilities.investment.internal.models import (
    AcceptedCrossLayerTransmission,
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
    ImpactLayer,
    IndustryAnalysisState,
    IndustryChainSnapshot,
    InvestmentAnalysisContext,
    InvestmentAnalysisRequest,
    InvestmentAnalysisResult,
    InvestmentAssessment,
    InvestmentConclusionArtifact,
    InvestmentReasoningInput,
    InvestmentReportPublicationOutput,
    InvestmentReportWorkflowOutput,
    LayerAnalysisContext,
    LayerAnalysisResult,
    LayerAssessment,
    LayerAssessmentBatch,
    LayerAssessmentProposal,
    MacroAnalysisState,
    NodeAnalysisBatch,
    NodeTrendView,
    PreparedInvestmentContext,
    ReasoningOntologyContext,
    ReasoningTraceNode,
    ReportNarrativeBatch,
    ReportNarrativeRewrite,
    RetrievalReceipt,
    ReviewedInvestmentState,
    ReviewResult,
    TopologyEdgeSnapshot,
    TransmissionBatch,
    TransmissionCandidate,
    TransmissionExecutionMetrics,
    TransmissionProposal,
    TransmissionSemanticIssue,
    TransmissionSemanticReview,
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


def create_data_service_report_publisher(*, base_url: str, service_token: str):
    """Build the private Data Service adapter at the application boundary."""

    from capabilities.investment.internal.report_publication import DataServiceReportPublisher

    return DataServiceReportPublisher(base_url, service_token)


__all__ = [
    "AcceptedCrossLayerTransmission",
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
    "ImpactLayer",
    "IndustryAnalysisState",
    "IndustryChainSnapshot",
    "InvestmentAnalysisContext",
    "InvestmentAnalysisRequest",
    "InvestmentAnalysisResult",
    "InvestmentConclusionArtifact",
    "InvestmentAssessment",
    "InvestmentReasoningInput",
    "InvestmentReportPublicationOutput",
    "InvestmentReportWorkflowOutput",
    "LayerAssessment",
    "LayerAssessmentBatch",
    "LayerAssessmentProposal",
    "LayerAnalysisContext",
    "LayerAnalysisResult",
    "MacroAnalysisState",
    "NodeAnalysisBatch",
    "NodeTrendView",
    "PreparedInvestmentContext",
    "ReportNarrativeBatch",
    "ReportNarrativeRewrite",
    "ReasoningOntologyContext",
    "ReasoningTraceNode",
    "RetrievalReceipt",
    "ReviewResult",
    "ReviewedInvestmentState",
    "TopologyEdgeSnapshot",
    "TransmissionBatch",
    "TransmissionCandidate",
    "TransmissionExecutionMetrics",
    "TransmissionProposal",
    "TransmissionSemanticIssue",
    "TransmissionSemanticReview",
    "Trend",
    "configure_investment_workflow_runtime",
    "create_data_service_report_publisher",
    "create_local_investment_workflow_runtime",
]
