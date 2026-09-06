"""Safe components available to the local AgentOS Studio registry."""

from agno.agent import Agent
from agno.models.base import Model
from agno.registry import Registry

from agents.event_association import EVENT_ASSOCIATION_AGENT_ID, load_event_association_agent
from agents.event_extractor import EVENT_EXTRACTOR_AGENT_ID, load_event_extractor_agent
from agents.event_identity import EVENT_IDENTITY_AGENT_ID, load_event_identity_agent
from agents.event_signal_analyst import EVENT_SIGNAL_ANALYST_AGENT_ID, load_event_signal_analyst_agent
from agents.evidence_extractor import EVIDENCE_EXTRACTOR_AGENT_ID, load_evidence_extractor_agent
from agents.investment_reasoner import INVESTMENT_REASONER_AGENT_ID, load_investment_reasoner_agent
from agents.investment_report_writer import INVESTMENT_REPORT_WRITER_AGENT_ID, load_investment_report_writer_agent
from agents.investment_reviewer import INVESTMENT_REVIEWER_AGENT_ID, load_investment_reviewer_agent
from agents.tidewise_assistant import tidewise_assistant
from agents.title_curator import TITLE_CURATOR_AGENT_ID, load_title_curator_agent
from app.settings import SOL_LOW_MODEL_ID, default_model, sol_low_model
from app.workflow_runtime import install_raw_collection_session_compatibility
from capabilities.collection import (
    CollectionRequest,
    PreparedArtifactSet,
    RawEvidenceFilterProgress,
    TitleCurationDraft,
    TitleCurationRequest,
)
from capabilities.collection.functions import (
    article_has_evidence,
    article_needs_review,
    article_processing_complete,
    collect_articles,
    collect_raw_evidence,
    evidence_collect,
    evidence_publish,
    prepare_evidence_review,
    prepare_next_article,
    prepare_raw_evidence_filter_batch,
    publish_raw_evidence,
    publish_reviewed_article,
    raw_evidence_filter_complete,
    save_article_review,
    save_raw_evidence_filter_batch,
    validate_article_review,
)
from capabilities.event import (
    AssociationDecision,
    BatchAssociationDecision,
    BatchIdentityDecision,
    BatchSignalDecision,
    ClassifiedEventDraft,
    EventExtractionBatch,
    EventExtractionDraft,
    EventExtractionResult,
    EventIdentityDecision,
    EventIdentityRequest,
    EventSignalAnalysisDraft,
    EventSignalAnalysisRequest,
    EventSignalClassificationRequest,
    IdentityClassificationDecision,
    SignalDecision,
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
from capabilities.event.functions.batch import BATCH_FUNCTIONS
from capabilities.event.functions.linear import LINEAR_EVENT_FUNCTIONS
from capabilities.event.functions.storyline import STORYLINE_FUNCTIONS
from capabilities.evidence import (
    ArticleReviewDraft,
    ArticleReviewRequest,
    EvidenceAnalysisRequest,
    EvidenceCategoryCatalog,
    EvidenceExtractionDraft,
    EvidenceReviewDraft,
    EvidenceReviewRequest,
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
    InvestmentReportPublicationOutput,
    InvestmentReportWorkflowOutput,
    LayerAnalysisContext,
    LayerAnalysisResult,
    LayerAssessment,
    LayerAssessmentBatch,
    MacroAnalysisState,
    PreparedInvestmentContext,
    ReasoningTraceNode,
    ReportNarrativeBatch,
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

    def get_model(self, model_id: str, provider: str | None = None, name: str | None = None) -> Model | None:
        # Historical Reviewer variants stay loadable without duplicate Studio options.
        if model_id == SOL_LOW_MODEL_ID and provider in (None, "OpenAI"):
            if name in ("RawEvidenceFilter-low", "RawEvidenceFilter-none"):
                name = "OpenAIResponses"
        return super().get_model(model_id, provider=provider, name=name)

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
        if agent_id == EVENT_ASSOCIATION_AGENT_ID:
            return load_event_association_agent(self).agent
        if agent_id == EVENT_SIGNAL_ANALYST_AGENT_ID:
            return load_event_signal_analyst_agent(self).agent
        if agent_id == INVESTMENT_REASONER_AGENT_ID:
            return load_investment_reasoner_agent(self)
        if agent_id == INVESTMENT_REPORT_WRITER_AGENT_ID:
            return load_investment_report_writer_agent(self)
        if agent_id == INVESTMENT_REVIEWER_AGENT_ID:
            return load_investment_reviewer_agent(self)
        return None


install_raw_collection_session_compatibility()

registry = TidewiseRegistry(
    name="Tidewise AgentOS Registry",
    models=[default_model(), sol_low_model()],
    dbs=[get_postgres_db()],
    schemas=[
        BatchAssociationDecision,
        BatchIdentityDecision,
        BatchSignalDecision,
        ClassifiedEventDraft,
        AssociationDecision,
        IdentityClassificationDecision,
        SignalDecision,
        ArticleReviewDraft,
        ArticleReviewRequest,
        EvidenceReviewDraft,
        EvidenceReviewRequest,
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
        ReportNarrativeBatch,
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
        InvestmentReportPublicationOutput,
        InvestmentReportWorkflowOutput,
    ],
    functions=[
        *BATCH_FUNCTIONS,
        *STORYLINE_FUNCTIONS,
        *LINEAR_EVENT_FUNCTIONS,
        article_has_evidence,
        article_needs_review,
        article_processing_complete,
        collect_articles,
        evidence_publish,
        evidence_collect,
        prepare_evidence_review,
        prepare_next_article,
        publish_reviewed_article,
        save_article_review,
        validate_article_review,
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
