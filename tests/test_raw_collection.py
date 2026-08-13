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
from unittest.mock import patch

from agno.run import RunContext
from agno.workflow import Step, StepInput, StepOutput
from pydantic import ValidationError

from capabilities.raw_collection.artifacts import build_artifact_set, publish_artifact_set
from capabilities.raw_collection.buffer import artifact_root, read_tool_batches, write_tool_batch
from capabilities.raw_collection.channels.models import ChannelType, CollectionChannel, OwnershipType
from capabilities.raw_collection.functions import (
    agentic_collect_step,
    build_artifact_step,
    execute_collection_channels_step,
    publish_collection_step,
)
from capabilities.raw_collection.functions.collection import request_from_input
from capabilities.raw_collection.models import Candidate, CollectionQueryPlan, CollectionRequest, SourceLevel
from capabilities.raw_collection.tools import COLLECTION_TOOLS
from workflows.raw_collection import _seed_workflow


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

    def test_collection_request_accepts_plain_objective(self) -> None:
        request = CollectionRequest.model_validate("  采集最近2小时A股事件  ")
        self.assertEqual(request.objective, "采集最近2小时A股事件")

    def test_workflow_rejects_second_time_constraint(self) -> None:
        with self.assertRaises(ValidationError):
            request_from_input('{"objective":"采集最近6小时A股政策","time_window_hours":6}')

    def test_network_tools_and_workflow_executors_are_async(self) -> None:
        workflow_executors = [
            agentic_collect_step,
            execute_collection_channels_step,
            build_artifact_step,
            publish_collection_step,
        ]
        self.assertEqual([tool.__name__ for tool in COLLECTION_TOOLS], ["web_fetch", "api_fetch", "rss_fetch"])
        self.assertTrue(all(inspect.iscoroutinefunction(tool) for tool in COLLECTION_TOOLS))
        self.assertTrue(all(inspect.iscoroutinefunction(executor) for executor in workflow_executors))

    async def test_workflow_function_executes_all_facades_with_one_frozen_window(self) -> None:
        now = datetime(2026, 8, 12, 10, tzinfo=UTC)
        channels = [
            CollectionChannel(
                code="bocha",
                name="博查",
                ownership_type=OwnershipType.FIXED,
                channel_type=ChannelType.WEB_SEARCH,
                adapter_key="bocha",
                enabled=True,
                endpoint="https://example.com/bocha",
                config={},
                priority=1,
                timeout_seconds=30,
                max_results=10,
                default_source_level=SourceLevel.L3_MEDIA,
                created_at=now,
                updated_at=now,
            ),
            CollectionChannel(
                code="cls_telegraph",
                name="财联社",
                ownership_type=OwnershipType.FIXED,
                channel_type=ChannelType.API,
                adapter_key="cls",
                enabled=True,
                endpoint="https://example.com/cls",
                config={},
                priority=1,
                timeout_seconds=30,
                max_results=10,
                default_source_level=SourceLevel.L2_WIRE,
                created_at=now,
                updated_at=now,
            ),
        ]

        class Adapter:
            async def fetch(self, channel: CollectionChannel, request: object) -> list[Candidate]:
                before = cast(datetime, getattr(request, "published_before"))
                return [
                    Candidate(
                        candidate_id=f"candidate-{channel.code}",
                        connector=channel.code,
                        query=cast(str, getattr(request, "query")),
                        title=channel.name,
                        url=f"https://example.com/{channel.code}/item",
                        content=f"{channel.name}正文",
                        source_name=channel.name,
                        source_level=channel.default_source_level,
                        published_at=before,
                        collected_at=before,
                    )
                ]

        context = RunContext(
            run_id="run-deterministic-channels",
            session_id="session",
            dependencies={
                "collector_agent_component_id": "raw-collector",
                "collector_agent_config_version": 7,
                "collector_instructions_sha256": "a" * 64,
                "collector_cutoff": now.isoformat(),
                "collection_channel_snapshot": tuple(channels),
                "collection_adapter_registry": {"bocha": Adapter(), "cls": Adapter()},
            },
        )
        step_input = StepInput(
            previous_step_outputs={
                "agentic-collect": StepOutput(content=CollectionQueryPlan(query="宏观政策", lookback_hours=48))
            }
        )

        output = await execute_collection_channels_step(step_input, context)

        content = cast(dict[str, object], output.content)
        receipts = cast(list[dict[str, object]], content["receipts"])
        self.assertEqual(
            [item["outcome"] for item in receipts],
            ["succeeded", "succeeded", "no_channels"],
        )
        batches = read_tool_batches(context.run_id)
        self.assertCountEqual([item.connector for item in batches], ["bocha", "cls_telegraph"])
        self.assertEqual(
            {(item.requested_after, item.requested_before) for item in batches}, {(now - timedelta(hours=48), now)}
        )

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
        manifest_index = root / "indexes/manifest-index.jsonl"
        index_entries = [json.loads(line) for line in manifest_index.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(index_entries), 1)
        self.assertEqual(index_entries[0]["collection_id"], "run-artifact")
        self.assertEqual(index_entries[0]["manifest_path"], "runs/run-artifact/manifest.json")
        self.assertEqual(index_entries[0]["accepted_documents"], 1)
        document = root / prepared.accepted_documents[0].relative_path
        self.assertTrue(document.is_file())
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest_payload["results_pending"], 0)
        self.assertEqual(manifest_payload["collector_agent"]["config_version"], 3)
        self.assertEqual(manifest_payload["tool_batches"][0]["requested_after"], "2026-08-10T14:30:00+00:00")
        self.assertEqual(
            manifest_payload["objective_sha256"], hashlib.sha256("采集最近1小时人工智能政策".encode()).hexdigest()
        )

    def test_artifact_build_rejects_tool_batches_with_mixed_time_windows(self) -> None:
        now = datetime(2026, 8, 10, 15, 30, tzinfo=UTC)
        candidate = Candidate(
            candidate_id="candidate-window",
            connector="bocha",
            query="政策",
            title="政策资讯",
            url="https://example.com/policy",
            content="政策正文",
            source_name="示例来源",
            published_at=now,
            collected_at=now,
        )
        for connector, hours in (("bocha", 48), ("cls_telegraph", 24)):
            write_tool_batch(
                collection_id="run-mixed-window",
                connector=connector,
                query="政策",
                requested_after=now - timedelta(hours=hours),
                requested_before=now,
                agent_component_id="raw-collector",
                agent_config_version=3,
                instructions_sha256="b" * 64,
                candidates=[
                    candidate.model_copy(update={"candidate_id": f"candidate-{connector}", "connector": connector})
                ],
            )

        with self.assertRaisesRegex(ValueError, "time window"):
            build_artifact_set("run-mixed-window", CollectionRequest(objective="采集政策"), completed_at=now)

    def test_studio_workflow_seed_round_trips_registered_functions(self) -> None:
        from app.registry import registry

        restored = type(_seed_workflow()).from_dict(_seed_workflow().to_dict(), registry=registry)
        self.assertIsInstance(restored.steps, list)
        steps = cast(list[Step], restored.steps)
        self.assertTrue(all(isinstance(step, Step) for step in steps))
        self.assertEqual(
            [getattr(step.executor, "__name__", None) for step in steps],
            [
                "agentic_collect_step",
                "execute_collection_channels_step",
                "build_artifact_step",
                "publish_collection_step",
            ],
        )
        self.assertTrue(all(step.max_retries == 0 for step in steps))
        self.assertTrue(all(str(step.on_error) == "fail" for step in steps))


if __name__ == "__main__":
    unittest.main()
