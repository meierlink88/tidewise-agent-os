"""Deterministic functions backing the three-stage Event Workflow."""

import hashlib
import json
import logging
import traceback
from typing import Any, NoReturn
from uuid import uuid4

from agno.workflow import StepInput, StepOutput

from capabilities.event.internal.models import (
    EventCandidateSubmission,
    EventExtractionBatch,
    EventExtractionBusy,
    EventExtractionDraft,
    EventExtractionIdle,
    EventExtractionResult,
    EventPublicationJournal,
    EventPublicationRecord,
    EventSignalJournal,
)
from capabilities.event.internal.runtime import event_workflow_runtime
from capabilities.event.internal.storage import (
    claim_event_batch,
    complete_batch,
    freeze_draft,
    load_draft,
    load_publication_journal,
    load_signal_journal,
    release_event_batch_lease,
    renew_event_batch_lease,
    write_publication_journal,
    write_signal_journal,
)

logger = logging.getLogger(__name__)


def _raise_stage_failure(stage: str, batch_id: str, error: Exception) -> NoReturn:
    """Emit a safe correlation handle without logging provider payloads or secrets."""

    diagnostic_id = str(uuid4())
    error_types: list[str] = []
    frames: list[str] = []
    current: BaseException | None = error
    while current is not None:
        error_types.append(type(current).__name__)
        frames.extend(
            f"{frame.filename}:{frame.lineno}:{frame.name}"
            for frame in traceback.extract_tb(current.__traceback__)[-3:]
        )
        current = current.__cause__ or current.__context__
    logger.error(
        "event_workflow_stage_failed stage=%s batch_id=%s diagnostic_id=%s error_types=%s frames=%s",
        stage,
        batch_id,
        diagnostic_id,
        "|".join(error_types),
        "|".join(frames),
    )
    raise RuntimeError(f"Event Workflow stage {stage} failed; diagnostic_id={diagnostic_id}") from None


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


async def publish_event_candidates(step_input: StepInput) -> StepOutput:
    """Resolve, publish and project frozen Candidates through the local runtime."""
    batch = _model_from_content(EventExtractionBatch, _step_content(step_input, "prepare-event-batch"))
    try:
        renew_event_batch_lease(batch)
        draft = load_draft(batch.batch_id)
        journal = load_publication_journal(batch.batch_id)
        completed = {item.candidate_key: item for item in journal.publications}
        ordered_keys = [_candidate_key(candidate) for candidate in draft.candidates]
        if not set(completed) <= set(ordered_keys):
            raise ValueError("Event publication journal contains an unknown Candidate key")
        runtime = event_workflow_runtime()

        def checkpoint(record: EventPublicationRecord) -> None:
            nonlocal journal
            records = {item.candidate_key: item for item in journal.publications}
            records[record.candidate_key] = record
            journal = EventPublicationJournal(
                batch_id=batch.batch_id,
                publications=[records[key] for key in ordered_keys if key in records],
            )
            write_publication_journal(journal)

        for candidate, key in zip(draft.candidates, ordered_keys, strict=True):
            existing = completed.get(key)
            if existing is not None and existing.decision in {"SAME_EVENT", "NEEDS_REVIEW"}:
                continue
            if existing is not None and existing.graph_projection_status == "SUCCEEDED":
                continue
            renew_event_batch_lease(batch)
            record = await runtime.publish(
                candidate,
                key,
                existing=existing,
                checkpoint=checkpoint,
            )
            checkpoint(record)
            renew_event_batch_lease(batch)
            completed[key] = record
        return StepOutput(content=journal)
    except Exception as exc:
        release_event_batch_lease(batch)
        _raise_stage_failure("EVENT_PUBLICATION", batch.batch_id, exc)


async def construct_event_signals(step_input: StepInput) -> StepOutput:
    """Build Signal Facts for newly projected Events and complete the frozen batch."""
    batch = _model_from_content(EventExtractionBatch, _step_content(step_input, "prepare-event-batch"))
    try:
        renew_event_batch_lease(batch)
        draft = load_draft(batch.batch_id)
        publications = load_publication_journal(batch.batch_id)
        signals = load_signal_journal(batch.batch_id)
        completed = {item.event_id: item for item in signals.signals}
        runtime = event_workflow_runtime()
        for publication in publications.publications:
            if not publication.event_created or publication.published_event is None:
                continue
            if publication.graph_projection_status != "SUCCEEDED" or publication.episode_uuid is None:
                raise ValueError("new Event must be projected before Signal construction")
            assert publication.event_id is not None
            if publication.event_id in completed:
                continue
            renew_event_batch_lease(batch)
            signal = await runtime.construct_signals(publication)
            completed[signal.event_id] = signal
            signals = EventSignalJournal(
                batch_id=batch.batch_id,
                signals=[completed[key] for key in sorted(completed)],
            )
            write_signal_journal(signals)
            renew_event_batch_lease(batch)

        result = EventExtractionResult(
            batch_id=batch.batch_id,
            evidence_ids=sorted(item.id for item in batch.evidences),
            candidate_count=len(draft.candidates),
            no_event_count=len(draft.no_event),
            needs_review_count=len(draft.needs_review),
            published_event_ids=sorted(
                item.event_id for item in publications.publications if item.event_created and item.event_id is not None
            ),
            duplicate_event_count=sum(item.decision == "SAME_EVENT" for item in publications.publications),
            review_event_count=sum(item.decision == "NEEDS_REVIEW" for item in publications.publications),
            signal_fact_uuids=sorted(fact_uuid for signal in signals.signals for fact_uuid in signal.signal_fact_uuids),
        )
        complete_batch(batch, result)
        return StepOutput(content=result)
    except Exception as exc:
        release_event_batch_lease(batch)
        _raise_stage_failure("SIGNAL_CONSTRUCTION", batch.batch_id, exc)
