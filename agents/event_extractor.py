"""Lifecycle helpers for the Studio-managed Event Extractor Agent."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry

from app.settings import default_model
from capabilities.event import EVENT_EXTRACTOR_AGENT_ID, EventExtractionDraft
from db import get_postgres_db

EVENT_EXTRACTOR_CONTRACT_VERSION = 5
EVENT_EXTRACTOR_DESCRIPTION = "Groups mapped local Evidence into single-real-world-action Event Candidates."
_SEED_PROMPT = Path(__file__).with_name("event_extractor.seed.md")
_RUNTIME_CONTRACT = """Event Extractor runtime contract version 5:
- Consume exactly the supplied frozen EventExtractionBatch.
- Partition every Evidence ID exactly once across candidates and no_event.
- Merge only the same core actor, real-world action, direct object, stage, and compatible occurrence time.
- Treat wording, source, and supplementary detail as non-identity differences.
- Return one atomic Event Candidate per real-world action.
- Return Event business fields only as title, summary, and semantic. Semantic owns modality and time.
- Preserve explicit compatible Evidence reason and method by selecting supported source text verbatim, and preserve
  every supporting EvidenceMetric; return null on semantic conflict and never copy attribution.
- Evidence without an explicit occurrence, announcement, or effective time must be returned as no_event
  with reason missing_reliable_time; never emit a timeless Candidate.
- Never query history, call tools, publish, or invent a formal Evidence ID.
"""


@dataclass(frozen=True)
class LoadedEventExtractorAgent:
    """Published Studio component resolved for exact Workflow composition."""

    agent: Agent
    version: int
    instructions_sha256: str


def _seed_instructions() -> str:
    instructions = _SEED_PROMPT.read_text(encoding="utf-8").strip()
    if not instructions:
        raise ValueError("Event Extractor seed prompt is empty")
    return instructions


def _configure(agent: Agent, instructions: str) -> Agent:
    agent.db = get_postgres_db()
    agent.name = "Event Extractor"
    agent.description = EVENT_EXTRACTOR_DESCRIPTION
    agent.tools = []
    agent.tool_call_limit = None
    agent.tool_choice = None
    agent.tool_hooks = None
    agent.knowledge = None
    agent.knowledge_retriever = None
    agent.add_knowledge_to_context = False
    agent.search_knowledge = False
    agent.update_knowledge = False
    agent.memory_manager = None
    agent.instructions = instructions
    agent.additional_context = _RUNTIME_CONTRACT
    agent.output_schema = EventExtractionDraft
    agent.parse_response = True
    agent.use_json_mode = True
    agent.retries = 0
    agent.add_datetime_to_context = False
    agent.add_history_to_context = False
    agent.read_chat_history = False
    agent.read_tool_call_history = False
    agent.search_past_sessions = False
    agent.enable_agentic_memory = False
    agent.update_memory_on_run = False
    agent.add_memories_to_context = False
    agent.enable_session_summaries = False
    agent.add_session_summary_to_context = False
    agent.store_history_messages = False
    agent.store_tool_messages = False
    agent.store_events = False
    agent.markdown = False
    agent.metadata = {
        **dict(agent.metadata or {}),
        "event_extractor_contract_version": EVENT_EXTRACTOR_CONTRACT_VERSION,
    }
    return agent


def _has_runtime_contract(agent: Agent) -> bool:
    """Reject Studio drift outside the prompt/model fields this Agent may own."""

    return all(
        (
            not agent.tools,
            agent.tool_call_limit is None,
            agent.tool_choice is None,
            agent.tool_hooks is None,
            agent.knowledge is None,
            agent.knowledge_retriever is None,
            agent.add_knowledge_to_context is False,
            agent.search_knowledge is False,
            agent.update_knowledge is False,
            agent.memory_manager is None,
            agent.additional_context == _RUNTIME_CONTRACT,
            agent.output_schema is EventExtractionDraft,
            agent.parse_response is True,
            agent.use_json_mode is True,
            agent.retries == 0,
            agent.add_datetime_to_context is False,
            agent.add_history_to_context is False,
            agent.read_chat_history is False,
            agent.read_tool_call_history is False,
            agent.search_past_sessions is False,
            agent.enable_agentic_memory is False,
            agent.update_memory_on_run is False,
            agent.add_memories_to_context is False,
            agent.enable_session_summaries is False,
            agent.add_session_summary_to_context is False,
            agent.store_history_messages is False,
            agent.store_tool_messages is False,
            agent.store_events is False,
            agent.markdown is False,
        )
    )


def build_event_extractor_agent() -> Agent:
    """Return the reviewed initial Agent used for seeding and tests."""
    instructions = _seed_instructions()
    return _configure(
        Agent(
            id=EVENT_EXTRACTOR_AGENT_ID,
            name="Event Extractor",
            description=EVENT_EXTRACTOR_DESCRIPTION,
            model=default_model(),
            instructions=instructions,
        ),
        instructions,
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
        current_contract = (
            dict(current.metadata or {}).get("event_extractor_contract_version") == EVENT_EXTRACTOR_CONTRACT_VERSION
        )
        if current_contract and _has_runtime_contract(current):
            return version
        instructions = current.instructions
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("Event Extractor published instructions are empty")
        migrated = _configure(current, instructions).save(
            db=db,
            stage="published",
            notes=(
                f"Event Extractor runtime contract repair {EVENT_EXTRACTOR_CONTRACT_VERSION}"
                if current_contract
                else f"Event Extractor runtime contract migration {EVENT_EXTRACTOR_CONTRACT_VERSION}"
            ),
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


def load_event_extractor_agent(registry: Registry) -> LoadedEventExtractorAgent:
    """Load and identify the exact published Studio Agent used by a Workflow."""
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
    if not isinstance(agent.instructions, str) or not agent.instructions.strip():
        raise ValueError("Event Extractor published instructions are empty")
    agent.db = None
    return LoadedEventExtractorAgent(
        agent=agent,
        version=version,
        instructions_sha256=hashlib.sha256(agent.instructions.encode("utf-8")).hexdigest(),
    )
