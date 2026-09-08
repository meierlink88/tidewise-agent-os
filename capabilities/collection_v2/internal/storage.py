"""Isolated immutable documents, per-identity locks and crash-safe V2 receipts."""

import fcntl
import hashlib
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from capabilities.collection import (
    Candidate,
    RawDocumentStore,
    canonical_source_url,
    raw_evidence_bucket,
    render_raw_markdown,
)
from capabilities.collection_v2.internal.models import RawArchiveItemV2, RawDocumentV2


def artifact_root() -> Path:
    return Path(os.getenv("COLLECTOR_V2_ARTIFACT_ROOT", "data/collector_v2")).resolve()


def run_root(collection_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", collection_id):
        raise ValueError("Invalid collection run ID")
    return artifact_root() / "runs" / collection_id


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def archive_candidate(candidate: Candidate, store: RawDocumentStore) -> RawArchiveItemV2:
    """Freeze one URL/body version before upload; only a successful upload earns a receipt."""
    try:
        url = canonical_source_url(str(candidate.url))
        body = candidate.content.strip()
        if not body:
            raise ValueError("Empty article body")
    except ValueError:
        return RawArchiveItemV2(candidate_id=candidate.candidate_id, status="invalid", error_code="invalid_article")
    digest = sha256(body)
    identity = sha256(f"{url}\n{digest}")
    publication_key = f"agentos.raw-evidence.v1:{identity}"
    # Preserve the existing publication identity, but isolate objects and local state.
    key = sha256(publication_key)
    directory = artifact_root() / "articles" / key
    with file_lock(directory / ".lock"):
        document_path = directory / "document.json"
        if document_path.exists():
            document = RawDocumentV2.model_validate_json(document_path.read_text())
            if (
                document.article_key != key
                or document.publication_key != publication_key
                or document.canonical_url != url
                or document.content_sha256 != digest
                or document.candidate.content.strip() != body
            ):
                raise ValueError("Frozen raw document identity conflict")
        else:
            markdown = render_raw_markdown(candidate, url, digest)
            bucket = raw_evidence_bucket()
            object_key = f"collection-v2/documents/{key}.md"
            document = RawDocumentV2(
                article_key=key,
                publication_key=publication_key,
                canonical_url=url,
                content_sha256=digest,
                document_sha256=sha256(markdown),
                bucket=bucket,
                object_key=object_key,
                url_path=f"/{bucket}/{object_key}",
                candidate=candidate,
            )
            write_text(document_path, document.model_dump_json(indent=2))
        markdown = render_raw_markdown(document.candidate, document.canonical_url, document.content_sha256)
        if sha256(markdown) != document.document_sha256:
            raise ValueError("Frozen raw Markdown checksum mismatch")
        local_path = directory / "original.md"
        if local_path.exists():
            if sha256(local_path.read_text()) != document.document_sha256:
                raise ValueError("Local raw Markdown checksum mismatch")
        else:
            write_text(local_path, markdown)
        archived_path = directory / "archived.json"
        if archived_path.exists():
            saved = RawArchiveItemV2.model_validate_json(archived_path.read_text())
            if saved.status != "archived" or saved.article_key != key or saved.url_path != document.url_path:
                raise ValueError("Raw archive receipt identity conflict")
            return RawArchiveItemV2(
                candidate_id=candidate.candidate_id, status="duplicate", article_key=key, url_path=document.url_path
            )
        try:
            store.publish_markdown(
                bucket=document.bucket,
                object_key=document.object_key,
                content=markdown.encode("utf-8"),
                sha256=document.document_sha256,
            )
        except Exception as exc:
            result = RawArchiveItemV2(
                candidate_id=candidate.candidate_id, status="failed", article_key=key, error_code=type(exc).__name__
            )
            # Raw provider/storage exception text can contain credentials. Retain only its type.
            write_text(directory / "last-error.json", result.model_dump_json(indent=2))
            return result
        result = RawArchiveItemV2(
            candidate_id=candidate.candidate_id, status="archived", article_key=key, url_path=document.url_path
        )
        write_text(archived_path, result.model_dump_json(indent=2))
        return result
