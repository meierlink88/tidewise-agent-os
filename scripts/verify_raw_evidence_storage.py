"""Initialize and verify the AgentOS-owned Raw Evidence MinIO boundary."""

from __future__ import annotations

import argparse
import hashlib
import os
from urllib.request import urlopen

from minio.error import S3Error

from capabilities.collection.internal.object_storage import (
    MinioRawDocumentStore,
    configured_minio_client,
    ensure_raw_evidence_bucket,
    raw_evidence_bucket,
    raw_evidence_url_path,
)


def _identity(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 40 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("storage verification identity must be a full lowercase Git SHA")
    return normalized


def _marker(identity: str) -> tuple[str, bytes, str]:
    content = f"tidewise-agentos-uat-minio-persistence:{identity}\n".encode()
    digest = hashlib.sha256(content).hexdigest()
    return f"_uat/persistence/{identity}.txt", content, digest


def write_marker(identity: str) -> None:
    ensure_raw_evidence_bucket()
    object_key, content, digest = _marker(_identity(identity))
    MinioRawDocumentStore(configured_minio_client()).publish_markdown(
        bucket=raw_evidence_bucket(),
        object_key=object_key,
        content=content,
        sha256=digest,
    )


def verify_marker(identity: str) -> None:
    object_key, expected, _digest = _marker(_identity(identity))
    client = configured_minio_client()
    response = client.get_object(raw_evidence_bucket(), object_key)
    try:
        actual = response.read()
    finally:
        response.close()
        response.release_conn()
    if actual != expected:
        raise ValueError("Raw Evidence MinIO persistence marker changed across restart")
    client.remove_object(raw_evidence_bucket(), object_key)


def smoke(identity: str) -> None:
    ensure_raw_evidence_bucket()
    normalized = _identity(identity)
    object_key = f"_uat/smoke/{normalized}.md"
    content = f"# AgentOS UAT MinIO smoke\n\nrelease: {normalized}\n".encode()
    digest = hashlib.sha256(content).hexdigest()
    client = configured_minio_client()
    store = MinioRawDocumentStore(client)
    store.publish_markdown(bucket=raw_evidence_bucket(), object_key=object_key, content=content, sha256=digest)
    # A repeated immutable publication must be harmless.
    store.publish_markdown(bucket=raw_evidence_bucket(), object_key=object_key, content=content, sha256=digest)
    base_url = os.environ.get("RAW_EVIDENCE_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise ValueError("RAW_EVIDENCE_PUBLIC_BASE_URL is required")
    try:
        with urlopen(f"{base_url}{raw_evidence_url_path(object_key)}", timeout=10) as response:  # noqa: S310
            if response.status != 200 or response.read() != content:
                raise ValueError("anonymous Raw Evidence object read did not return the exact stored content")
            content_type = response.headers.get_content_type()
            if content_type != "text/markdown":
                raise ValueError(f"Raw Evidence object content type is {content_type!r}, expected 'text/markdown'")
    finally:
        client.remove_object(raw_evidence_bucket(), object_key)
    try:
        client.stat_object(raw_evidence_bucket(), object_key)
    except S3Error as exc:
        if exc.code not in {"NoSuchKey", "NoSuchObject"}:
            raise
    else:
        raise ValueError("Raw Evidence MinIO smoke object was not cleaned up")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("initialize", "write-marker", "verify-marker", "smoke"))
    parser.add_argument("--identity", default=os.environ.get("RELEASE_SHA", ""))
    args = parser.parse_args()
    if args.mode == "initialize":
        ensure_raw_evidence_bucket()
    elif args.mode == "write-marker":
        write_marker(args.identity)
    elif args.mode == "verify-marker":
        verify_marker(args.identity)
    else:
        smoke(args.identity)
    print(f"PASS raw-evidence-minio-{args.mode}")


if __name__ == "__main__":
    main()
