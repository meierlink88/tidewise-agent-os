"""Lifecycle and orchestration for the Studio-managed Raw Collection Workflow."""

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Loop, Step, Workflow
from agno.workflow.types import HumanReview, OnError

from agents.title_curator import LoadedTitleCuratorAgent, load_title_curator_agent
from capabilities.collection.functions import (
    collect_raw_evidence,
    prepare_raw_evidence_filter_batch,
    publish_raw_evidence,
    raw_evidence_filter_complete,
    save_raw_evidence_filter_batch,
)
from db import get_postgres_db

RAW_COLLECTION_WORKFLOW_ID = "raw-collection"
RAW_COLLECTION_CONTRACT_VERSION = 18
RETIRED_COLLECTION_QUERY_PLANNER_AGENT_ID = "raw-collector"


def _fail_fast_review() -> HumanReview:
    """Preserve the v2 fail-fast step contract through Agno v3 HumanReview."""
    return HumanReview(on_error=OnError.fail)


def _workflow_dependencies(
    curator: LoadedTitleCuratorAgent,
) -> dict[str, object]:
    """Serialize the pinned filter Agent provenance without branch drift."""
    return {
        "title_curator_agent_component_id": curator.agent.id,
        "title_curator_agent_config_version": curator.version,
        "title_curator_instructions_sha256": curator.instructions_sha256,
    }


def _seed_workflow(curator: Agent, *, dependencies: dict[str, object] | None = None) -> Workflow:
    """Return the code-reviewed initial Workflow graph saved to Studio once."""
    return Workflow(
        id=RAW_COLLECTION_WORKFLOW_ID,
        name="Raw Collection",
        description="Collect, filter and publish the latest Raw Evidence from configured channels.",
        db=get_postgres_db(),
        dependencies=dependencies,
        metadata={"raw_collection_contract_version": RAW_COLLECTION_CONTRACT_VERSION},
        steps=[
            Step(
                name="collect-raw-evidence",
                executor=collect_raw_evidence,  # type: ignore[arg-type]  # Agno injects RunContext by name.
                max_retries=0,
                human_review=_fail_fast_review(),
            ),
            Loop(
                name="filter-raw-evidence",
                description="Filter complete documents in bounded batches until every Candidate is decided.",
                max_iterations=1_000,
                end_condition=raw_evidence_filter_complete,
                # Each iteration reloads the next batch from the run-scoped file buffer.
                # Forwarding the prior progress object would replace the Agent's batch input.
                forward_iteration_output=False,
                human_review=_fail_fast_review(),
                steps=[
                    Step(
                        name="prepare-raw-evidence-filter-batch",
                        executor=prepare_raw_evidence_filter_batch,  # type: ignore[arg-type]  # Agno injects RunContext.
                        max_retries=0,
                        human_review=_fail_fast_review(),
                    ),
                    Step(
                        name="filter-raw-evidence-batch",
                        agent=curator,
                        max_retries=0,
                        human_review=_fail_fast_review(),
                        strict_input_validation=True,
                    ),
                    Step(
                        name="save-raw-evidence-filter-batch",
                        executor=save_raw_evidence_filter_batch,  # type: ignore[arg-type]  # Agno injects RunContext.
                        max_retries=0,
                        human_review=_fail_fast_review(),
                        strict_input_validation=True,
                    ),
                ],
            ),
            Step(
                name="publish-raw-evidence",
                executor=publish_raw_evidence,  # type: ignore[arg-type]  # Agno injects RunContext by name.
                max_retries=0,
                human_review=_fail_fast_review(),
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
        saved = db.get_config(component_id=RAW_COLLECTION_WORKFLOW_ID, version=version)
        config = saved.get("config") if isinstance(saved, dict) else None
        if not isinstance(config, dict):
            raise ValueError("Raw Collection published Studio config is missing")
        metadata = dict(config.get("metadata") or {})
        if metadata.get("raw_collection_contract_version") == RAW_COLLECTION_CONTRACT_VERSION:
            current = Workflow.load(RAW_COLLECTION_WORKFLOW_ID, db=db, registry=registry, version=version)
            if current is None or not isinstance(current.steps, list) or not current.steps:
                raise ValueError("Raw Collection published Studio version could not be rehydrated")
            return version
        curator = load_title_curator_agent(registry)
        migrated = _seed_workflow(
            curator.agent,
            dependencies=_workflow_dependencies(curator),
        )
        migrated.id = str(config.get("id") or RAW_COLLECTION_WORKFLOW_ID)
        migrated.name = str(config.get("name") or "Raw Collection")
        migrated.metadata = {**metadata, "raw_collection_contract_version": RAW_COLLECTION_CONTRACT_VERSION}
        published = migrated.save(
            db=db,
            stage="published",
            notes=f"Raw Collection runtime contract migration {RAW_COLLECTION_CONTRACT_VERSION}",
        )
        if not isinstance(published, int):
            raise ValueError("Raw Collection runtime contract migration failed")
        return published

    curator = load_title_curator_agent(registry)
    version = _seed_workflow(
        curator.agent,
        dependencies=_workflow_dependencies(curator),
    ).save(
        db=db,
        stage="published",
        notes="Initial code-reviewed Raw Collection Workflow seed",
    )
    if not isinstance(version, int):
        raise ValueError("Raw Collection Workflow seed did not produce a published version")
    return version


def retire_collection_query_planner_agent() -> bool:
    """Soft-archive the removed Planner after its Workflow dependency is migrated away."""
    db = get_postgres_db()
    component = db.get_component(RETIRED_COLLECTION_QUERY_PLANNER_AGENT_ID, component_type=ComponentType.AGENT)
    if component is None:
        return False
    version = component.get("current_version")
    if not isinstance(version, int):
        raise ValueError("retired Collection Query Planner has no published version")
    return db.delete_component(
        RETIRED_COLLECTION_QUERY_PLANNER_AGENT_ID,
        expected_current_version=version,
        require_no_dependents=False,
    )
