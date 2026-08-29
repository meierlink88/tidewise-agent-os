"""Behavior tests for database-driven collection channels."""

import asyncio
import json
import os
import tempfile
import unittest
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
from agno.run import RunContext

from capabilities.collection.internal.acquisition import execute_channel_group
from capabilities.collection.internal.adapters.base import FetchRequest
from capabilities.collection.internal.adapters.common import get_text
from capabilities.collection.internal.adapters.registry import ADAPTERS
from capabilities.collection.internal.adapters.rss import GenericRssAdapter
from capabilities.collection.internal.adapters.web_search import BochaAdapter, TavilyAdapter
from capabilities.collection.internal.channels.models import (
    ChannelType,
    CollectionChannel,
    OwnershipType,
)
from capabilities.collection.internal.models import Candidate, FetchReceipt, SourceLevel


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
        collected_at = datetime(2026, 8, 12, 10, tzinfo=UTC)
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
                published_at=collected_at,
                collected_at=collected_at,
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


class CollectionChannelFunctionTest(unittest.IsolatedAsyncioTestCase):
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
                "collection_channel_catalog": _Catalog(channels),
                "collection_adapter_registry": adapters,
            },
        )

    async def test_web_fetch_executes_the_only_enabled_search_channel(self) -> None:
        channel = _channel("bocha", ChannelType.WEB_SEARCH, "bocha")
        adapter = _Adapter()

        response = await execute_channel_group(
            "web_search",
            ChannelType.WEB_SEARCH,
            "中国宏观经济",
            self._context([channel], {"bocha": adapter}, "run-web"),
        )

        receipt = FetchReceipt.model_validate_json(response)
        self.assertEqual(receipt.channel_group, "web_search")
        self.assertEqual(receipt.outcome, "succeeded")
        self.assertEqual([item.channel_code for item in receipt.channels], ["bocha"])
        self.assertEqual(receipt.channels[0].result_count, 1)
        self.assertEqual(adapter.calls, 1)

    async def test_web_fetch_fails_closed_when_catalog_exposes_multiple_enabled_channels(self) -> None:
        channels = [
            _channel("bocha", ChannelType.WEB_SEARCH, "bocha"),
            _channel("tavily", ChannelType.WEB_SEARCH, "tavily"),
        ]

        response = await execute_channel_group(
            "web_search",
            ChannelType.WEB_SEARCH,
            "中国宏观经济",
            self._context(channels, {"bocha": _Adapter(), "tavily": _Adapter()}, "run-invalid-web"),
        )

        self.assertEqual(json.loads(response), {"channel_group": "web_search", "error": "invalid_channel_catalog"})

    async def test_fetch_returns_no_channels_without_writing_a_batch(self) -> None:
        response = await execute_channel_group(
            "rss",
            ChannelType.RSS,
            "政治经济",
            self._context([], {}, "run-no-rss"),
        )

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
        response = await execute_channel_group("api", ChannelType.API, "产业政策", context)
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
            response = await execute_channel_group("api", ChannelType.API, "产业政策", context)
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

        response = await execute_channel_group(
            "rss",
            ChannelType.RSS,
            "政治经济",
            self._context(channels, {"generic_rss": adapter}, "run-rss"),
        )

        receipt = FetchReceipt.model_validate_json(response)
        self.assertEqual(receipt.outcome, "succeeded")
        self.assertEqual([item.channel_code for item in receipt.channels], ["people-rss", "xinhua-rss"])
        self.assertEqual(adapter.calls, 2)


class CollectionAdapterContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_tavily_requests_plain_text_and_handles_null_raw_content(self) -> None:
        channel = _channel("tavily", ChannelType.WEB_SEARCH, "tavily").model_copy(update={"app_key": "database-key"})
        request = FetchRequest(query="A股公告")
        payload = {
            "results": [
                {
                    "title": "公告标题",
                    "url": "https://example.com/article",
                    "raw_content": None,
                    "content": "可读的纯文本正文",
                    "published_date": "2026-08-12T09:00:00Z",
                }
            ]
        }
        with patch(
            "capabilities.collection.internal.adapters.web_search.post_json",
            new=AsyncMock(return_value=payload),
        ) as request_mock:
            candidates = await TavilyAdapter().fetch(channel, request)

        assert request_mock.await_args is not None
        self.assertEqual(request_mock.await_args.args[1]["include_raw_content"], "text")
        self.assertNotIn("time_range", request_mock.await_args.args[1])
        self.assertNotIn("start_date", request_mock.await_args.args[1])
        self.assertEqual(candidates[0].content, "可读的纯文本正文")

    async def test_bocha_normalizes_results_and_resolves_source_level_by_host(self) -> None:
        channel = _channel("bocha", ChannelType.WEB_SEARCH, "bocha").model_copy(
            update={
                "app_key": "database-key",
                "config": {"source_levels": {"gov.cn": "L1_OFFICIAL"}},
                "default_source_level": SourceLevel.L3_MEDIA,
            }
        )
        request = FetchRequest(query="宏观政策")
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
            "capabilities.collection.internal.adapters.web_search.post_json",
            new=AsyncMock(return_value=payload),
        ) as request_mock:
            candidates = await BochaAdapter().fetch(channel, request)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].connector, "bocha")
        self.assertEqual(candidates[0].source_level, SourceLevel.L1_OFFICIAL)
        self.assertIsNotNone(request_mock.await_args)
        assert request_mock.await_args is not None
        self.assertEqual(request_mock.await_args.args[1]["freshness"], "oneDay")
        self.assertEqual(request_mock.await_args.args[2]["Authorization"], "Bearer database-key")

    async def test_generic_rss_adapter_normalizes_rss_and_atom_sources(self) -> None:
        request = FetchRequest(query="政治经济")
        rss = """<?xml version="1.0"?><rss version="2.0"><channel><item>
        <guid>rss-1</guid><title>宏观政策</title><link>https://example.com/rss-1</link>
        <description>政策正文</description><pubDate>Wed, 12 Aug 2026 09:00:00 GMT</pubDate>
        </item></channel></rss>"""
        atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <id>atom-1</id><title>产业新闻</title><link href="https://example.com/atom-1"/>
        <summary>产业正文</summary><updated>2026-08-12T09:30:00Z</updated>
        </entry></feed>"""
        adapter = GenericRssAdapter()
        rss_channel = _channel(
            "rss-source", ChannelType.RSS, "generic_rss", ownership_type=OwnershipType.DYNAMIC
        ).model_copy(update={"config": {"fetch_article_body": False}})
        atom_channel = _channel(
            "atom-source", ChannelType.RSS, "generic_rss", ownership_type=OwnershipType.DYNAMIC
        ).model_copy(update={"config": {"fetch_article_body": False}})
        with patch(
            "capabilities.collection.internal.adapters.rss.get_text",
            new=AsyncMock(side_effect=[rss, atom]),
        ):
            rss_candidates = await adapter.fetch(rss_channel, request)
            atom_candidates = await adapter.fetch(atom_channel, request)

        self.assertEqual([item.title for item in rss_candidates], ["宏观政策"])
        self.assertEqual([item.title for item in atom_candidates], ["产业新闻"])
        self.assertEqual(atom_candidates[0].source_level, SourceLevel.L3_MEDIA)

    async def test_generic_rss_adapter_uses_article_body_and_respects_source_limit(self) -> None:
        request = FetchRequest(query="产业变化")
        rss = """<?xml version="1.0"?><rss version="2.0"><channel>
        <item><guid>rss-1</guid><title>第一条</title><link>https://example.com/one</link><description>短摘要一</description></item>
        <item><guid>rss-2</guid><title>第二条</title><link>https://example.com/two</link><description>短摘要二</description></item>
        </channel></rss>"""
        article = """<html><body><nav>导航噪声</nav><main><h1>第一条</h1>
        <p>这是第一段具有投研价值的文章正文，描述产业供需发生变化。</p>
        <p>这是第二段文章正文，补充价格、库存和产能事实。</p></main><footer>页脚噪声</footer></body></html>"""
        channel = _channel(
            "rss-source",
            ChannelType.RSS,
            "generic_rss",
            ownership_type=OwnershipType.DYNAMIC,
        ).model_copy(update={"max_results": 1})

        with patch(
            "capabilities.collection.internal.adapters.rss.get_text",
            new=AsyncMock(side_effect=[rss, article]),
        ) as request_mock:
            candidates = await GenericRssAdapter().fetch(channel, request)

        self.assertEqual(len(candidates), 1)
        self.assertIn("产业供需发生变化", candidates[0].content)
        self.assertIn("价格、库存和产能事实", candidates[0].content)
        self.assertNotIn("导航噪声", candidates[0].content)
        self.assertEqual(request_mock.await_count, 2)
        self.assertEqual(request_mock.await_args_list[1].kwargs["timeout_seconds"], 10)

    async def test_generic_rss_adapter_falls_back_to_feed_content_when_article_fetch_fails(self) -> None:
        request = FetchRequest(query="宏观政策")
        rss = """<?xml version="1.0"?><rss version="2.0"><channel><item>
        <guid>rss-1</guid><title>政策发布</title><link>https://example.com/policy</link>
        <description>政策摘要仍可用于后续过滤。</description></item></channel></rss>"""
        with patch(
            "capabilities.collection.internal.adapters.rss.get_text",
            new=AsyncMock(side_effect=[rss, RuntimeError("article unavailable")]),
        ):
            candidates = await GenericRssAdapter().fetch(
                _channel("rss-source", ChannelType.RSS, "generic_rss", ownership_type=OwnershipType.DYNAMIC),
                request,
            )

        self.assertEqual([item.content for item in candidates], ["政策摘要仍可用于后续过滤。"])

    async def test_get_text_preserves_an_endpoint_query_when_no_extra_params_are_required(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=b"<rss/>")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("capabilities.collection.internal.adapters.common.httpx.AsyncClient", return_value=client):
            payload = await get_text(
                "https://example.com/feed/rss.php?mid=21",
                {},
                timeout_seconds=10,
            )

        self.assertEqual(payload, "<rss/>")
        self.assertEqual(str(requests[0].url), "https://example.com/feed/rss.php?mid=21")

    async def test_get_text_stops_streaming_as_soon_as_the_byte_limit_is_exceeded(self) -> None:
        class TrackingStream(httpx.AsyncByteStream):
            def __init__(self) -> None:
                self.consumed = 0

            async def __aiter__(self) -> AsyncIterator[bytes]:
                for chunk in (b"123456", b"abcdef", b"must-not-be-read"):
                    self.consumed += 1
                    yield chunk

        stream = TrackingStream()

        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, stream=stream)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with (
            patch("capabilities.collection.internal.adapters.common.httpx.AsyncClient", return_value=client),
            self.assertRaisesRegex(ValueError, "too large"),
        ):
            await get_text("https://example.com/large", {}, timeout_seconds=10, max_bytes=10)

        self.assertEqual(stream.consumed, 2)

    def test_adapter_registry_covers_fixed_and_generic_protocols(self) -> None:
        self.assertEqual(
            set(ADAPTERS),
            {"bocha", "tavily", "parallel", "cls", "eastmoney_fast", "eastmoney_stock", "stcn", "generic_rss"},
        )


if __name__ == "__main__":
    unittest.main()
