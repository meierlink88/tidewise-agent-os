"""Safe components available to the local AgentOS Studio registry."""

from agno.agent import Agent
from agno.registry import Registry

from agents.event_extractor import EVENT_EXTRACTOR_AGENT_ID, load_event_extractor_agent
from agents.event_identity import EVENT_IDENTITY_AGENT_ID, load_event_identity_agent
from agents.event_signal_analyst import EVENT_SIGNAL_ANALYST_AGENT_ID, load_event_signal_analyst_agent
from agents.evidence_extractor import EVIDENCE_EXTRACTOR_AGENT_ID, load_evidence_extractor_agent
from agents.investment_reasoner import INVESTMENT_REASONER_AGENT_ID, load_investment_reasoner_agent
from agents.investment_reviewer import INVESTMENT_REVIEWER_AGENT_ID, load_investment_reviewer_agent
from agents.tidewise_assistant import tidewise_assistant
from agents.title_curator import TITLE_CURATOR_AGENT_ID, load_title_curator_agent
from app.settings import default_model
from capabilities.collection import (
    CollectionRequest,
    PreparedArtifactSet,
    RawEvidenceFilterProgress,
    TitleCurationDraft,
    TitleCurationRequest,
)
from capabilities.collection.functions import (
    collect_raw_evidence,
    prepare_raw_evidence_filter_batch,
    publish_raw_evidence,
    raw_evidence_filter_complete,
    save_raw_evidence_filter_batch,
)
from capabilities.event import (
    EventExtractionBatch,
    EventExtractionDraft,
    EventExtractionResult,
    EventIdentityDecision,
    EventIdentityRequest,
    EventSignalAnalysisDraft,
    EventSignalAnalysisRequest,
    EventSignalClassificationRequest,
)
from capabilities.event.functions import (
    analyze_signals,
    event_extraction_complete,
    event_extraction_required,
    event_resolution_complete,
    extract_events,
    freeze_event_extraction,
    has_pending_event_resolution,
    has_pending_signal_analysis,
    persist_event_resolution,
    persist_signal_task,
    prepare_event_extraction,
    prepare_event_resolution,
    prepare_signal_task,
    publish_events,
    publish_signals,
    resolve_events,
    signal_analysis_complete,
)
from capabilities.evidence import (
    EvidenceAnalysisRequest,
    EvidenceCategoryCatalog,
    EvidenceExtractionDraft,
    PreparedEvidencePublication,
    PreparedRawDocument,
)
from capabilities.evidence.functions import (
    curate_evidence,
    evidence_extraction_complete,
    prepare_evidence,
    publish_evidence,
)
from capabilities.investment import (
    AcceptedCrossLayerTransmission,
    AnalysisDraft,
    CandidateCrossLayerMechanism,
    CrossLayerAnalysisResult,
    CrossLayerTransmissionBatch,
    CrossLayerTransmissionProposal,
    GeopoliticalAnalysisState,
    IndustryAnalysisState,
    InvestmentAnalysisContext,
    InvestmentAnalysisResult,
    InvestmentReasoningInput,
    InvestmentReportWorkflowOutput,
    LayerAnalysisContext,
    LayerAnalysisResult,
    LayerAssessment,
    LayerAssessmentBatch,
    MacroAnalysisState,
    PreparedInvestmentContext,
    ReasoningTraceNode,
    ReviewedInvestmentState,
    ReviewResult,
    TransmissionBatch,
)
from capabilities.investment.functions import (
    analyze_geopolitical_impact,
    analyze_industry_impact,
    analyze_macro_impact,
    generate_investment_report,
    prepare_investment_context,
    publish_investment_report,
    review_and_finalize,
)
from db import get_postgres_db


def platform_identity() -> str:
    """Return the stable product identity exposed to Studio-built components."""
    return "Tidewise AgentOS"


class TidewiseRegistry(Registry):
    """Resolve Studio Agents as sessionless runtime copies when composing Workflows."""

    def get_agent(self, agent_id: str) -> Agent | None:
        code_defined = super().get_agent(agent_id)
        if code_defined is not None:
            return code_defined
        if agent_id == TITLE_CURATOR_AGENT_ID:
            return load_title_curator_agent(self).agent
        if agent_id == EVIDENCE_EXTRACTOR_AGENT_ID:
            return load_evidence_extractor_agent(self)
        if agent_id == EVENT_EXTRACTOR_AGENT_ID:
            return load_event_extractor_agent(self).agent
        if agent_id == EVENT_IDENTITY_AGENT_ID:
            return load_event_identity_agent(self).agent
        if agent_id == EVENT_SIGNAL_ANALYST_AGENT_ID:
            return load_event_signal_analyst_agent(self).agent
        if agent_id == INVESTMENT_REASONER_AGENT_ID:
            return load_investment_reasoner_agent(self)
        if agent_id == INVESTMENT_REVIEWER_AGENT_ID:
            return load_investment_reviewer_agent(self)
        return None


registry = TidewiseRegistry(
    name="Tidewise AgentOS Registry",
    models=[default_model()],
    dbs=[get_postgres_db()],
    schemas=[
        CollectionRequest,
        TitleCurationRequest,
        TitleCurationDraft,
        PreparedArtifactSet,
        RawEvidenceFilterProgress,
        PreparedRawDocument,
        EvidenceCategoryCatalog,
        EvidenceAnalysisRequest,
        EvidenceExtractionDraft,
        PreparedEvidencePublication,
        EventExtractionBatch,
        EventExtractionDraft,
        EventExtractionResult,
        EventIdentityRequest,
        EventIdentityDecision,
        EventSignalClassificationRequest,
        EventSignalAnalysisRequest,
        EventSignalAnalysisDraft,
        InvestmentReasoningInput,
        InvestmentAnalysisContext,
        PreparedInvestmentContext,
        ReasoningTraceNode,
        LayerAnalysisContext,
        LayerAssessmentBatch,
        LayerAssessment,
        CrossLayerTransmissionProposal,
        CrossLayerTransmissionBatch,
        AcceptedCrossLayerTransmission,
        CandidateCrossLayerMechanism,
        CrossLayerAnalysisResult,
        LayerAnalysisResult,
        GeopoliticalAnalysisState,
        MacroAnalysisState,
        IndustryAnalysisState,
        TransmissionBatch,
        AnalysisDraft,
        ReviewResult,
        InvestmentAnalysisResult,
        ReviewedInvestmentState,
        InvestmentReportWorkflowOutput,
    ],
    functions=[
        platform_identity,
        collect_raw_evidence,
        prepare_raw_evidence_filter_batch,
        publish_raw_evidence,
        raw_evidence_filter_complete,
        save_raw_evidence_filter_batch,
        prepare_evidence,
        evidence_extraction_complete,
        curate_evidence,
        publish_evidence,
        extract_events,
        resolve_events,
        analyze_signals,
        event_extraction_complete,
        # Preserve these registrations so previously published Event Workflow
        # versions remain strict-rehydratable after the five-Step flattening.
        prepare_event_extraction,
        event_extraction_required,
        freeze_event_extraction,
        has_pending_event_resolution,
        prepare_event_resolution,
        persist_event_resolution,
        event_resolution_complete,
        publish_events,
        has_pending_signal_analysis,
        prepare_signal_task,
        persist_signal_task,
        signal_analysis_complete,
        publish_signals,
        prepare_investment_context,
        analyze_geopolitical_impact,
        analyze_macro_impact,
        analyze_industry_impact,
        review_and_finalize,
        generate_investment_report,
        publish_investment_report,
    ],
    agents=[tidewise_assistant],
)
