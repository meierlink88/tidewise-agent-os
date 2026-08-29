"""Studio-managed Investment Planner Agent."""

from pathlib import Path

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry

from app.settings import default_model
from capabilities.investment import InvestmentAnalysisPlan
from db import get_postgres_db

INVESTMENT_PLANNER_AGENT_ID = "investment-planner"
INVESTMENT_PLANNER_CONTRACT_VERSION = 1
_PROMPT = Path(__file__).with_name("investment_planner.seed.md")


def _configure(agent: Agent) -> Agent:
    agent.db = get_postgres_db()
    agent.name = "Investment Planner"
    agent.description = "Parses an editable Schedule proposition into a bounded investment analysis plan."
    agent.tools = []
    agent.instructions = _PROMPT.read_text(encoding="utf-8").strip()
    agent.output_schema = InvestmentAnalysisPlan
    agent.parse_response = True
    agent.use_json_mode = True
    agent.retries = 0
    agent.add_datetime_to_context = False
    agent.add_history_to_context = False
    agent.markdown = False
    agent.metadata = {
        **dict(agent.metadata or {}),
        "investment_planner_contract_version": INVESTMENT_PLANNER_CONTRACT_VERSION,
    }
    return agent


def build_investment_planner_agent() -> Agent:
    return _configure(Agent(id=INVESTMENT_PLANNER_AGENT_ID, model=default_model()))


def ensure_investment_planner_agent(registry: Registry) -> int:
    db = get_postgres_db()
    component = db.get_component(INVESTMENT_PLANNER_AGENT_ID, component_type=ComponentType.AGENT)
    if component is not None:
        version = component.get("current_version")
        if not isinstance(version, int):
            raise ValueError("Investment Planner has no published Studio version")
        current = Agent.load(INVESTMENT_PLANNER_AGENT_ID, db=db, registry=registry, version=version)
        if current is None:
            raise ValueError("Investment Planner published version could not be rehydrated")
        if (
            dict(current.metadata or {}).get("investment_planner_contract_version")
            == INVESTMENT_PLANNER_CONTRACT_VERSION
        ):
            return version
        saved = _configure(current).save(
            db=db,
            stage="published",
            notes=f"Investment Planner contract migration {INVESTMENT_PLANNER_CONTRACT_VERSION}",
        )
        if not isinstance(saved, int):
            raise ValueError("Investment Planner migration failed")
        return saved
    saved = build_investment_planner_agent().save(db=db, stage="published", notes="Initial Investment Planner seed")
    if not isinstance(saved, int):
        raise ValueError("Investment Planner seed failed")
    return saved


def load_investment_planner_agent(registry: Registry) -> Agent:
    db = get_postgres_db()
    component = db.get_component(INVESTMENT_PLANNER_AGENT_ID, component_type=ComponentType.AGENT)
    if component is None or not isinstance(component.get("current_version"), int):
        raise ValueError("Investment Planner Studio component is missing")
    agent = Agent.load(INVESTMENT_PLANNER_AGENT_ID, db=db, registry=registry, version=component["current_version"])
    if agent is None:
        raise ValueError("Investment Planner could not be loaded")
    agent.db = None
    return agent
