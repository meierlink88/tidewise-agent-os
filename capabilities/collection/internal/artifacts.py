"""Deterministic construction and manifest-last publication of collection Artifacts."""

import hashlib
import json
import os
import re
import shutil
import unicodedata
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TextIO
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from capabilities.collection.internal.buffer import (
    artifact_root,
    collection_staging_root,
    read_title_curation,
    read_tool_batches,
    write_json,
    write_text,
)
from capabilities.collection.internal.models import (
    AcceptedDocument,
    Candidate,
    CollectionRequest,
    CollectionResult,
    PreparedArtifactSet,
)
from capabilities.collection.internal.object_storage import (
    RawDocumentStore,
    bucket_from_url_path,
    configured_raw_document_store,
    raw_evidence_url_path,
)

_TRACKING_PARAMETERS = {"fbclid", "gclid", "spm", "from", "source"}
_WORD = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_CJK = re.compile(r"[\u3400-\u9fff]")
_LEGACY_INDEX_HEADER = "url_sha256\tcontent_sha256\tsimhash64\tdocument_path"
_INDEX_HEADER = "schema\turl_sha256\ttitle_sha256\ttitle_simhash64\tdocument_path"
_INDEX_SCHEMA = "title-v1"
_LEGACY_INDEX_SCHEMA = "legacy-v1"
_MANIFEST_INDEX_SCHEMA = "raw_collection_manifest_index.v1"


def _sha256(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("candidate URL must be HTTP(S)")
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in _TRACKING_PARAMETERS:
            continue
        query.append((key, item))
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, urlencode(sorted(query)), ""))


def _simhash_tokens(value: str) -> list[str]:
    lowered = value.lower()
    tokens = _WORD.findall(lowered)
    cjk = "".join(_CJK.findall(lowered))
    if len(cjk) < 3:
        tokens.extend(cjk)
    else:
        tokens.extend(cjk[index : index + 3] for index in range(len(cjk) - 2))
    return tokens or [lowered]


def _simhash64(value: str) -> int:
    vector = [0] * 64
    for token in _simhash_tokens(value):
        digest = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")
        for bit in range(64):
            vector[bit] += 1 if digest & (1 << bit) else -1
    result = 0
    for bit, score in enumerate(vector):
        if score >= 0:
            result |= 1 << bit
    return result


def _hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character for character in normalized if not unicodedata.category(character).startswith(("P", "Z", "C"))
    )


def _load_index(path: Path) -> tuple[list[str], set[str], set[str], list[int]]:
    if not path.exists():
        return [], set(), set(), []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    header = lines[0] if lines else ""
    data_lines = lines[1:] if header in {_INDEX_HEADER, _LEGACY_INDEX_HEADER} else lines
    urls: set[str] = set()
    title_hashes: set[str] = set()
    simhashes: list[int] = []
    valid_lines: list[str] = []
    for line in data_lines:
        fields = line.split("\t")
        if header == _INDEX_HEADER and len(fields) == 5 and fields[0] == _INDEX_SCHEMA:
            schema, url_sha256, title_sha256, fingerprint_hex, document_path = fields
            del schema
            try:
                fingerprint = int(fingerprint_hex, 16)
            except ValueError:
                continue
            urls.add(url_sha256)
            title_hashes.add(title_sha256)
            simhashes.append(fingerprint)
            valid_lines.append(line)
            continue
        if len(fields) == 5 and fields[0] == _LEGACY_INDEX_SCHEMA and fields[1]:
            urls.add(fields[1])
            valid_lines.append(line)
    return list(dict.fromkeys(valid_lines)), urls, title_hashes, simhashes


def _load_legacy_urls(path: Path) -> set[str]:
    """Read only canonical URL identities from the immutable pre-title index."""
    if not path.is_file():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    data_lines = lines[1:] if lines and lines[0] == _LEGACY_INDEX_HEADER else lines
    return {fields[0] for line in data_lines if len(fields := line.split("\t")) == 4 and fields[0]}


def _document_markdown(candidate: Candidate, canonical_url: str, content_sha256: str) -> str:
    item = candidate
    metadata = {
        "schema": "raw_collection_document.v1",
        "candidate_id": item.candidate_id,
        "connector": item.connector,
        "query": item.query,
        "title": item.title,
        "url": canonical_url,
        "source_name": item.source_name,
        "source_level": item.source_level.value,
        "source_external_id": item.source_external_id,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "collected_at": item.collected_at.isoformat(),
        "content_sha256": content_sha256,
    }
    frontmatter = "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items() if value is not None
    )
    return f"---\n{frontmatter}\n---\n\n# {item.title}\n\n{item.content.strip()}\n"


def _relative_document_path(published_at: datetime | None, collected_at: datetime, digest: str) -> Path:
    partition = (published_at or collected_at).astimezone(UTC)
    return Path("documents") / partition.strftime("%Y/%m/%d") / f"{digest}.md"


def build_artifact_set(
    collection_id: str,
    request: CollectionRequest,
    *,
    completed_at: datetime | None = None,
) -> PreparedArtifactSet:
    """Build a complete pending Artifact set without mutating published files."""
    batches = read_tool_batches(collection_id)
    if not batches:
        raise ValueError("collection Agent completed without any Tool Batch")

    windows = {(batch.requested_after, batch.requested_before) for batch in batches}
    if len(windows) != 1:
        raise ValueError("Tool Batches disagree on collection time window")

    provenance = {
        (batch.agent_component_id, batch.agent_config_version, batch.instructions_sha256) for batch in batches
    }
    if len(provenance) != 1:
        raise ValueError("Tool Batches disagree on Collector Agent version")
    agent_component_id, agent_config_version, instructions_sha256 = provenance.pop()

    curation = read_title_curation(collection_id)
    decisions = {item.candidate_id: item for item in curation.decisions}
    if len(decisions) != len(curation.decisions):
        raise ValueError("validated title curation contains duplicate Candidate IDs")

    finished = (completed_at or datetime.now(UTC)).astimezone(UTC)
    window_start = min(batch.requested_after for batch in batches).astimezone(UTC)
    window_end = max(batch.requested_before for batch in batches).astimezone(UTC)
    root = artifact_root()
    staging = collection_staging_root(collection_id)
    legacy_index = root / "indexes" / "dedup-index.tsv"
    final_index = root / "indexes" / "title-dedup-index.tsv"
    index_lines, known_urls, known_title_hashes, known_simhashes = _load_index(final_index)
    known_urls.update(_load_legacy_urls(legacy_index))

    current_urls: set[str] = set()
    current_titles: set[str] = set()
    current_simhashes: list[int] = []
    candidate_counts: Counter[str] = Counter()
    ledger: list[dict[str, object]] = []
    accepted: list[AcceptedDocument] = []
    accepted_index_lines: list[str] = []

    candidates = [(batch, candidate) for batch in batches for candidate in batch.candidates]
    candidates.sort(key=lambda item: (str(item[1].url), item[1].connector, item[1].candidate_id))
    candidate_ids = {candidate.candidate_id for _, candidate in candidates}
    if set(decisions) != candidate_ids:
        raise ValueError("validated title curation does not cover the exact Candidate set")
    for batch, candidate in candidates:
        decision = decisions[candidate.candidate_id]
        disposition = "accepted"
        reason = "new_direct_result"
        canonical_url = ""
        content_sha256 = _sha256(candidate.content.strip())
        normalized_title = _normalize_title(candidate.title)
        if not normalized_title:
            disposition, reason = "invalid_result", "empty_normalized_title"
        title_sha256 = _sha256(normalized_title)
        fingerprint = _simhash64(normalized_title)
        if not decision.is_relevant:
            disposition, reason = "irrelevant", "title_irrelevant"
        try:
            canonical_url = _canonical_url(str(candidate.url))
        except ValueError:
            if disposition == "accepted":
                disposition, reason = "invalid_result", "invalid_url"
        effective_time = candidate.published_at or candidate.collected_at
        if disposition == "accepted" and not (batch.requested_after <= effective_time <= batch.requested_before):
            disposition, reason = "out_of_window", "published_at_outside_time_window"
        url_sha256 = _sha256(canonical_url) if canonical_url else ""
        if disposition == "accepted" and (url_sha256 in known_urls or url_sha256 in current_urls):
            disposition, reason = "known_url", "canonical_url_already_indexed"
        if disposition == "accepted" and (title_sha256 in known_title_hashes or title_sha256 in current_titles):
            disposition, reason = "exact_duplicate", "normalized_title_sha256_already_indexed"
        if disposition == "accepted" and any(
            _hamming_distance(fingerprint, existing) <= 3 for existing in [*known_simhashes, *current_simhashes]
        ):
            disposition, reason = "near_duplicate", "simhash64_within_hamming_radius_3"

        document_path: str | None = None
        if disposition == "accepted":
            markdown = _document_markdown(candidate, canonical_url, content_sha256)
            document_hash = _sha256(markdown)
            relative_path = _relative_document_path(candidate.published_at, candidate.collected_at, document_hash)
            staged_document = staging / relative_path
            write_text(staged_document, markdown)
            document_path = relative_path.as_posix()
            accepted.append(
                AcceptedDocument(
                    candidate_id=candidate.candidate_id,
                    relative_path=document_path,
                    url_path=raw_evidence_url_path(document_path),
                    sha256=document_hash,
                )
            )
            current_urls.add(url_sha256)
            current_titles.add(title_sha256)
            current_simhashes.append(fingerprint)
            accepted_index_lines.append(
                f"{_INDEX_SCHEMA}\t{url_sha256}\t{title_sha256}\t{fingerprint:016x}\t{document_path}"
            )

        candidate_counts[disposition] += 1
        ledger.append(
            {
                "candidate_id": candidate.candidate_id,
                "connector": candidate.connector,
                "query": candidate.query,
                "requested_after": batch.requested_after.isoformat(),
                "requested_before": batch.requested_before.isoformat(),
                "title": candidate.title,
                "url": str(candidate.url),
                "canonical_url": canonical_url or None,
                "source_name": candidate.source_name,
                "source_level": candidate.source_level.value,
                "published_at": candidate.published_at.isoformat() if candidate.published_at else None,
                "collected_at": candidate.collected_at.isoformat(),
                "title_relevance": "relevant" if decision.is_relevant else "irrelevant",
                "disposition": disposition,
                "reason": reason,
                "document_path": document_path,
            }
        )

    results_terminal = len(ledger)
    candidate_counts["raw_results"] = results_terminal
    candidate_counts["results_terminal"] = results_terminal
    candidate_counts["results_pending"] = 0
    outcome = "changed" if accepted else "no_change"

    run_root = staging / "runs" / collection_id
    candidate_payload = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in ledger)
    write_text(run_root / "candidates.jsonl", candidate_payload)
    summary_lines = [
        f"# Raw Collection {collection_id}",
        "",
        f"- outcome: {outcome}",
        f"- window_start: {window_start.isoformat()}",
        f"- window_end: {window_end.isoformat()}",
        f"- tool_batches: {len(batches)}",
    ]
    summary_lines.extend(f"- {key}: {value}" for key, value in sorted(candidate_counts.items()))
    write_text(run_root / "summary.md", "\n".join(summary_lines) + "\n")

    staged_index = staging / "indexes" / "title-dedup-index.tsv"
    write_text(staged_index, "\n".join([_INDEX_HEADER, *index_lines, *accepted_index_lines]) + "\n")

    publication_items = [item.relative_path for item in accepted]
    publication_items.extend(
        [
            f"runs/{collection_id}/candidates.jsonl",
            f"runs/{collection_id}/summary.md",
            "indexes/title-dedup-index.tsv",
            f"runs/{collection_id}/manifest.json",
        ]
    )
    manifest = {
        "schema": "raw_collection_manifest.v2",
        "collection_id": collection_id,
        "outcome": outcome,
        "objective_sha256": _sha256(request.objective),
        "objective_bytes": len(request.objective.encode("utf-8")),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "collector_agent": {
            "component_id": agent_component_id,
            "config_version": agent_config_version,
            "instructions_sha256": instructions_sha256,
        },
        "tool_batches": [
            {
                "batch_id": batch.batch_id,
                "connector": batch.connector,
                "query": batch.query,
                "requested_after": batch.requested_after.isoformat(),
                "requested_before": batch.requested_before.isoformat(),
                "result_count": len(batch.candidates),
            }
            for batch in batches
        ],
        "candidate_counts": dict(sorted(candidate_counts.items())),
        "results_pending": 0,
        "accepted_documents": [item.model_dump(mode="json") for item in accepted],
        "artifacts": {
            "candidates": f"runs/{collection_id}/candidates.jsonl",
            "summary": f"runs/{collection_id}/summary.md",
            "index": "indexes/title-dedup-index.tsv",
            "manifest": f"runs/{collection_id}/manifest.json",
        },
        "completed_at": finished.isoformat(),
    }
    write_json(run_root / "manifest.json", manifest)

    return PreparedArtifactSet(
        collection_id=collection_id,
        outcome=outcome,
        staging_root=str(staging),
        results_terminal=results_terminal,
        candidate_counts=dict(candidate_counts),
        accepted_documents=accepted,
        publication_items=publication_items,
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _locked_manifest_index(root: Path) -> Iterator[TextIO]:
    path = root / "indexes" / "manifest-index.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        flock(handle.fileno(), LOCK_EX)
        yield handle
    finally:
        flock(handle.fileno(), LOCK_UN)
        handle.close()


def _append_manifest_index(root: Path, manifest_path: Path, manifest: dict[str, object]) -> None:
    """Append one completed run pointer idempotently after its manifest exists."""
    collection_id = manifest.get("collection_id")
    completed_at = manifest.get("completed_at")
    accepted_documents = manifest.get("accepted_documents")
    if not isinstance(collection_id, str) or not collection_id:
        raise ValueError("published manifest has no collection identity")
    if not isinstance(completed_at, str) or not completed_at:
        raise ValueError("published manifest has no completion time")
    if not isinstance(accepted_documents, list):
        raise ValueError("published manifest accepted_documents is invalid")

    relative_manifest = manifest_path.relative_to(root).as_posix()
    entry = {
        "schema": _MANIFEST_INDEX_SCHEMA,
        "collection_id": collection_id,
        "manifest_path": relative_manifest,
        "manifest_sha256": _file_sha256(manifest_path),
        "completed_at": completed_at,
        "outcome": manifest.get("outcome"),
        "accepted_documents": len(accepted_documents),
    }
    encoded = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with _locked_manifest_index(root) as handle:
        handle.seek(0)
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                existing = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("manifest index contains invalid JSON") from exc
            if existing.get("collection_id") != collection_id:
                continue
            if existing != entry:
                raise ValueError("manifest index identity conflict")
            return
        handle.seek(0, os.SEEK_END)
        handle.write(encoded + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _publish_file(source: Path, target: Path, *, replace: bool) -> None:
    if not source.is_file():
        raise ValueError(f"prepared Artifact is missing: {source.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not replace:
        if _file_sha256(source) == _file_sha256(target):
            return
        raise ValueError(f"immutable Artifact identity conflict: {target.name}")
    with NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
        handle.write(source.read_bytes())
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, target)


def publish_artifact_set(
    prepared: PreparedArtifactSet,
    *,
    document_store: RawDocumentStore | None = None,
) -> CollectionResult:
    """Publish a prepared collection idempotently, with the manifest last."""
    if prepared.results_pending != 0:
        raise ValueError("cannot publish pending candidates")
    root = artifact_root()
    staging = Path(prepared.staging_root).resolve()
    pending_root = (root / ".pending").resolve()
    if pending_root not in staging.parents:
        raise ValueError("prepared Artifact set escapes the pending root")

    manifest_relative = f"runs/{prepared.collection_id}/manifest.json"
    manifest_target = root / manifest_relative
    if manifest_target.is_file() and not staging.exists():
        manifest = json.loads(manifest_target.read_text(encoding="utf-8"))
        if manifest.get("collection_id") != prepared.collection_id or manifest.get("outcome") != prepared.outcome:
            raise ValueError("published manifest identity conflict")
        _append_manifest_index(root, manifest_target, manifest)
        completed_at = datetime.fromisoformat(str(manifest["completed_at"]))
        return CollectionResult(
            collection_id=prepared.collection_id,
            outcome=prepared.outcome,
            accepted_documents=len(prepared.accepted_documents),
            candidate_counts=prepared.candidate_counts,
            manifest_path=str(manifest_target),
            completed_at=completed_at,
        )
    if not prepared.publication_items or prepared.publication_items[-1] != manifest_relative:
        raise ValueError("manifest must be the final publication item")

    if prepared.accepted_documents:
        store = document_store or configured_raw_document_store()
        for document in prepared.accepted_documents:
            source = staging / document.relative_path
            if not source.is_file():
                raise ValueError(f"prepared Artifact is missing: {source.name}")
            store.publish_markdown(
                bucket=bucket_from_url_path(document.url_path, document.relative_path),
                object_key=document.relative_path,
                content=source.read_bytes(),
                sha256=document.sha256,
            )

    for relative in prepared.publication_items:
        if relative == manifest_relative:
            continue
        source = staging / relative
        target = root / relative
        _publish_file(source, target, replace=relative == "indexes/title-dedup-index.tsv")

    manifest_source = staging / manifest_relative
    _publish_file(manifest_source, manifest_target, replace=False)
    manifest = json.loads(manifest_target.read_text(encoding="utf-8"))
    _append_manifest_index(root, manifest_target, manifest)
    completed_at = datetime.now(UTC)
    shutil.rmtree(staging, ignore_errors=True)
    return CollectionResult(
        collection_id=prepared.collection_id,
        outcome=prepared.outcome,
        accepted_documents=len(prepared.accepted_documents),
        candidate_counts=prepared.candidate_counts,
        manifest_path=str(manifest_target),
        completed_at=completed_at,
    )
