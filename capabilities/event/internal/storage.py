"""Crash-safe local batch storage and exclusive leases for Event extraction."""

import fcntl
import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from pydantic import ValidationError

from capabilities.event.internal.models import (
    EventAgentExecutionJournal,
    EventAgentExecutionRecord,
    EventExtractionBatch,
    EventExtractionBusy,
    EventExtractionDraft,
    EventExtractionLease,
    EventExtractionResult,
    EventIdentityRequest,
    EventIdentityRequestJournal,
    EventPublicationJournal,
    EventResolutionJournal,
    EventResolutionRecord,
    EventSignalAnalysisJournal,
    EventSignalAnalysisRecord,
    EventSignalCandidateJournal,
    EventSignalCandidateRecord,
    EventSignalClassificationJournal,
    EventSignalClassificationRecord,
    EventSignalJournal,
    EventSignalPreparationJournal,
    EventSignalProjectionJournal,
    EventSignalProjectionRecord,
    FrozenEventExtractionBatch,
)
from capabilities.event.internal.queue import (
    ensure_processing_items,
    finalize_queue_items,
    pending_queue_items,
    resolve_queue_item,
)
from sematica.analysis.event.contracts import EventAnalysisInput


def event_artifact_root() -> Path:
    return Path(os.getenv("EVENT_ARTIFACT_ROOT", "data/event")).resolve()


def _atomic_write_json(path: Path, value: object) -> None:
    root = event_artifact_root()
    path = path.resolve()
    if root != path and root not in path.parents:
        raise ValueError("Event Artifact path escapes its root")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


@contextmanager
def _claim_lock() -> Iterator[None]:
    path = event_artifact_root() / ".claim.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _batch_size() -> int:
    try:
        value = int(os.getenv("EVENT_EXTRACTION_BATCH_SIZE", "20"))
    except ValueError as exc:
        raise ValueError("EVENT_EXTRACTION_BATCH_SIZE must be an integer") from exc
    if not 1 <= value <= 50:
        raise ValueError("EVENT_EXTRACTION_BATCH_SIZE must be between 1 and 50")
    return value


def _lease_duration() -> timedelta:
    try:
        seconds = int(os.getenv("EVENT_EXTRACTION_LEASE_SECONDS", "600"))
    except ValueError as exc:
        raise ValueError("EVENT_EXTRACTION_LEASE_SECONDS must be an integer") from exc
    if not 60 <= seconds <= 3_600:
        raise ValueError("EVENT_EXTRACTION_LEASE_SECONDS must be between 60 and 3600")
    return timedelta(seconds=seconds)


def pending_directory(batch_id: str) -> Path:
    return event_artifact_root() / ".pending" / batch_id


def _single_pending_directory() -> Path | None:
    directories = sorted(path for path in (event_artifact_root() / ".pending").glob("*") if path.is_dir())
    if len(directories) > 1:
        raise ValueError("multiple pending Event extraction batches violate the single-worker invariant")
    return directories[0] if directories else None


def _load_frozen_batch(path: Path) -> FrozenEventExtractionBatch:
    try:
        batch = FrozenEventExtractionBatch.model_validate_json((path / "input.json").read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("pending Event extraction batch is invalid") from exc
    expected_id = hashlib.sha256("\n".join(item.id for item in batch.evidences).encode("utf-8")).hexdigest()
    if batch.batch_id != path.name or batch.batch_id != expected_id:
        raise ValueError("pending Event extraction batch identity conflict")
    return batch


def _load_lease(path: Path) -> EventExtractionLease | None:
    lease_path = path / "lease.json"
    if not lease_path.exists():
        return None
    try:
        lease = EventExtractionLease.model_validate_json(lease_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("pending Event extraction lease is invalid") from exc
    if lease.batch_id != path.name:
        raise ValueError("pending Event extraction lease identity conflict")
    return lease


def _new_lease(batch_id: str, *, now: datetime) -> EventExtractionLease:
    return EventExtractionLease(
        batch_id=batch_id,
        lease_id=str(uuid4()),
        expires_at=now + _lease_duration(),
    )


def _runtime_batch(
    frozen: FrozenEventExtractionBatch,
    lease: EventExtractionLease,
    *,
    needs_analysis: bool,
) -> EventExtractionBatch:
    return EventExtractionBatch(
        batch_id=frozen.batch_id,
        created_at=frozen.created_at,
        evidences=frozen.evidences,
        needs_analysis=needs_analysis,
        lease_id=lease.lease_id,
        lease_expires_at=lease.expires_at,
    )


def claim_event_batch() -> EventExtractionBatch | EventExtractionBusy | None:
    """Exclusively resume one frozen batch or claim pending Evidence queue items."""
    with _claim_lock():
        now = datetime.now(UTC)
        pending = _single_pending_directory()
        if pending is not None:
            frozen = _load_frozen_batch(pending)
            current = _load_lease(pending)
            if current is not None and current.expires_at > now:
                return EventExtractionBusy(batch_id=frozen.batch_id, retry_after=current.expires_at)
            ensure_processing_items(frozen.batch_id, [item.id for item in frozen.evidences])
            lease = _new_lease(frozen.batch_id, now=now)
            _atomic_write_json(pending / "lease.json", lease.model_dump(mode="json"))
            return _runtime_batch(frozen, lease, needs_analysis=not (pending / "draft.json").is_file())

        queue_items = pending_queue_items()[: _batch_size()]
        if not queue_items:
            return None
        selected = [resolve_queue_item(item) for item in queue_items]
        identity = "\n".join(item.id for item in selected).encode("utf-8")
        batch_id = hashlib.sha256(identity).hexdigest()
        frozen = FrozenEventExtractionBatch(
            batch_id=batch_id,
            created_at=now,
            evidences=selected,
        )
        lease = _new_lease(batch_id, now=now)
        staging = event_artifact_root() / ".claims" / f"{batch_id}-{lease.lease_id}"
        if staging.exists():
            raise ValueError("Event extraction claim staging identity conflict")
        try:
            _atomic_write_json(staging / "input.json", frozen.model_dump(mode="json"))
            _atomic_write_json(staging / "lease.json", lease.model_dump(mode="json"))
            target = pending_directory(batch_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, target)
            ensure_processing_items(batch_id, [item.id for item in selected])
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return _runtime_batch(frozen, lease, needs_analysis=True)


def _assert_event_batch_lease(batch: EventExtractionBatch, *, now: datetime) -> None:
    current = _load_lease(pending_directory(batch.batch_id))
    if current is None or current.lease_id != batch.lease_id or current.expires_at <= now:
        raise ValueError("Event extraction batch lease is not owned by this run")


@contextmanager
def _owned_batch_lock(batch: EventExtractionBatch) -> Iterator[None]:
    """Fence one local mutation by the current unexpired batch lease."""

    with _claim_lock():
        _assert_event_batch_lease(batch, now=datetime.now(UTC))
        yield


def renew_event_batch_lease(batch: EventExtractionBatch) -> None:
    """Extend the current lease after durable progress."""
    with _claim_lock():
        now = datetime.now(UTC)
        _assert_event_batch_lease(batch, now=now)
        renewed = EventExtractionLease(
            batch_id=batch.batch_id,
            lease_id=batch.lease_id,
            expires_at=now + _lease_duration(),
        )
        _atomic_write_json(pending_directory(batch.batch_id) / "lease.json", renewed.model_dump(mode="json"))


def release_event_batch_lease(batch: EventExtractionBatch) -> None:
    """Release only this run's lease so a failed frozen batch can retry promptly."""
    with _claim_lock():
        path = pending_directory(batch.batch_id) / "lease.json"
        current = _load_lease(pending_directory(batch.batch_id))
        if current is not None and current.lease_id == batch.lease_id:
            path.unlink(missing_ok=True)


def freeze_draft(batch: EventExtractionBatch, draft: EventExtractionDraft) -> EventExtractionDraft:
    with _owned_batch_lock(batch):
        path = pending_directory(batch.batch_id) / "draft.json"
        if path.exists():
            try:
                existing = EventExtractionDraft.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValidationError) as exc:
                raise ValueError("frozen Event extraction draft is invalid") from exc
            if existing != draft:
                raise ValueError("frozen Event extraction draft is immutable")
            return existing
        _atomic_write_json(path, draft.model_dump(mode="json"))
        return draft


def load_draft(batch_id: str) -> EventExtractionDraft:
    try:
        return EventExtractionDraft.model_validate_json(
            (pending_directory(batch_id) / "draft.json").read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as exc:
        raise ValueError("frozen Event extraction draft is invalid") from exc


def load_agent_execution_journal(batch_id: str) -> EventAgentExecutionJournal:
    """Load the durable exact Agent bindings for one pending Event batch."""

    path = pending_directory(batch_id) / "agent-executions.json"
    if not path.exists():
        return EventAgentExecutionJournal(batch_id=batch_id, executions=[])
    try:
        journal = EventAgentExecutionJournal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("Event Agent execution journal is invalid") from exc
    if journal.batch_id != batch_id:
        raise ValueError("Event Agent execution journal batch identity conflict")
    return journal


def freeze_agent_execution(
    batch: EventExtractionBatch,
    execution: EventAgentExecutionRecord,
) -> EventAgentExecutionRecord:
    """Bind one recoverable semantic operation to one exact published Agent version."""

    with _owned_batch_lock(batch):
        journal = load_agent_execution_journal(batch.batch_id)
        executions = {item.operation_key: item for item in journal.executions}
        existing = executions.get(execution.operation_key)
        if existing is not None:
            if existing != execution:
                raise ValueError("frozen Event Agent execution binding is immutable")
            return existing
        executions[execution.operation_key] = execution
        updated = EventAgentExecutionJournal(
            batch_id=batch.batch_id,
            executions=[executions[key] for key in sorted(executions)],
        )
        _atomic_write_json(
            pending_directory(batch.batch_id) / "agent-executions.json",
            updated.model_dump(mode="json"),
        )
        return execution


def load_identity_request_journal(batch_id: str) -> EventIdentityRequestJournal:
    path = pending_directory(batch_id) / "identity-requests.json"
    if not path.exists():
        return EventIdentityRequestJournal(batch_id=batch_id, requests=[])
    try:
        journal = EventIdentityRequestJournal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("Event identity request journal is invalid") from exc
    if journal.batch_id != batch_id:
        raise ValueError("Event identity request journal batch identity conflict")
    return journal


def freeze_identity_request(batch: EventExtractionBatch, request: EventIdentityRequest) -> EventIdentityRequest:
    with _owned_batch_lock(batch):
        journal = load_identity_request_journal(batch.batch_id)
        requests = {item.candidate_key: item for item in journal.requests}
        existing = requests.get(request.candidate_key)
        if existing is not None:
            if existing != request:
                raise ValueError("frozen Event identity request is immutable")
            return existing
        requests[request.candidate_key] = request
        updated = EventIdentityRequestJournal(
            batch_id=batch.batch_id,
            requests=[requests[key] for key in sorted(requests)],
        )
        _atomic_write_json(
            pending_directory(batch.batch_id) / "identity-requests.json",
            updated.model_dump(mode="json"),
        )
        return request


def load_resolution_journal(batch_id: str) -> EventResolutionJournal:
    path = pending_directory(batch_id) / "resolutions.json"
    if not path.exists():
        return EventResolutionJournal(batch_id=batch_id, resolutions=[])
    try:
        journal = EventResolutionJournal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("Event resolution journal is invalid") from exc
    if journal.batch_id != batch_id:
        raise ValueError("Event resolution journal batch identity conflict")
    return journal


def freeze_resolution(batch: EventExtractionBatch, resolution: EventResolutionRecord) -> EventResolutionRecord:
    with _owned_batch_lock(batch):
        journal = load_resolution_journal(batch.batch_id)
        resolutions = {item.candidate_key: item for item in journal.resolutions}
        existing = resolutions.get(resolution.candidate_key)
        if existing is not None:
            if existing != resolution:
                raise ValueError("frozen Event resolution is immutable")
            return existing
        resolutions[resolution.candidate_key] = resolution
        updated = EventResolutionJournal(
            batch_id=batch.batch_id,
            resolutions=[resolutions[key] for key in sorted(resolutions)],
        )
        _atomic_write_json(
            pending_directory(batch.batch_id) / "resolutions.json",
            updated.model_dump(mode="json"),
        )
        return resolution


def load_publication_journal(batch_id: str) -> EventPublicationJournal:
    path = pending_directory(batch_id) / "publications.json"
    if not path.exists():
        return EventPublicationJournal(batch_id=batch_id, publications=[])
    try:
        journal = EventPublicationJournal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("Event publication journal is invalid") from exc
    if journal.batch_id != batch_id:
        raise ValueError("Event publication journal batch identity conflict")
    return journal


def write_publication_journal(batch: EventExtractionBatch, journal: EventPublicationJournal) -> None:
    with _owned_batch_lock(batch):
        if journal.batch_id != batch.batch_id:
            raise ValueError("Event publication journal batch identity conflict")
        _atomic_write_json(
            pending_directory(journal.batch_id) / "publications.json",
            journal.model_dump(mode="json"),
        )


def load_signal_preparation_journal(batch_id: str) -> EventSignalPreparationJournal:
    path = pending_directory(batch_id) / "signal-preparations.json"
    if not path.exists():
        return EventSignalPreparationJournal(batch_id=batch_id, analyses=[])
    try:
        journal = EventSignalPreparationJournal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("Event Signal preparation journal is invalid") from exc
    if journal.batch_id != batch_id:
        raise ValueError("Event Signal preparation journal batch identity conflict")
    return journal


def freeze_signal_preparation(batch: EventExtractionBatch, analysis: EventAnalysisInput) -> EventAnalysisInput:
    with _owned_batch_lock(batch):
        journal = load_signal_preparation_journal(batch.batch_id)
        analyses = {item.event.id: item for item in journal.analyses}
        existing = analyses.get(analysis.event.id)
        if existing is not None:
            if existing != analysis:
                raise ValueError("frozen Event Signal preparation is immutable")
            return existing
        analyses[analysis.event.id] = analysis
        updated = EventSignalPreparationJournal(
            batch_id=batch.batch_id,
            analyses=[analyses[key] for key in sorted(analyses)],
        )
        _atomic_write_json(
            pending_directory(batch.batch_id) / "signal-preparations.json",
            updated.model_dump(mode="json"),
        )
        return analysis


def load_signal_classification_journal(batch_id: str) -> EventSignalClassificationJournal:
    path = pending_directory(batch_id) / "signal-classifications.json"
    if not path.exists():
        return EventSignalClassificationJournal(batch_id=batch_id, classifications=[])
    try:
        journal = EventSignalClassificationJournal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("Event Signal classification journal is invalid") from exc
    if journal.batch_id != batch_id:
        raise ValueError("Event Signal classification journal batch identity conflict")
    return journal


def freeze_signal_classification(
    batch: EventExtractionBatch,
    classification: EventSignalClassificationRecord,
) -> EventSignalClassificationRecord:
    with _owned_batch_lock(batch):
        journal = load_signal_classification_journal(batch.batch_id)
        classifications = {item.event_id: item for item in journal.classifications}
        existing = classifications.get(classification.event_id)
        if existing is not None:
            if existing != classification:
                raise ValueError("frozen Event Signal classification is immutable")
            return existing
        classifications[classification.event_id] = classification
        updated = EventSignalClassificationJournal(
            batch_id=batch.batch_id,
            classifications=[classifications[key] for key in sorted(classifications)],
        )
        _atomic_write_json(
            pending_directory(batch.batch_id) / "signal-classifications.json",
            updated.model_dump(mode="json"),
        )
        return classification


def load_signal_candidate_journal(batch_id: str) -> EventSignalCandidateJournal:
    path = pending_directory(batch_id) / "signal-candidates.json"
    if not path.exists():
        return EventSignalCandidateJournal(batch_id=batch_id, candidates=[])
    try:
        journal = EventSignalCandidateJournal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("Event Signal candidate journal is invalid") from exc
    if journal.batch_id != batch_id:
        raise ValueError("Event Signal candidate journal batch identity conflict")
    return journal


def freeze_signal_candidates(
    batch: EventExtractionBatch,
    candidate_set: EventSignalCandidateRecord,
) -> EventSignalCandidateRecord:
    with _owned_batch_lock(batch):
        journal = load_signal_candidate_journal(batch.batch_id)
        candidate_sets = {item.event_id: item for item in journal.candidates}
        existing = candidate_sets.get(candidate_set.event_id)
        if existing is not None:
            if existing != candidate_set:
                raise ValueError("frozen Event Signal candidates are immutable")
            return existing
        candidate_sets[candidate_set.event_id] = candidate_set
        updated = EventSignalCandidateJournal(
            batch_id=batch.batch_id,
            candidates=[candidate_sets[key] for key in sorted(candidate_sets)],
        )
        _atomic_write_json(
            pending_directory(batch.batch_id) / "signal-candidates.json",
            updated.model_dump(mode="json"),
        )
        return candidate_set


def load_signal_analysis_journal(batch_id: str) -> EventSignalAnalysisJournal:
    path = pending_directory(batch_id) / "signal-analyses.json"
    if not path.exists():
        return EventSignalAnalysisJournal(batch_id=batch_id, analyses=[])
    try:
        journal = EventSignalAnalysisJournal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("Event Signal analysis journal is invalid") from exc
    if journal.batch_id != batch_id:
        raise ValueError("Event Signal analysis journal batch identity conflict")
    return journal


def freeze_signal_analysis(
    batch: EventExtractionBatch,
    analysis: EventSignalAnalysisRecord,
) -> EventSignalAnalysisRecord:
    with _owned_batch_lock(batch):
        journal = load_signal_analysis_journal(batch.batch_id)
        analyses = {item.event_id: item for item in journal.analyses}
        existing = analyses.get(analysis.event_id)
        if existing is not None:
            if existing != analysis:
                raise ValueError("frozen Event Signal analysis is immutable")
            return existing
        analyses[analysis.event_id] = analysis
        updated = EventSignalAnalysisJournal(
            batch_id=batch.batch_id,
            analyses=[analyses[key] for key in sorted(analyses)],
        )
        _atomic_write_json(
            pending_directory(batch.batch_id) / "signal-analyses.json",
            updated.model_dump(mode="json"),
        )
        return analysis


def load_signal_projection_journal(batch_id: str) -> EventSignalProjectionJournal:
    path = pending_directory(batch_id) / "signal-projections.json"
    if not path.exists():
        return EventSignalProjectionJournal(batch_id=batch_id, projections=[])
    try:
        journal = EventSignalProjectionJournal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("Event Signal projection journal is invalid") from exc
    if journal.batch_id != batch_id:
        raise ValueError("Event Signal projection journal batch identity conflict")
    return journal


def freeze_signal_projection(
    batch: EventExtractionBatch,
    projection: EventSignalProjectionRecord,
) -> EventSignalProjectionRecord:
    with _owned_batch_lock(batch):
        journal = load_signal_projection_journal(batch.batch_id)
        projections = {(item.event_id, item.proposal_key): item for item in journal.projections}
        key = (projection.event_id, projection.proposal_key)
        existing = projections.get(key)
        if existing is not None:
            if existing != projection:
                raise ValueError("frozen Event Signal projection is immutable")
            return existing
        projections[key] = projection
        updated = EventSignalProjectionJournal(
            batch_id=batch.batch_id,
            projections=[projections[key] for key in sorted(projections)],
        )
        _atomic_write_json(
            pending_directory(batch.batch_id) / "signal-projections.json",
            updated.model_dump(mode="json"),
        )
        return projection


def load_signal_journal(batch_id: str) -> EventSignalJournal:
    path = pending_directory(batch_id) / "signals.json"
    if not path.exists():
        return EventSignalJournal(batch_id=batch_id, signals=[])
    try:
        journal = EventSignalJournal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("Event signal journal is invalid") from exc
    if journal.batch_id != batch_id:
        raise ValueError("Event signal journal batch identity conflict")
    return journal


def write_signal_journal(batch: EventExtractionBatch, journal: EventSignalJournal) -> None:
    with _owned_batch_lock(batch):
        if journal.batch_id != batch.batch_id:
            raise ValueError("Event signal journal batch identity conflict")
        _atomic_write_json(
            pending_directory(journal.batch_id) / "signals.json",
            journal.model_dump(mode="json"),
        )


def complete_batch(batch: EventExtractionBatch, result: EventExtractionResult) -> None:
    with _claim_lock():
        _assert_event_batch_lease(batch, now=datetime.now(UTC))
        source = pending_directory(result.batch_id)
        _atomic_write_json(source / "manifest.json", result.model_dump(mode="json"))
        finalize_queue_items(
            result.batch_id,
            result.evidence_ids,
            set(result.failed_evidence_ids),
        )
        target = event_artifact_root() / "batches" / result.batch_id
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            try:
                existing = EventExtractionResult.model_validate_json(
                    (target / "manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, ValidationError) as exc:
                raise ValueError("completed Event extraction Artifact is invalid") from exc
            if existing != result:
                raise ValueError("completed Event extraction Artifact is immutable")
            return
        os.replace(source, target)
