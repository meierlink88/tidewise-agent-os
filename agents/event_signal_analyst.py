"""Lifecycle helpers for the Studio-managed Event Signal Analyst Agent."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry

from app.settings import default_model
from capabilities.event import EVENT_SIGNAL_ANALYST_AGENT_ID, EventSignalAnalysisDraft
from db import get_postgres_db

EVENT_SIGNAL_ANALYST_CONTRACT_VERSION = 2
EVENT_SIGNAL_ANALYST_SEED_SHA256_KEY = "event_signal_analyst_seed_sha256"
EVENT_SIGNAL_ANALYST_DESCRIPTION = (
    "Classifies one published Event and proposes bounded direct Signals against supplied graph identities."
)
_SEED_PROMPT = Path(__file__).with_name("event_signal_analyst.seed.md")
_RUNTIME_CONTRACT = """Event Signal Analyst runtime contract version 2:
- Consume exactly one successfully projected new Event and its bounded, deterministically retrieved candidates.
- For task CLASSIFY, return the Event classification and no Signal proposals.
- For task PROPOSE_SIGNALS, preserve the supplied frozen classification and propose only from supplied candidates.
- Always return the Event classification, including when no supported Signal exists.
- Use Event reason, method, and metrics only as direct supporting business semantics;
  Event attribution is unavailable by design.
- Propose only direct Signals between supplied existing Anchor and Variable UUIDs.
- Never create or alter an Anchor, Variable, Company, security, or other graph identity.
- Do not perform topology propagation or produce investment, valuation, or trading conclusions.
- Do not call Tools, query a graph, publish data, add Graphiti triplets, or perform any write.
- The deterministic Workflow validates UUID membership, Anchor types, directions, duplicate pairs, journals,
  idempotency, and all Graphiti-native side effects.
"""


@dataclass(frozen=True)
class LoadedEventSignalAnalystAgent:
    """Published Studio component resolved for exact Workflow composition."""

    agent: Agent
    version: int
    instructions_sha256: str


def _seed_instructions() -> str:
    instructions = _SEED_PROMPT.read_text(encoding="utf-8").strip()
    if not instructions:
        raise ValueError("Event Signal Analyst seed prompt is empty")
    return instructions


def _seed_sha256(instructions: str) -> str:
    return hashlib.sha256(instructions.encode("utf-8")).hexdigest()


def _seed_metadata(instructions: str) -> dict[str, int | str]:
    return {
        "event_signal_analyst_contract_version": EVENT_SIGNAL_ANALYST_CONTRACT_VERSION,
        EVENT_SIGNAL_ANALYST_SEED_SHA256_KEY: _seed_sha256(instructions),
    }


def _configure(agent: Agent, instructions: str) -> Agent:
    agent.db = get_postgres_db()
    agent.name = "Event Signal Analyst"
    agent.description = EVENT_SIGNAL_ANALYST_DESCRIPTION
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
    agent.output_schema = EventSignalAnalysisDraft
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
            agent.output_schema is EventSignalAnalysisDraft,
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


def build_event_signal_analyst_agent() -> Agent:
    """Return the code-reviewed initial Agent used for seeding and tests."""
    instructions = _seed_instructions()
    return _configure(
        Agent(
            id=EVENT_SIGNAL_ANALYST_AGENT_ID,
            name="Event Signal Analyst",
            description=EVENT_SIGNAL_ANALYST_DESCRIPTION,
            model=default_model(),
            instructions=instructions,
        ),
        instructions,
    )


def ensure_event_signal_analyst_agent(registry: Registry) -> int:
    """Create once; migrate code-owned runtime fields without replacing Studio prompts."""
    db = get_postgres_db()
    component = db.get_component(EVENT_SIGNAL_ANALYST_AGENT_ID, component_type=ComponentType.AGENT)
    if component is not None:
        version = component.get("current_version")
        if not isinstance(version, int):
            raise ValueError("Event Signal Analyst has no published Studio version")
        current = Agent.load(EVENT_SIGNAL_ANALYST_AGENT_ID, db=db, registry=registry, version=version)
        if current is None:
            raise ValueError("Event Signal Analyst published version could not be rehydrated")
        metadata = dict(current.metadata or {})
        current_contract = (
            metadata.get("event_signal_analyst_contract_version") == EVENT_SIGNAL_ANALYST_CONTRACT_VERSION
        )
        if current_contract and _has_runtime_contract(current):
            return version
        instructions = current.instructions
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("Event Signal Analyst published instructions are empty")
        migrated = _configure(current, instructions).save(
            db=db,
            stage="published",
            notes=(
                f"Event Signal Analyst runtime contract repair {EVENT_SIGNAL_ANALYST_CONTRACT_VERSION}"
                if current_contract
                else f"Event Signal Analyst runtime contract migration {EVENT_SIGNAL_ANALYST_CONTRACT_VERSION}"
            ),
        )
        if not isinstance(migrated, int):
            raise ValueError("Event Signal Analyst runtime contract migration failed")
        return migrated

    version = build_event_signal_analyst_agent().save(
        db=db,
        stage="published",
        notes="Initial code-reviewed Event Signal Analyst seed",
    )
    if not isinstance(version, int):
        raise ValueError("Event Signal Analyst seed did not produce a published version")
    return version


def load_event_signal_analyst_agent(registry: Registry) -> LoadedEventSignalAnalystAgent:
    """Load and identify the exact published Studio Agent used by a Workflow."""
    db = get_postgres_db()
    component = db.get_component(EVENT_SIGNAL_ANALYST_AGENT_ID, component_type=ComponentType.AGENT)
    if component is None:
        raise ValueError("Event Signal Analyst Studio component is missing")
    version = component.get("current_version")
    if not isinstance(version, int):
        raise ValueError("Event Signal Analyst has no published Studio version")
    agent = Agent.load(EVENT_SIGNAL_ANALYST_AGENT_ID, db=db, registry=registry, version=version)
    if agent is None:
        raise ValueError("Event Signal Analyst published version could not be rehydrated")
    if not isinstance(agent.instructions, str) or not agent.instructions.strip():
        raise ValueError("Event Signal Analyst published instructions are empty")
    agent.db = None
    return LoadedEventSignalAnalystAgent(
        agent=agent,
        version=version,
        instructions_sha256=_seed_sha256(agent.instructions),
    )
