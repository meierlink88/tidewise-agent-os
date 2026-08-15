"""Raw Collection document object-storage contract and MinIO Adapter."""

import os
from io import BytesIO
from typing import Any, Protocol
from urllib.parse import urlsplit

from minio import Minio
from minio.error import S3Error


class RawDocumentStore(Protocol):
    """Publish immutable normalized Markdown documents."""

    def publish_markdown(self, *, bucket: str, object_key: str, content: bytes, sha256: str) -> None: ...


class MinioRawDocumentStore:
    """Publish immutable Markdown objects through one configured MinIO client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def publish_markdown(self, *, bucket: str, object_key: str, content: bytes, sha256: str) -> None:
        try:
            existing = self._client.stat_object(bucket, object_key)
        except S3Error as exc:
            if exc.code not in {"NoSuchKey", "NoSuchObject"}:
                raise
        else:
            metadata = {key.lower(): value for key, value in (existing.metadata or {}).items()}
            if existing.size == len(content) and metadata.get("x-amz-meta-content-sha256") == sha256:
                return
            raise ValueError(f"immutable Raw Evidence object conflict: {object_key}")

        self._client.put_object(
            bucket,
            object_key,
            BytesIO(content),
            len(content),
            content_type="text/markdown; charset=utf-8",
            metadata={"Content-Disposition": "inline", "content-sha256": sha256},
        )


def configured_raw_document_store() -> MinioRawDocumentStore:
    configured = os.getenv("MINIO_ENDPOINT", "").strip()
    access_key = os.getenv("MINIO_ACCESS_KEY", "").strip()
    secret_key = os.getenv("MINIO_SECRET_KEY", "").strip()
    if not configured or not access_key or not secret_key:
        raise ValueError("MINIO_ENDPOINT, MINIO_ACCESS_KEY and MINIO_SECRET_KEY are required")
    parsed = urlsplit(configured if "://" in configured else f"http://{configured}")
    if not parsed.hostname:
        raise ValueError("MINIO_ENDPOINT is invalid")
    endpoint = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
    return MinioRawDocumentStore(
        Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=parsed.scheme == "https")
    )


def raw_evidence_bucket() -> str:
    bucket = os.getenv("RAW_EVIDENCE_BUCKET", "raw-evidence").strip()
    if not bucket:
        raise ValueError("RAW_EVIDENCE_BUCKET must not be blank")
    return bucket


def raw_evidence_url_path(object_key: str) -> str:
    key = object_key.strip().lstrip("/")
    if not key or any(part in {"", ".", ".."} for part in key.split("/")):
        raise ValueError("Raw Evidence object key must not be blank")
    return f"/{raw_evidence_bucket()}/{key}"


def bucket_from_url_path(url_path: str, object_key: str) -> str:
    """Recover the bucket frozen into one prepared document URL path."""
    key = object_key.strip().lstrip("/")
    suffix = f"/{key}"
    parsed = urlsplit(url_path)
    if (
        not key
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
        or not parsed.path.endswith(suffix)
    ):
        raise ValueError("Raw Evidence URL path does not match its object key")
    bucket = parsed.path[1 : -len(suffix)]
    if not bucket or "/" in bucket:
        raise ValueError("Raw Evidence URL path has an invalid bucket")
    return bucket
