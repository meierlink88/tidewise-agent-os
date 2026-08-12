"""Workflow Function executors for the raw-collection capability."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput

from agents.raw_collector import COLLECTOR_AGENT_ID, load_collector_agent
from capabilities.raw_collection.acquisition import execute_fetch
from capabilities.raw_collection.artifacts import build_artifact_set, publish_artifact_set
from capabilities.raw_collection.channels.models import ChannelType
from capabilities.raw_collection.channels.repository import get_channel_repository
from capabilities.raw_collection.models import CollectionQueryPlan, CollectionRequest, FetchReceipt, PreparedArtifactSet


def request_from_input(value: Any) -> CollectionRequest:
    """Validate a Workflow input as a raw-collection request."""
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
    """Validate input, freeze channels, and ask the Agent for one semantic query plan."""
    # Imported lazily so app.registry can register this executor without a cycle.
    from app.registry import registry

    request = request_from_input(step_input.input)
    loaded = await asyncio.to_thread(load_collector_agent, registry)
    repository = get_channel_repository()
    channels = await asyncio.to_thread(repository.list_enabled_snapshot)
    dependencies = dict(run_context.dependencies or {})
    dependencies.update(
        {
            "collector_objective": request.objective,
            "collector_agent_component_id": COLLECTOR_AGENT_ID,
            "collector_agent_config_version": loaded.version,
            "collector_instructions_sha256": loaded.instructions_sha256,
            "collector_cutoff": datetime.now(UTC).isoformat(),
            "collection_channel_snapshot": tuple(item.model_copy(deep=True) for item in channels),
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
    if isinstance(response.content, CollectionQueryPlan):
        plan = response.content
    elif isinstance(response.content, str):
        plan = CollectionQueryPlan.model_validate_json(response.content)
    else:
        plan = CollectionQueryPlan.model_validate(response.content)
    return StepOutput(content=plan)


async def execute_collection_channels_step(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Execute all three acquisition façades exactly once from the frozen run snapshot."""
    output = step_input.get_step_output("agentic-collect")
    content = output.content if output is not None else step_input.get_last_step_content()
    if content is None:
        raise ValueError("collection query plan is missing")
    if isinstance(content, CollectionQueryPlan):
        plan = content
    elif isinstance(content, str):
        plan = CollectionQueryPlan.model_validate_json(content)
    else:
        plan = CollectionQueryPlan.model_validate(content)
    responses = await asyncio.gather(
        execute_fetch("web_fetch", ChannelType.WEB_SEARCH, plan.query, run_context, plan.lookback_hours),
        execute_fetch("api_fetch", ChannelType.API, plan.query, run_context, plan.lookback_hours),
        execute_fetch("rss_fetch", ChannelType.RSS, plan.query, run_context, plan.lookback_hours),
    )
    receipts: list[FetchReceipt] = []
    for response in responses:
        decoded = json.loads(response)
        if "error" in decoded:
            raise ValueError("collection Tool façade rejected the deterministic request")
        receipts.append(FetchReceipt.model_validate(decoded))
    return StepOutput(
        content={"plan": plan.model_dump(mode="json"), "receipts": [item.model_dump(mode="json") for item in receipts]}
    )


async def build_artifact_step(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Build the complete pending Artifact set from run-scoped Tool Batches."""
    request = request_from_input(step_input.input)
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
