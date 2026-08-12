"""Behavior tests for database-driven collection channels."""

import asyncio
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, cast
from unittest.mock import AsyncMock, patch

from agno.run import RunContext
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from capabilities.raw_collection.adapters.base import FetchRequest
from capabilities.raw_collection.adapters.registry import ADAPTERS
from capabilities.raw_collection.adapters.rss import GenericRssAdapter
from capabilities.raw_collection.adapters.web_search import BochaAdapter
from capabilities.raw_collection.channels.models import (
    ChannelType,
    CollectionChannel,
    OwnershipType,
)
from capabilities.raw_collection.channels.repository import ChannelRepository
from capabilities.raw_collection.models import Candidate, FetchReceipt, SourceLevel
from capabilities.raw_collection.tools import api_fetch, rss_fetch, web_fetch


class _Catalog:
    def __init__(self, channels: list[CollectionChannel]) -> None:
        self.channels = channels

    def list_enabled(self, channel_type: ChannelType) -> list[CollectionChannel]:
        return [item for item in self.channels if item.enabled and item.channel_type == channel_type]


class _Adapter:
    def __init__(
        self,
        *,
        title: str = "测试资讯",
        delay: float = 0,
        error: Exception | None = None,
    ) -> None:
        self.title = title
        self.delay = delay
        self.error = error
        self.calls = 0

    async def fetch(self, channel: CollectionChannel, request: object) -> list[Candidate]:
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        query = cast(str, getattr(request, "query"))
        published_before = cast(datetime, getattr(request, "published_before"))
        return [
            Candidate(
                candidate_id=f"candidate-{channel.code}",
                connector=channel.code,
                query=query,
                title=self.title,
                url=f"https://example.com/{channel.code}",
                content=f"{self.title}正文",
                source_name=channel.name,
                source_level=channel.default_source_level,
                published_at=published_before,
                collected_at=published_before,
            )
        ]


def _channel(
    code: str,
    channel_type: ChannelType,
    adapter_key: str,
    *,
    ownership_type: OwnershipType = OwnershipType.FIXED,
    priority: int = 1,
) -> CollectionChannel:
    return CollectionChannel(
        code=code,
        name=code,
        ownership_type=ownership_type,
        channel_type=channel_type,
        adapter_key=adapter_key,
        enabled=True,
        endpoint=f"https://example.com/{code}",
        app_key=None,
        config={},
        priority=priority,
        timeout_seconds=30,
        max_results=10,
        default_source_level=SourceLevel.L3_MEDIA,
        created_at=datetime(2026, 8, 12, tzinfo=UTC),
        updated_at=datetime(2026, 8, 12, tzinfo=UTC),
    )


class CollectionChannelToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            os.environ,
            {"COLLECTOR_ARTIFACT_ROOT": str(Path(self.temporary.name) / "collector")},
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    @staticmethod
    def _context(channels: list[CollectionChannel], adapters: dict[str, _Adapter], run_id: str) -> RunContext:
        return RunContext(
            run_id=run_id,
            session_id="session",
            dependencies={
                "collector_agent_component_id": "raw-collector",
                "collector_agent_config_version": 7,
                "collector_instructions_sha256": "a" * 64,
                "collector_cutoff": "2026-08-12T10:00:00+00:00",
                "collection_channel_catalog": _Catalog(channels),
                "collection_adapter_registry": adapters,
            },
        )

    async def test_web_fetch_executes_the_only_enabled_search_channel(self) -> None:
        channel = _channel("bocha", ChannelType.WEB_SEARCH, "bocha")
        adapter = _Adapter()

        response = await web_fetch("中国宏观经济", self._context([channel], {"bocha": adapter}, "run-web"), 48)

        receipt = FetchReceipt.model_validate_json(response)
        self.assertEqual(receipt.tool, "web_fetch")
        self.assertEqual(receipt.outcome, "succeeded")
        self.assertEqual(receipt.requested_after, datetime(2026, 8, 10, 10, tzinfo=UTC))
        self.assertEqual(receipt.requested_before, datetime(2026, 8, 12, 10, tzinfo=UTC))
        self.assertEqual([item.channel_code for item in receipt.channels], ["bocha"])
        self.assertEqual(receipt.channels[0].result_count, 1)
        self.assertEqual(adapter.calls, 1)

    async def test_web_fetch_fails_closed_when_catalog_exposes_multiple_enabled_channels(self) -> None:
        channels = [
            _channel("bocha", ChannelType.WEB_SEARCH, "bocha"),
            _channel("tavily", ChannelType.WEB_SEARCH, "tavily"),
        ]

        response = await web_fetch(
            "中国宏观经济",
            self._context(channels, {"bocha": _Adapter(), "tavily": _Adapter()}, "run-invalid-web"),
            48,
        )

        self.assertEqual(json.loads(response), {"tool": "web_fetch", "error": "invalid_channel_catalog"})

    async def test_fetch_returns_no_channels_without_writing_a_batch(self) -> None:
        response = await rss_fetch("政治经济", self._context([], {}, "run-no-rss"), 48)

        receipt = FetchReceipt.model_validate_json(response)
        self.assertEqual(receipt.outcome, "no_channels")
        self.assertEqual(receipt.channels, [])

    async def test_api_fetch_runs_all_channels_concurrently_and_isolates_failure(self) -> None:
        channels = [
            _channel("cls_telegraph", ChannelType.API, "cls", priority=1),
            _channel("eastmoney_fastnews", ChannelType.API, "eastmoney_fast", priority=1),
        ]
        slow_success = _Adapter(delay=0.05)
        fast_failure = _Adapter(delay=0.05, error=RuntimeError("provider secret must not escape"))
        context = self._context(
            channels,
            {"cls": slow_success, "eastmoney_fast": fast_failure},
            "run-api",
        )

        started = asyncio.get_running_loop().time()
        response = await api_fetch("产业政策", context, 24)
        elapsed = asyncio.get_running_loop().time() - started

        receipt = FetchReceipt.model_validate_json(response)
        self.assertLess(elapsed, 0.09)
        self.assertEqual(receipt.outcome, "partial")
        self.assertEqual([item.channel_code for item in receipt.channels], ["cls_telegraph", "eastmoney_fastnews"])
        self.assertEqual([item.outcome for item in receipt.channels], ["succeeded", "failed"])
        self.assertEqual(receipt.channels[1].error_code, "request_failed")
        self.assertNotIn("provider secret", response)

    async def test_api_fetch_respects_the_configured_concurrency_bound(self) -> None:
        channels = [
            _channel("first-api", ChannelType.API, "cls"),
            _channel("second-api", ChannelType.API, "eastmoney_fast"),
        ]
        context = self._context(
            channels,
            {"cls": _Adapter(delay=0.04), "eastmoney_fast": _Adapter(delay=0.04)},
            "run-bound",
        )

        with patch.dict(os.environ, {"COLLECTOR_CHANNEL_CONCURRENCY": "1"}):
            started = asyncio.get_running_loop().time()
            response = await api_fetch("产业政策", context, 48)
            elapsed = asyncio.get_running_loop().time() - started

        self.assertEqual(FetchReceipt.model_validate_json(response).outcome, "succeeded")
        self.assertGreaterEqual(elapsed, 0.075)

    async def test_rss_fetch_uses_one_generic_adapter_for_dynamic_channels(self) -> None:
        channels = [
            _channel(
                "people-rss",
                ChannelType.RSS,
                "generic_rss",
                ownership_type=OwnershipType.DYNAMIC,
            ),
            _channel(
                "xinhua-rss",
                ChannelType.RSS,
                "generic_rss",
                ownership_type=OwnershipType.DYNAMIC,
            ),
        ]
        adapter = _Adapter()

        response = await rss_fetch(
            "政治经济",
            self._context(channels, {"generic_rss": adapter}, "run-rss"),
            48,
        )

        receipt = FetchReceipt.model_validate_json(response)
        self.assertEqual(receipt.outcome, "succeeded")
        self.assertEqual([item.channel_code for item in receipt.channels], ["people-rss", "xinhua-rss"])
        self.assertEqual(adapter.calls, 2)


class CollectionChannelRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = ChannelRepository(create_engine("sqlite+pysqlite:///:memory:"))
        self.repository.ensure_catalog()

    def test_fixed_channels_are_seeded_once_without_overwriting_operator_state(self) -> None:
        channels = self.repository.list_all()
        self.assertEqual(len(channels), 7)
        self.assertEqual(
            [item.code for item in channels if item.channel_type == ChannelType.WEB_SEARCH and item.enabled],
            ["bocha"],
        )

        self.repository.update_channel("bocha", enabled=False, app_key="operator-key")
        self.repository.ensure_catalog()

        bocha = next(item for item in self.repository.list_all() if item.code == "bocha")
        self.assertFalse(bocha.enabled)
        self.assertEqual(bocha.app_key, "operator-key")

    def test_fixed_channel_cannot_be_deleted_but_dynamic_rss_can(self) -> None:
        with self.assertRaisesRegex(ValueError, "fixed channel cannot be deleted"):
            self.repository.delete_channel("bocha")

        dynamic = _channel(
            "people-rss",
            ChannelType.RSS,
            "generic_rss",
            ownership_type=OwnershipType.DYNAMIC,
        )
        self.repository.create_dynamic(dynamic)
        self.assertEqual([item.code for item in self.repository.list_enabled(ChannelType.RSS)], ["people-rss"])

        self.repository.delete_channel("people-rss")
        self.assertEqual(self.repository.list_enabled(ChannelType.RSS), [])

    def test_only_one_web_search_channel_can_be_enabled(self) -> None:
        with self.assertRaisesRegex(ValueError, "only one web_search channel may be enabled"):
            self.repository.update_channel("tavily", enabled=True)


@unittest.skipUnless(os.getenv("TEST_POSTGRES_URL"), "TEST_POSTGRES_URL is required for PostgreSQL integration")
class CollectionChannelPostgresIntegrationTest(unittest.TestCase):
    engine: ClassVar[Engine]

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(os.environ["TEST_POSTGRES_URL"], pool_pre_ping=True)
        with cls.engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE IF EXISTS collection_channels CASCADE")
            connection.exec_driver_sql("DROP TABLE IF EXISTS collection_channels_decoy CASCADE")
            connection.exec_driver_sql("DROP FUNCTION IF EXISTS guard_collection_channel_identity() CASCADE")
            connection.exec_driver_sql("DROP FUNCTION IF EXISTS decoy_collection_channel_guard() CASCADE")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def test_01_concurrent_startup_is_idempotent(self) -> None:
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE collection_channels_decoy (value integer, "
                "CONSTRAINT ck_collection_channels_adapter_type CHECK (value > 0), "
                "CONSTRAINT ck_collection_channels_dynamic_protocol CHECK (value > 0))"
            )
            connection.exec_driver_sql(
                "CREATE FUNCTION decoy_collection_channel_guard() RETURNS trigger AS $$ "
                "BEGIN RETURN NEW; END; $$ LANGUAGE plpgsql"
            )
            connection.exec_driver_sql(
                "CREATE TRIGGER collection_channel_identity_guard BEFORE UPDATE "
                "ON collection_channels_decoy FOR EACH ROW EXECUTE FUNCTION decoy_collection_channel_guard()"
            )

        def initialize(_: int) -> None:
            ChannelRepository(self.engine).ensure_catalog()

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(initialize, range(8)))

        repository = ChannelRepository(self.engine)
        self.assertEqual(len(repository.list_all()), 7)
        with self.engine.connect() as connection:
            trigger_count = connection.execute(
                text(
                    "SELECT count(*) FROM pg_trigger "
                    "WHERE tgname = 'collection_channel_identity_guard' AND NOT tgisinternal "
                    "AND tgrelid = 'collection_channels'::regclass"
                )
            ).scalar_one()
            constraint_count = connection.execute(
                text(
                    "SELECT count(*) FROM pg_constraint WHERE conrelid = 'collection_channels'::regclass "
                    "AND conname IN ('ck_collection_channels_adapter_type', "
                    "'ck_collection_channels_dynamic_protocol')"
                )
            ).scalar_one()
        self.assertEqual(trigger_count, 1)
        self.assertEqual(constraint_count, 2)

    def test_02_postgres_enforces_identity_protocol_and_ranges(self) -> None:
        with self.assertRaises(DBAPIError):
            with self.engine.begin() as connection:
                connection.execute(text("DELETE FROM collection_channels WHERE code = 'bocha'"))

        with self.assertRaises(DBAPIError):
            with self.engine.begin() as connection:
                connection.execute(text("UPDATE collection_channels SET code = 'renamed' WHERE code = 'bocha'"))

        base: dict[str, object] = {
            "name": "Invalid",
            "ownership_type": "fixed",
            "channel_type": "api",
            "adapter_key": "cls",
            "endpoint": "https://example.com/api",
            "priority": 1,
            "timeout_seconds": 30,
            "max_results": 10,
            "default_source_level": "L3_MEDIA",
        }
        invalid_cases: dict[str, dict[str, object]] = {
            "ownership-enum": {"ownership_type": "INVALID"},
            "channel-enum": {"channel_type": "INVALID"},
            "dynamic-protocol": {"ownership_type": "dynamic"},
            "adapter-type": {"adapter_key": "bocha"},
            "priority": {"priority": 0},
            "timeout": {"timeout_seconds": 0},
            "max-results": {"max_results": 0},
            "source-level": {"default_source_level": "INVALID"},
        }
        statement = text(
            "INSERT INTO collection_channels "
            "(code,name,ownership_type,channel_type,adapter_key,enabled,endpoint,config,priority,"
            "timeout_seconds,max_results,default_source_level,created_at,updated_at) VALUES "
            "(:code,:name,:ownership_type,:channel_type,:adapter_key,false,:endpoint,'{}',:priority,"
            ":timeout_seconds,:max_results,:default_source_level,now(),now())"
        )
        for code, changes in invalid_cases.items():
            with self.subTest(code=code), self.assertRaises(IntegrityError):
                with self.engine.begin() as connection:
                    connection.execute(statement, {**base, **changes, "code": f"invalid-{code}"})

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(statement, {**base, "code": "bocha"})

    def test_03_postgres_allows_only_one_enabled_web_search(self) -> None:
        repository = ChannelRepository(self.engine)
        with self.assertRaisesRegex(ValueError, "only one web_search"):
            repository.update_channel("tavily", enabled=True)

    def test_04_postgres_dynamic_rss_lifecycle(self) -> None:
        repository = ChannelRepository(self.engine)
        now = datetime.now(UTC)
        dynamic = CollectionChannel(
            code="postgres-dynamic-rss",
            name="PostgreSQL Dynamic RSS",
            ownership_type=OwnershipType.DYNAMIC,
            channel_type=ChannelType.RSS,
            adapter_key="generic_rss",
            enabled=True,
            endpoint="https://example.com/feed.xml",
            config={},
            priority=1,
            timeout_seconds=30,
            max_results=10,
            default_source_level=SourceLevel.L3_MEDIA,
            created_at=now,
            updated_at=now,
        )

        repository.create_dynamic(dynamic)
        self.assertIn(dynamic.code, [item.code for item in repository.list_enabled(ChannelType.RSS)])
        repository.delete_channel(dynamic.code)
        self.assertNotIn(dynamic.code, [item.code for item in repository.list_enabled(ChannelType.RSS)])


class CollectionAdapterContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_bocha_normalizes_results_and_resolves_source_level_by_host(self) -> None:
        channel = _channel("bocha", ChannelType.WEB_SEARCH, "bocha").model_copy(
            update={
                "app_key": "database-key",
                "config": {"source_levels": {"gov.cn": "L1_OFFICIAL"}},
                "default_source_level": SourceLevel.L3_MEDIA,
            }
        )
        request = FetchRequest(
            query="宏观政策",
            published_after=datetime(2026, 8, 10, 10, tzinfo=UTC),
            published_before=datetime(2026, 8, 12, 10, tzinfo=UTC),
        )
        payload = {
            "data": {
                "webPages": {
                    "value": [
                        {
                            "name": "政策发布",
                            "url": "https://www.gov.cn/zhengce/example",
                            "summary": "国务院发布政策。",
                            "siteName": "中国政府网",
                            "datePublished": "2026-08-12T09:00:00Z",
                        }
                    ]
                }
            }
        }
        with patch(
            "capabilities.raw_collection.adapters.web_search.post_json",
            new=AsyncMock(return_value=payload),
        ) as request_mock:
            candidates = await BochaAdapter().fetch(channel, request)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].connector, "bocha")
        self.assertEqual(candidates[0].source_level, SourceLevel.L1_OFFICIAL)
        self.assertIsNotNone(request_mock.await_args)
        assert request_mock.await_args is not None
        self.assertEqual(request_mock.await_args.args[2]["Authorization"], "Bearer database-key")

    async def test_generic_rss_adapter_normalizes_rss_and_atom_sources(self) -> None:
        request = FetchRequest(
            query="政治经济",
            published_after=datetime(2026, 8, 10, 10, tzinfo=UTC),
            published_before=datetime(2026, 8, 12, 10, tzinfo=UTC),
        )
        rss = """<?xml version="1.0"?><rss version="2.0"><channel><item>
        <guid>rss-1</guid><title>宏观政策</title><link>https://example.com/rss-1</link>
        <description>政策正文</description><pubDate>Wed, 12 Aug 2026 09:00:00 GMT</pubDate>
        </item></channel></rss>"""
        atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <id>atom-1</id><title>产业新闻</title><link href="https://example.com/atom-1"/>
        <summary>产业正文</summary><updated>2026-08-12T09:30:00Z</updated>
        </entry></feed>"""
        adapter = GenericRssAdapter()
        with patch(
            "capabilities.raw_collection.adapters.rss.get_text",
            new=AsyncMock(side_effect=[rss, atom]),
        ):
            rss_candidates = await adapter.fetch(
                _channel("rss-source", ChannelType.RSS, "generic_rss", ownership_type=OwnershipType.DYNAMIC),
                request,
            )
            atom_candidates = await adapter.fetch(
                _channel("atom-source", ChannelType.RSS, "generic_rss", ownership_type=OwnershipType.DYNAMIC),
                request,
            )

        self.assertEqual([item.title for item in rss_candidates], ["宏观政策"])
        self.assertEqual([item.title for item in atom_candidates], ["产业新闻"])
        self.assertEqual(atom_candidates[0].source_level, SourceLevel.L3_MEDIA)

    def test_adapter_registry_covers_fixed_and_generic_protocols(self) -> None:
        self.assertEqual(
            set(ADAPTERS),
            {"bocha", "tavily", "parallel", "cls", "eastmoney_fast", "eastmoney_stock", "stcn", "generic_rss"},
        )


if __name__ == "__main__":
    unittest.main()
