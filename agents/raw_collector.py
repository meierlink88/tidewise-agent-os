"""Lifecycle helpers for the Agno Studio-managed Raw Collector Agent."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry

from app.settings import default_model
from capabilities.collection import CollectionQueryPlan
from db import get_postgres_db

COLLECTOR_AGENT_ID = "raw-collector"
COLLECTOR_CONTRACT_VERSION = 9
COLLECTOR_AGENT_NAME = "Collection Query Planner"
_SEED_PROMPT = Path(__file__).with_name("raw_collector.seed.md")
_RUNTIME_CONTRACT = """Raw Collection runtime contract version 9:
- You are a semantic query planner and must not call acquisition Tools.
- Return exactly one CollectionQueryPlan with a focused Chinese query and integer lookback_hours.
- Infer lookback_hours from the user's relative temporal requirement; default to 48 when no duration is stated.
- Do not calculate absolute published_after or published_before timestamps.
- The deterministic Workflow owns channel snapshots, all three acquisition façades, failure handling and publication.
"""


@dataclass(frozen=True)
class LoadedCollectorAgent:
    """Published Studio component resolved for one Workflow run."""

    agent: Agent
    version: int
    instructions_sha256: str


def _seed_instructions() -> str:
    instructions = _SEED_PROMPT.read_text(encoding="utf-8").strip()
    if not instructions:
        raise ValueError("Collector seed prompt is empty")
    return instructions


def ensure_collector_agent(registry: Registry) -> int:
    """Create published version 1 once; never overwrite Studio-managed versions."""
    db = get_postgres_db()
    component = db.get_component(COLLECTOR_AGENT_ID, component_type=ComponentType.AGENT)
    if component is not None:
        version = component.get("current_version")
        if not isinstance(version, int):
            raise ValueError("Raw Collector has no published Studio version")
        current = Agent.load(COLLECTOR_AGENT_ID, db=db, registry=registry, version=version)
        if current is None:
            raise ValueError("Raw Collector published version could not be rehydrated")
        metadata = dict(current.metadata or {})
        if metadata.get("collector_contract_version") == COLLECTOR_CONTRACT_VERSION:
            return version

        # Migrate runtime wiring without replacing the operator-managed instructions.
        current.db = db
        current.name = COLLECTOR_AGENT_NAME
        current.description = "Plans one semantic query for the Raw Collection Workflow."
        current.tools = []
        current.tool_call_limit = None
        current.retries = 0
        current.add_datetime_to_context = True
        current.timezone_identifier = "Asia/Shanghai"
        current.add_history_to_context = False
        current.store_tool_messages = True
        current.markdown = False
        current.additional_context = _RUNTIME_CONTRACT
        if isinstance(current.instructions, str) and "resolve_collection_time_window" in current.instructions:
            current.instructions = _seed_instructions()
        current.output_schema = CollectionQueryPlan
        current.structured_outputs = True
        current.metadata = {**metadata, "collector_contract_version": COLLECTOR_CONTRACT_VERSION}
        migrated = current.save(
            db=db,
            stage="published",
            notes=f"Collector runtime contract migration {COLLECTOR_CONTRACT_VERSION}",
        )
        if not isinstance(migrated, int):
            raise ValueError("Raw Collector runtime contract migration failed")
        return migrated

    seed = Agent(
        id=COLLECTOR_AGENT_ID,
        name=COLLECTOR_AGENT_NAME,
        description="Plans one semantic query for the Raw Collection Workflow.",
        model=default_model(),
        db=db,
        tools=[],
        instructions=_seed_instructions(),
        additional_context=_RUNTIME_CONTRACT,
        metadata={"collector_contract_version": COLLECTOR_CONTRACT_VERSION},
        output_schema=CollectionQueryPlan,
        structured_outputs=True,
        retries=0,
        add_datetime_to_context=True,
        timezone_identifier="Asia/Shanghai",
        add_history_to_context=False,
        store_tool_messages=True,
        markdown=False,
    )
    version = seed.save(db=db, stage="published", notes="Initial code-reviewed Collector seed")
    if not isinstance(version, int):
        raise ValueError("Raw Collector seed did not produce a published version")
    return version


def load_collector_agent(registry: Registry) -> LoadedCollectorAgent:
    """Load the current published Studio version for a single Workflow run."""
    db = get_postgres_db()
    component = db.get_component(COLLECTOR_AGENT_ID, component_type=ComponentType.AGENT)
    if component is None:
        raise ValueError("Raw Collector Studio component is missing")
    version = component.get("current_version")
    if not isinstance(version, int):
        raise ValueError("Raw Collector has no published Studio version")

    agent = Agent.load(COLLECTOR_AGENT_ID, db=db, registry=registry, version=version)
    if agent is None:
        raise ValueError("Raw Collector published version could not be rehydrated")
    if not isinstance(agent.instructions, str) or not agent.instructions.strip():
        raise ValueError("Raw Collector published instructions are empty")
    instructions_sha256 = hashlib.sha256(agent.instructions.encode("utf-8")).hexdigest()
    return LoadedCollectorAgent(agent=agent, version=version, instructions_sha256=instructions_sha256)
