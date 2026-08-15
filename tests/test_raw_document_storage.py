"""Tests for immutable Raw Evidence publication to MinIO."""

import unittest
from types import SimpleNamespace
from typing import Any, cast

from minio.error import S3Error

from capabilities.collection.internal.object_storage import MinioRawDocumentStore


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


class MinioRawDocumentStoreTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
