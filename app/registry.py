"""Safe components available to the local AgentOS Studio registry."""

from agno.agent import Agent
from agno.registry import Registry

from agents.event_extractor import EVENT_EXTRACTOR_AGENT_ID, load_event_extractor_agent
from agents.evidence_extractor import EVIDENCE_EXTRACTOR_AGENT_ID, load_evidence_extractor_agent
from agents.raw_collector import COLLECTOR_AGENT_ID, load_collector_agent
from agents.tidewise_assistant import tidewise_assistant
from agents.title_curator import TITLE_CURATOR_AGENT_ID, load_title_curator_agent
from app.settings import default_model
from capabilities.collection import (
    CollectionQueryPlan,
    CollectionRequest,
    PreparedArtifactSet,
    TitleCurationDraft,
    TitleCurationRequest,
)
from capabilities.collection.functions import (
    build_artifact_step,
    execute_collection_channels_step,
    prepare_collection_context,
    prepare_title_curation,
    publish_collection_step,
    validate_title_curation,
)
from capabilities.collection.tools import COLLECTION_TOOLS
from capabilities.event import EventExtractionBatch, EventExtractionDraft, EventExtractionResult
from capabilities.event.functions import (
    event_batch_requires_analysis,
    freeze_event_analysis,
    prepare_event_batch,
    submit_event_candidates,
)
from capabilities.evidence import (
    EvidenceAnalysisRequest,
    EvidenceCategoryCatalog,
    EvidenceExtractionDraft,
    PreparedEvidencePublication,
    PreparedRawDocument,
)
from capabilities.evidence.functions import (
    prepare_evidence_analysis,
    prepare_raw_document,
    publish_evidences,
    validate_evidence_analysis,
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
        if agent_id == COLLECTOR_AGENT_ID:
            return load_collector_agent(self).agent
        if agent_id == TITLE_CURATOR_AGENT_ID:
            return load_title_curator_agent(self).agent
        if agent_id == EVIDENCE_EXTRACTOR_AGENT_ID:
            return load_evidence_extractor_agent(self)
        if agent_id == EVENT_EXTRACTOR_AGENT_ID:
            return load_event_extractor_agent(self)
        return None


registry = TidewiseRegistry(
    name="Tidewise AgentOS Registry",
    tools=COLLECTION_TOOLS,
    models=[default_model()],
    dbs=[get_postgres_db()],
    schemas=[
        CollectionRequest,
        CollectionQueryPlan,
        TitleCurationRequest,
        TitleCurationDraft,
        PreparedArtifactSet,
        PreparedRawDocument,
        EvidenceCategoryCatalog,
        EvidenceAnalysisRequest,
        EvidenceExtractionDraft,
        PreparedEvidencePublication,
        EventExtractionBatch,
        EventExtractionDraft,
        EventExtractionResult,
    ],
    functions=[
        platform_identity,
        prepare_collection_context,
        execute_collection_channels_step,
        prepare_title_curation,
        validate_title_curation,
        build_artifact_step,
        publish_collection_step,
        prepare_raw_document,
        prepare_evidence_analysis,
        validate_evidence_analysis,
        publish_evidences,
        prepare_event_batch,
        event_batch_requires_analysis,
        freeze_event_analysis,
        submit_event_candidates,
    ],
    agents=[tidewise_assistant],
)
