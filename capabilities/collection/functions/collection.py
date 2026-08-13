"""Workflow Function executors for the raw-collection capability."""

import asyncio
import json
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput

from capabilities.collection.internal.acquisition import execute_fetch
from capabilities.collection.internal.artifacts import build_artifact_set, publish_artifact_set
from capabilities.collection.internal.buffer import read_tool_batches, write_title_curation
from capabilities.collection.internal.channels.models import ChannelType
from capabilities.collection.internal.channels.repository import get_channel_repository
from capabilities.collection.internal.models import (
    CollectionQueryPlan,
    CollectionRequest,
    FetchReceipt,
    PreparedArtifactSet,
    TitleCurationDraft,
    TitleCurationItem,
    TitleCurationRequest,
)


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


async def prepare_collection_context(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Validate input and freeze one channel/cutoff snapshot before Agent planning."""
    request = request_from_input(step_input.input)
    repository = get_channel_repository()
    channels = await asyncio.to_thread(repository.list_enabled_snapshot)
    dependencies = dict(run_context.dependencies or {})
    dependencies.update(
        {
            "collector_objective": request.objective,
            "collector_cutoff": datetime.now(UTC).isoformat(),
            "collection_channel_snapshot": tuple(item.model_copy(deep=True) for item in channels),
        }
    )
    run_context.dependencies = dependencies
    return StepOutput(content=request.objective)


async def execute_collection_channels_step(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Execute all three acquisition façades exactly once from the frozen run snapshot."""
    output = step_input.get_step_output("plan-collection-query")
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


def _display_title(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


async def prepare_title_curation(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Expose only Candidate IDs and display titles to the curator Agent."""
    del step_input
    candidates = [candidate for batch in read_tool_batches(run_context.run_id) for candidate in batch.candidates]
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("title curation candidate IDs must be unique")
    request = TitleCurationRequest(
        candidates=[
            TitleCurationItem(candidate_id=candidate.candidate_id, title=_display_title(candidate.title))
            for candidate in candidates
        ]
    )
    return StepOutput(content=request)


async def validate_title_curation(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Require one valid title decision for every and only every Candidate."""
    request_output = step_input.get_step_output("prepare-title-curation")
    draft_output = step_input.get_step_output("curate-collection-titles")
    if request_output is None or request_output.content is None:
        raise ValueError("title curation request is missing")
    if draft_output is None or draft_output.content is None:
        raise ValueError("Title Curator output is missing")
    request = (
        request_output.content
        if isinstance(request_output.content, TitleCurationRequest)
        else TitleCurationRequest.model_validate(request_output.content)
    )
    if isinstance(draft_output.content, TitleCurationDraft):
        draft = draft_output.content
    elif isinstance(draft_output.content, str):
        draft = TitleCurationDraft.model_validate_json(draft_output.content)
    else:
        draft = TitleCurationDraft.model_validate(draft_output.content)
    expected = [item.candidate_id for item in request.candidates]
    actual = [item.candidate_id for item in draft.decisions]
    if len(actual) != len(set(actual)):
        raise ValueError("Title Curator returned duplicate Candidate IDs")
    missing = sorted(set(expected) - set(actual))
    unknown = sorted(set(actual) - set(expected))
    if missing or unknown:
        raise ValueError(f"Title Curator Candidate coverage mismatch: missing={missing}, unknown={unknown}")
    write_title_curation(run_context.run_id, draft)
    return StepOutput(content=draft)


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
