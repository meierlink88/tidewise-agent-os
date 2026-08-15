"""Crash-safe file cursor and Artifact helpers for Evidence extraction."""

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from capabilities.collection import artifact_root as collector_artifact_root
from capabilities.evidence.internal.models import EvidenceCheckpoint, PreparedRawDocument

_SOURCE_LEVELS = {
    "cls_telegraph": "L2_WIRE",
    "stcn_quicknews": "L2_WIRE",
    "parallel_search": "L3_MEDIA",
    "tavily": "L3_MEDIA",
    "bocha": "L3_MEDIA",
    "eastmoney_fastnews": "L3_MEDIA",
    "eastmoney_stock_news": "L3_MEDIA",
}


def evidence_artifact_root() -> Path:
    """Return the configured persistent Evidence Artifact root."""
    return Path(os.getenv("EVIDENCE_ARTIFACT_ROOT", "data/evidence")).resolve()


def checkpoint_path() -> Path:
    return evidence_artifact_root() / "checkpoint.json"


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def write_json(path: Path, value: object) -> None:
    """Atomically write JSON under the Evidence Artifact root."""
    root = evidence_artifact_root()
    resolved = path.resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError("Evidence Artifact path escapes its root")
    _atomic_write_json(resolved, value)


def read_checkpoint() -> EvidenceCheckpoint:
    path = checkpoint_path()
    if not path.exists():
        return EvidenceCheckpoint()
    return EvidenceCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))


def write_checkpoint(checkpoint: EvidenceCheckpoint) -> None:
    _atomic_write_json(checkpoint_path(), checkpoint.model_dump(mode="json"))


def advance_checkpoint(prepared: PreparedRawDocument) -> EvidenceCheckpoint:
    """Advance exactly one document, accepting a repeated already-advanced call."""
    current = read_checkpoint()
    expected = EvidenceCheckpoint(
        manifest_offset=prepared.manifest_offset,
        document_index=prepared.document_index,
    )
    if prepared.document_index + 1 < prepared.document_count:
        target = EvidenceCheckpoint(
            manifest_offset=prepared.manifest_offset,
            document_index=prepared.document_index + 1,
        )
    else:
        target = EvidenceCheckpoint(manifest_offset=prepared.next_manifest_offset, document_index=0)
    if current == target:
        return current
    if current != expected:
        raise ValueError("Evidence checkpoint changed during publication")
    write_checkpoint(target)
    return target


def _safe_relative(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if root != path and root not in path.parents:
        raise ValueError("Artifact path escapes its root")
    return path


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse_document(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("Raw document front matter is missing")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("Raw document front matter is incomplete") from exc
    metadata: dict[str, Any] = {}
    for line in lines[1:closing]:
        key, separator, encoded = line.partition(":")
        if not separator:
            raise ValueError("Raw document front matter is invalid")
        metadata[key.strip()] = json.loads(encoded.strip())
    body_lines = lines[closing + 1 :]
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    title = metadata.get("title")
    if body_lines and isinstance(title, str) and body_lines[0].strip() == f"# {title}":
        body_lines.pop(0)
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
    body = "\n".join(body_lines).strip()
    if not body:
        raise ValueError("Raw document body is empty")
    return metadata, body


def _source_id(connector: str) -> str:
    if 1 <= len(connector) <= 32:
        return connector
    return "SRC_" + hashlib.sha256(connector.encode("utf-8")).hexdigest()[:28]


def _raw_evidence_id(source_url: str, content_sha256: str) -> str:
    identity = f"{source_url}\n{content_sha256}".encode()
    return "RAW_" + hashlib.sha256(identity).hexdigest()[:28]


def read_next_raw_document(checkpoint: EvidenceCheckpoint) -> tuple[PreparedRawDocument | None, EvidenceCheckpoint]:
    """Read the next indexed accepted document without scanning historical bodies."""
    root = collector_artifact_root()
    index_path = root / "indexes" / "manifest-index.jsonl"
    if not index_path.exists():
        return None, checkpoint

    current = checkpoint
    with index_path.open("rb") as handle:
        while True:
            handle.seek(current.manifest_offset)
            line = handle.readline()
            if not line:
                return None, current
            next_offset = handle.tell()
            if not line.strip():
                current = EvidenceCheckpoint(manifest_offset=next_offset, document_index=0)
                write_checkpoint(current)
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("Raw Collection manifest index contains invalid JSON") from exc
            if entry.get("schema") != "raw_collection_manifest_index.v1":
                raise ValueError("Raw Collection manifest index schema is unsupported")
            manifest_relative = entry.get("manifest_path")
            if not isinstance(manifest_relative, str):
                raise ValueError("Raw Collection manifest index path is invalid")
            manifest_path = _safe_relative(root, manifest_relative)
            manifest_bytes = manifest_path.read_bytes()
            if _sha256_bytes(manifest_bytes) != entry.get("manifest_sha256"):
                raise ValueError("Raw Collection manifest hash mismatch")
            manifest = json.loads(manifest_bytes)
            accepted = manifest.get("accepted_documents")
            if not isinstance(accepted, list):
                raise ValueError("Raw Collection manifest accepted_documents is invalid")
            if current.document_index >= len(accepted):
                current = EvidenceCheckpoint(manifest_offset=next_offset, document_index=0)
                write_checkpoint(current)
                continue
            item = accepted[current.document_index]
            if not isinstance(item, dict):
                raise ValueError("Raw Collection accepted document is invalid")
            relative_document = item.get("relative_path")
            document_url_path = item.get("url_path")
            document_sha256 = item.get("sha256")
            if (
                not isinstance(relative_document, str)
                or not isinstance(document_url_path, str)
                or not document_url_path.startswith("/")
                or document_url_path.startswith("//")
                or not document_url_path.endswith(f"/{relative_document}")
                or any(part in {"", ".", ".."} for part in document_url_path[1:].split("/"))
                or not isinstance(document_sha256, str)
            ):
                raise ValueError("Raw Collection accepted document identity is invalid")
            document_path = _safe_relative(root, relative_document)
            document_bytes = document_path.read_bytes()
            if _sha256_bytes(document_bytes) != document_sha256:
                raise ValueError("Raw Collection document hash mismatch")
            metadata, raw_text = _parse_document(document_path)
            content_sha256 = _sha256_bytes(raw_text.encode("utf-8"))
            if content_sha256 != metadata.get("content_sha256"):
                raise ValueError("Raw Collection document content hash mismatch")
            connector = metadata.get("connector")
            source_name = metadata.get("source_name")
            source_url = metadata.get("url")
            collected_at = metadata.get("collected_at")
            source_level = metadata.get("source_level")
            if not isinstance(connector, str) or not connector.strip():
                raise ValueError("Raw Collection document connector is missing")
            if not isinstance(source_name, str) or not source_name.strip():
                raise ValueError("Raw Collection document source name is missing")
            if not isinstance(source_url, str) or not source_url.strip():
                raise ValueError("Raw Collection document source URL is missing")
            if not isinstance(collected_at, str) or not collected_at.strip():
                raise ValueError("Raw Collection document source metadata is incomplete")
            prepared = PreparedRawDocument(
                collection_id=str(manifest.get("collection_id")),
                manifest_path=manifest_relative,
                manifest_offset=current.manifest_offset,
                next_manifest_offset=next_offset,
                document_index=current.document_index,
                document_count=len(accepted),
                document_path=relative_document,
                document_url_path=document_url_path,
                document_sha256=document_sha256,
                content_sha256=content_sha256,
                raw_evidence_id=_raw_evidence_id(source_url, content_sha256),
                source_id=_source_id(connector),
                source_name=source_name,
                source_level=source_level
                if isinstance(source_level, str)
                else _SOURCE_LEVELS.get(connector, "L3_MEDIA"),
                source_url=source_url,
                title=metadata.get("title"),
                raw_text=raw_text,
                published_at=metadata.get("published_at"),
                collected_at=collected_at,
            )
            return prepared, current
