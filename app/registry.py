"""Safe components available to the local AgentOS Studio registry."""

from agno.registry import Registry

from agents.tidewise_assistant import tidewise_assistant
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
from capabilities.evidence import (
    EvidenceExtractionDraft,
    PreparedEvidencePublication,
    PreparedRawDocument,
)
from capabilities.evidence.functions import (
    prepare_raw_document,
    publish_evidences,
    validate_evidence_draft,
)
from db import get_postgres_db


def platform_identity() -> str:
    """Return the stable product identity exposed to Studio-built components."""
    return "Tidewise AgentOS"


registry = Registry(
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
        EvidenceExtractionDraft,
        PreparedEvidencePublication,
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
        validate_evidence_draft,
        publish_evidences,
    ],
    agents=[tidewise_assistant],
)
