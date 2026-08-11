"""Safe components available to the local AgentOS Studio registry."""

from agno.registry import Registry

from agents.tidewise_assistant import tidewise_assistant
from app.settings import default_model
from capabilities.raw_collection.functions import agentic_collect_step, build_artifact_step, publish_collection_step
from capabilities.raw_collection.models import CollectionRequest, PreparedArtifactSet
from capabilities.raw_collection.tools import COLLECTION_TOOLS
from db import get_postgres_db


def platform_identity() -> str:
    """Return the stable product identity exposed to Studio-built components."""
    return "Tidewise AgentOS"


registry = Registry(
    name="Tidewise AgentOS Registry",
    tools=COLLECTION_TOOLS,
    models=[default_model()],
    dbs=[get_postgres_db()],
    schemas=[CollectionRequest, PreparedArtifactSet],
    functions=[platform_identity, agentic_collect_step, build_artifact_step, publish_collection_step],
    agents=[tidewise_assistant],
)
