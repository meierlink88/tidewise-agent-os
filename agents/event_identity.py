"""Lifecycle helpers for the Studio-managed Event Identity Agent."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry

from app.settings import default_model
from capabilities.event import EventIdentityDecision
from db import get_postgres_db

EVENT_IDENTITY_AGENT_ID = "event-identity"
EVENT_IDENTITY_CONTRACT_VERSION = 1
EVENT_IDENTITY_SEED_SHA256_KEY = "event_identity_seed_sha256"
EVENT_IDENTITY_DESCRIPTION = (
    "Assesses Event atomicity and resolves identity against a bounded, authoritative Event history."
)
_SEED_PROMPT = Path(__file__).with_name("event_identity.seed.md")
_RUNTIME_CONTRACT = """Event Identity runtime contract version 1:
- Consume exactly one prepared Event Candidate and its bounded authoritative historical Event candidates.
- Decide only NEW_EVENT, SAME_EVENT, RELATED_BUT_DISTINCT, or IGNORED.
- Treat actor, one real-world action, direct object, stage, and occurrence time as the Event identity dimensions.
- Return IGNORED when the Candidate is not atomic or when identity cannot be resolved safely.
- Reference only historical Event IDs supplied in the input; never invent or retrieve an identity.
- Do not call Tools, query history, publish data, project a graph, or perform any write.
- The deterministic Workflow validates the structured decision, matched IDs, journals, idempotency, and side effects.
"""


@dataclass(frozen=True)
class LoadedEventIdentityAgent:
    """Published Studio component resolved for exact Workflow composition."""

    agent: Agent
    version: int
    instructions_sha256: str


def _seed_instructions() -> str:
    instructions = _SEED_PROMPT.read_text(encoding="utf-8").strip()
    if not instructions:
        raise ValueError("Event Identity seed prompt is empty")
    return instructions


def _seed_sha256(instructions: str) -> str:
    return hashlib.sha256(instructions.encode("utf-8")).hexdigest()


def _seed_metadata(instructions: str) -> dict[str, int | str]:
    return {
        "event_identity_contract_version": EVENT_IDENTITY_CONTRACT_VERSION,
        EVENT_IDENTITY_SEED_SHA256_KEY: _seed_sha256(instructions),
    }


def _configure(agent: Agent, instructions: str) -> Agent:
    agent.db = get_postgres_db()
    agent.name = "Event Identity"
    agent.description = EVENT_IDENTITY_DESCRIPTION
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
    agent.output_schema = EventIdentityDecision
    agent.parse_response = True
    agent.use_json_mode = True
    agent.retries = 0
    agent.search_past_sessions = False
    agent.read_chat_history = False
    agent.read_tool_call_history = False
    agent.enable_agentic_memory = False
    agent.update_memory_on_run = False
    agent.add_memories_to_context = False
    agent.enable_session_summaries = False
    agent.add_session_summary_to_context = False
    agent.add_datetime_to_context = False
    agent.add_history_to_context = False
    agent.store_history_messages = False
    agent.store_tool_messages = False
    agent.store_events = False
    agent.markdown = False
    agent.metadata = {
        **dict(agent.metadata or {}),
        **_seed_metadata(instructions),
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
            agent.output_schema is EventIdentityDecision,
            agent.parse_response is True,
            agent.use_json_mode is True,
            agent.retries == 0,
            agent.add_datetime_to_context is False,
            agent.search_past_sessions is False,
            agent.read_chat_history is False,
            agent.read_tool_call_history is False,
            agent.enable_agentic_memory is False,
            agent.update_memory_on_run is False,
            agent.add_memories_to_context is False,
            agent.enable_session_summaries is False,
            agent.add_session_summary_to_context is False,
            agent.add_history_to_context is False,
            agent.store_history_messages is False,
            agent.store_tool_messages is False,
            agent.store_events is False,
            agent.markdown is False,
        )
    )


def build_event_identity_agent() -> Agent:
    """Return the code-reviewed initial Agent used for seeding and tests."""
    instructions = _seed_instructions()
    return _configure(
        Agent(
            id=EVENT_IDENTITY_AGENT_ID,
            name="Event Identity",
            description=EVENT_IDENTITY_DESCRIPTION,
            model=default_model(),
            instructions=instructions,
        ),
        instructions,
    )


def ensure_event_identity_agent(registry: Registry) -> int:
    """Create once; migrate code-owned runtime fields without replacing Studio prompts."""
    db = get_postgres_db()
    component = db.get_component(EVENT_IDENTITY_AGENT_ID, component_type=ComponentType.AGENT)
    if component is not None:
        version = component.get("current_version")
        if not isinstance(version, int):
            raise ValueError("Event Identity has no published Studio version")
        current = Agent.load(EVENT_IDENTITY_AGENT_ID, db=db, registry=registry, version=version)
        if current is None:
            raise ValueError("Event Identity published version could not be rehydrated")
        metadata = dict(current.metadata or {})
        current_contract = metadata.get("event_identity_contract_version") == EVENT_IDENTITY_CONTRACT_VERSION
        if current_contract and _has_runtime_contract(current):
            return version
        instructions = current.instructions
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("Event Identity published instructions are empty")
        migrated = _configure(current, instructions).save(
            db=db,
            stage="published",
            notes=(
                f"Event Identity runtime contract repair {EVENT_IDENTITY_CONTRACT_VERSION}"
                if current_contract
                else f"Event Identity runtime contract migration {EVENT_IDENTITY_CONTRACT_VERSION}"
            ),
        )
        if not isinstance(migrated, int):
            raise ValueError("Event Identity runtime contract migration failed")
        return migrated

    version = build_event_identity_agent().save(
        db=db,
        stage="published",
        notes="Initial code-reviewed Event Identity seed",
    )
    if not isinstance(version, int):
        raise ValueError("Event Identity seed did not produce a published version")
    return version


def load_event_identity_agent(registry: Registry) -> LoadedEventIdentityAgent:
    """Load and identify the exact published Studio Agent used by a Workflow."""
    db = get_postgres_db()
    component = db.get_component(EVENT_IDENTITY_AGENT_ID, component_type=ComponentType.AGENT)
    if component is None:
        raise ValueError("Event Identity Studio component is missing")
    version = component.get("current_version")
    if not isinstance(version, int):
        raise ValueError("Event Identity has no published Studio version")
    agent = Agent.load(EVENT_IDENTITY_AGENT_ID, db=db, registry=registry, version=version)
    if agent is None:
        raise ValueError("Event Identity published version could not be rehydrated")
    if not isinstance(agent.instructions, str) or not agent.instructions.strip():
        raise ValueError("Event Identity published instructions are empty")
    agent.db = None
    return LoadedEventIdentityAgent(
        agent=agent,
        version=version,
        instructions_sha256=_seed_sha256(agent.instructions),
    )
