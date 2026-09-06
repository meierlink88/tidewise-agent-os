"""Durable per-article review queue, exact version identities and fenced claims."""

import hashlib
import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any
from uuid import uuid4

from capabilities.collection.internal.artifacts import _canonical_url, _document_markdown, _relative_document_path
from capabilities.collection.internal.buffer import artifact_root, write_json, write_text
from capabilities.collection.internal.models import Candidate
from capabilities.collection.internal.object_storage import configured_raw_document_store, raw_evidence_bucket
from capabilities.evidence import PreparedRawDocument

CLAIM_SECONDS = 900


def queue_root() -> Path:
    return artifact_root() / "article-queue"


def item_root(key: str) -> Path:
    if re.fullmatch(r"[a-f0-9]{64}", key) is None:
        raise ValueError("Invalid article identity")
    return queue_root() / "items" / key


@contextmanager
def queue_lock() -> Iterator[None]:
    root = queue_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "queue.lock").open("a+") as handle:
        flock(handle, LOCK_EX)
        try:
            yield
        finally:
            flock(handle, LOCK_UN)


def read_state(key: str) -> dict[str, Any]:
    return json.loads((item_root(key) / "state.json").read_text())


def read_prepared(key: str) -> PreparedRawDocument:
    return PreparedRawDocument.model_validate_json((item_root(key) / "article.json").read_text())


def _enqueue(prepared: PreparedRawDocument, markdown: str, exclusion_reason: str | None = None) -> tuple[str, bool]:
    key = hashlib.sha256(prepared.publication_key.encode()).hexdigest()
    path = item_root(key)
    with queue_lock():
        if (path / "state.json").exists():
            existing = read_prepared(key)
            if existing.source_url != prepared.source_url or existing.content_sha256 != prepared.content_sha256:
                raise ValueError("Article identity conflict")
            # A crash between state creation and marker creation must not lose work.
            if read_state(key)["status"] not in {"completed", "excluded"}:
                write_json(queue_root() / "pending" / f"{key}.json", {"article_key": key})
            return key, False
        write_json(path / "article.json", prepared.model_dump(mode="json"))
        write_text(path / "original.md", markdown)
        status = "excluded" if exclusion_reason else "pending"
        result = {"reason": exclusion_reason} if exclusion_reason else {}
        write_json(path / "state.json", {"status": status, "attempts": 0, "result": result})
        write_json(queue_root() / status / f"{key}.json", {"article_key": key, **result})
    return key, True


def enqueue_candidate(
    candidate: Candidate, collection_id: str, *, exclusion_reason: str | None = None
) -> tuple[str, bool]:
    """URL + body version, not title similarity, determines article identity."""
    url = _canonical_url(str(candidate.url))
    body = candidate.content.strip()
    if not body:
        raise ValueError("Article body is empty")
    digest = hashlib.sha256(body.encode()).hexdigest()
    publication_key = "agentos.raw-evidence.v1:" + hashlib.sha256(f"{url}\n{digest}".encode()).hexdigest()
    markdown = _document_markdown(candidate, url, digest)
    document_sha = hashlib.sha256(markdown.encode()).hexdigest()
    relative = _relative_document_path(candidate.published_at, candidate.collected_at, document_sha).as_posix()
    prepared = PreparedRawDocument(
        collection_id=collection_id,
        manifest_path=f"article-queue/{hashlib.sha256(publication_key.encode()).hexdigest()}",
        manifest_offset=0,
        next_manifest_offset=0,
        document_index=0,
        document_count=1,
        document_path=relative,
        document_url_path=f"/{raw_evidence_bucket()}/{relative}",
        document_sha256=document_sha,
        content_sha256=digest,
        publication_key=publication_key,
        source_id=candidate.connector
        if len(candidate.connector) <= 32
        else "SRC_" + hashlib.sha256(candidate.connector.encode()).hexdigest()[:28],
        source_name=candidate.source_name[:100],
        source_level=candidate.source_level.value,
        source_url=url,
        title=candidate.title[:500],
        raw_text=body,
        published_at=candidate.published_at,
        collected_at=candidate.collected_at,
    )
    return _enqueue(prepared, markdown, exclusion_reason)


def enqueue_legacy_document(prepared: PreparedRawDocument) -> tuple[str, bool]:
    """Keep historical source/payload identity exact for idempotent recovery."""
    path = (artifact_root() / prepared.document_path).resolve()
    if not path.is_relative_to(artifact_root().resolve()):
        raise ValueError("Legacy document escapes artifact root")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != prepared.document_sha256:
        raise ValueError("Legacy document hash mismatch")
    return _enqueue(prepared, payload.decode())


def claim_next(owner: str) -> dict[str, str] | None:
    """Expired claims can be retried; tokens fence writes from superseded workers."""
    with queue_lock():
        for marker in sorted((queue_root() / "pending").glob("*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name)):
            key = marker.stem
            state = read_state(key)
            if state["status"] in {"completed", "excluded"}:
                marker.unlink(missing_ok=True)
                continue
            if state.get("expires_at", 0) > time.time():
                continue
            token = str(uuid4())
            state.update(
                owner=owner, token=token, expires_at=time.time() + CLAIM_SECONDS, attempts=state["attempts"] + 1
            )
            write_json(item_root(key) / "state.json", state)
            return {"article_key": key, "token": token}
    return None


def _owned(claim: dict[str, str]) -> dict[str, Any]:
    state = read_state(claim["article_key"])
    if state.get("token") != claim["token"] or state.get("expires_at", 0) <= time.time():
        raise ValueError("Article claim expired or superseded")
    return state


def save_claimed(claim: dict[str, str], filename: str, payload: object) -> None:
    if filename not in {"review.json", "publication.json"}:
        raise ValueError("Unknown article payload")
    with queue_lock():
        _owned(claim)
        path = item_root(claim["article_key"]) / filename
        if path.exists():
            if json.loads(path.read_text()) != payload:
                raise ValueError("Immutable article payload conflict")
            return
        write_json(path, payload)


def finish_claim(claim: dict[str, str], status: str, result: dict[str, Any]) -> None:
    if status not in {"completed", "excluded"}:
        raise ValueError("Unknown terminal article state")
    with queue_lock():
        state = _owned(claim)
        key = claim["article_key"]
        state.update(status=status, result=result, expires_at=0)
        # Write the terminal archive before committing state and removing its queue marker.
        write_json(queue_root() / status / f"{key}.json", {"article_key": key, **result})
        write_json(item_root(key) / "state.json", state)
        (queue_root() / "pending" / f"{key}.json").unlink(missing_ok=True)


def fail_claim(claim: dict[str, str], error_code: str) -> None:
    with queue_lock():
        key = claim["article_key"]
        state = read_state(key)
        if state.get("token") != claim["token"] or state["status"] in {"completed", "excluded"}:
            return
        state.update(expires_at=0, last_error=error_code)
        write_json(queue_root() / "failed" / f"{key}.json", {"article_key": key, "error_code": error_code})
        write_json(item_root(key) / "state.json", state)


def reject_review(claim: dict[str, str]) -> None:
    """Keep invalid output for audit but permit a fresh semantic attempt."""
    with queue_lock():
        state = _owned(claim)
        root = item_root(claim["article_key"])
        if (root / "publication.json").exists():
            return
        review = root / "review.json"
        if review.exists():
            archive = root / "rejected-reviews" / f"{state['attempts']}-{claim['token']}.json"
            archive.parent.mkdir(parents=True, exist_ok=True)
            review.replace(archive)


def upload_article(claim: dict[str, str], prepared: PreparedRawDocument | None = None) -> None:
    """Only publication-eligible articles leave the local queue for MinIO."""
    with queue_lock():
        _owned(claim)
    queued = read_prepared(claim["article_key"])
    prepared = prepared or queued
    if prepared.publication_key != queued.publication_key:
        raise ValueError("Article publication identity mismatch")
    path = item_root(claim["article_key"]) / "original.md"
    if prepared.document_sha256 != queued.document_sha256:
        path = (artifact_root() / prepared.document_path).resolve()
        if not path.is_relative_to(artifact_root().resolve()):
            raise ValueError("Recovered document escapes artifact root")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != prepared.document_sha256:
        raise ValueError("Article Markdown hash mismatch")
    configured_raw_document_store().publish_markdown(
        bucket=prepared.document_url_path.split("/")[1],
        object_key=prepared.document_path,
        content=content,
        sha256=prepared.document_sha256,
    )


def queue_counts() -> dict[str, int]:
    return {name: sum(1 for _ in (queue_root() / name).glob("*.json")) for name in ("pending", "completed", "excluded")}
