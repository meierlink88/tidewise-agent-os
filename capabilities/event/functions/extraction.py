"""Deterministic Functions backing the five-phase Event Workflow."""

from __future__ import annotations

import hashlib
import json
import logging
import traceback
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn, cast
from uuid import uuid4

from agno.run import RunContext
from agno.run.base import RunStatus
from agno.workflow import StepInput, StepOutput
from pydantic import ValidationError

from capabilities.event.internal.identity import same_occurrence
from capabilities.event.internal.models import (
    EVENT_AGENT_IDS,
    EVENT_EXTRACTOR_AGENT_ID,
    EVENT_IDENTITY_AGENT_ID,
    EVENT_SIGNAL_ANALYST_AGENT_ID,
    EventAgentExecutionRecord,
    EventAgentVersions,
    EventCandidateSubmission,
    EventDisposition,
    EventExtractionBatch,
    EventExtractionBusy,
    EventExtractionDraft,
    EventExtractionIdle,
    EventExtractionResult,
    EventIdentityDecision,
    EventIdentityRequest,
    EventPublicationJournal,
    EventPublicationRecord,
    EventResolutionRecord,
    EventSignalAnalysisDraft,
    EventSignalAnalysisRecord,
    EventSignalAnalysisRequest,
    EventSignalCandidateRecord,
    EventSignalClassificationRecord,
    EventSignalClassificationRequest,
    EventSignalJournal,
    EventSignalProjectionRecord,
    EventSignalRecord,
    EventWorkflowProgress,
)
from capabilities.event.internal.queue import pending_queue_items
from capabilities.event.internal.review import ControlledSignalReviewer
from capabilities.event.internal.runtime import event_workflow_runtime
from capabilities.event.internal.storage import (
    claim_event_batch,
    complete_batch,
    freeze_agent_execution,
    freeze_draft,
    freeze_identity_request,
    freeze_resolution,
    freeze_signal_analysis,
    freeze_signal_candidates,
    freeze_signal_classification,
    freeze_signal_preparation,
    freeze_signal_projection,
    load_draft,
    load_identity_request_journal,
    load_publication_journal,
    load_resolution_journal,
    load_signal_analysis_journal,
    load_signal_candidate_journal,
    load_signal_classification_journal,
    load_signal_journal,
    load_signal_preparation_journal,
    load_signal_projection_journal,
    release_event_batch_lease,
    renew_event_batch_lease,
    write_publication_journal,
    write_signal_journal,
)
from sematica.analysis.event.contracts import EventAnalysisInput, EventClassification, SignalProposal
from sematica.analysis.event.errors import PermanentEventAnalysisFailure
from sematica.ingestion.episcode.event.adapters import PublicationRejected
from sematica.ingestion.episcode.event.contracts import EventCandidateDTO, HistoricalEvent

logger = logging.getLogger(__name__)

_EVENT_RUN_STATE = "event_workflow_state"
_BATCH = "batch"
_IDENTITY_REQUEST = "identity_request"
_SIGNAL_REQUEST = "signal_request"
_EVENT_AGENT_EXECUTION_VERSIONS = "event_agent_execution_versions"
_EVENT_RESOLUTION_LIMIT = 50
_EVENT_SIGNAL_TASK_LIMIT = _EVENT_RESOLUTION_LIMIT * 2


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


def _previous_content(step_input: StepInput) -> Any:
    """Return direct predecessor content without coupling to a display name."""

    content = step_input.previous_step_content
    if content is None:
        content = step_input.get_last_step_content()
    if content is None:
        raise ValueError("required previous Workflow step output is missing")
    return content


def _direct_predecessor(step_input: StepInput) -> StepOutput:
    """Return the last actual Step output by order, never by mutable display name."""

    outputs = step_input.previous_step_outputs
    if outputs:
        return list(outputs.values())[-1]
    return StepOutput(content=_previous_content(step_input))


def _pinned_agent_versions(run_context: RunContext) -> dict[str, int]:
    """Return the complete immutable Agent binding stored on this Workflow run."""

    metadata = run_context.metadata
    versions = metadata.get("event_agent_versions") if isinstance(metadata, dict) else None
    if not isinstance(versions, dict) or set(versions) != EVENT_AGENT_IDS:
        raise ValueError("Event Workflow run metadata does not contain all exact Agent versions")
    try:
        pinned = EventAgentVersions.model_validate(versions)
    except ValidationError as exc:
        raise ValueError("Event Workflow run metadata contains an invalid Agent version") from exc
    return pinned.as_mapping()


async def _invoke_pinned_agent(
    agent_id: str,
    request: Any,
    run_context: RunContext,
    *,
    operation_key: str,
) -> Any:
    """Freeze and invoke one exact Agent version, then mirror it in run metadata."""

    versions = _pinned_agent_versions(run_context)
    version = versions[agent_id]
    freeze_agent_execution(
        _batch(run_context),
        EventAgentExecutionRecord(
            operation_key=operation_key,
            agent_id=agent_id,
            version=version,
        ),
    )
    metadata = run_context.metadata
    assert metadata is not None
    execution_versions = metadata.setdefault(_EVENT_AGENT_EXECUTION_VERSIONS, {})
    if not isinstance(execution_versions, dict):
        raise ValueError("Event Workflow Agent execution audit metadata is invalid")
    existing = execution_versions.get(agent_id)
    if existing is not None and existing != version:
        raise ValueError("Event Workflow Agent execution version changed during one run")
    execution_versions[agent_id] = version
    response = await event_workflow_runtime().invoke_agent(agent_id, version, request, run_context)
    if response.status != RunStatus.completed:
        raise RuntimeError(f"pinned Event Agent {agent_id}@{version} did not complete")
    return response.content


def _agent_predecessor(content: Any) -> StepInput:
    """Build an internal predecessor handoff without using an editable display name."""

    return StepInput(previous_step_outputs={"agent_result": StepOutput(content=content)})


def _event_run_state(run_context: RunContext) -> dict[str, Any]:
    """Return mutable state shared by shallow RunContext copies in one run."""

    dependencies = run_context.dependencies
    if dependencies is not None:
        state = dependencies.get(_EVENT_RUN_STATE)
        if not isinstance(state, dict):
            state = {}
            dependencies[_EVENT_RUN_STATE] = state
        return cast(dict[str, Any], state)
    session_state = run_context.session_state
    if session_state is None:
        session_state = {}
        run_context.session_state = session_state
    state = session_state.get(_EVENT_RUN_STATE)
    if not isinstance(state, dict) or state.get("run_id") != run_context.run_id:
        state = {"run_id": run_context.run_id}
        session_state[_EVENT_RUN_STATE] = state
    return cast(dict[str, Any], state)


def _batch(run_context: RunContext) -> EventExtractionBatch:
    payload = _event_run_state(run_context).get(_BATCH)
    if payload is None:
        raise ValueError("run-scoped Event extraction batch is missing")
    return EventExtractionBatch.model_validate(payload)


def _event_draft_from_content(content: Any) -> EventExtractionDraft:
    """Normalize recoverable Candidate defects without weakening the formal contract."""

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
        try:
            candidates.append(EventCandidateSubmission.model_validate(item))
        except ValidationError as exc:
            missing_time = bool(exc.errors()) and all(
                error.get("type") == "value_error"
                and "Event time requires an occurrence, announcement, or effective time" in str(error.get("msg"))
                for error in exc.errors()
            )
            evidence_ids = item.get("evidence_ids") if isinstance(item, dict) else None
            reason = "missing_reliable_time" if missing_time else "invalid_event_semantics"
            if isinstance(evidence_ids, list):
                for evidence_id in evidence_ids:
                    try:
                        dispositions.append(EventDisposition(evidence_id=evidence_id, reason=reason))
                    except ValidationError:
                        continue
    for item in no_event_payload:
        try:
            dispositions.append(EventDisposition.model_validate(item))
        except ValidationError:
            continue
    return EventExtractionDraft(candidates=candidates, no_event=dispositions)


def _validate_partition(batch: EventExtractionBatch, draft: EventExtractionDraft) -> EventExtractionDraft:
    expected = {item.id for item in batch.evidences}
    evidence_by_id = {item.id: item for item in batch.evidences}
    occurrence_merged: list[EventCandidateSubmission] = []
    for candidate in draft.candidates:
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(occurrence_merged)
                if same_occurrence(candidate.event, existing.event)
            ),
            None,
        )
        if duplicate_index is None:
            occurrence_merged.append(candidate)
            continue
        existing = occurrence_merged[duplicate_index]
        occurrence_merged[duplicate_index] = existing.model_copy(
            update={"evidence_ids": sorted(set(existing.evidence_ids) | set(candidate.evidence_ids))}
        )

    candidate_counts = Counter(
        evidence_id
        for candidate in occurrence_merged
        for evidence_id in candidate.evidence_ids
        if evidence_id in expected
    )
    ambiguous = {evidence_id for evidence_id, count in candidate_counts.items() if count > 1}
    normalized_candidates: list[EventCandidateSubmission] = []
    for candidate in occurrence_merged:
        retained = sorted(
            evidence_id
            for evidence_id in candidate.evidence_ids
            if evidence_id in expected and evidence_id not in ambiguous
        )
        if retained:
            supporting_semantics = [evidence_by_id[evidence_id].semantic for evidence_id in retained]

            def compatible_text(field_name: Literal["reason", "method"]) -> str | None:
                # The pinned Extractor owns semantic compatibility. This gate
                # admits only its verbatim Evidence-supported choice, recovers
                # a sole source value, and otherwise preserves a null conflict
                # disposition instead of accepting an invented paraphrase.
                values = sorted(
                    {value for semantic in supporting_semantics if (value := getattr(semantic, field_name)) is not None}
                )
                proposed = getattr(candidate.event.semantic, field_name)
                if proposed in values:
                    return proposed
                return values[0] if len(values) == 1 else None

            metrics = [metric for semantic in supporting_semantics for metric in semantic.metrics]
            semantic_payload = candidate.event.semantic.model_dump(mode="json")
            semantic_payload.update(
                reason=compatible_text("reason"),
                method=compatible_text("method"),
                metrics=[metric.model_dump(mode="json") for metric in metrics],
            )
            event_payload = candidate.event.model_dump(mode="json")
            event_payload["semantic"] = semantic_payload
            normalized_candidates.append(
                EventCandidateSubmission.model_validate(
                    {
                        "event": event_payload,
                        "evidence_ids": retained,
                    }
                )
            )

    candidate_ids = {evidence_id for candidate in normalized_candidates for evidence_id in candidate.evidence_ids}
    dispositions_by_id: dict[str, EventDisposition] = {}
    for disposition in draft.no_event:
        if disposition.evidence_id in expected and disposition.evidence_id not in candidate_ids:
            dispositions_by_id.setdefault(disposition.evidence_id, disposition)
    for evidence_id in ambiguous:
        dispositions_by_id[evidence_id] = EventDisposition(
            evidence_id=evidence_id,
            reason="ambiguous_candidate_assignment",
        )
    for evidence_id in expected - candidate_ids - set(dispositions_by_id):
        dispositions_by_id[evidence_id] = EventDisposition(evidence_id=evidence_id, reason="unassigned_by_model")
    return EventExtractionDraft(
        candidates=normalized_candidates,
        no_event=[dispositions_by_id[evidence_id] for evidence_id in sorted(dispositions_by_id)],
    )


def _candidate_key(candidate: EventCandidateSubmission) -> str:
    """Keep the v8 journal identity stable while the public Event wire evolves."""

    event = candidate.event
    time = event.semantic.time
    stable_v8_projection = {
        "event": {
            "title": event.title,
            "summary": event.summary,
            "semantic": {
                "actors": event.semantic.actors,
                "action": event.semantic.action,
                "objects": event.semantic.objects,
                "stage": event.semantic.stage,
                "jurisdictions": event.semantic.jurisdictions,
                "effective_at": time.effective_at,
                "time_precision": time.precision,
            },
            "modality": event.semantic.modality,
            "occurred_at": time.occurred_at,
            "announced_at": time.announced_at,
        },
        "evidence_ids": candidate.evidence_ids,
    }
    encoded = json.dumps(
        stable_v8_projection,
        default=lambda value: value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _recover_pre_v8_publication_checkpoints(batch: EventExtractionBatch) -> None:
    """Rebuild missing identity decisions from durable pre-v8 publication ACKs.

    Older pending batches could contain a Data or Graphiti checkpoint without a
    separate resolution journal. Re-running identity after that external write
    can relabel the Candidate as a duplicate and skip its unfinished Episode.
    """

    draft_keys = {_candidate_key(candidate) for candidate in load_draft(batch.batch_id).candidates}
    publication_records = load_publication_journal(batch.batch_id).publications
    if any(record.candidate_key not in draft_keys for record in publication_records):
        raise ValueError("legacy Event publication checkpoint does not belong to the frozen draft")
    resolutions = {item.candidate_key: item for item in load_resolution_journal(batch.batch_id).resolutions}
    resolved = set(resolutions)
    for publication in publication_records:
        if publication.candidate_key in resolved:
            resolution = resolutions[publication.candidate_key]
            compatible = publication.decision == resolution.decision or (
                publication.decision == "FAILED"
                and resolution.decision in {"NEW_EVENT", "RELATED_BUT_DISTINCT", "IGNORED"}
            )
            if not compatible or publication.matched_event_ids != resolution.matched_event_ids:
                raise ValueError("durable Event publication checkpoint conflicts with its frozen resolution")
            continue
        decision = publication.decision if publication.decision != "FAILED" else "IGNORED"
        matched_event_ids = list(publication.matched_event_ids)
        if decision == "SAME_EVENT" and not matched_event_ids and publication.event_id is not None:
            matched_event_ids = [publication.event_id]
        if decision == "NEW_EVENT":
            matched_event_ids = []
        freeze_resolution(
            batch,
            EventResolutionRecord(
                candidate_key=publication.candidate_key,
                decision=decision,
                atomic=True,
                matched_event_ids=matched_event_ids,
                reason_codes=publication.reason_codes or ["RECOVERED_PUBLICATION_CHECKPOINT"],
                summary="Recovered from a durable pre-v8 publication checkpoint.",
            ),
        )
        resolved.add(publication.candidate_key)


async def prepare_event_extraction(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Claim one bounded batch and expose it directly to the Event Extractor Agent."""

    del step_input
    claimed = claim_event_batch()
    if claimed is None:
        return StepOutput(content=EventExtractionIdle(), stop=True)
    if isinstance(claimed, EventExtractionBusy):
        return StepOutput(content=claimed, stop=True)
    try:
        renew_event_batch_lease(claimed)
        if not claimed.needs_analysis:
            _recover_pre_v8_publication_checkpoints(claimed)
        _event_run_state(run_context)[_BATCH] = claimed.model_dump(mode="json")
        return StepOutput(content=claimed)
    except Exception as exc:
        release_event_batch_lease(claimed)
        _raise_stage_failure("EVENT_PREPARATION", claimed.batch_id, exc)


def event_extraction_required(step_input: StepInput, run_context: RunContext) -> bool:
    """Skip the semantic Agent when a crash-safe draft already exists."""

    del step_input
    return _batch(run_context).needs_analysis


def freeze_event_extraction(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Validate the direct Agent output and freeze a complete Evidence partition."""

    batch = _batch(run_context)
    predecessor = _direct_predecessor(step_input)
    try:
        if not predecessor.success:
            raise RuntimeError("Event Extractor Agent did not complete")
        try:
            draft = _event_draft_from_content(predecessor.content)
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
            draft = EventExtractionDraft(
                candidates=[],
                no_event=[
                    EventDisposition(evidence_id=evidence.id, reason="noncompliant_event_extraction")
                    for evidence in batch.evidences
                ],
            )
        frozen = freeze_draft(batch, _validate_partition(batch, draft))
        renew_event_batch_lease(batch)
        return StepOutput(content=frozen)
    except Exception as exc:
        release_event_batch_lease(batch)
        _raise_stage_failure("EVENT_EXTRACTION", batch.batch_id, exc)


def has_pending_event_resolution(step_input: StepInput, run_context: RunContext) -> bool:
    """Run the identity phase only while frozen Candidates remain unresolved."""

    del step_input
    batch = _batch(run_context)
    draft = load_draft(batch.batch_id)
    resolved = {item.candidate_key for item in load_resolution_journal(batch.batch_id).resolutions}
    return any(_candidate_key(candidate) not in resolved for candidate in draft.candidates)


async def prepare_event_resolution(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Freeze one bounded identity request before invoking the Event Identity Agent."""

    del step_input
    batch = _batch(run_context)
    try:
        renew_event_batch_lease(batch)
        draft = load_draft(batch.batch_id)
        resolved = {item.candidate_key for item in load_resolution_journal(batch.batch_id).resolutions}
        pending = [(candidate, _candidate_key(candidate)) for candidate in draft.candidates]
        candidate, key = next(item for item in pending if item[1] not in resolved)
        prepared = {item.candidate_key: item for item in load_identity_request_journal(batch.batch_id).requests}.get(
            key
        )
        if prepared is None:
            history = await event_workflow_runtime().retrieve_history(candidate)
            prepared = freeze_identity_request(
                batch,
                EventIdentityRequest(
                    candidate_key=key,
                    candidate=candidate,
                    historical_candidates=history,
                ),
            )
        _event_run_state(run_context)[_IDENTITY_REQUEST] = prepared.model_dump(mode="json")
        renew_event_batch_lease(batch)
        return StepOutput(content=prepared)
    except Exception as exc:
        release_event_batch_lease(batch)
        _raise_stage_failure("EVENT_IDENTITY_PREPARATION", batch.batch_id, exc)


def _validated_resolution(request: EventIdentityRequest, content: Any) -> EventResolutionRecord:
    history_by_id = {item.id: item for item in request.historical_candidates}
    exact_ids = sorted(
        item.id
        for item in request.historical_candidates
        if same_occurrence(
            EventCandidateDTO.model_validate(request.candidate.event.model_dump(mode="json")),
            item.event,
        )
    )
    if len(exact_ids) > 1:
        return EventResolutionRecord(
            candidate_key=request.candidate_key,
            decision="IGNORED",
            atomic=True,
            matched_event_ids=exact_ids,
            reason_codes=["MULTIPLE_STRONG_EVENT_MATCHES"],
            summary="Multiple exact historical Event identities conflict.",
        )
    if len(exact_ids) == 1:
        return EventResolutionRecord(
            candidate_key=request.candidate_key,
            decision="SAME_EVENT",
            atomic=True,
            matched_event_ids=exact_ids,
            reason_codes=["SAME_REAL_WORLD_OCCURRENCE"],
            summary="The exact formal Event occurrence already exists.",
        )
    try:
        decision = _model_from_content(EventIdentityDecision, content)
        if not set(decision.matched_event_ids) <= set(history_by_id):
            raise ValueError("Event Identity referenced a historical Event outside the frozen request")
        return EventResolutionRecord(candidate_key=request.candidate_key, **decision.model_dump())
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
        return EventResolutionRecord(
            candidate_key=request.candidate_key,
            decision="IGNORED",
            atomic=False,
            matched_event_ids=[],
            reason_codes=["NONCOMPLIANT_IDENTITY_OUTPUT"],
            summary="The Event Identity Agent output did not satisfy the frozen contract.",
        )


def persist_event_resolution(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Freeze one validated identity decision without any external side effect."""

    batch = _batch(run_context)
    predecessor = _direct_predecessor(step_input)
    try:
        if not predecessor.success:
            raise RuntimeError("Event Identity Agent did not complete")
        payload = _event_run_state(run_context).get(_IDENTITY_REQUEST)
        if payload is None:
            raise ValueError("run-scoped Event Identity request is missing")
        request = EventIdentityRequest.model_validate(payload)
        freeze_resolution(batch, _validated_resolution(request, predecessor.content))
        renew_event_batch_lease(batch)
        total = len(load_draft(batch.batch_id).candidates)
        processed = len(load_resolution_journal(batch.batch_id).resolutions)
        return StepOutput(
            content=EventWorkflowProgress(
                phase="RESOLVE_EVENTS",
                processed=processed,
                total=total,
                done=processed == total,
            )
        )
    except Exception as exc:
        release_event_batch_lease(batch)
        _raise_stage_failure("EVENT_IDENTITY", batch.batch_id, exc)


def event_resolution_complete(iteration_outputs: list[StepOutput]) -> bool:
    return _phase_complete(iteration_outputs, "RESOLVE_EVENTS")


def _phase_complete(iteration_outputs: list[StepOutput], phase: str) -> bool:
    for output in reversed(iteration_outputs):
        candidates = [output, *(reversed(output.steps or []))]
        for candidate in candidates:
            try:
                progress = _model_from_content(EventWorkflowProgress, candidate.content)
            except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if progress.phase == phase:
                return bool(progress.done)
    return False


async def publish_events(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Apply frozen resolutions, then checkpoint Data and native Event Episode writes."""

    del step_input
    batch = _batch(run_context)
    try:
        renew_event_batch_lease(batch)
        draft = load_draft(batch.batch_id)
        resolutions = {item.candidate_key: item for item in load_resolution_journal(batch.batch_id).resolutions}
        journal = load_publication_journal(batch.batch_id)
        ordered = [(candidate, _candidate_key(candidate)) for candidate in draft.candidates]
        if set(resolutions) != {key for _, key in ordered}:
            raise ValueError("every Event Candidate must have one frozen resolution before publication")

        def checkpoint(record: EventPublicationRecord) -> None:
            nonlocal journal
            records = {item.candidate_key: item for item in journal.publications}
            records[record.candidate_key] = record
            journal = EventPublicationJournal(
                batch_id=batch.batch_id,
                publications=[records[key] for _, key in ordered if key in records],
            )
            write_publication_journal(batch, journal)

        runtime = event_workflow_runtime()
        for candidate, key in ordered:
            resolution = resolutions[key]
            existing = {item.candidate_key: item for item in journal.publications}.get(key)
            if existing is not None and (
                existing.decision in {"SAME_EVENT", "IGNORED", "FAILED"}
                or existing.graph_projection_status == "SUCCEEDED"
            ):
                continue
            renew_event_batch_lease(batch)
            if resolution.decision == "SAME_EVENT":
                checkpoint(
                    EventPublicationRecord(
                        candidate_key=key,
                        decision="SAME_EVENT",
                        event_id=resolution.matched_event_ids[0],
                        event_created=False,
                        evidence_link_result="IGNORED",
                        graph_projection_status="IGNORED",
                        reason_codes=resolution.reason_codes,
                        matched_event_ids=resolution.matched_event_ids,
                    )
                )
                continue
            if resolution.decision == "IGNORED":
                checkpoint(
                    EventPublicationRecord(
                        candidate_key=key,
                        decision="IGNORED",
                        event_id=None,
                        event_created=False,
                        evidence_link_result="NOT_ATTEMPTED",
                        graph_projection_status="NOT_ATTEMPTED",
                        reason_codes=resolution.reason_codes,
                        matched_event_ids=resolution.matched_event_ids,
                    )
                )
                continue
            try:
                checkpoint(
                    await runtime.publish(
                        candidate,
                        key,
                        resolution,
                        existing=existing,
                        checkpoint=checkpoint,
                    )
                )
            except PublicationRejected:
                checkpoint(
                    EventPublicationRecord(
                        candidate_key=key,
                        decision="FAILED",
                        publication_started=True,
                        event_id=None,
                        event_created=False,
                        evidence_link_result="NOT_ATTEMPTED",
                        graph_projection_status="NOT_ATTEMPTED",
                        reason_codes=["DATA_PUBLICATION_REJECTED"],
                        matched_event_ids=resolution.matched_event_ids,
                    )
                )
            renew_event_batch_lease(batch)
        return StepOutput(content=journal)
    except Exception as exc:
        release_event_batch_lease(batch)
        _raise_stage_failure("EVENT_PUBLICATION", batch.batch_id, exc)


def has_pending_signal_analysis(step_input: StepInput, run_context: RunContext) -> bool:
    """Analyze only successfully projected Events created by this frozen batch."""

    del step_input
    batch = _batch(run_context)
    projected = {
        item.event_id
        for item in load_publication_journal(batch.batch_id).publications
        if item.event_created
        and item.event_id is not None
        and item.graph_projection_status == "SUCCEEDED"
        and item.published_event is not None
    }
    terminal = _terminal_signal_event_ids(batch.batch_id)
    return bool(projected - terminal)


def _terminal_signal_event_ids(batch_id: str) -> set[str]:
    """Recognize both current analysis records and legacy terminal Signal records."""

    return {
        *(item.event_id for item in load_signal_analysis_journal(batch_id).analyses),
        *(item.event_id for item in load_signal_journal(batch_id).signals),
    }


def _analysis_input(publication: EventPublicationRecord) -> EventAnalysisInput:
    if publication.published_event is None or publication.episode_uuid is None:
        raise ValueError("Signal analysis requires a projected formal Event")
    historical = HistoricalEvent(
        id=publication.published_event.id,
        event=EventCandidateDTO.model_validate(publication.published_event.event.model_dump(mode="json")),
    )
    return EventAnalysisInput(event=historical, episode_uuid=publication.episode_uuid, reference_time=datetime.now(UTC))


async def prepare_signal_task(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Prepare either classification or proposal work for one projected Event."""

    del step_input
    batch = _batch(run_context)
    try:
        renew_event_batch_lease(batch)
        publications = [
            item
            for item in load_publication_journal(batch.batch_id).publications
            if item.event_created
            and item.event_id is not None
            and item.graph_projection_status == "SUCCEEDED"
            and item.published_event is not None
        ]
        terminal = _terminal_signal_event_ids(batch.batch_id)
        publication = next(item for item in publications if item.event_id not in terminal)
        assert publication.event_id is not None
        prepared = {item.event.id: item for item in load_signal_preparation_journal(batch.batch_id).analyses}.get(
            publication.event_id
        )
        if prepared is None:
            prepared = freeze_signal_preparation(batch, _analysis_input(publication))
        classified = {
            item.event_id: item for item in load_signal_classification_journal(batch.batch_id).classifications
        }.get(publication.event_id)
        request: EventSignalClassificationRequest | EventSignalAnalysisRequest
        if classified is None:
            request = EventSignalClassificationRequest(analysis=prepared)
        else:
            candidate_set = {
                item.event_id: item for item in load_signal_candidate_journal(batch.batch_id).candidates
            }.get(publication.event_id)
            if candidate_set is None:
                candidate_set = freeze_signal_candidates(
                    batch,
                    EventSignalCandidateRecord(
                        event_id=publication.event_id,
                        candidates=await event_workflow_runtime().retrieve_signal_candidates(
                            prepared,
                            classified.classification,
                        ),
                    ),
                )
            request = EventSignalAnalysisRequest(
                analysis=prepared,
                classification=classified.classification,
                candidates=candidate_set.candidates,
            )
        _event_run_state(run_context)[_SIGNAL_REQUEST] = request.model_dump(mode="json")
        renew_event_batch_lease(batch)
        return StepOutput(content=request)
    except Exception as exc:
        release_event_batch_lease(batch)
        _raise_stage_failure("SIGNAL_PREPARATION", batch.batch_id, exc)


def _signal_request(run_context: RunContext) -> EventSignalClassificationRequest | EventSignalAnalysisRequest:
    payload = _event_run_state(run_context).get(_SIGNAL_REQUEST)
    if not isinstance(payload, dict):
        raise ValueError("run-scoped Event Signal request is missing")
    if payload.get("task") == "CLASSIFY":
        return EventSignalClassificationRequest.model_validate(payload)
    return EventSignalAnalysisRequest.model_validate(payload)


def _noncompliant_signal(
    event_id: str,
    classification: EventClassification | None = None,
) -> EventSignalAnalysisRecord:
    return EventSignalAnalysisRecord(
        event_id=event_id,
        status="NONCOMPLIANT",
        classification=classification,
        proposals=[],
        reason_codes=["NONCOMPLIANT_SIGNAL_OUTPUT"],
    )


def _persist_classification(
    batch: EventExtractionBatch,
    request: EventSignalClassificationRequest,
    content: Any,
) -> None:
    try:
        draft = _model_from_content(EventSignalAnalysisDraft, content)
        if draft.proposals:
            raise ValueError("classification pass cannot propose Signals")
        if draft.no_signal_reason is not None:
            raise ValueError("classification pass cannot decide that no Signal exists")
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
        freeze_signal_analysis(batch, _noncompliant_signal(request.analysis.event.id))
        return
    freeze_signal_classification(
        batch,
        EventSignalClassificationRecord(
            event_id=request.analysis.event.id,
            classification=draft.classification,
        ),
    )


async def _validated_signal_analysis(
    request: EventSignalAnalysisRequest,
    content: Any,
) -> EventSignalAnalysisRecord:
    event_id = request.analysis.event.id
    try:
        draft = _model_from_content(EventSignalAnalysisDraft, content)
        if draft.classification != request.classification:
            raise ValueError("Signal Agent changed the frozen Event classification")
        if draft.proposals and draft.no_signal_reason is not None:
            raise ValueError("Signal proposals cannot coexist with a no-Signal reason")
        if not draft.proposals and not (draft.no_signal_reason or "").strip():
            raise ValueError("an empty Signal proposal set requires a no-Signal reason")
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
        return _noncompliant_signal(event_id, request.classification)

    anchors = {item.uuid: item for item in request.candidates.anchors}
    variables = {item.uuid: item for item in request.candidates.variables}
    event_time = (
        request.analysis.event.event.semantic.time.occurred_at
        or request.analysis.event.event.semantic.time.announced_at
        or request.analysis.event.event.semantic.time.effective_at
    )
    assert event_time is not None
    assertion_modality = cast(
        Literal["ACTUAL", "ANTICIPATED", "SOURCE_FORECAST", "ASSUMED"],
        {"FACT": "ACTUAL", "PLAN": "ANTICIPATED", "SPEC": "ASSUMED"}[request.analysis.event.event.semantic.modality],
    )
    reviewer = ControlledSignalReviewer()
    accepted: list[SignalProposal] = []
    pairs: set[tuple[str, str]] = set()
    reason_codes = set(draft.reason_codes)
    for source in draft.proposals:
        pair = (source.anchor_uuid, source.variable_uuid)
        anchor = anchors.get(source.anchor_uuid)
        variable = variables.get(source.variable_uuid)
        if anchor is None or variable is None or pair in pairs:
            reason_codes.add("SIGNAL_REVIEW_REJECTED")
            continue
        pairs.add(pair)
        try:
            proposal = source.proposal(
                event_time=event_time,
                reference_time=request.analysis.reference_time,
                assertion_modality=assertion_modality,
            )
        except (ValidationError, ValueError, TypeError):
            reason_codes.add("SIGNAL_REVIEW_REJECTED")
            continue
        if not await reviewer.review(request.analysis, request.classification, proposal, variable, anchor):
            reason_codes.add("SIGNAL_REVIEW_REJECTED")
            continue
        accepted.append(proposal)

    if accepted:
        status = "SUCCEEDED"
        reason_codes.add("DIRECT_SIGNAL_FACTS_VALIDATED")
    elif not request.candidates.anchors or not request.candidates.variables:
        status = "NO_SUPPORTED_ANCHOR"
        reason_codes.add("NO_SUPPORTED_ANCHOR")
    else:
        status = "NO_SIGNAL"
        reason_codes.add("NO_DIRECT_SIGNAL")
    return EventSignalAnalysisRecord(
        event_id=event_id,
        status=status,
        classification=request.classification,
        proposals=accepted,
        reason_codes=sorted(reason_codes),
    )


async def persist_signal_task(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Freeze classification/candidates or a terminal validated Signal analysis."""

    batch = _batch(run_context)
    predecessor = _direct_predecessor(step_input)
    try:
        if not predecessor.success:
            raise RuntimeError("Event Signal Analyst Agent did not complete")
        request = _signal_request(run_context)
        if isinstance(request, EventSignalClassificationRequest):
            _persist_classification(batch, request, predecessor.content)
        else:
            freeze_signal_analysis(
                batch,
                await _validated_signal_analysis(request, predecessor.content),
            )
        renew_event_batch_lease(batch)
        total = sum(
            item.event_created and item.graph_projection_status == "SUCCEEDED"
            for item in load_publication_journal(batch.batch_id).publications
        )
        processed = len(_terminal_signal_event_ids(batch.batch_id))
        return StepOutput(
            content=EventWorkflowProgress(
                phase="ANALYZE_SIGNALS",
                processed=processed,
                total=total,
                done=processed == total,
            )
        )
    except Exception as exc:
        release_event_batch_lease(batch)
        _raise_stage_failure("SIGNAL_ANALYSIS", batch.batch_id, exc)


def signal_analysis_complete(iteration_outputs: list[StepOutput]) -> bool:
    return _phase_complete(iteration_outputs, "ANALYZE_SIGNALS")


def _proposal_key(event_id: str, proposal: SignalProposal) -> str:
    encoded = json.dumps(
        {"event_id": event_id, "proposal": proposal.model_dump(mode="json")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def publish_signals(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Project validated Signals with per-Fact acknowledgements, then complete the batch."""

    del step_input
    batch = _batch(run_context)
    try:
        renew_event_batch_lease(batch)
        draft = load_draft(batch.batch_id)
        publications = load_publication_journal(batch.batch_id)
        analyses = load_signal_analysis_journal(batch.batch_id)
        preparations = {item.event.id: item for item in load_signal_preparation_journal(batch.batch_id).analyses}
        classifications = {
            item.event_id: item for item in load_signal_classification_journal(batch.batch_id).classifications
        }
        candidate_sets = {item.event_id: item for item in load_signal_candidate_journal(batch.batch_id).candidates}
        signals = load_signal_journal(batch.batch_id)
        terminal = {item.event_id: item for item in signals.signals}
        runtime = event_workflow_runtime()

        for analysis in analyses.analyses:
            if analysis.event_id in terminal:
                continue
            fact_uuids: list[str] = []
            signal_reason_codes = set(analysis.reason_codes)
            if analysis.status == "SUCCEEDED":
                prepared = preparations[analysis.event_id]
                classified = classifications[analysis.event_id]
                candidates = candidate_sets[analysis.event_id].candidates
                anchors = {item.uuid: item for item in candidates.anchors}
                variables = {item.uuid: item for item in candidates.variables}
                projected = {
                    (item.event_id, item.proposal_key): item
                    for item in load_signal_projection_journal(batch.batch_id).projections
                }
                for proposal in analysis.proposals:
                    key = _proposal_key(analysis.event_id, proposal)
                    record = projected.get((analysis.event_id, key))
                    if record is None:
                        renew_event_batch_lease(batch)
                        try:
                            fact_uuid = await runtime.project_signal(
                                prepared,
                                classified.classification,
                                variables[proposal.variable_uuid],
                                anchors[proposal.anchor_uuid],
                                proposal,
                            )
                            pending_record = EventSignalProjectionRecord(
                                event_id=analysis.event_id,
                                proposal_key=key,
                                status="SUCCEEDED",
                                fact_uuid=fact_uuid,
                            )
                        except PermanentEventAnalysisFailure:
                            pending_record = EventSignalProjectionRecord(
                                event_id=analysis.event_id,
                                proposal_key=key,
                                status="REJECTED",
                                reason_code="PERMANENT_SIGNAL_PROJECTION_REJECTED",
                            )
                        record = freeze_signal_projection(
                            batch,
                            pending_record,
                        )
                        projected[(analysis.event_id, key)] = record
                        renew_event_batch_lease(batch)
                    if record.status == "SUCCEEDED":
                        assert record.fact_uuid is not None
                        fact_uuids.append(record.fact_uuid)
                    else:
                        assert record.reason_code is not None
                        signal_reason_codes.add(record.reason_code)
            if analysis.status == "SUCCEEDED":
                public_status = "SUCCEEDED" if fact_uuids else "NO_SIGNAL"
                if not fact_uuids:
                    signal_reason_codes.add("NO_PROJECTABLE_SIGNAL")
            elif analysis.status in {"NO_SIGNAL", "NO_SUPPORTED_ANCHOR"}:
                public_status = analysis.status
            else:
                public_status = "NO_SIGNAL"
            terminal[analysis.event_id] = EventSignalRecord(
                event_id=analysis.event_id,
                status=public_status,
                signal_fact_uuids=sorted(fact_uuids),
                reason_codes=sorted(signal_reason_codes),
            )
            signals = EventSignalJournal(
                batch_id=batch.batch_id,
                signals=[terminal[key] for key in sorted(terminal)],
            )
            write_signal_journal(batch, signals)
            renew_event_batch_lease(batch)

        publications_by_key = {item.candidate_key: item for item in publications.publications}
        failed_evidence_ids = sorted(
            evidence_id
            for candidate in draft.candidates
            if publications_by_key[_candidate_key(candidate)].decision == "FAILED"
            for evidence_id in candidate.evidence_ids
        )
        ignored_evidence_ids = sorted(
            evidence_id
            for candidate in draft.candidates
            if publications_by_key[_candidate_key(candidate)].decision == "IGNORED"
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
            ignored_candidate_count=sum(item.decision == "IGNORED" for item in publications.publications),
            ignored_evidence_ids=ignored_evidence_ids,
            failed_candidate_count=sum(item.decision == "FAILED" for item in publications.publications),
            failed_evidence_ids=failed_evidence_ids,
            signal_fact_uuids=sorted(fact_uuid for signal in signals.signals for fact_uuid in signal.signal_fact_uuids),
        )
        complete_batch(batch, result)
        return StepOutput(content=result, stop=not pending_queue_items())
    except Exception as exc:
        release_event_batch_lease(batch)
        _raise_stage_failure("SIGNAL_PUBLICATION", batch.batch_id, exc)


async def extract_events(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Claim, prepare, analyze and freeze one bounded Event extraction batch."""

    prepared = await prepare_event_extraction(step_input, run_context)
    if prepared.stop:
        return prepared
    batch = _batch(run_context)
    if not batch.needs_analysis:
        return StepOutput(content=load_draft(batch.batch_id))
    try:
        content = await _invoke_pinned_agent(
            EVENT_EXTRACTOR_AGENT_ID,
            batch,
            run_context,
            operation_key="EXTRACT_EVENTS",
        )
    except Exception as exc:
        release_event_batch_lease(batch)
        _raise_stage_failure("EVENT_EXTRACTION", batch.batch_id, exc)
    return freeze_event_extraction(_agent_predecessor(content), run_context)


async def resolve_events(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Resolve every unfrozen Candidate through the exact pinned Event Identity Agent."""

    del step_input
    batch = _batch(run_context)
    draft = load_draft(batch.batch_id)
    last = StepOutput(
        content=EventWorkflowProgress(
            phase="RESOLVE_EVENTS",
            processed=len(load_resolution_journal(batch.batch_id).resolutions),
            total=len(draft.candidates),
            done=not has_pending_event_resolution(StepInput(), run_context),
        )
    )
    for _ in range(_EVENT_RESOLUTION_LIMIT):
        if not has_pending_event_resolution(StepInput(), run_context):
            return last
        prepared = await prepare_event_resolution(StepInput(), run_context)
        request = EventIdentityRequest.model_validate(prepared.content)
        try:
            content = await _invoke_pinned_agent(
                EVENT_IDENTITY_AGENT_ID,
                request,
                run_context,
                operation_key=f"RESOLVE_EVENT:{request.candidate_key}",
            )
        except Exception as exc:
            release_event_batch_lease(batch)
            _raise_stage_failure("EVENT_IDENTITY", batch.batch_id, exc)
        last = persist_event_resolution(_agent_predecessor(content), run_context)
    if has_pending_event_resolution(StepInput(), run_context):
        release_event_batch_lease(batch)
        _raise_stage_failure(
            "EVENT_IDENTITY",
            batch.batch_id,
            ValueError("Event Candidate resolution exceeded its frozen batch bound"),
        )
    return last


async def analyze_signals(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Classify and analyze every eligible Event through the exact pinned Signal Agent."""

    del step_input
    batch = _batch(run_context)
    publications = load_publication_journal(batch.batch_id).publications
    total = sum(item.event_created and item.graph_projection_status == "SUCCEEDED" for item in publications)
    last = StepOutput(
        content=EventWorkflowProgress(
            phase="ANALYZE_SIGNALS",
            processed=len(_terminal_signal_event_ids(batch.batch_id)),
            total=total,
            done=not has_pending_signal_analysis(StepInput(), run_context),
        )
    )
    for _ in range(_EVENT_SIGNAL_TASK_LIMIT):
        if not has_pending_signal_analysis(StepInput(), run_context):
            return last
        await prepare_signal_task(StepInput(), run_context)
        request = _signal_request(run_context)
        try:
            content = await _invoke_pinned_agent(
                EVENT_SIGNAL_ANALYST_AGENT_ID,
                request,
                run_context,
                operation_key=f"SIGNAL_{request.task}:{request.analysis.event.id}",
            )
        except Exception as exc:
            release_event_batch_lease(batch)
            _raise_stage_failure("SIGNAL_ANALYSIS", batch.batch_id, exc)
        last = await persist_signal_task(_agent_predecessor(content), run_context)
    if has_pending_signal_analysis(StepInput(), run_context):
        release_event_batch_lease(batch)
        _raise_stage_failure(
            "SIGNAL_ANALYSIS",
            batch.batch_id,
            ValueError("Event Signal analysis exceeded its frozen batch bound"),
        )
    return last


def event_extraction_complete(iteration_outputs: list[StepOutput]) -> bool:
    """End the outer batch Loop after an idle, busy, or fully drained queue result."""

    return any(output.stop for output in iteration_outputs)
