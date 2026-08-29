"""Lifecycle and orchestration for Schedule-driven investment reasoning."""

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Step, Workflow
from agno.workflow.types import HumanReview, OnError

from agents.investment_planner import load_investment_planner_agent
from agents.investment_reasoner import load_investment_reasoner_agent
from agents.investment_reviewer import load_investment_reviewer_agent
from capabilities.investment.functions import (
    prepare_investment_context,
    reason_signal_transmissions,
    review_and_finalize,
    synthesize_investment_conclusion,
)
from db import get_postgres_db

INVESTMENT_REASONING_WORKFLOW_ID = "investment-reasoning"
INVESTMENT_REASONING_CONTRACT_VERSION = 1


def _fail_fast_review() -> HumanReview:
    return HumanReview(on_error=OnError.fail)


def _seed_workflow(planner: Agent, reasoner: Agent, reviewer: Agent) -> Workflow:
    """Return the fixed five-stage graph; model judgment stays inside three governed Agents."""

    return Workflow(
        id=INVESTMENT_REASONING_WORKFLOW_ID,
        name="Investment Reasoning",
        description=(
            "Plans one Schedule proposition, freezes Graphiti context, propagates active Signal Facts "
            "through canonical topology, synthesizes node trends, and reviews the final conclusion."
        ),
        db=get_postgres_db(),
        dependencies={
            "planner_agent_id": getattr(planner, "id", "investment-planner"),
            "reasoner_agent_id": getattr(reasoner, "id", "investment-reasoner"),
            "reviewer_agent_id": getattr(reviewer, "id", "investment-reviewer"),
        },
        metadata={"investment_reasoning_contract_version": INVESTMENT_REASONING_CONTRACT_VERSION},
        steps=[
            Step(
                name="plan-investment-analysis",
                agent=planner,
                max_retries=0,
                human_review=_fail_fast_review(),
            ),
            Step(
                name="prepare-investment-context",
                executor=prepare_investment_context,  # type: ignore[arg-type]
                max_retries=0,
                human_review=_fail_fast_review(),
                strict_input_validation=True,
            ),
            Step(
                name="reason-signal-transmissions",
                executor=reason_signal_transmissions,
                max_retries=0,
                human_review=_fail_fast_review(),
                strict_input_validation=True,
            ),
            Step(
                name="synthesize-investment-conclusion",
                executor=synthesize_investment_conclusion,
                max_retries=0,
                human_review=_fail_fast_review(),
                strict_input_validation=True,
            ),
            Step(
                name="review-and-finalize",
                executor=review_and_finalize,
                max_retries=0,
                human_review=_fail_fast_review(),
                strict_input_validation=True,
            ),
        ],
    )


def ensure_investment_reasoning_workflow(registry: Registry) -> int:
    """Seed once and migrate only the code-governed runtime contract."""

    db = get_postgres_db()
    component = db.get_component(INVESTMENT_REASONING_WORKFLOW_ID, component_type=ComponentType.WORKFLOW)
    planner = load_investment_planner_agent(registry)
    reasoner = load_investment_reasoner_agent(registry)
    reviewer = load_investment_reviewer_agent(registry)
    if component is not None:
        version = component.get("current_version")
        if not isinstance(version, int):
            raise ValueError("Investment Reasoning has no published Studio version")
        saved = db.get_config(component_id=INVESTMENT_REASONING_WORKFLOW_ID, version=version)
        config = saved.get("config") if isinstance(saved, dict) else None
        if not isinstance(config, dict):
            raise ValueError("Investment Reasoning published Studio config is missing")
        metadata = dict(config.get("metadata") or {})
        if metadata.get("investment_reasoning_contract_version") == INVESTMENT_REASONING_CONTRACT_VERSION:
            current = Workflow.load(INVESTMENT_REASONING_WORKFLOW_ID, db=db, registry=registry, version=version)
            if current is None or not isinstance(current.steps, list) or not current.steps:
                raise ValueError("Investment Reasoning published version could not be rehydrated")
            return version
        migrated = _seed_workflow(planner, reasoner, reviewer)
        migrated.id = str(config.get("id") or INVESTMENT_REASONING_WORKFLOW_ID)
        migrated.name = str(config.get("name") or "Investment Reasoning")
        migrated.description = str(config.get("description") or migrated.description)
        migrated.metadata = {
            **metadata,
            "investment_reasoning_contract_version": INVESTMENT_REASONING_CONTRACT_VERSION,
        }
        published = migrated.save(
            db=db,
            stage="published",
            notes=f"Investment Reasoning runtime contract migration {INVESTMENT_REASONING_CONTRACT_VERSION}",
        )
        if not isinstance(published, int):
            raise ValueError("Investment Reasoning migration failed")
        return published
    published = _seed_workflow(planner, reasoner, reviewer).save(
        db=db,
        stage="published",
        notes="Initial code-reviewed Investment Reasoning Workflow seed",
    )
    if not isinstance(published, int):
        raise ValueError("Investment Reasoning seed failed")
    return published
