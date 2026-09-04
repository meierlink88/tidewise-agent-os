"""Run-scoped file buffer used by Agent tools and deterministic Workflow steps."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from capabilities.collection.internal.models import Candidate, TitleCurationDraft, ToolBatch


def artifact_root() -> Path:
    """Return the configured persistent Artifact root."""
    return Path(os.getenv("COLLECTOR_ARTIFACT_ROOT", "data/collector")).resolve()


def collection_staging_root(collection_id: str) -> Path:
    """Return a collection-scoped pending directory without creating it."""
    return artifact_root() / ".pending" / collection_id


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def write_tool_batch(
    *,
    collection_id: str,
    connector: str,
    query: str,
    candidates: list[Candidate],
) -> ToolBatch:
    """Persist one complete tool result and return its typed identity."""
    batch = ToolBatch(
        batch_id=str(uuid4()),
        collection_id=collection_id,
        connector=connector,
        query=query,
        collected_at=min((item.collected_at for item in candidates), default=datetime.now(UTC)),
        candidates=candidates,
    )
    path = collection_staging_root(collection_id) / "batches" / f"{batch.batch_id}.json"
    _atomic_write_text(path, batch.model_dump_json(indent=2, exclude_none=True))
    return batch


def read_tool_batches(collection_id: str) -> list[ToolBatch]:
    """Load every complete Tool Batch for a collection in stable order."""
    batch_root = collection_staging_root(collection_id) / "batches"
    if not batch_root.exists():
        return []
    batches: list[ToolBatch] = []
    for path in sorted(batch_root.glob("*.json")):
        batches.append(ToolBatch.model_validate_json(path.read_text(encoding="utf-8")))
    return batches


def write_title_curation(collection_id: str, draft: TitleCurationDraft) -> None:
    """Persist the validated title-only decisions for deterministic Artifact use."""
    path = collection_staging_root(collection_id) / "curation" / "title-decisions.json"
    _atomic_write_text(path, draft.model_dump_json(indent=2))


def read_title_curation(collection_id: str) -> TitleCurationDraft:
    """Load the validated title-only decisions for one collection run."""
    path = collection_staging_root(collection_id) / "curation" / "title-decisions.json"
    if not path.is_file():
        raise ValueError("validated title curation is missing")
    return TitleCurationDraft.model_validate_json(path.read_text(encoding="utf-8"))


def read_title_curation_if_present(collection_id: str) -> TitleCurationDraft | None:
    """Load partial filter decisions when a collection loop has already persisted some."""
    path = collection_staging_root(collection_id) / "curation" / "title-decisions.json"
    if not path.is_file():
        return None
    return TitleCurationDraft.model_validate_json(path.read_text(encoding="utf-8"))


def write_title_curation_omissions(collection_id: str, omissions: dict[str, int]) -> None:
    """Persist bounded per-Candidate omission counts for partial filter responses."""
    path = collection_staging_root(collection_id) / "curation" / "omission-counts.json"
    _atomic_write_text(path, json.dumps(omissions, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def read_title_curation_omissions(collection_id: str) -> dict[str, int]:
    """Load and validate omission counts for one collection run."""
    path = collection_staging_root(collection_id) / "curation" / "omission-counts.json"
    if not path.is_file():
        return {}
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict) or any(
        not isinstance(candidate_id, str)
        or not candidate_id
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 1
        for candidate_id, count in decoded.items()
    ):
        raise ValueError("Raw Evidence filter omission state is invalid")
    return decoded


def write_json(path: Path, value: object) -> None:
    """Atomically persist JSON with stable human-readable formatting."""
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def write_text(path: Path, content: str) -> None:
    """Atomically persist UTF-8 text."""
    _atomic_write_text(path, content)
