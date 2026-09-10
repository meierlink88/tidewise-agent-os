"""Lifecycle helpers for the Studio-managed Title Curator Agent."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.models.deepseek import DeepSeek
from agno.registry import Registry

from app.settings import default_model
from capabilities.evidence import EvidenceReviewDraft
from db import get_postgres_db

TITLE_CURATOR_AGENT_ID = "title-curator"
TITLE_CURATOR_CONTRACT_VERSION = 14
TITLE_CURATOR_AGENT_NAME = "Evidence Reviewer"
TITLE_CURATOR_SEED_SHA256_KEY = "article_review_seed_sha256"
_SEED_PROMPT = Path(__file__).with_name("title_curator.seed.md")


def filter_model() -> DeepSeek:
    model = default_model()
    model.use_thinking = False
    model.reasoning_effort = None
    model.timeout = 120
    return model


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
    # Reuse the exact Evidence prompt rather than maintaining a second extraction policy.
    evidence_rules = _SEED_PROMPT.with_name("evidence_extractor.seed.md").read_text(encoding="utf-8").strip()
    return instructions + "\n\n以下提取规则仅适用于相关文章的 extraction 字段：\n" + evidence_rules


def _configure(agent: Agent) -> Agent:
    agent.db = get_postgres_db()
    agent.model = filter_model()
    agent.name = TITLE_CURATOR_AGENT_NAME
    agent.description = "Reviews one complete article and extracts source-grounded Evidence in the same reading."
    agent.instructions = _seed_instructions()
    agent.tools = []
    agent.retries = 0
    agent.output_schema = EvidenceReviewDraft
    # DeepSeek uses JSON-object mode; Pydantic and business validation enforce the draft schema.
    agent.structured_outputs = False
    agent.use_json_mode = True
    agent.parse_response = True
    agent.add_datetime_to_context = False
    agent.add_history_to_context = False
    agent.store_tool_messages = False
    agent.markdown = False
    agent.metadata = {
        **dict(agent.metadata or {}),
        "title_curator_contract_version": TITLE_CURATOR_CONTRACT_VERSION,
        "raw_evidence_filter_reasoning_effort": agent.model.reasoning_effort,
        TITLE_CURATOR_SEED_SHA256_KEY: hashlib.sha256(_seed_instructions().encode()).hexdigest(),
    }
    return agent


def build_title_curator_agent() -> Agent:
    """Return the code-reviewed Agent used for initial seeding and local evals."""
    return _configure(
        Agent(
            id=TITLE_CURATOR_AGENT_ID,
            name=TITLE_CURATOR_AGENT_NAME,
            description="Filters collected material for political-economic and equity-research relevance.",
            model=filter_model(),
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
        expected_model = filter_model()
        if (
            dict(current.metadata or {}).get("title_curator_contract_version") == TITLE_CURATOR_CONTRACT_VERSION
            and dict(current.metadata or {}).get(TITLE_CURATOR_SEED_SHA256_KEY)
            == hashlib.sha256(_seed_instructions().encode()).hexdigest()
            and isinstance(current.model, DeepSeek)
            and current.model.id == expected_model.id
            and current.model.name == expected_model.name
            and current.model.reasoning_effort is None
            and current.model.use_thinking is False
            and current.use_json_mode is True
            and current.structured_outputs is False
            and dict(current.metadata or {}).get("raw_evidence_filter_reasoning_effort", "medium")
            == expected_model.reasoning_effort
            and current.model.base_url == expected_model.base_url
        ):
            return version
        # A model-only migration from contract 13 must preserve the Studio-maintained prompt.
        instructions = current.instructions
        same_contract = (
            dict(current.metadata or {}).get("title_curator_contract_version") in (13, TITLE_CURATOR_CONTRACT_VERSION)
            and dict(current.metadata or {}).get(TITLE_CURATOR_SEED_SHA256_KEY)
            == hashlib.sha256(_seed_instructions().encode()).hexdigest()
        )
        configured = _configure(current)
        if same_contract:
            configured.instructions = instructions
        migrated = configured.save(
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
    # Keep Reviewer-specific transport settings on a fresh non-thinking DeepSeek model.
    agent.model = filter_model()
    agent.db = None
    return LoadedTitleCuratorAgent(
        agent=agent,
        version=version,
        instructions_sha256=hashlib.sha256(agent.instructions.encode("utf-8")).hexdigest(),
    )
