"""Workflow Function executors for the raw-collection capability."""

import asyncio
import json
import re
import unicodedata
from dataclasses import replace
from typing import Any

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput

from capabilities.collection.internal.acquisition import execute_channel_group
from capabilities.collection.internal.artifacts import build_artifact_set, publish_artifact_set
from capabilities.collection.internal.buffer import read_tool_batches, write_title_curation
from capabilities.collection.internal.channels.models import ChannelType
from capabilities.collection.internal.models import (
    CollectionRequest,
    FetchReceipt,
    TitleCurationDraft,
    TitleCurationItem,
    TitleCurationRequest,
)
from capabilities.collection.internal.source_snapshot import load_active_source_snapshot


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


async def collect_raw_evidence(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Collect the latest channel results and prepare bounded filter input."""
    request = request_from_input(step_input.input)
    channels = await asyncio.to_thread(load_active_source_snapshot)
    execution_dependencies = dict(run_context.dependencies or {})
    execution_dependencies.update(
        {
            "collection_channel_snapshot": tuple(item.model_copy(deep=True) for item in channels),
        }
    )
    acquisition_context = replace(run_context, dependencies=execution_dependencies)
    responses = await asyncio.gather(
        execute_channel_group("web_search", ChannelType.WEB_SEARCH, request.objective, acquisition_context),
        execute_channel_group("api", ChannelType.API, request.objective, acquisition_context),
        execute_channel_group("rss", ChannelType.RSS, request.objective, acquisition_context),
    )
    receipts: list[FetchReceipt] = []
    for response in responses:
        decoded = json.loads(response)
        if "error" in decoded:
            raise ValueError("collection channel group rejected the deterministic request")
        receipts.append(FetchReceipt.model_validate(decoded))
    candidates = [candidate for batch in read_tool_batches(run_context.run_id) for candidate in batch.candidates]
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("Raw Evidence filter Candidate IDs must be unique")
    return StepOutput(
        content=TitleCurationRequest(
            candidates=[
                TitleCurationItem(
                    candidate_id=candidate.candidate_id,
                    title=_display_text(candidate.title, 1_024),
                    source_name=_display_text(candidate.source_name or candidate.connector, 200),
                    published_at=candidate.published_at,
                    content_excerpt=_display_text(candidate.content or candidate.title, 2_000),
                )
                for candidate in candidates
            ]
        )
    )


def _display_text(value: str, maximum: int) -> str:
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()
    return normalized[:maximum]


async def publish_raw_evidence(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Validate filtering, build immutable Raw Documents and publish the run."""
    request_output = step_input.get_step_output("collect-raw-evidence")
    draft_output = step_input.get_step_output("filter-raw-evidence")
    if request_output is None or request_output.content is None:
        raise ValueError("Raw Evidence filter request is missing")
    if draft_output is None or draft_output.content is None:
        raise ValueError("Raw Evidence Filter output is missing")
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
        raise ValueError("Raw Evidence Filter returned duplicate Candidate IDs")
    missing = sorted(set(expected) - set(actual))
    unknown = sorted(set(actual) - set(expected))
    if missing or unknown:
        raise ValueError(f"Raw Evidence Filter Candidate coverage mismatch: missing={missing}, unknown={unknown}")
    write_title_curation(run_context.run_id, draft)
    collection_request = request_from_input(step_input.input)
    prepared = await asyncio.to_thread(build_artifact_set, run_context.run_id, collection_request)
    published = await asyncio.to_thread(publish_artifact_set, prepared)
    return StepOutput(content=published)
