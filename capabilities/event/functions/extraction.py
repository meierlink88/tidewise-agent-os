"""Deterministic Workflow functions for Event extraction and handoff."""

import asyncio
import hashlib
import json
from typing import Any

from agno.workflow import StepInput, StepOutput

from capabilities.event.internal.client import post_event_candidate
from capabilities.event.internal.models import (
    EventCandidateAcceptance,
    EventCandidateSubmission,
    EventExtractionBatch,
    EventExtractionBusy,
    EventExtractionDraft,
    EventExtractionIdle,
    EventExtractionResult,
    EventSubmissionJournal,
    EventSubmissionRecord,
)
from capabilities.event.internal.storage import (
    claim_event_batch,
    complete_batch,
    freeze_draft,
    load_draft,
    load_journal,
    release_event_batch_lease,
    renew_event_batch_lease,
    write_journal,
)


def _model_from_content(model: type[Any], content: Any) -> Any:
    if isinstance(content, model):
        return content
    if isinstance(content, str):
        return model.model_validate_json(content)
    return model.model_validate(content)


def _step_content(step_input: StepInput, name: str) -> Any:
    output = step_input.get_step_output(name)
    if output is None or output.content is None:
        raise ValueError(f"required Workflow step output is missing: {name}")
    return output.content


def prepare_event_batch(step_input: StepInput) -> StepOutput:
    """Resume the pending batch or claim mapped local Evidence exactly once."""
    del step_input
    batch = claim_event_batch()
    if batch is None:
        return StepOutput(content=EventExtractionIdle(), stop=True)
    if isinstance(batch, EventExtractionBusy):
        return StepOutput(content=batch, stop=True)
    return StepOutput(content=batch)


def event_batch_requires_analysis(step_input: StepInput) -> bool:
    """Return whether the pending batch still needs the semantic Agent step."""
    batch = _model_from_content(EventExtractionBatch, _step_content(step_input, "prepare-event-batch"))
    return bool(batch.needs_analysis)


def _validate_partition(batch: EventExtractionBatch, draft: EventExtractionDraft) -> EventExtractionDraft:
    expected = {item.id for item in batch.evidences}
    observed: list[str] = []
    normalized_candidates: list[EventCandidateSubmission] = []
    for candidate in draft.candidates:
        normalized = candidate.model_copy(update={"evidence_ids": sorted(candidate.evidence_ids)})
        normalized_candidates.append(normalized)
        observed.extend(normalized.evidence_ids)
    observed.extend(item.evidence_id for item in draft.no_event)
    observed.extend(item.evidence_id for item in draft.needs_review)
    if len(observed) != len(set(observed)) or set(observed) != expected:
        raise ValueError("Event extraction draft must partition every frozen Evidence exactly once")
    return draft.model_copy(update={"candidates": normalized_candidates})


def freeze_event_analysis(step_input: StepInput) -> StepOutput:
    """Validate the Agent result against the frozen batch and persist it immutably."""
    batch = _model_from_content(EventExtractionBatch, _step_content(step_input, "prepare-event-batch"))
    try:
        renew_event_batch_lease(batch)
        draft = _model_from_content(EventExtractionDraft, _step_content(step_input, "analyze-event-batch"))
        frozen = freeze_draft(batch, _validate_partition(batch, draft))
        renew_event_batch_lease(batch)
        return StepOutput(content=frozen)
    except Exception:
        release_event_batch_lease(batch)
        raise


def _candidate_key(candidate: EventCandidateSubmission) -> str:
    encoded = json.dumps(
        candidate.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def submit_event_candidates(step_input: StepInput) -> StepOutput:
    """POST every frozen Candidate separately and journal each reliable acceptance."""
    batch = _model_from_content(EventExtractionBatch, _step_content(step_input, "prepare-event-batch"))
    try:
        renew_event_batch_lease(batch)
        draft = load_draft(batch.batch_id)
        journal = load_journal(batch.batch_id)
        accepted = {item.candidate_key: item for item in journal.submissions}
        ordered_keys = [_candidate_key(candidate) for candidate in draft.candidates]
        if not set(accepted) <= set(ordered_keys):
            raise ValueError("Event submission journal contains an unknown Candidate key")
        for candidate, key in zip(draft.candidates, ordered_keys, strict=True):
            if key in accepted:
                continue
            renew_event_batch_lease(batch)
            response_payload = await asyncio.to_thread(
                post_event_candidate,
                candidate.model_dump(mode="json"),
            )
            try:
                response = EventCandidateAcceptance.model_validate(response_payload)
            except Exception as exc:
                raise ValueError("Reasoning Server acceptance response is invalid") from exc
            record = EventSubmissionRecord(candidate_key=key, **response.model_dump())
            journal = EventSubmissionJournal(
                batch_id=batch.batch_id,
                submissions=[*journal.submissions, record],
            )
            write_journal(journal)
            renew_event_batch_lease(batch)
            accepted[key] = record
        result = EventExtractionResult(
            batch_id=batch.batch_id,
            evidence_ids=sorted(item.id for item in batch.evidences),
            candidate_count=len(draft.candidates),
            no_event_count=len(draft.no_event),
            needs_review_count=len(draft.needs_review),
            submission_ids=[accepted[key].submission_id for key in ordered_keys],
        )
        complete_batch(batch, result)
        return StepOutput(content=result)
    except Exception:
        release_event_batch_lease(batch)
        raise
