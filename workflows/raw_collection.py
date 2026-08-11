"""Lifecycle and registered executors for the Studio-managed Raw Collection Workflow."""

import asyncio
import json
from typing import Any
from uuid import uuid4

from agno.db.base import ComponentType
from agno.registry import Registry
from agno.run import RunContext
from agno.workflow import Step, StepInput, StepOutput, Workflow

from agents.raw_collector import COLLECTOR_AGENT_ID, load_collector_agent
from capabilities.raw_collection import (
    CollectionRequest,
    PreparedArtifactSet,
    build_artifact_set,
    publish_artifact_set,
)
from db import get_postgres_db

RAW_COLLECTION_WORKFLOW_ID = "raw-collection"
RAW_COLLECTION_CONTRACT_VERSION = 1


def _request_from_input(value: Any) -> CollectionRequest:
    if isinstance(value, CollectionRequest):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                pass
            else:
                if not isinstance(decoded, dict):
                    raise ValueError("structured collection input must be a JSON object")
                value = decoded
    return CollectionRequest.model_validate(value)


async def agentic_collect_step(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Validate natural-language input and run the Collector Agent with the Workflow context."""
    # Imported lazily so app.registry can register this executor without a cycle.
    from app.registry import registry

    request = _request_from_input(step_input.input)
    loaded = await asyncio.to_thread(load_collector_agent, registry)
    dependencies = dict(run_context.dependencies or {})
    dependencies.update(
        {
            "collector_objective": request.objective,
            "collector_agent_component_id": COLLECTOR_AGENT_ID,
            "collector_agent_config_version": loaded.version,
            "collector_instructions_sha256": loaded.instructions_sha256,
        }
    )
    run_context.dependencies = dependencies
    response = await loaded.agent.arun(
        input=request.objective,
        run_context=run_context,
        run_id=str(uuid4()),
        session_id=run_context.session_id,
        user_id=run_context.user_id,
    )
    if response.content is None:
        raise ValueError("collection Agent returned no completion")
    return StepOutput(content=response.content)


async def build_artifact_step(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Build the complete pending Artifact set from run-scoped Tool Batches."""
    request = _request_from_input(step_input.input)
    prepared = await asyncio.to_thread(build_artifact_set, run_context.run_id, request)
    return StepOutput(content=prepared)


async def publish_collection_step(step_input: StepInput) -> StepOutput:
    """Publish the prepared set and return the canonical collection result."""
    output = step_input.get_step_output("build-artifact-set")
    content = output.content if output is not None else step_input.get_last_step_content()
    if content is None:
        raise ValueError("prepared Artifact step output is missing")
    if isinstance(content, PreparedArtifactSet):
        prepared = content
    else:
        prepared = PreparedArtifactSet.model_validate(content)
    published = await asyncio.to_thread(publish_artifact_set, prepared)
    return StepOutput(content=published)


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
