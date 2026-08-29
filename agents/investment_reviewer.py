"""Studio-managed Investment Reviewer Agent."""

from pathlib import Path

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry

from app.settings import default_model
from capabilities.investment import ReviewResult
from db import get_postgres_db

INVESTMENT_REVIEWER_AGENT_ID = "investment-reviewer"
INVESTMENT_REVIEWER_CONTRACT_VERSION = 1
_PROMPT = Path(__file__).with_name("investment_reviewer.seed.md")


def _configure(agent: Agent) -> Agent:
    agent.db = get_postgres_db()
    agent.name = "Investment Reviewer"
    agent.description = "Audits Signal lineage, topology, time horizons and unsupported investment claims."
    agent.tools = []
    agent.instructions = _PROMPT.read_text(encoding="utf-8").strip()
    agent.output_schema = ReviewResult
    agent.parse_response = True
    agent.use_json_mode = True
    agent.retries = 0
    agent.add_datetime_to_context = False
    agent.add_history_to_context = False
    agent.markdown = False
    agent.metadata = {
        **dict(agent.metadata or {}),
        "investment_reviewer_contract_version": INVESTMENT_REVIEWER_CONTRACT_VERSION,
    }
    return agent


def build_investment_reviewer_agent() -> Agent:
    return _configure(Agent(id=INVESTMENT_REVIEWER_AGENT_ID, model=default_model()))


def ensure_investment_reviewer_agent(registry: Registry) -> int:
    db = get_postgres_db()
    component = db.get_component(INVESTMENT_REVIEWER_AGENT_ID, component_type=ComponentType.AGENT)
    if component is not None:
        version = component.get("current_version")
        if not isinstance(version, int):
            raise ValueError("Investment Reviewer has no published Studio version")
        current = Agent.load(INVESTMENT_REVIEWER_AGENT_ID, db=db, registry=registry, version=version)
        if current is None:
            raise ValueError("Investment Reviewer could not be rehydrated")
        if (
            dict(current.metadata or {}).get("investment_reviewer_contract_version")
            == INVESTMENT_REVIEWER_CONTRACT_VERSION
        ):
            return version
        saved = _configure(current).save(
            db=db,
            stage="published",
            notes=f"Investment Reviewer contract migration {INVESTMENT_REVIEWER_CONTRACT_VERSION}",
        )
        if not isinstance(saved, int):
            raise ValueError("Investment Reviewer migration failed")
        return saved
    saved = build_investment_reviewer_agent().save(db=db, stage="published", notes="Initial Investment Reviewer seed")
    if not isinstance(saved, int):
        raise ValueError("Investment Reviewer seed failed")
    return saved


def load_investment_reviewer_agent(registry: Registry) -> Agent:
    db = get_postgres_db()
    component = db.get_component(INVESTMENT_REVIEWER_AGENT_ID, component_type=ComponentType.AGENT)
    if component is None or not isinstance(component.get("current_version"), int):
        raise ValueError("Investment Reviewer Studio component is missing")
    agent = Agent.load(INVESTMENT_REVIEWER_AGENT_ID, db=db, registry=registry, version=component["current_version"])
    if agent is None:
        raise ValueError("Investment Reviewer could not be loaded")
    agent.db = None
    return agent
