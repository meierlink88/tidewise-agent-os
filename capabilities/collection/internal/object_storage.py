"""Raw Collection document object-storage contract and MinIO Adapter."""

import json
import os
import re
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


def configured_minio_client() -> Minio:
    """Build the authenticated MinIO client from runtime configuration."""
    configured = os.getenv("MINIO_ENDPOINT", "").strip()
    access_key = os.getenv("MINIO_ACCESS_KEY", "").strip()
    secret_key = os.getenv("MINIO_SECRET_KEY", "").strip()
    if not configured or not access_key or not secret_key:
        raise ValueError("MINIO_ENDPOINT, MINIO_ACCESS_KEY and MINIO_SECRET_KEY are required")
    parsed = urlsplit(configured if "://" in configured else f"http://{configured}")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("MINIO_ENDPOINT is invalid")
    endpoint = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
    return Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=parsed.scheme == "https")


def configured_raw_document_store() -> MinioRawDocumentStore:
    return MinioRawDocumentStore(configured_minio_client())


def raw_evidence_bucket() -> str:
    bucket = os.getenv("RAW_EVIDENCE_BUCKET", "raw-evidence").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("RAW_EVIDENCE_BUCKET must be a valid lowercase S3 bucket name")
    return bucket


def ensure_raw_evidence_bucket() -> None:
    """Create the dedicated bucket if absent and allow anonymous object reads only."""
    client = configured_minio_client()
    bucket = raw_evidence_bucket()
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/*"],
            }
        ],
    }
    client.set_bucket_policy(bucket, json.dumps(policy, separators=(",", ":")))


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
