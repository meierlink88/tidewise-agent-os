"""Lifecycle and orchestration for the Studio-managed Event Extraction Workflow."""

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Condition, Step, Workflow

from agents.event_extractor import load_event_extractor_agent
from capabilities.event.functions import (
    event_batch_requires_analysis,
    freeze_event_analysis,
    prepare_event_batch,
    submit_event_candidates,
)
from db import get_postgres_db

EVENT_EXTRACTION_WORKFLOW_ID = "event-extraction"
EVENT_EXTRACTION_CONTRACT_VERSION = 1


def _seed_workflow(agent: Agent) -> Workflow:
    """Return the reviewed Workflow graph saved to Studio once."""
    return Workflow(
        id=EVENT_EXTRACTION_WORKFLOW_ID,
        name="Event Extraction",
        description="Extracts frozen local Evidence into Event Candidates and hands them to Reasoning Server.",
        db=get_postgres_db(),
        metadata={"event_extraction_contract_version": EVENT_EXTRACTION_CONTRACT_VERSION},
        steps=[
            Step(
                name="prepare-event-batch",
                executor=prepare_event_batch,
                max_retries=0,
                on_error="fail",
            ),
            Condition(
                name="analyze-unfrozen-event-batch",
                evaluator=event_batch_requires_analysis,
                on_error="fail",
                steps=[
                    Step(
                        name="analyze-event-batch",
                        agent=agent,
                        max_retries=0,
                        on_error="fail",
                        strict_input_validation=True,
                    ),
                    Step(
                        name="freeze-event-analysis",
                        executor=freeze_event_analysis,
                        max_retries=0,
                        on_error="fail",
                        strict_input_validation=True,
                    ),
                ],
            ),
            Step(
                name="submit-event-candidates",
                executor=submit_event_candidates,
                max_retries=0,
                on_error="fail",
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
        agent = load_event_extractor_agent(registry)
        migrated = _seed_workflow(agent)
        migrated.id = str(config.get("id") or EVENT_EXTRACTION_WORKFLOW_ID)
        migrated.name = str(config.get("name") or "Event Extraction")
        migrated.description = str(config.get("description") or migrated.description)
        migrated.metadata = {**metadata, "event_extraction_contract_version": EVENT_EXTRACTION_CONTRACT_VERSION}
        published = migrated.save(
            db=db,
            stage="published",
            notes=f"Event Extraction runtime contract migration {EVENT_EXTRACTION_CONTRACT_VERSION}",
        )
        if not isinstance(published, int):
            raise ValueError("Event Extraction runtime contract migration failed")
        return published
    agent = load_event_extractor_agent(registry)
    version = _seed_workflow(agent).save(
        db=db,
        stage="published",
        notes="Initial code-reviewed Event Extraction Workflow seed",
    )
    if not isinstance(version, int):
        raise ValueError("Event Extraction Workflow seed did not produce a published version")
    return version
