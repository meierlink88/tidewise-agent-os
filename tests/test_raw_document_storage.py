"""Tests for immutable Raw Evidence publication to MinIO."""

import json
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from minio.error import S3Error

from capabilities.collection.internal.object_storage import (
    MinioRawDocumentStore,
    bucket_from_url_path,
    configured_minio_client,
    ensure_raw_evidence_bucket,
    raw_evidence_bucket,
)


def _s3_error(code: str) -> S3Error:
    return S3Error(cast(Any, None), code, "test", None, None, None)


class FakeMinioClient:
    def __init__(self, stat_result: object | Exception) -> None:
        self.stat_result = stat_result
        self.puts: list[tuple[str, str, bytes, int, str, dict[str, str]]] = []

    def stat_object(self, bucket_name: str, object_name: str) -> object:
        del bucket_name, object_name
        if isinstance(self.stat_result, Exception):
            raise self.stat_result
        return self.stat_result

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: Any,
        length: int,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> object:
        payload = cast(bytes, data.read())
        self.puts.append((bucket_name, object_name, payload, length, content_type, metadata))
        return object()


class FakeBucketClient:
    def __init__(self, exists: bool) -> None:
        self.exists = exists
        self.created: list[str] = []
        self.policies: list[tuple[str, str]] = []

    def bucket_exists(self, bucket: str) -> bool:
        del bucket
        return self.exists

    def make_bucket(self, bucket: str) -> None:
        self.created.append(bucket)

    def set_bucket_policy(self, bucket: str, policy: str) -> None:
        self.policies.append((bucket, policy))


class MinioRawDocumentStoreTest(unittest.TestCase):
    def test_minio_endpoint_rejects_credentials_paths_and_non_http_schemes(self) -> None:
        common = {"MINIO_ACCESS_KEY": "user", "MINIO_SECRET_KEY": "secret-value"}
        for endpoint in ("ftp://minio:9000", "http://user:pass@minio:9000", "http://minio:9000/path"):
            with (
                self.subTest(endpoint=endpoint),
                patch.dict("os.environ", {**common, "MINIO_ENDPOINT": endpoint}, clear=True),
                self.assertRaisesRegex(ValueError, "MINIO_ENDPOINT is invalid"),
            ):
                configured_minio_client()

    def test_bucket_name_is_validated_before_use(self) -> None:
        with patch.dict("os.environ", {"RAW_EVIDENCE_BUCKET": "Invalid_Bucket"}, clear=True):
            with self.assertRaisesRegex(ValueError, "valid lowercase S3 bucket"):
                raw_evidence_bucket()

    def test_bucket_is_recovered_from_the_frozen_url_path(self) -> None:
        self.assertEqual(
            bucket_from_url_path(
                "/raw-evidence/documents/2026/08/15/article.md",
                "documents/2026/08/15/article.md",
            ),
            "raw-evidence",
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            bucket_from_url_path(
                "/raw-evidence/documents/2026/08/15/other.md",
                "documents/2026/08/15/article.md",
            )

    def test_missing_object_is_uploaded_as_inline_markdown_with_hash_metadata(self) -> None:
        client = FakeMinioClient(_s3_error("NoSuchKey"))
        store = MinioRawDocumentStore(client)

        store.publish_markdown(
            bucket="raw-evidence",
            object_key="documents/2026/08/15/article.md",
            content=b"# Article\n",
            sha256="a" * 64,
        )

        self.assertEqual(
            client.puts,
            [
                (
                    "raw-evidence",
                    "documents/2026/08/15/article.md",
                    b"# Article\n",
                    10,
                    "text/markdown; charset=utf-8",
                    {"Content-Disposition": "inline", "content-sha256": "a" * 64},
                )
            ],
        )

    def test_matching_object_is_an_idempotent_success(self) -> None:
        client = FakeMinioClient(SimpleNamespace(size=10, metadata={"X-Amz-Meta-Content-Sha256": "a" * 64}))
        store = MinioRawDocumentStore(client)
        store.publish_markdown(
            bucket="raw-evidence",
            object_key="documents/2026/08/15/article.md",
            content=b"# Article\n",
            sha256="a" * 64,
        )
        self.assertEqual(client.puts, [])

    def test_existing_object_with_different_identity_is_rejected(self) -> None:
        client = FakeMinioClient(SimpleNamespace(size=10, metadata={"x-amz-meta-content-sha256": "b" * 64}))
        store = MinioRawDocumentStore(client)
        with self.assertRaisesRegex(ValueError, "immutable Raw Evidence object conflict"):
            store.publish_markdown(
                bucket="raw-evidence",
                object_key="documents/2026/08/15/article.md",
                content=b"# Article\n",
                sha256="a" * 64,
            )
        self.assertEqual(client.puts, [])

    def test_missing_bucket_is_not_treated_as_a_missing_object(self) -> None:
        client = FakeMinioClient(_s3_error("NoSuchBucket"))
        store = MinioRawDocumentStore(client)
        with self.assertRaises(S3Error):
            store.publish_markdown(
                bucket="raw-evidence",
                object_key="documents/2026/08/15/article.md",
                content=b"# Article\n",
                sha256="a" * 64,
            )
        self.assertEqual(client.puts, [])

    def test_bucket_initialization_creates_bucket_and_limits_anonymous_access_to_get(self) -> None:
        for exists in (False, True):
            with self.subTest(exists=exists):
                client = FakeBucketClient(exists)
                with patch(
                    "capabilities.collection.internal.object_storage.configured_minio_client",
                    return_value=client,
                ):
                    ensure_raw_evidence_bucket()
                self.assertEqual(client.created, [] if exists else ["raw-evidence"])
                self.assertEqual(len(client.policies), 1)
                policy = json.loads(client.policies[0][1])
                self.assertEqual(policy["Statement"][0]["Action"], ["s3:GetObject"])
                self.assertEqual(policy["Statement"][0]["Resource"], ["arn:aws:s3:::raw-evidence/*"])


if __name__ == "__main__":
    unittest.main()
