"""Deterministic functions backing the three-stage Event Workflow."""

import hashlib
import json
import logging
import traceback
from collections import Counter
from typing import Any, NoReturn
from uuid import uuid4

from agno.workflow import StepInput, StepOutput
from pydantic import ValidationError

from capabilities.event.internal.models import (
    EventCandidateSubmission,
    EventDisposition,
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


def _event_batch_from_input(step_input: StepInput) -> EventExtractionBatch:
    """Read the batch from a direct child call or the completed extraction stage."""

    for name in ("prepare-event-batch", "extract-event-candidates"):
        output = step_input.get_step_output(name)
        if output is None or output.content is None:
            continue
        try:
            return _model_from_content(EventExtractionBatch, output.content)
        except (ValidationError, ValueError, TypeError):
            continue
    raise ValueError("required Workflow Event extraction batch output is missing")


def _event_draft_from_content(content: Any) -> EventExtractionDraft:
    """Normalize the one recoverable LLM contract violation without losing the batch."""

    if isinstance(content, EventExtractionDraft):
        return content
    payload = json.loads(content) if isinstance(content, str) else content
    if not isinstance(payload, dict) or set(payload) != {"candidates", "no_event"}:
        return _model_from_content(EventExtractionDraft, payload)
    candidates_payload = payload.get("candidates")
    no_event_payload = payload.get("no_event")
    if not isinstance(candidates_payload, list) or not isinstance(no_event_payload, list):
        return _model_from_content(EventExtractionDraft, payload)

    candidates: list[EventCandidateSubmission] = []
    dispositions: list[EventDisposition] = []
    for item in candidates_payload:
        if isinstance(item, dict) and isinstance(item.get("event"), dict):
            item = dict(item)
            event = dict(item["event"])
            semantic = event.get("semantic")
            if isinstance(semantic, dict):
                semantic = dict(semantic)
                semantic.setdefault("effective_at", None)
                semantic.setdefault("time_precision", "UNKNOWN")
                event["semantic"] = semantic
            event.setdefault("occurred_at", None)
            event.setdefault("announced_at", None)
            item["event"] = event
        try:
            candidates.append(EventCandidateSubmission.model_validate(item))
        except ValidationError as exc:
            missing_time = bool(exc.errors()) and all(
                error.get("type") == "value_error"
                and "Event requires an occurrence, announcement, or effective time" in str(error.get("msg"))
                for error in exc.errors()
            )
            evidence_ids = item.get("evidence_ids") if isinstance(item, dict) else None
            if not isinstance(evidence_ids, list) or not evidence_ids:
                raise
            reason = "missing_reliable_time" if missing_time else "invalid_event_semantics"
            dispositions.extend(
                EventDisposition(evidence_id=evidence_id, reason=reason) for evidence_id in evidence_ids
            )
    dispositions.extend(EventDisposition.model_validate(item) for item in no_event_payload)
    return EventExtractionDraft(candidates=candidates, no_event=dispositions)


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
    batch = _event_batch_from_input(step_input)
    return bool(batch.needs_analysis)


def _validate_partition(batch: EventExtractionBatch, draft: EventExtractionDraft) -> EventExtractionDraft:
    expected = {item.id for item in batch.evidences}
    supplied = [evidence_id for candidate in draft.candidates for evidence_id in candidate.evidence_ids]
    supplied.extend(item.evidence_id for item in draft.no_event)
    unknown = set(supplied) - expected
    if unknown:
        raise ValueError("Event extraction draft contains Evidence outside the frozen batch")

    candidate_counts = Counter(evidence_id for candidate in draft.candidates for evidence_id in candidate.evidence_ids)
    ambiguous = {evidence_id for evidence_id, count in candidate_counts.items() if count > 1}
    normalized_candidates: list[EventCandidateSubmission] = []
    for candidate in draft.candidates:
        retained = sorted(evidence_id for evidence_id in candidate.evidence_ids if evidence_id not in ambiguous)
        if retained:
            normalized_candidates.append(candidate.model_copy(update={"evidence_ids": retained}))

    candidate_ids = {evidence_id for candidate in normalized_candidates for evidence_id in candidate.evidence_ids}
    dispositions_by_id: dict[str, EventDisposition] = {}
    for disposition in draft.no_event:
        if disposition.evidence_id not in candidate_ids:
            dispositions_by_id.setdefault(disposition.evidence_id, disposition)
    for evidence_id in ambiguous:
        dispositions_by_id[evidence_id] = EventDisposition(
            evidence_id=evidence_id,
            reason="ambiguous_candidate_assignment",
        )
    for evidence_id in expected - candidate_ids - set(dispositions_by_id):
        dispositions_by_id[evidence_id] = EventDisposition(
            evidence_id=evidence_id,
            reason="unassigned_by_model",
        )
    return EventExtractionDraft(
        candidates=normalized_candidates,
        no_event=[dispositions_by_id[evidence_id] for evidence_id in sorted(dispositions_by_id)],
    )


def freeze_event_analysis(step_input: StepInput) -> StepOutput:
    """Validate the Agent result against the frozen batch and persist it immutably."""
    batch = _event_batch_from_input(step_input)
    try:
        renew_event_batch_lease(batch)
        draft = _event_draft_from_content(_step_content(step_input, "analyze-event-batch"))
        freeze_draft(batch, _validate_partition(batch, draft))
        renew_event_batch_lease(batch)
        return StepOutput(content=batch)
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
    batch = _event_batch_from_input(step_input)
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
            if existing is not None and existing.decision in {"SAME_EVENT", "FAILED"}:
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
    batch = _event_batch_from_input(step_input)
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

        publications_by_key = {item.candidate_key: item for item in publications.publications}
        failed_evidence_ids = sorted(
            evidence_id
            for candidate in draft.candidates
            if publications_by_key[_candidate_key(candidate)].decision == "FAILED"
            for evidence_id in candidate.evidence_ids
        )
        result = EventExtractionResult(
            batch_id=batch.batch_id,
            evidence_ids=sorted(item.id for item in batch.evidences),
            candidate_count=len(draft.candidates),
            no_event_count=len(draft.no_event),
            published_event_ids=sorted(
                item.event_id for item in publications.publications if item.event_created and item.event_id is not None
            ),
            duplicate_event_count=sum(item.decision == "SAME_EVENT" for item in publications.publications),
            failed_candidate_count=sum(item.decision == "FAILED" for item in publications.publications),
            failed_evidence_ids=failed_evidence_ids,
            signal_fact_uuids=sorted(fact_uuid for signal in signals.signals for fact_uuid in signal.signal_fact_uuids),
        )
        complete_batch(batch, result)
        return StepOutput(content=result)
    except Exception as exc:
        release_event_batch_lease(batch)
        _raise_stage_failure("SIGNAL_CONSTRUCTION", batch.batch_id, exc)
