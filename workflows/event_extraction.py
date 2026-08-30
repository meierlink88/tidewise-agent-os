"""Lifecycle and orchestration for the Studio-managed Event Extraction Workflow."""

from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Step, Workflow
from agno.workflow.types import HumanReview, OnError

from capabilities.event.functions import (
    build_signals,
    extract_events,
    publish_events,
)
from db import get_postgres_db

EVENT_EXTRACTION_WORKFLOW_ID = "event-extraction"
EVENT_EXTRACTION_CONTRACT_VERSION = 7


def _fail_fast_review() -> HumanReview:
    """Preserve the v2 fail-fast step contract through Agno v3 HumanReview."""
    return HumanReview(on_error=OnError.fail)


def _seed_workflow() -> Workflow:
    """Return the reviewed Workflow graph saved to Studio once."""
    return Workflow(
        id=EVENT_EXTRACTION_WORKFLOW_ID,
        name="Event Extraction",
        description=(
            "Extracts frozen Evidence, resolves and publishes new Events locally, projects them "
            "through Graphiti, and constructs Variable Signal Facts."
        ),
        db=get_postgres_db(),
        metadata={"event_extraction_contract_version": EVENT_EXTRACTION_CONTRACT_VERSION},
        steps=[
            Step(
                name="extract-events",
                description="Claim Evidence and freeze atomic Event Candidates exactly once.",
                executor=extract_events,
                max_retries=0,
                human_review=_fail_fast_review(),
                strict_input_validation=True,
            ),
            Step(
                name="publish-events",
                executor=publish_events,
                max_retries=0,
                human_review=_fail_fast_review(),
                strict_input_validation=True,
            ),
            Step(
                name="build-signals",
                executor=build_signals,
                max_retries=0,
                human_review=_fail_fast_review(),
                strict_input_validation=True,
            ),
        ],
    )


def ensure_event_extraction_workflow(registry: Registry) -> int:
    """Create the initial Workflow once and migrate only its runtime contract."""
    db = get_postgres_db()
    component = db.get_component(EVENT_EXTRACTION_WORKFLOW_ID, component_type=ComponentType.WORKFLOW)
    if component is not None:
        version = component.get("current_version")
        if not isinstance(version, int):
            raise ValueError("Event Extraction has no published Studio version")
        saved = db.get_config(component_id=EVENT_EXTRACTION_WORKFLOW_ID, version=version)
        config = saved.get("config") if isinstance(saved, dict) else None
        if not isinstance(config, dict):
            raise ValueError("Event Extraction published Studio config is missing")
        metadata = dict(config.get("metadata") or {})
        if metadata.get("event_extraction_contract_version") == EVENT_EXTRACTION_CONTRACT_VERSION:
            current = Workflow.load(EVENT_EXTRACTION_WORKFLOW_ID, db=db, registry=registry, version=version)
            if current is None or not isinstance(current.steps, list) or not current.steps:
                raise ValueError("Event Extraction published Studio version could not be rehydrated")
            return version
        migrated = _seed_workflow()
        migrated.id = str(config.get("id") or EVENT_EXTRACTION_WORKFLOW_ID)
        migrated.name = str(config.get("name") or "Event Extraction")
        migrated.metadata = {**metadata, "event_extraction_contract_version": EVENT_EXTRACTION_CONTRACT_VERSION}
        published = migrated.save(
            db=db,
            stage="published",
            notes=f"Event Extraction runtime contract migration {EVENT_EXTRACTION_CONTRACT_VERSION}",
        )
        if not isinstance(published, int):
            raise ValueError("Event Extraction runtime contract migration failed")
        return published
    version = _seed_workflow().save(
        db=db,
        stage="published",
        notes="Initial code-reviewed Event Extraction Workflow seed",
    )
    if not isinstance(version, int):
        raise ValueError("Event Extraction Workflow seed did not produce a published version")
    return version
