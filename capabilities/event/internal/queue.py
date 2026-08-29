"""Filesystem queue connecting formal Evidence publication to Event extraction."""

import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from capabilities.event.internal.models import EventEvidenceInput, EventEvidenceQueueItem
from capabilities.evidence import read_resolved_evidences


def event_queue_root() -> Path:
    return Path(os.getenv("EVENT_ARTIFACT_ROOT", "data/event")).resolve() / "evidence-queue"


def _atomic_write(path: Path, item: EventEvidenceQueueItem) -> None:
    from capabilities.event.internal.storage import _atomic_write_json

    _atomic_write_json(path, item.model_dump(mode="json"))


def _queue_locations(evidence_id: str) -> list[Path]:
    root = event_queue_root()
    paths = [root / "pending" / f"{evidence_id}.json"]
    paths.extend(sorted((root / "processing").glob(f"*/{evidence_id}.json")))
    paths.extend(
        [
            root / "completed" / f"{evidence_id}.json",
            root / "failed" / f"{evidence_id}.json",
        ]
    )
    return [path for path in paths if path.is_file()]


def _load(path: Path) -> EventEvidenceQueueItem:
    try:
        item = EventEvidenceQueueItem.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("Event Evidence queue item is invalid") from exc
    if path.name != f"{item.evidence_id}.json":
        raise ValueError("Event Evidence queue item identity conflict")
    return item


def enqueue_evidence_artifact(artifact_manifest_path: str, evidence_ids: list[str]) -> None:
    """Idempotently enqueue every formal Evidence exposed by one immutable Artifact."""

    resolved = read_resolved_evidences(artifact_manifest_path)
    resolved_ids = [item.id for item in resolved]
    if len(evidence_ids) != len(set(evidence_ids)) or set(evidence_ids) != set(resolved_ids):
        raise ValueError("Event queue Evidence identities do not match the published Artifact")
    manifest_path = str(Path(artifact_manifest_path).resolve())
    for evidence_id in sorted(evidence_ids):
        existing_paths = _queue_locations(evidence_id)
        if len(existing_paths) > 1:
            raise ValueError("Event Evidence exists in multiple queue states")
        if existing_paths:
            existing = _load(existing_paths[0])
            if existing.artifact_manifest_path != manifest_path:
                raise ValueError("Event Evidence queue Artifact identity conflict")
            continue
        item = EventEvidenceQueueItem(
            evidence_id=evidence_id,
            artifact_manifest_path=manifest_path,
            created_at=datetime.now(UTC),
        )
        _atomic_write(event_queue_root() / "pending" / f"{evidence_id}.json", item)


def pending_queue_items() -> list[EventEvidenceQueueItem]:
    """Return strict pending items in deterministic identity order."""

    return [_load(path) for path in sorted((event_queue_root() / "pending").glob("*.json"))]


def resolve_queue_item(item: EventEvidenceQueueItem) -> EventEvidenceInput:
    """Load one Evidence from its immutable publication Artifact."""

    matches = [
        evidence for evidence in read_resolved_evidences(item.artifact_manifest_path) if evidence.id == item.evidence_id
    ]
    if len(matches) != 1:
        raise ValueError("Event Evidence queue item cannot resolve one formal Evidence")
    return EventEvidenceInput.model_validate(matches[0].model_dump(mode="json"))


def ensure_processing_items(batch_id: str, evidence_ids: list[str]) -> None:
    """Reconcile the frozen batch into its processing state after a crash."""

    root = event_queue_root()
    processing = root / "processing" / batch_id
    for evidence_id in evidence_ids:
        source = root / "pending" / f"{evidence_id}.json"
        target = processing / f"{evidence_id}.json"
        completed = root / "completed" / f"{evidence_id}.json"
        failed = root / "failed" / f"{evidence_id}.json"
        terminal = [path for path in (completed, failed) if path.is_file()]
        if terminal:
            if len(terminal) != 1 or source.exists() or target.exists():
                raise ValueError("Event Evidence queue state conflict")
            _load(terminal[0])
            continue
        if target.exists():
            target_item = _load(target)
            if source.exists():
                source_item = _load(source)
                if source_item.evidence_id != target_item.evidence_id or (
                    source_item.artifact_manifest_path != target_item.artifact_manifest_path
                ):
                    raise ValueError("Event Evidence queue state conflict")
                source.unlink()
            continue
        if not source.exists():
            raise ValueError("frozen Event Evidence is missing from its queue")
        processing.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)


def finalize_queue_items(batch_id: str, evidence_ids: list[str], failed_evidence_ids: set[str]) -> None:
    """Move every processed Evidence to exactly one terminal queue state."""

    root = event_queue_root()
    if not failed_evidence_ids <= set(evidence_ids):
        raise ValueError("failed Event Evidence does not belong to the frozen batch")
    for evidence_id in evidence_ids:
        source = root / "processing" / batch_id / f"{evidence_id}.json"
        terminal_name = "failed" if evidence_id in failed_evidence_ids else "completed"
        target = root / terminal_name / f"{evidence_id}.json"
        if target.exists():
            _load(target)
            if source.exists():
                source_item = _load(source)
                target_item = _load(target)
                if source_item.artifact_manifest_path != target_item.artifact_manifest_path:
                    raise ValueError("terminal Event Evidence queue identity conflict")
                source.unlink()
            continue
        if not source.exists():
            raise ValueError("processed Event Evidence queue item is missing")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
    processing = root / "processing" / batch_id
    if processing.exists():
        try:
            processing.rmdir()
        except OSError as exc:
            raise ValueError("Event Evidence processing queue contains unknown files") from exc


def queue_counts() -> dict[str, int]:
    """Expose deterministic state counts for tests and operator diagnostics."""

    root = event_queue_root()
    return {
        "pending": len(list((root / "pending").glob("*.json"))),
        "processing": len(list((root / "processing").glob("*/*.json"))),
        "completed": len(list((root / "completed").glob("*.json"))),
        "failed": len(list((root / "failed").glob("*.json"))),
    }
