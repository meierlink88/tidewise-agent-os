"""Tests for the first raw-collection vertical slice."""

import hashlib
import inspect
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from agno.run import RunContext
from agno.workflow import Step
from pydantic import ValidationError

from capabilities.raw_collection.artifacts import build_artifact_set, publish_artifact_set
from capabilities.raw_collection.buffer import artifact_root, read_tool_batches, write_tool_batch
from capabilities.raw_collection.models import Candidate, CollectionRequest, ToolBatchReceipt
from capabilities.raw_collection.tools.bocha import search_bocha_news
from capabilities.raw_collection.tools.eastmoney import search_eastmoney_stock_news
from capabilities.raw_collection.tools.parallel import search_parallel_news
from capabilities.raw_collection.tools.professional import (
    search_cls_telegraph,
    search_eastmoney_fast_news,
    search_stcn_quick_news,
)
from capabilities.raw_collection.tools.tavily import search_tavily_news
from capabilities.raw_collection.tools.time_window import resolve_time_window
from workflows.raw_collection import _request_from_input, _seed_workflow


class CollectionVerticalSliceTest(unittest.IsolatedAsyncioTestCase):
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
    def _run_context(run_id: str) -> RunContext:
        return RunContext(
            run_id=run_id,
            session_id="session",
            dependencies={
                "collector_agent_component_id": "raw-collector",
                "collector_agent_config_version": 3,
                "collector_instructions_sha256": "a" * 64,
            },
        )

    def test_collection_request_accepts_plain_objective(self) -> None:
        request = CollectionRequest.model_validate("  采集最近2小时A股事件  ")
        self.assertEqual(request.objective, "采集最近2小时A股事件")

    def test_relative_time_window_is_exact_across_midnight(self) -> None:
        now = datetime.fromisoformat("2026-08-11T00:54:00+08:00")
        after, before, interpretation = resolve_time_window("采集最近20分钟A股资讯", now=now)
        self.assertEqual(interpretation, "relative")
        self.assertEqual(after.isoformat(), "2026-08-11T00:34:00+08:00")
        self.assertEqual(before.isoformat(), "2026-08-11T00:54:00+08:00")

        after, before, interpretation = resolve_time_window("采集2026年8月1日至2026年8月2日资讯", now=now)
        self.assertEqual(interpretation, "explicit_range")
        self.assertEqual(after.isoformat(), "2026-08-01T00:00:00+08:00")
        self.assertEqual(before.isoformat(), "2026-08-03T00:00:00+08:00")

        after, before, interpretation = resolve_time_window("采集A股产业资讯", now=now)
        self.assertEqual(interpretation, "default_last_48_hours")
        self.assertEqual(after.isoformat(), "2026-08-09T00:54:00+08:00")
        self.assertEqual(before.isoformat(), "2026-08-11T00:54:00+08:00")

    def test_workflow_rejects_second_time_constraint(self) -> None:
        with self.assertRaises(ValidationError):
            _request_from_input('{"objective":"采集最近6小时A股政策","time_window_hours":6}')

    async def test_eastmoney_tool_persists_complete_batch_and_returns_receipt(self) -> None:
        payload = """callback({
          "code": 0,
          "result": {"cmsArticleWebOld": [{
            "date": "2026-08-10 23:05:00",
            "code": "202608103836967018",
            "title": "<em>人工智能</em>产业动态",
            "content": "广东发布<em>人工智能</em>产业政策。",
            "mediaName": "测试媒体",
            "url": "http://finance.eastmoney.com/a/202608103836967018.html"
          }]}
        })"""
        context = RunContext(
            run_id="run-tool",
            session_id="session",
            dependencies={
                "collector_agent_component_id": "raw-collector",
                "collector_agent_config_version": 2,
                "collector_instructions_sha256": "a" * 64,
            },
        )
        with patch(
            "capabilities.raw_collection.tools.eastmoney.get_text",
            new=AsyncMock(return_value=payload),
        ):
            response = await search_eastmoney_stock_news(
                "人工智能",
                "2026-08-10T22:30:00+08:00",
                "2026-08-10T23:30:00+08:00",
                context,
                limit=3,
            )

        receipt = ToolBatchReceipt.model_validate_json(response)
        self.assertEqual(receipt.connector, "eastmoney_stock_news")
        self.assertEqual(receipt.result_count, 1)
        self.assertEqual(receipt.in_window_result_count, 1)
        batches = read_tool_batches("run-tool")
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].agent_config_version, 2)
        self.assertEqual(batches[0].requested_after, datetime(2026, 8, 10, 14, 30, tzinfo=UTC))
        self.assertEqual(batches[0].candidates[0].title, "人工智能产业动态")
        self.assertEqual(batches[0].candidates[0].content, "广东发布人工智能产业政策。")

    async def test_tool_rejects_a_time_window_without_timezone(self) -> None:
        context = RunContext(run_id="run-invalid-window", session_id="session")
        response = await search_eastmoney_stock_news(
            "人工智能",
            "2026-08-10T14:30:00",
            "2026-08-10T15:30:00",
            context,
        )
        self.assertIn("error", json.loads(response))
        self.assertEqual(read_tool_batches("run-invalid-window"), [])

    async def test_api_key_backed_search_tools_read_environment_configuration(self) -> None:
        window = ("2026-08-10T22:30:00+08:00", "2026-08-10T23:30:00+08:00")
        parallel_payload = json.dumps(
            {
                "results": [
                    {
                        "title": "AI 产业政策",
                        "url": "https://example.com/parallel",
                        "publish_date": "2026-08-10T15:05:00Z",
                        "excerpts": ["政策原文摘录"],
                    }
                ]
            }
        )
        with (
            patch.dict(os.environ, {"PARALLEL_API_KEY": "parallel-test-key"}),
            patch(
                "capabilities.raw_collection.tools.parallel.post_json",
                new=AsyncMock(return_value=json.loads(parallel_payload)),
            ),
        ):
            response = await search_parallel_news(
                "采集 AI 产业政策",
                ["AI 产业政策"],
                *window,
                self._run_context("run-parallel"),
            )
        receipt = ToolBatchReceipt.model_validate_json(response)
        self.assertEqual((receipt.connector, receipt.result_count), ("parallel_search", 1))

        tavily_payload = json.dumps(
            {
                "results": [
                    {
                        "title": "AI 服务器订单",
                        "url": "https://example.com/tavily",
                        "published_date": "2026-08-10T15:10:00Z",
                        "content": "搜索摘要",
                        "raw_content": "直接 Markdown 正文",
                    }
                ]
            }
        )
        with (
            patch.dict(os.environ, {"TAVILY_API_KEY": "tavily-test-key"}),
            patch(
                "capabilities.raw_collection.tools.tavily.post_json",
                new=AsyncMock(return_value=json.loads(tavily_payload)),
            ),
        ):
            response = await search_tavily_news(
                "AI 服务器订单",
                *window,
                self._run_context("run-tavily"),
            )
        receipt = ToolBatchReceipt.model_validate_json(response)
        self.assertEqual((receipt.connector, receipt.result_count), ("tavily", 1))
        self.assertEqual(read_tool_batches("run-tavily")[0].candidates[0].content, "直接 Markdown 正文")

        bocha_payload = json.dumps(
            {
                "data": {
                    "webPages": {
                        "value": [
                            {
                                "name": "先进封装动态",
                                "url": "https://example.com/bocha",
                                "summary": "直接摘要",
                                "siteName": "示例媒体",
                                "datePublished": "2026-08-10T15:15:00Z",
                            }
                        ]
                    }
                }
            }
        )
        with (
            patch.dict(os.environ, {"BOCHA_API_KEY": "bocha-test-key"}),
            patch(
                "capabilities.raw_collection.tools.bocha.post_json",
                new=AsyncMock(return_value=json.loads(bocha_payload)),
            ),
        ):
            response = await search_bocha_news(
                "先进封装动态",
                *window,
                self._run_context("run-bocha"),
            )
        receipt = ToolBatchReceipt.model_validate_json(response)
        self.assertEqual((receipt.connector, receipt.result_count), ("bocha", 1))

    async def test_public_professional_channels_persist_direct_results(self) -> None:
        window = ("2026-08-10T22:30:00+08:00", "2026-08-10T23:30:00+08:00")
        cases = [
            (
                "run-cls",
                search_cls_telegraph,
                {
                    "data": {
                        "roll_data": [
                            {
                                "id": 123,
                                "ctime": 1786374300,
                                "title": "产业快讯",
                                "brief": "简讯",
                                "content": "<p>财联社直接内容</p>",
                            }
                        ]
                    }
                },
                "cls_telegraph",
            ),
            (
                "run-eastmoney-fast",
                search_eastmoney_fast_news,
                {
                    "data": {
                        "fastNewsList": [
                            {
                                "code": "202608103836967018",
                                "title": "东方财富快讯",
                                "summary": "直接摘要",
                                "showTime": "2026-08-10 23:10:00",
                            }
                        ]
                    }
                },
                "eastmoney_fastnews",
            ),
            (
                "run-stcn",
                search_stcn_quick_news,
                {
                    "state": 1,
                    "data": [
                        {
                            "id": "4022599",
                            "url": "/article/detail/4022599.html",
                            "title": "证券时报快讯",
                            "source": "人民财讯",
                            "content": "候选快讯内容",
                            "time": 1786374600,
                        }
                    ],
                },
                "stcn_quicknews",
            ),
        ]
        for run_id, tool, payload, connector in cases:
            with self.subTest(connector=connector):
                with patch(
                    "capabilities.raw_collection.tools.professional.get_json",
                    new=AsyncMock(return_value=payload),
                ):
                    response = await tool("AI 产业", *window, self._run_context(run_id), limit=10)
                receipt = ToolBatchReceipt.model_validate_json(response)
                self.assertEqual((receipt.connector, receipt.result_count), (connector, 1))

    def test_network_tools_and_workflow_executors_are_async(self) -> None:
        from workflows.raw_collection import agentic_collect_step, build_artifact_step, publish_collection_step

        network_tools = [
            search_parallel_news,
            search_tavily_news,
            search_bocha_news,
            search_cls_telegraph,
            search_eastmoney_fast_news,
            search_eastmoney_stock_news,
            search_stcn_quick_news,
        ]
        workflow_executors = [agentic_collect_step, build_artifact_step, publish_collection_step]
        self.assertTrue(all(inspect.iscoroutinefunction(tool) for tool in network_tools))
        self.assertTrue(all(inspect.iscoroutinefunction(executor) for executor in workflow_executors))

    def test_build_then_manifest_last_publish_is_deterministic_and_idempotent(self) -> None:
        now = datetime(2026, 8, 10, 15, 30, tzinfo=UTC)
        url = "http://finance.eastmoney.com/a/202608103836967018.html?utm_source=test"
        first = Candidate(
            candidate_id="candidate-1",
            connector="eastmoney_stock_news",
            query="人工智能",
            title="人工智能产业动态",
            url=url,
            content="广东发布人工智能产业政策。",
            source_name="测试媒体",
            source_external_id="1",
            published_at=now,
            collected_at=now,
        )
        duplicate = first.model_copy(update={"candidate_id": "candidate-2"})
        outside = Candidate.model_validate(
            {
                **first.model_dump(),
                "candidate_id": "candidate-outside",
                "url": "https://finance.eastmoney.com/a/outside.html",
                "published_at": now - timedelta(hours=2),
            }
        )
        write_tool_batch(
            collection_id="run-artifact",
            connector="eastmoney_stock_news",
            query="人工智能",
            requested_after=now - timedelta(hours=1),
            requested_before=now + timedelta(minutes=1),
            agent_component_id="raw-collector",
            agent_config_version=3,
            instructions_sha256="b" * 64,
            candidates=[first, duplicate, outside],
        )

        prepared = build_artifact_set(
            "run-artifact",
            CollectionRequest(objective="采集最近1小时人工智能政策"),
            completed_at=now,
        )
        root = artifact_root()
        manifest = root / "runs/run-artifact/manifest.json"
        self.assertFalse(manifest.exists())
        self.assertEqual(prepared.publication_items[-1], "runs/run-artifact/manifest.json")
        self.assertEqual(prepared.candidate_counts["accepted"], 1)
        self.assertEqual(prepared.candidate_counts["known_url"], 1)
        self.assertEqual(prepared.candidate_counts["out_of_window"], 1)

        result = publish_artifact_set(prepared)
        repeated = publish_artifact_set(prepared)
        self.assertEqual(result.collection_id, repeated.collection_id)
        self.assertTrue(manifest.is_file())
        document = root / prepared.accepted_documents[0].relative_path
        self.assertTrue(document.is_file())
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest_payload["results_pending"], 0)
        self.assertEqual(manifest_payload["collector_agent"]["config_version"], 3)
        self.assertEqual(manifest_payload["tool_batches"][0]["requested_after"], "2026-08-10T14:30:00+00:00")
        self.assertEqual(
            manifest_payload["objective_sha256"], hashlib.sha256("采集最近1小时人工智能政策".encode()).hexdigest()
        )

    def test_studio_workflow_seed_round_trips_registered_functions(self) -> None:
        from app.registry import registry

        restored = type(_seed_workflow()).from_dict(_seed_workflow().to_dict(), registry=registry)
        self.assertIsInstance(restored.steps, list)
        steps = cast(list[Step], restored.steps)
        self.assertTrue(all(isinstance(step, Step) for step in steps))
        self.assertEqual(
            [getattr(step.executor, "__name__", None) for step in steps],
            ["agentic_collect_step", "build_artifact_step", "publish_collection_step"],
        )
        self.assertTrue(all(step.max_retries == 0 for step in steps))
        self.assertTrue(all(str(step.on_error) == "fail" for step in steps))


if __name__ == "__main__":
    unittest.main()
