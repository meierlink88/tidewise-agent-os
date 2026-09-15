"""Geopolitical conflict research, retaining the historical Workflow ID and versions."""

from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Loop, Step, Workflow
from agno.workflow.types import HumanReview, OnError

from capabilities.geopolitical_research.functions import (
    geopolitical_research_complete,
    research_next_geopolitical_story,
    select_geopolitical_stories,
)
from db import get_postgres_db

INVESTMENT_REASONING_WORKFLOW_ID = "investment-reasoning"
INVESTMENT_REASONING_CONTRACT_VERSION = 14
RETIRED_INVESTMENT_PLANNER_AGENT_ID = "investment-planner"
INVESTMENT_REASONING_DESCRIPTION = (
    "筛选最近24小时新增Event关联的地缘政治故事线，逐条调用Tidewise Research地缘冲突团队，"
    "每条故事线一次研究，保存最终报告及Research运行引用。"
)


def _fail_fast_review() -> HumanReview:
    return HumanReview(on_error=OnError.fail)


def _seed_workflow() -> Workflow:
    return Workflow(
        id=INVESTMENT_REASONING_WORKFLOW_ID,
        name="地缘冲突研究",
        description=INVESTMENT_REASONING_DESCRIPTION,
        db=get_postgres_db(),
        metadata={"investment_reasoning_contract_version": INVESTMENT_REASONING_CONTRACT_VERSION},
        steps=[
            Step(
                name="筛选24小时新增事件故事线",
                executor=select_geopolitical_stories,  # type: ignore[arg-type]
                max_retries=0,
                human_review=_fail_fast_review(),
            ),
            Loop(
                name="逐条故事线研究",
                max_iterations=1_000,
                end_condition=geopolitical_research_complete,
                forward_iteration_output=False,
                human_review=_fail_fast_review(),
                steps=[
                    Step(
                        name="调用地缘冲突研究团队",
                        executor=research_next_geopolitical_story,  # type: ignore[arg-type]
                        max_retries=0,
                        human_review=_fail_fast_review(),
                    )
                ],
            ),
        ],
    )


def ensure_investment_reasoning_workflow(registry: Registry) -> int:
    """Publish the new graph once, preserving identity and all historical versions."""
    db = get_postgres_db()
    component = db.get_component(INVESTMENT_REASONING_WORKFLOW_ID, component_type=ComponentType.WORKFLOW)
    metadata = {}
    if component is not None:
        version = component.get("current_version")
        if not isinstance(version, int):
            raise ValueError("Geopolitical research has no published Studio version")
        saved = db.get_config(component_id=INVESTMENT_REASONING_WORKFLOW_ID, version=version)
        config = saved.get("config") if isinstance(saved, dict) else None
        if not isinstance(config, dict):
            raise ValueError("Geopolitical research published Studio config is missing")
        metadata = dict(config.get("metadata") or {})
        if metadata.get("investment_reasoning_contract_version") == INVESTMENT_REASONING_CONTRACT_VERSION:
            current = Workflow.load(INVESTMENT_REASONING_WORKFLOW_ID, db=db, registry=registry, version=version)
            if current is None or not isinstance(current.steps, list) or not current.steps:
                raise ValueError("Geopolitical research published version could not be rehydrated")
            return version
    workflow = _seed_workflow()
    workflow.metadata = {**metadata, **(workflow.metadata or {})}
    published = workflow.save(
        db=db,
        stage="published",
        notes=f"Geopolitical conflict research runtime contract {INVESTMENT_REASONING_CONTRACT_VERSION}",
    )
    if not isinstance(published, int):
        raise ValueError("Geopolitical research seed/migration failed")
    return published


def retire_investment_planner_agent() -> bool:
    """Soft-archive the unused legacy Planner while preserving historical audit links."""
    db = get_postgres_db()
    component = db.get_component(RETIRED_INVESTMENT_PLANNER_AGENT_ID, component_type=ComponentType.AGENT)
    if component is None:
        return False
    version = component.get("current_version")
    if not isinstance(version, int):
        raise ValueError("retired Investment Planner has no published version")
    return db.delete_component(
        RETIRED_INVESTMENT_PLANNER_AGENT_ID,
        expected_current_version=version,
        require_no_dependents=False,
    )
