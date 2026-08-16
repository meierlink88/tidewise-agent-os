"""Lifecycle helpers for the Studio-managed Title Curator Agent."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry

from app.settings import default_model
from capabilities.collection import TitleCurationDraft
from db import get_postgres_db

TITLE_CURATOR_AGENT_ID = "title-curator"
TITLE_CURATOR_CONTRACT_VERSION = 4
TITLE_CURATOR_AGENT_NAME = "Collection Title Curator"
_SEED_PROMPT = Path(__file__).with_name("title_curator.seed.md")


@dataclass(frozen=True)
class LoadedTitleCuratorAgent:
    """Published Studio component resolved for Workflow composition."""

    agent: Agent
    version: int
    instructions_sha256: str


def _seed_instructions() -> str:
    instructions = _SEED_PROMPT.read_text(encoding="utf-8").strip()
    if not instructions:
        raise ValueError("Title Curator seed prompt is empty")
    return instructions


def _configure(agent: Agent) -> Agent:
    agent.db = get_postgres_db()
    agent.name = TITLE_CURATOR_AGENT_NAME
    agent.instructions = _seed_instructions()
    agent.tools = []
    agent.retries = 0
    agent.output_schema = TitleCurationDraft
    agent.structured_outputs = True
    agent.add_datetime_to_context = False
    agent.add_history_to_context = False
    agent.store_tool_messages = False
    agent.markdown = False
    agent.metadata = {
        **dict(agent.metadata or {}),
        "title_curator_contract_version": TITLE_CURATOR_CONTRACT_VERSION,
    }
    return agent


def build_title_curator_agent() -> Agent:
    """Return the code-reviewed Agent used for initial seeding and local evals."""
    return _configure(
        Agent(
            id=TITLE_CURATOR_AGENT_ID,
            name=TITLE_CURATOR_AGENT_NAME,
            description="Title-only political-economic and equity-research relevance curator.",
            model=default_model(),
            instructions=_seed_instructions(),
        )
    )


def ensure_title_curator_agent(registry: Registry) -> int:
    """Create the initial component and migrate its contract-bound runtime configuration."""
    db = get_postgres_db()
    component = db.get_component(TITLE_CURATOR_AGENT_ID, component_type=ComponentType.AGENT)
    if component is not None:
        version = component.get("current_version")
        if not isinstance(version, int):
            raise ValueError("Title Curator has no published Studio version")
        current = Agent.load(TITLE_CURATOR_AGENT_ID, db=db, registry=registry, version=version)
        if current is None:
            raise ValueError("Title Curator published version could not be rehydrated")
        if dict(current.metadata or {}).get("title_curator_contract_version") == TITLE_CURATOR_CONTRACT_VERSION:
            return version
        migrated = _configure(current).save(
            db=db,
            stage="published",
            notes=f"Title Curator runtime contract migration {TITLE_CURATOR_CONTRACT_VERSION}",
        )
        if not isinstance(migrated, int):
            raise ValueError("Title Curator runtime contract migration failed")
        return migrated

    seed = build_title_curator_agent()
    version = seed.save(db=db, stage="published", notes="Initial code-reviewed Title Curator seed")
    if not isinstance(version, int):
        raise ValueError("Title Curator seed did not produce a published version")
    return version


def load_title_curator_agent(registry: Registry) -> LoadedTitleCuratorAgent:
    """Load the current published Studio version without independent Workflow session storage."""
    db = get_postgres_db()
    component = db.get_component(TITLE_CURATOR_AGENT_ID, component_type=ComponentType.AGENT)
    if component is None:
        raise ValueError("Title Curator Studio component is missing")
    version = component.get("current_version")
    if not isinstance(version, int):
        raise ValueError("Title Curator has no published Studio version")
    agent = Agent.load(TITLE_CURATOR_AGENT_ID, db=db, registry=registry, version=version)
    if agent is None:
        raise ValueError("Title Curator published version could not be rehydrated")
    if not isinstance(agent.instructions, str) or not agent.instructions.strip():
        raise ValueError("Title Curator published instructions are empty")
    agent.db = None
    return LoadedTitleCuratorAgent(
        agent=agent,
        version=version,
        instructions_sha256=hashlib.sha256(agent.instructions.encode("utf-8")).hexdigest(),
    )
