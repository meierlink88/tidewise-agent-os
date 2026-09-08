"""Pure collection Workflow seed and Studio lifecycle; no default Schedule or Agent."""

from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Step, Workflow
from agno.workflow.types import HumanReview, OnError

from capabilities.collection_v2.functions import collect_raw_v2, publish_raw_v2
from db import get_postgres_db

RAW_COLLECTION_V2_WORKFLOW_ID = "raw-collection-v2"

raw_collection_v2 = Workflow(
    id=RAW_COLLECTION_V2_WORKFLOW_ID,
    name="Raw Collection V2",
    description="Collect sources, deduplicate article versions and archive raw documents to MinIO without an LLM.",
    db=get_postgres_db(),
    steps=[
        Step(
            name="Collect Raw V2",
            executor=collect_raw_v2,  # type: ignore[arg-type]  # Agno injects RunContext by name.
            max_retries=0,
            human_review=HumanReview(on_error=OnError.fail),
        ),
        Step(
            name="Publish Raw V2",
            executor=publish_raw_v2,  # type: ignore[arg-type]  # Agno injects RunContext by name.
            max_retries=0,
            human_review=HumanReview(on_error=OnError.fail),
        ),
    ],
)


def ensure_raw_collection_v2_workflow(registry: Registry) -> int:
    """Seed Studio once and preserve every subsequent published configuration."""
    db = get_postgres_db()
    component = db.get_component(RAW_COLLECTION_V2_WORKFLOW_ID, component_type=ComponentType.WORKFLOW)
    if component is None:
        version = raw_collection_v2.save(db=db, stage="published", notes="Initial pure collection V2 Studio seed")
        if not isinstance(version, int):
            raise ValueError("Raw Collection V2 Studio seed failed")
        return version
    version = component.get("current_version")
    if not isinstance(version, int):
        raise ValueError("Raw Collection V2 has no published Studio version")
    loaded = Workflow.load(RAW_COLLECTION_V2_WORKFLOW_ID, db=db, registry=registry, version=version)
    if loaded is None or not isinstance(loaded.steps, list) or not loaded.steps:
        raise ValueError("Raw Collection V2 published Studio configuration cannot be loaded")
    return version
