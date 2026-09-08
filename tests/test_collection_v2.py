"""Pure collection boundaries, failure ordering and concurrent archive regression tests."""

import asyncio
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.registry import Registry
from agno.workflow import Step, Workflow

from capabilities.collection import AdapterKey, Candidate, CollectionChannel, CollectionRequest, FetchRequest
from capabilities.collection_v2.functions import collect_raw_v2, publish_raw_v2
from capabilities.collection_v2.functions.collection import _publish
from capabilities.collection_v2.internal.acquisition import acquire
from capabilities.collection_v2.internal.models import RawCollectionResultV2
from capabilities.collection_v2.internal.storage import archive_candidate, run_root, write_text
from workflows.raw_collection_v2 import raw_collection_v2

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def candidate(identifier: str = "a", **updates: object) -> Candidate:
    return Candidate.model_validate(
        {
            "candidate_id": identifier,
            "connector": "rss",
            "query": "原查询",
            "title": "相同标题",
            "url": "https://example.com/news?a=1&utm_source=search#top",
            "content": "公司宣布新产线投产。",
            "source_name": "Source",
            "collected_at": NOW,
            **updates,
        }
    )


def channel(code: str = "rss", **updates: object) -> CollectionChannel:
    return CollectionChannel.model_validate(
        {
            "code": code,
            "name": code,
            "ownership_type": "dynamic",
            "channel_type": "rss",
            "adapter_key": "generic_rss",
            "enabled": True,
            "endpoint": "https://example.com/rss",
            "app_key": "secret-source-key",
            "config": {},
            "priority": 1,
            "max_results": 10,
            "default_source_level": "L3_MEDIA",
            "created_at": NOW,
            "updated_at": NOW,
            **updates,
        }
    )


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, bytes]] = []
        self.fail = False

    def publish_markdown(self, *, bucket: str, object_key: str, content: bytes, sha256: str) -> None:
        self.calls.append((object_key, content))
        if self.fail:
            raise RuntimeError("sensitive-storage-key")
        if object_key in self.objects and self.objects[object_key] != content:
            raise ValueError("Immutable object conflict")
        self.objects[object_key] = content


class StaticAdapter:
    def __init__(self, candidates: list[Candidate]) -> None:
        self.candidates = candidates
        self.calls: list[tuple[str, str]] = []

    async def fetch(self, source: CollectionChannel, request: FetchRequest) -> list[Candidate]:
        self.calls.append((source.code, request.query))
        if source.code == "broken":
            raise RuntimeError("secret-source-key")
        return self.candidates


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"COLLECTOR_V2_ARTIFACT_ROOT": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = MemoryStore()

    def test_url_body_version_identity_preserves_sources_and_changes(self) -> None:
        first = archive_candidate(candidate(), self.store)
        repeat = archive_candidate(
            candidate("b", url="https://example.com/news?a=1", collected_at=NOW + timedelta(days=1)), self.store
        )
        changed = archive_candidate(candidate("c", content="公司宣布新产线尚未投产。"), self.store)
        other_source = archive_candidate(candidate("d", url="https://other.example.com/news"), self.store)
        self.assertEqual(
            [first.status, repeat.status, changed.status, other_source.status],
            ["archived", "duplicate", "archived", "archived"],
        )
        self.assertEqual(first.article_key, repeat.article_key)
        self.assertEqual(len(self.store.calls), 3)
        self.assertTrue(first.url_path and first.url_path.startswith("/raw-evidence/collection-v2/documents/"))

    def test_failed_upload_has_no_success_receipt_and_freezes_retry_bytes(self) -> None:
        self.store.fail = True
        failed = archive_candidate(candidate(), self.store)
        root = Path(self.temp.name) / "articles" / str(failed.article_key)
        self.assertEqual(failed.status, "failed")
        self.assertFalse((root / "archived.json").exists())
        self.assertNotIn("sensitive-storage-key", (root / "last-error.json").read_text())
        self.store.fail = False
        retried = archive_candidate(candidate("later", collected_at=NOW + timedelta(hours=1)), self.store)
        self.assertEqual(retried.status, "archived")
        self.assertEqual(self.store.calls[0], self.store.calls[1])

    def test_upload_receipt_crash_is_idempotent(self) -> None:
        def fail_receipt(path: Path, content: str) -> None:
            if path.name == "archived.json":
                raise OSError("disk error")
            write_text(path, content)

        with patch("capabilities.collection_v2.internal.storage.write_text", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                archive_candidate(candidate(), self.store)
        result = archive_candidate(candidate("later", collected_at=NOW + timedelta(hours=2)), self.store)
        self.assertEqual(result.status, "archived")
        self.assertEqual(len(self.store.objects), 1)
        self.assertEqual(self.store.calls[0], self.store.calls[1])

    def test_concurrent_runs_upload_once(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: archive_candidate(candidate(), self.store), range(8)))
        self.assertEqual(sum(item.status == "archived" for item in results), 1)
        self.assertEqual(len(self.store.calls), 1)

    def test_empty_body_and_corrupt_original_never_upload(self) -> None:
        self.assertEqual(archive_candidate(candidate(content="  "), self.store).status, "invalid")
        first = archive_candidate(candidate(), self.store)
        root = Path(self.temp.name) / "articles" / str(first.article_key)
        (root / "original.md").write_text("corrupt")
        with self.assertRaisesRegex(ValueError, "checksum"):
            archive_candidate(candidate(), self.store)
        self.assertEqual(len(self.store.calls), 1)

    def test_run_id_cannot_escape_root(self) -> None:
        with self.assertRaises(ValueError):
            run_root("../../old-collector")


class AcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"COLLECTOR_V2_ARTIFACT_ROOT": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = MemoryStore()

    async def test_channels_keep_direct_query_and_isolate_provider_failure(self) -> None:
        adapter = StaticAdapter([candidate()])
        snapshot = [channel(), channel("broken"), channel("disabled", enabled=False)]
        with (
            patch(
                "capabilities.collection_v2.internal.acquisition.load_active_source_snapshot", return_value=snapshot
            ) as source,
            patch("capabilities.collection_v2.internal.acquisition.SOURCE_ADAPTERS", {AdapterKey.GENERIC_RSS: adapter}),
        ):
            result = await acquire("run-a", CollectionRequest(objective="政策、供需、上市公司；不要分解"))
            same = await acquire("run-a", CollectionRequest(objective="政策、供需、上市公司；不要分解"))
        self.assertEqual(source.call_count, 1)
        self.assertEqual(result, same)
        self.assertEqual({query for _, query in adapter.calls}, {"政策、供需、上市公司；不要分解"})
        self.assertEqual({code for code, _ in adapter.calls}, {"rss", "broken"})
        self.assertEqual(result.receipts[-1].outcome, "partial")
        self.assertEqual([group.outcome for group in result.receipts[:2]], ["no_channels", "no_channels"])
        self.assertNotIn("secret-source-key", (run_root("run-a") / "acquisition.json").read_text())
        with patch(
            "capabilities.collection_v2.functions.collection.configured_raw_document_store", return_value=self.store
        ):
            published = await asyncio.to_thread(_publish, "run-a")
        self.assertEqual(published.outcome, "partial")
        self.assertEqual(published.failed_channels, 1)
        self.assertEqual(published.archived, 1)

    async def test_invalid_query_and_multiple_search_sources_fail_before_fetch(self) -> None:
        with patch("capabilities.collection_v2.internal.acquisition.load_active_source_snapshot") as source:
            with self.assertRaises(ValueError):
                await acquire("long-query", CollectionRequest(objective="x" * 513))
            source.assert_not_called()
        search = channel("search", ownership_type="fixed", channel_type="web_search", adapter_key="bocha")
        with (
            patch(
                "capabilities.collection_v2.internal.acquisition.load_active_source_snapshot",
                return_value=[search, search.model_copy(update={"code": "search2"})],
            ),
            patch("capabilities.collection_v2.internal.acquisition.dispatch_channels") as dispatch,
        ):
            with self.assertRaisesRegex(ValueError, "one enabled"):
                await acquire("two-search", CollectionRequest(objective="query"))
            dispatch.assert_not_called()

    async def test_empty_collection_does_not_need_minio(self) -> None:
        with patch("capabilities.collection_v2.internal.acquisition.load_active_source_snapshot", return_value=[]):
            await acquire("empty", CollectionRequest(objective="query"))
        with patch("capabilities.collection_v2.functions.collection.configured_raw_document_store") as store:
            result = await asyncio.to_thread(_publish, "empty")
            store.assert_not_called()
        self.assertEqual(result.outcome, "no_change")

    async def test_upload_failure_continues_and_manifest_replay_does_not_repeat(self) -> None:
        adapter = StaticAdapter([candidate(), candidate("b", url="https://example.com/b")])
        with (
            patch(
                "capabilities.collection_v2.internal.acquisition.load_active_source_snapshot", return_value=[channel()]
            ),
            patch("capabilities.collection_v2.internal.acquisition.SOURCE_ADAPTERS", {AdapterKey.GENERIC_RSS: adapter}),
        ):
            await acquire("failed", CollectionRequest(objective="query"))
        self.store.fail = True
        with patch(
            "capabilities.collection_v2.functions.collection.configured_raw_document_store", return_value=self.store
        ):
            first = await asyncio.to_thread(_publish, "failed")
            repeated = await asyncio.to_thread(_publish, "failed")
        self.assertEqual(first, repeated)
        self.assertEqual(first.failed, 2)
        self.assertEqual(first.outcome, "failed")
        self.assertEqual(len(self.store.calls), 2)
        self.assertFalse(list(Path(self.temp.name).glob("articles/*/archived.json")))

    async def test_same_run_duplicate_does_not_retry_failed_upload(self) -> None:
        adapter = StaticAdapter([candidate(), candidate("duplicate")])
        with (
            patch(
                "capabilities.collection_v2.internal.acquisition.load_active_source_snapshot", return_value=[channel()]
            ),
            patch("capabilities.collection_v2.internal.acquisition.SOURCE_ADAPTERS", {AdapterKey.GENERIC_RSS: adapter}),
        ):
            await acquire("duplicate-failure", CollectionRequest(objective="query"))
        self.store.fail = True
        with patch(
            "capabilities.collection_v2.functions.collection.configured_raw_document_store", return_value=self.store
        ):
            result = await asyncio.to_thread(_publish, "duplicate-failure")
        self.assertEqual(result.failed, 2)
        self.assertEqual(len(self.store.calls), 1)

    async def test_registry_round_trip_executes_model_free_and_writes_only_v2(self) -> None:
        db = SqliteDb(db_file=str(Path(self.temp.name) / "workflow.db"))
        registry = Registry(functions=[collect_raw_v2, publish_raw_v2], schemas=[RawCollectionResultV2], dbs=[db])
        config = raw_collection_v2.to_dict()
        config["db"] = db.to_dict()
        workflow = Workflow.from_dict(config, db=db, registry=registry, strict=True)
        self.assertIsInstance(workflow.db, SqliteDb)
        assert isinstance(workflow.steps, list)
        self.assertEqual(len(workflow.steps), 2)
        for step in workflow.steps:
            assert isinstance(step, Step)
            self.assertIsNone(step.agent)
            self.assertIsNone(step.team)
        adapter = StaticAdapter([candidate()])
        old_root = Path(self.temp.name) / "old-collector"
        with (
            patch.dict(os.environ, {"COLLECTOR_ARTIFACT_ROOT": str(old_root)}),
            patch(
                "capabilities.collection_v2.internal.acquisition.load_active_source_snapshot", return_value=[channel()]
            ),
            patch("capabilities.collection_v2.internal.acquisition.SOURCE_ADAPTERS", {AdapterKey.GENERIC_RSS: adapter}),
            patch(
                "capabilities.collection_v2.functions.collection.configured_raw_document_store", return_value=self.store
            ),
            patch.object(Agent, "arun", side_effect=AssertionError("Model call forbidden")),
        ):
            result = await workflow.arun(input="原查询", session_id="isolated", stream=False)
        assert isinstance(result.content, RawCollectionResultV2)
        self.assertEqual(result.content.archived, 1)
        self.assertFalse(old_root.exists())
        manifest = json.loads(Path(result.content.manifest_path).read_text())
        self.assertEqual(manifest["items"][0]["status"], "archived")


if __name__ == "__main__":
    unittest.main()
