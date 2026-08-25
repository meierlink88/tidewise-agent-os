"""Crash-safe local batch storage for Event extraction."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import ValidationError

from capabilities.event.internal.models import (
    EventEvidenceInput,
    EventExtractionBatch,
    EventExtractionDraft,
    EventExtractionResult,
    EventSubmissionJournal,
)
from capabilities.evidence import read_resolved_evidences


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


def _batch_size() -> int:
    try:
        value = int(os.getenv("EVENT_EXTRACTION_BATCH_SIZE", "50"))
    except ValueError as exc:
        raise ValueError("EVENT_EXTRACTION_BATCH_SIZE must be an integer") from exc
    if not 1 <= value <= 50:
        raise ValueError("EVENT_EXTRACTION_BATCH_SIZE must be between 1 and 50")
    return value


def pending_directory(batch_id: str) -> Path:
    return event_artifact_root() / ".pending" / batch_id


def _load_pending_batch(path: Path) -> EventExtractionBatch:
    try:
        batch = EventExtractionBatch.model_validate_json((path / "input.json").read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("pending Event extraction batch is invalid") from exc
    needs_analysis = not (path / "draft.json").is_file()
    return batch.model_copy(update={"needs_analysis": needs_analysis})


def _single_pending_batch() -> EventExtractionBatch | None:
    directories = sorted(path for path in (event_artifact_root() / ".pending").glob("*") if path.is_dir())
    if len(directories) > 1:
        raise ValueError("multiple pending Event extraction batches require operator review")
    return _load_pending_batch(directories[0]) if directories else None


def _processed_ids() -> set[str]:
    processed: set[str] = set()
    for path in sorted((event_artifact_root() / "batches").glob("*/manifest.json")):
        try:
            result = EventExtractionResult.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            raise ValueError("completed Event extraction Artifact is invalid") from exc
        processed.update(result.evidence_ids)
    return processed


def _available_evidences() -> list[EventEvidenceInput]:
    evidence_root = Path(os.getenv("EVIDENCE_ARTIFACT_ROOT", "data/evidence")).resolve()
    resolved: dict[str, EventEvidenceInput] = {}
    for manifest in sorted((evidence_root / "documents").glob("*/manifest.json")):
        try:
            items = read_resolved_evidences(manifest)
        except ValueError:
            continue
        for item in items:
            candidate = EventEvidenceInput.model_validate(item.model_dump(mode="json"))
            previous = resolved.get(candidate.id)
            if previous is not None and previous != candidate:
                raise ValueError("formal Evidence identity has conflicting local Artifacts")
            resolved[candidate.id] = candidate
    return [resolved[key] for key in sorted(resolved)]


def claim_event_batch() -> EventExtractionBatch | None:
    """Resume one pending batch or atomically claim mapped, unprocessed Evidence."""
    pending = _single_pending_batch()
    if pending is not None:
        return pending
    processed = _processed_ids()
    selected = [item for item in _available_evidences() if item.id not in processed][: _batch_size()]
    if not selected:
        return None
    identity = "\n".join(item.id for item in selected).encode("utf-8")
    batch_id = hashlib.sha256(identity).hexdigest()
    batch = EventExtractionBatch(
        batch_id=batch_id,
        created_at=datetime.now(UTC),
        needs_analysis=True,
        evidences=selected,
    )
    directory = pending_directory(batch_id)
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return _load_pending_batch(directory)
    _atomic_write_json(directory / "input.json", batch.model_dump(mode="json"))
    return batch


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


def load_journal(batch_id: str) -> EventSubmissionJournal:
    path = pending_directory(batch_id) / "submissions.json"
    if not path.exists():
        return EventSubmissionJournal(batch_id=batch_id, submissions=[])
    try:
        journal = EventSubmissionJournal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("Event submission journal is invalid") from exc
    if journal.batch_id != batch_id:
        raise ValueError("Event submission journal batch identity conflict")
    return journal


def write_journal(journal: EventSubmissionJournal) -> None:
    _atomic_write_json(
        pending_directory(journal.batch_id) / "submissions.json",
        journal.model_dump(mode="json"),
    )


def complete_batch(result: EventExtractionResult) -> None:
    source = pending_directory(result.batch_id)
    _atomic_write_json(source / "manifest.json", result.model_dump(mode="json"))
    target = event_artifact_root() / "batches" / result.batch_id
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            existing = EventExtractionResult.model_validate_json((target / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            raise ValueError("completed Event extraction Artifact is invalid") from exc
        if existing != result:
            raise ValueError("completed Event extraction Artifact is immutable")
        return
    os.replace(source, target)
