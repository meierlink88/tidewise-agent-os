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
from capabilities.collection.internal.buffer import (
    read_title_curation,
    read_title_curation_if_present,
    read_tool_batches,
    write_title_curation,
)
from capabilities.collection.internal.channels.models import ChannelType
from capabilities.collection.internal.models import (
    Candidate,
    CollectionRequest,
    FetchReceipt,
    RawEvidenceFilterProgress,
    TitleCurationDecision,
    TitleCurationDraft,
    TitleCurationItem,
    TitleCurationRequest,
)
from capabilities.collection.internal.source_snapshot import load_active_source_snapshot

RAW_EVIDENCE_FILTER_MAX_CANDIDATES = 25
RAW_EVIDENCE_FILTER_MAX_CONTENT_CHARS = 40_000


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
    """Collect and persist complete channel results, returning only loop progress."""
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
        content=RawEvidenceFilterProgress(
            total_candidates=len(candidates),
            decided_candidates=0,
            remaining_candidates=len(candidates),
        )
    )


def _display_text(value: str, maximum: int) -> str:
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()
    return normalized[:maximum]


def _complete_content(value: str, fallback: str) -> str:
    """Return the complete collected body without normalization or truncation."""
    return value if value.strip() else fallback


def _all_candidates(collection_id: str) -> list[Candidate]:
    candidates = [candidate for batch in read_tool_batches(collection_id) for candidate in batch.candidates]
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("Raw Evidence filter Candidate IDs must be unique")
    return candidates


async def prepare_raw_evidence_filter_batch(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Load the next complete-body filter batch under count and aggregate-size budgets."""
    del step_input
    candidates = _all_candidates(run_context.run_id)
    existing = read_title_curation_if_present(run_context.run_id)
    decided_ids = {item.candidate_id for item in existing.decisions} if existing is not None else set()
    unknown = decided_ids - {item.candidate_id for item in candidates}
    if unknown:
        raise ValueError(f"Raw Evidence filter state contains unknown Candidate IDs: {sorted(unknown)}")

    selected: list[TitleCurationItem] = []
    content_chars = 0
    for candidate in candidates:
        if candidate.candidate_id in decided_ids:
            continue
        content = _complete_content(candidate.content, candidate.title)
        if selected and (
            len(selected) >= RAW_EVIDENCE_FILTER_MAX_CANDIDATES
            or content_chars + len(content) > RAW_EVIDENCE_FILTER_MAX_CONTENT_CHARS
        ):
            break
        selected.append(
            TitleCurationItem(
                candidate_id=candidate.candidate_id,
                title=_display_text(candidate.title, 1_024),
                source_name=_display_text(candidate.source_name or candidate.connector, 200),
                published_at=candidate.published_at,
                content=content,
            )
        )
        content_chars += len(content)
    return StepOutput(content=TitleCurationRequest(candidates=selected))


def _curation_draft(value: Any) -> TitleCurationDraft:
    if isinstance(value, TitleCurationDraft):
        return value
    if isinstance(value, str):
        return TitleCurationDraft.model_validate_json(value)
    return TitleCurationDraft.model_validate(value)


async def save_raw_evidence_filter_batch(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Fail closed on batch coverage and atomically merge one filter result."""
    request_output = step_input.get_step_output("prepare-raw-evidence-filter-batch")
    if request_output is None or request_output.content is None:
        raise ValueError("Raw Evidence filter batch request is missing")
    if step_input.previous_step_content is None:
        raise ValueError("Raw Evidence Filter batch output is missing")
    request = TitleCurationRequest.model_validate(request_output.content)
    draft = _curation_draft(step_input.previous_step_content)

    expected = [item.candidate_id for item in request.candidates]
    actual = [item.candidate_id for item in draft.decisions]
    if len(actual) != len(set(actual)):
        raise ValueError("Raw Evidence Filter returned duplicate Candidate IDs")
    missing = sorted(set(expected) - set(actual))
    unknown = sorted(set(actual) - set(expected))
    if missing or unknown:
        raise ValueError(f"Raw Evidence Filter Candidate coverage mismatch: missing={missing}, unknown={unknown}")

    candidates = _all_candidates(run_context.run_id)
    all_ids = [item.candidate_id for item in candidates]
    existing = read_title_curation_if_present(run_context.run_id)
    existing_decisions = list(existing.decisions) if existing is not None else []
    existing_ids = [item.candidate_id for item in existing_decisions]
    if len(existing_ids) != len(set(existing_ids)):
        raise ValueError("Raw Evidence filter state contains duplicate Candidate IDs")
    overlap = sorted(set(existing_ids) & set(actual))
    if overlap:
        raise ValueError(f"Raw Evidence Filter attempted to decide Candidate IDs twice: {overlap}")

    decisions_by_id: dict[str, TitleCurationDecision] = {
        item.candidate_id: item for item in [*existing_decisions, *draft.decisions]
    }
    unknown_state = sorted(set(decisions_by_id) - set(all_ids))
    if unknown_state:
        raise ValueError(f"Raw Evidence filter state contains unknown Candidate IDs: {unknown_state}")
    merged = TitleCurationDraft(decisions=[decisions_by_id[item] for item in all_ids if item in decisions_by_id])
    write_title_curation(run_context.run_id, merged)
    remaining = len(all_ids) - len(merged.decisions)
    return StepOutput(
        content=RawEvidenceFilterProgress(
            total_candidates=len(all_ids),
            decided_candidates=len(merged.decisions),
            remaining_candidates=remaining,
        )
    )


def raw_evidence_filter_complete(iteration_outputs: list[StepOutput]) -> bool:
    """Stop the native Agno Loop only after every collected Candidate is decided."""
    if not iteration_outputs or iteration_outputs[-1].content is None:
        return False
    progress = RawEvidenceFilterProgress.model_validate(iteration_outputs[-1].content)
    return progress.remaining_candidates == 0


async def publish_raw_evidence(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Validate filtering, build immutable Raw Documents and publish the run."""
    draft = read_title_curation(run_context.run_id)
    expected = [item.candidate_id for item in _all_candidates(run_context.run_id)]
    actual = [item.candidate_id for item in draft.decisions]
    if len(actual) != len(set(actual)):
        raise ValueError("Raw Evidence Filter returned duplicate Candidate IDs")
    missing = sorted(set(expected) - set(actual))
    unknown = sorted(set(actual) - set(expected))
    if missing or unknown:
        raise ValueError(f"Raw Evidence Filter Candidate coverage mismatch: missing={missing}, unknown={unknown}")
    collection_request = request_from_input(step_input.input)
    prepared = await asyncio.to_thread(build_artifact_set, run_context.run_id, collection_request)
    published = await asyncio.to_thread(publish_artifact_set, prepared)
    return StepOutput(content=published)
