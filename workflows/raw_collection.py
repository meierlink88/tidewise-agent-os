"""Lifecycle and orchestration for the Studio-managed Raw Collection Workflow."""

from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Step, Workflow

from capabilities.raw_collection.functions import (
    agentic_collect_step,
    build_artifact_step,
    publish_collection_step,
)
from db import get_postgres_db

RAW_COLLECTION_WORKFLOW_ID = "raw-collection"
RAW_COLLECTION_CONTRACT_VERSION = 1


def _seed_workflow() -> Workflow:
    """Return the code-reviewed initial Workflow graph saved to Studio once."""
    return Workflow(
        id=RAW_COLLECTION_WORKFLOW_ID,
        name="Raw Collection",
        description="Agentic channel selection followed by deterministic raw-document publication.",
        db=get_postgres_db(),
        metadata={"raw_collection_contract_version": RAW_COLLECTION_CONTRACT_VERSION},
        steps=[
            Step(
                name="agentic-collect",
                executor=agentic_collect_step,  # type: ignore[arg-type]  # Agno injects RunContext by name.
                max_retries=0,
                on_error="fail",
            ),
            Step(
                name="build-artifact-set",
                executor=build_artifact_step,  # type: ignore[arg-type]  # Agno injects RunContext by name.
                max_retries=0,
                on_error="fail",
                strict_input_validation=True,
            ),
            Step(
                name="publish-collection",
                executor=publish_collection_step,
                max_retries=0,
                on_error="fail",
                strict_input_validation=True,
            ),
        ],
    )


def ensure_raw_collection_workflow(registry: Registry) -> int:
    """Create the initial published Workflow once; never overwrite Studio versions."""
    db = get_postgres_db()
    component = db.get_component(RAW_COLLECTION_WORKFLOW_ID, component_type=ComponentType.WORKFLOW)
    if component is not None:
        version = component.get("current_version")
        if not isinstance(version, int):
            raise ValueError("Raw Collection has no published Studio version")
        current = Workflow.load(RAW_COLLECTION_WORKFLOW_ID, db=db, registry=registry, version=version)
        if current is None or not isinstance(current.steps, list) or not current.steps:
            raise ValueError("Raw Collection published Studio version could not be rehydrated")
        return version

    version = _seed_workflow().save(
        db=db,
        stage="published",
        notes="Initial code-reviewed Raw Collection Workflow seed",
    )
    if not isinstance(version, int):
        raise ValueError("Raw Collection Workflow seed did not produce a published version")
    return version
