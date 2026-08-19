"""Lifecycle and orchestration for the Studio-managed Raw Collection Workflow."""

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Step, Workflow

from agents.raw_collector import LoadedCollectorAgent, load_collector_agent
from agents.title_curator import LoadedTitleCuratorAgent, load_title_curator_agent
from capabilities.collection.functions import (
    build_artifact_step,
    execute_collection_channels_step,
    prepare_collection_context,
    prepare_title_curation,
    publish_collection_step,
    validate_title_curation,
)
from db import get_postgres_db

RAW_COLLECTION_WORKFLOW_ID = "raw-collection"
RAW_COLLECTION_CONTRACT_VERSION = 11


def _workflow_dependencies(
    collector: LoadedCollectorAgent,
    curator: LoadedTitleCuratorAgent,
) -> dict[str, object]:
    """Serialize the two pinned Agent component provenances without branch drift."""
    return {
        "collector_agent_component_id": collector.agent.id,
        "collector_agent_config_version": collector.version,
        "collector_instructions_sha256": collector.instructions_sha256,
        "title_curator_agent_component_id": curator.agent.id,
        "title_curator_agent_config_version": curator.version,
        "title_curator_instructions_sha256": curator.instructions_sha256,
    }


def _seed_workflow(collector: Agent, curator: Agent, *, dependencies: dict[str, object] | None = None) -> Workflow:
    """Return the code-reviewed initial Workflow graph saved to Studio once."""
    return Workflow(
        id=RAW_COLLECTION_WORKFLOW_ID,
        name="Raw Collection",
        description="Agentic query planning followed by deterministic channel acquisition and publication.",
        db=get_postgres_db(),
        dependencies=dependencies,
        metadata={"raw_collection_contract_version": RAW_COLLECTION_CONTRACT_VERSION},
        steps=[
            Step(
                name="prepare-collection-context",
                executor=prepare_collection_context,  # type: ignore[arg-type]  # Agno injects RunContext by name.
                max_retries=0,
                on_error="fail",
            ),
            Step(
                name="plan-collection-query",
                agent=collector,
                max_retries=0,
                on_error="fail",
                strict_input_validation=True,
            ),
            Step(
                name="execute-collection-channels",
                executor=execute_collection_channels_step,  # type: ignore[arg-type]  # Agno injects RunContext.
                max_retries=0,
                on_error="fail",
                strict_input_validation=True,
            ),
            Step(
                name="prepare-title-curation",
                executor=prepare_title_curation,  # type: ignore[arg-type]  # Agno injects RunContext by name.
                max_retries=0,
                on_error="fail",
                strict_input_validation=True,
            ),
            Step(
                name="curate-collection-titles",
                agent=curator,
                max_retries=0,
                on_error="fail",
                strict_input_validation=True,
            ),
            Step(
                name="validate-title-curation",
                executor=validate_title_curation,  # type: ignore[arg-type]  # Agno injects RunContext by name.
                max_retries=0,
                on_error="fail",
                strict_input_validation=True,
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
        collector = load_collector_agent(registry)
        curator = load_title_curator_agent(registry)
        migrated = _seed_workflow(
            collector.agent,
            curator.agent,
            dependencies=_workflow_dependencies(collector, curator),
        )
        migrated.id = str(config.get("id") or RAW_COLLECTION_WORKFLOW_ID)
        migrated.name = str(config.get("name") or "Raw Collection")
        migrated.description = str(config.get("description") or migrated.description)
        migrated.metadata = {**metadata, "raw_collection_contract_version": RAW_COLLECTION_CONTRACT_VERSION}
        published = migrated.save(
            db=db,
            stage="published",
            notes=f"Raw Collection runtime contract migration {RAW_COLLECTION_CONTRACT_VERSION}",
        )
        if not isinstance(published, int):
            raise ValueError("Raw Collection runtime contract migration failed")
        return published

    collector = load_collector_agent(registry)
    curator = load_title_curator_agent(registry)
    version = _seed_workflow(
        collector.agent,
        curator.agent,
        dependencies=_workflow_dependencies(collector, curator),
    ).save(
        db=db,
        stage="published",
        notes="Initial code-reviewed Raw Collection Workflow seed",
    )
    if not isinstance(version, int):
        raise ValueError("Raw Collection Workflow seed did not produce a published version")
    return version
