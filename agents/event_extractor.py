"""Lifecycle helpers for the Studio-managed Event Extractor Agent."""

from pathlib import Path

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry

from app.settings import default_model
from capabilities.event import EventExtractionDraft
from db import get_postgres_db

EVENT_EXTRACTOR_AGENT_ID = "event-extractor"
EVENT_EXTRACTOR_CONTRACT_VERSION = 1
EVENT_EXTRACTOR_DESCRIPTION = "Groups mapped local Evidence into single-real-world-action Event Candidates."
_SEED_PROMPT = Path(__file__).with_name("event_extractor.seed.md")
_RUNTIME_CONTRACT = """Event Extractor runtime contract version 1:
- Consume exactly the supplied frozen EventExtractionBatch.
- Partition every Evidence ID exactly once across candidates, no_event, and needs_review.
- Merge only the same core actor, real-world action, direct object, stage, and compatible occurrence time.
- Treat wording, source, and supplementary detail as non-identity differences.
- Return one atomic Reasoning Server Event Candidate per real-world action.
- Never query history, call tools, publish, or invent a formal Evidence ID.
"""


def _seed_instructions() -> str:
    instructions = _SEED_PROMPT.read_text(encoding="utf-8").strip()
    if not instructions:
        raise ValueError("Event Extractor seed prompt is empty")
    return instructions


def _configure(agent: Agent) -> Agent:
    agent.db = get_postgres_db()
    agent.name = "Event Extractor"
    agent.description = EVENT_EXTRACTOR_DESCRIPTION
    agent.tools = []
    agent.tool_call_limit = None
    agent.instructions = _seed_instructions()
    agent.additional_context = _RUNTIME_CONTRACT
    agent.output_schema = EventExtractionDraft
    agent.parse_response = True
    agent.use_json_mode = True
    agent.retries = 0
    agent.add_datetime_to_context = False
    agent.add_history_to_context = False
    agent.store_tool_messages = False
    agent.markdown = False
    agent.metadata = {
        **dict(agent.metadata or {}),
        "event_extractor_contract_version": EVENT_EXTRACTOR_CONTRACT_VERSION,
    }
    return agent


def build_event_extractor_agent() -> Agent:
    """Return the reviewed initial Agent used for seeding and tests."""
    return _configure(
        Agent(
            id=EVENT_EXTRACTOR_AGENT_ID,
            name="Event Extractor",
            description=EVENT_EXTRACTOR_DESCRIPTION,
            model=default_model(),
            instructions=_seed_instructions(),
        )
    )


def ensure_event_extractor_agent(registry: Registry) -> int:
    """Create the Agent once and migrate contract-bound runtime configuration."""
    db = get_postgres_db()
    component = db.get_component(EVENT_EXTRACTOR_AGENT_ID, component_type=ComponentType.AGENT)
    if component is not None:
        version = component.get("current_version")
        if not isinstance(version, int):
            raise ValueError("Event Extractor has no published Studio version")
        current = Agent.load(EVENT_EXTRACTOR_AGENT_ID, db=db, registry=registry, version=version)
        if current is None:
            raise ValueError("Event Extractor published version could not be rehydrated")
        if dict(current.metadata or {}).get("event_extractor_contract_version") == EVENT_EXTRACTOR_CONTRACT_VERSION:
            return version
        migrated = _configure(current).save(
            db=db,
            stage="published",
            notes=f"Event Extractor runtime contract migration {EVENT_EXTRACTOR_CONTRACT_VERSION}",
        )
        if not isinstance(migrated, int):
            raise ValueError("Event Extractor runtime contract migration failed")
        return migrated
    version = build_event_extractor_agent().save(
        db=db,
        stage="published",
        notes="Initial code-reviewed Event Extractor seed",
    )
    if not isinstance(version, int):
        raise ValueError("Event Extractor seed did not produce a published version")
    return version


def load_event_extractor_agent(registry: Registry) -> Agent:
    """Load the published Agent without independent Workflow session storage."""
    db = get_postgres_db()
    component = db.get_component(EVENT_EXTRACTOR_AGENT_ID, component_type=ComponentType.AGENT)
    if component is None:
        raise ValueError("Event Extractor Studio component is missing")
    version = component.get("current_version")
    if not isinstance(version, int):
        raise ValueError("Event Extractor has no published Studio version")
    agent = Agent.load(EVENT_EXTRACTOR_AGENT_ID, db=db, registry=registry, version=version)
    if agent is None:
        raise ValueError("Event Extractor published version could not be rehydrated")
    agent.db = None
    return agent
