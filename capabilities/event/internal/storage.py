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
    EventExtractionBatch,
    EventExtractionBusy,
    EventExtractionDraft,
    EventExtractionLease,
    EventExtractionResult,
    EventPublicationJournal,
    EventSignalJournal,
    FrozenEventExtractionBatch,
)
from capabilities.event.internal.queue import (
    ensure_processing_items,
    finalize_queue_items,
    pending_queue_items,
    resolve_queue_item,
)


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


def write_publication_journal(journal: EventPublicationJournal) -> None:
    _atomic_write_json(
        pending_directory(journal.batch_id) / "publications.json",
        journal.model_dump(mode="json"),
    )


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


def write_signal_journal(journal: EventSignalJournal) -> None:
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
