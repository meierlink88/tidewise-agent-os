"""Lifecycle and orchestration for Schedule-driven layered investment reasoning."""

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Step, Workflow
from agno.workflow.types import HumanReview, OnError

from agents.investment_reasoner import load_investment_reasoner_agent
from agents.investment_report_writer import load_investment_report_writer_agent
from agents.investment_reviewer import load_investment_reviewer_agent
from capabilities.investment.functions import (
    analyze_geopolitical_impact,
    analyze_industry_impact,
    analyze_macro_impact,
    generate_investment_report,
    prepare_investment_context,
    review_and_finalize,
)
from db import get_postgres_db

INVESTMENT_REASONING_WORKFLOW_ID = "investment-reasoning"
INVESTMENT_REASONING_CONTRACT_VERSION = 11
INVESTMENT_REASONING_DESCRIPTION = (
    "Freezes the Schedule Event window, analyzes geopolitical and macro impacts in sequence, "
    "then loads all Signal-rooted industry topology for bounded node transmission, reviews lineage, "
    "and writes a fixed, immutable AgentOS Report Artifact."
)


def _fail_fast_review() -> HumanReview:
    return HumanReview(on_error=OnError.fail)


def _seed_workflow(reasoner: Agent, reviewer: Agent, report_writer: Agent | None = None) -> Workflow:
    """Return the fixed six-stage graph; one Reasoner is reused across three layers."""

    return Workflow(
        id=INVESTMENT_REASONING_WORKFLOW_ID,
        name="Investment Reasoning",
        description=INVESTMENT_REASONING_DESCRIPTION,
        db=get_postgres_db(),
        # Agno JSON-decodes a raw message before invoking a Pydantic
        # ``input_schema``. Existing Schedule rows contain natural-language
        # propositions, so the deterministic prepare Function owns normalization
        # into InvestmentReasoningInput instead.
        dependencies={
            "reasoner_agent_id": getattr(reasoner, "id", "investment-reasoner"),
            "report_writer_agent_id": getattr(report_writer, "id", "investment-report-writer"),
            "reviewer_agent_id": getattr(reviewer, "id", "investment-reviewer"),
        },
        metadata={"investment_reasoning_contract_version": INVESTMENT_REASONING_CONTRACT_VERSION},
        steps=[
            Step(
                name="prepare-investment-context",
                executor=prepare_investment_context,  # type: ignore[arg-type]
                max_retries=0,
                human_review=_fail_fast_review(),
                strict_input_validation=True,
            ),
            Step(
                name="analyze-geopolitical-impact",
                executor=analyze_geopolitical_impact,
                max_retries=0,
                human_review=_fail_fast_review(),
                strict_input_validation=True,
            ),
            Step(
                name="analyze-macro-impact",
                executor=analyze_macro_impact,
                max_retries=0,
                human_review=_fail_fast_review(),
                strict_input_validation=True,
            ),
            Step(
                name="analyze-industry-impact",
                executor=analyze_industry_impact,
                max_retries=0,
                human_review=_fail_fast_review(),
                strict_input_validation=True,
            ),
            Step(
                name="review-and-finalize",
                executor=review_and_finalize,  # type: ignore[arg-type]
                max_retries=0,
                human_review=_fail_fast_review(),
                strict_input_validation=True,
            ),
            Step(
                name="generate-investment-report",
                executor=generate_investment_report,  # type: ignore[arg-type]
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
    reasoner = load_investment_reasoner_agent(registry)
    report_writer = load_investment_report_writer_agent(registry)
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
        migrated = _seed_workflow(reasoner, reviewer, report_writer)
        migrated.id = str(config.get("id") or INVESTMENT_REASONING_WORKFLOW_ID)
        migrated.name = str(config.get("name") or "Investment Reasoning")
        migrated.description = INVESTMENT_REASONING_DESCRIPTION
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
    published = _seed_workflow(reasoner, reviewer, report_writer).save(
        db=db,
        stage="published",
        notes="Initial code-reviewed layered Investment Reasoning Workflow seed",
    )
    if not isinstance(published, int):
        raise ValueError("Investment Reasoning seed failed")
    return published
