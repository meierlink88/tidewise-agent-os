"""Safe components available to the local AgentOS Studio registry."""

from agno.agent import Agent
from agno.registry import Registry

from agents.event_extractor import EVENT_EXTRACTOR_AGENT_ID, load_event_extractor_agent
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
from capabilities.event import EventExtractionBatch, EventExtractionDraft, EventExtractionResult
from capabilities.event.functions import (
    construct_event_signals,
    event_batch_requires_analysis,
    freeze_event_analysis,
    prepare_event_batch,
    publish_event_candidates,
)
from capabilities.evidence import (
    EvidenceAnalysisRequest,
    EvidenceCategoryCatalog,
    EvidenceExtractionDraft,
    PreparedEvidencePublication,
    PreparedRawDocument,
)
from capabilities.evidence.functions import (
    evidence_extraction_complete,
    prepare_evidence_analysis,
    prepare_raw_document,
    publish_evidences,
    validate_evidence_analysis,
)
from capabilities.investment import (
    AcceptedImpactClaim,
    AnalysisDraft,
    GeopoliticalAnalysisState,
    IndustryAnalysisState,
    InvestmentAnalysisContext,
    InvestmentAnalysisResult,
    InvestmentReasoningInput,
    LayerAnalysisContext,
    LayerAnalysisResult,
    LayerImpactBatch,
    MacroAnalysisState,
    PreparedInvestmentContext,
    ReasoningTraceNode,
    ReviewResult,
    TransmissionBatch,
)
from capabilities.investment.functions import (
    analyze_geopolitical_impact,
    analyze_industry_impact,
    analyze_macro_impact,
    prepare_investment_context,
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
            return load_event_extractor_agent(self)
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
        InvestmentReasoningInput,
        InvestmentAnalysisContext,
        PreparedInvestmentContext,
        ReasoningTraceNode,
        LayerAnalysisContext,
        LayerImpactBatch,
        AcceptedImpactClaim,
        LayerAnalysisResult,
        GeopoliticalAnalysisState,
        MacroAnalysisState,
        IndustryAnalysisState,
        TransmissionBatch,
        AnalysisDraft,
        ReviewResult,
        InvestmentAnalysisResult,
    ],
    functions=[
        platform_identity,
        collect_raw_evidence,
        prepare_raw_evidence_filter_batch,
        publish_raw_evidence,
        raw_evidence_filter_complete,
        save_raw_evidence_filter_batch,
        prepare_raw_document,
        evidence_extraction_complete,
        prepare_evidence_analysis,
        validate_evidence_analysis,
        publish_evidences,
        prepare_event_batch,
        event_batch_requires_analysis,
        freeze_event_analysis,
        publish_event_candidates,
        construct_event_signals,
        prepare_investment_context,
        analyze_geopolitical_impact,
        analyze_macro_impact,
        analyze_industry_impact,
        review_and_finalize,
    ],
    agents=[tidewise_assistant],
)
