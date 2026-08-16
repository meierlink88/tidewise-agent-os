"""Tests for the first raw-collection vertical slice."""

import hashlib
import inspect
import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

from agno.agent import Agent
from agno.run import RunContext
from agno.workflow import Step, StepInput, StepOutput
from pydantic import ValidationError

from agents.title_curator import ensure_title_curator_agent
from capabilities.collection.functions import (
    build_artifact_step,
    execute_collection_channels_step,
    prepare_collection_context,
    prepare_title_curation,
    publish_collection_step,
    validate_title_curation,
)
from capabilities.collection.functions.collection import request_from_input
from capabilities.collection.internal.artifacts import build_artifact_set, publish_artifact_set
from capabilities.collection.internal.buffer import (
    artifact_root,
    read_tool_batches,
    write_title_curation,
    write_tool_batch,
)
from capabilities.collection.internal.channels.models import ChannelType, CollectionChannel, OwnershipType
from capabilities.collection.internal.models import (
    Candidate,
    CollectionQueryPlan,
    CollectionRequest,
    SourceLevel,
    TitleCurationDecision,
    TitleCurationDraft,
    TitleCurationRequest,
)
from capabilities.collection.tools import COLLECTION_TOOLS
from workflows.raw_collection import _seed_workflow


class RecordingRawDocumentStore:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, bytes, str]] = []

    def publish_markdown(self, *, bucket: str, object_key: str, content: bytes, sha256: str) -> None:
        self.uploads.append((bucket, object_key, content, sha256))


class FailingRawDocumentStore:
    def publish_markdown(self, *, bucket: str, object_key: str, content: bytes, sha256: str) -> None:
        del bucket, object_key, content, sha256
        raise RuntimeError("MinIO unavailable")


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

    def test_title_curation_contract_is_binary_and_strict(self) -> None:
        decision = TitleCurationDecision(candidate_id="candidate-a", is_relevant=True)

        self.assertEqual(
            decision.model_dump(),
            {"candidate_id": "candidate-a", "is_relevant": True},
        )
        with self.assertRaises(ValidationError):
            TitleCurationDecision.model_validate(
                {
                    "candidate_id": "candidate-a",
                    "is_relevant": True,
                    "reason_code": "policy_signal",
                }
            )
        with self.assertRaises(ValidationError):
            TitleCurationDecision.model_validate({"candidate_id": "candidate-a", "is_relevant": "true"})

    def test_workflow_rejects_second_time_constraint(self) -> None:
        with self.assertRaises(ValidationError):
            request_from_input('{"objective":"采集最近6小时A股政策","time_window_hours":6}')

    def test_network_tools_and_workflow_executors_are_async(self) -> None:
        workflow_executors = [
            prepare_collection_context,
            execute_collection_channels_step,
            prepare_title_curation,
            validate_title_curation,
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
                "plan-collection-query": StepOutput(content=CollectionQueryPlan(query="宏观政策", lookback_hours=48))
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
        write_title_curation(
            "run-artifact",
            TitleCurationDraft(
                decisions=[
                    TitleCurationDecision(
                        candidate_id=item.candidate_id,
                        is_relevant=True,
                    )
                    for item in [first, duplicate, outside]
                ]
            ),
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

        with self.assertRaisesRegex(RuntimeError, "MinIO unavailable"):
            publish_artifact_set(prepared, document_store=FailingRawDocumentStore())
        self.assertFalse(manifest.exists())
        self.assertFalse((root / "indexes/manifest-index.jsonl").exists())

        document_store = RecordingRawDocumentStore()
        with patch.dict(os.environ, {"RAW_EVIDENCE_BUCKET": "changed-after-build"}):
            result = publish_artifact_set(prepared, document_store=document_store)
            repeated = publish_artifact_set(prepared, document_store=document_store)
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
        self.assertEqual(
            [(bucket, key) for bucket, key, _, _ in document_store.uploads],
            [
                (
                    "raw-evidence",
                    "documents/2026/08/10/42bb236684abe391ab33aa932ed2acb73fce00a378a3b250fba157d1e8995feb.md",
                )
            ],
        )
        self.assertEqual(document_store.uploads[0][2], document.read_bytes())
        self.assertEqual(document.stem, prepared.accepted_documents[0].sha256)
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest_payload["accepted_documents"][0]["url_path"],
            "/raw-evidence/documents/2026/08/10/42bb236684abe391ab33aa932ed2acb73fce00a378a3b250fba157d1e8995feb.md",
        )
        self.assertEqual(manifest_payload["schema"], "raw_collection_manifest.v2")
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

    def test_title_curation_and_normalized_title_dedup_control_artifacts(self) -> None:
        now = datetime(2026, 8, 13, 2, tzinfo=UTC)
        candidates = [
            Candidate(
                candidate_id="policy-first",
                connector="bocha",
                query="A股政策",
                title="　人工智能 产业政策！",
                url="https://example.com/policy-first",
                content="正文A，应当保存。",
                source_name="测试媒体",
                published_at=now,
                collected_at=now,
            ),
            Candidate(
                candidate_id="policy-duplicate",
                connector="tavily",
                query="A股政策",
                title="人工智能产业政策!",
                url="https://example.com/policy-duplicate",
                content="完全不同的正文B，不得影响标题去重。",
                source_name="另一媒体",
                published_at=now,
                collected_at=now,
            ),
            Candidate(
                candidate_id="sports-noise",
                connector="rss",
                query="A股政策",
                title="世界杯决赛球队首发阵容公布",
                url="https://example.com/sports",
                content="体育新闻正文。",
                source_name="体育媒体",
                published_at=now,
                collected_at=now,
            ),
        ]
        write_tool_batch(
            collection_id="run-title-policy",
            connector="mixed-test",
            query="A股政策",
            requested_after=now - timedelta(hours=1),
            requested_before=now + timedelta(minutes=1),
            agent_component_id="raw-collector",
            agent_config_version=8,
            instructions_sha256="c" * 64,
            candidates=candidates,
        )
        write_title_curation(
            "run-title-policy",
            TitleCurationDraft(
                decisions=[
                    TitleCurationDecision(
                        candidate_id="policy-first",
                        is_relevant=True,
                    ),
                    TitleCurationDecision(
                        candidate_id="policy-duplicate",
                        is_relevant=True,
                    ),
                    TitleCurationDecision(
                        candidate_id="sports-noise",
                        is_relevant=False,
                    ),
                ]
            ),
        )

        prepared = build_artifact_set(
            "run-title-policy",
            CollectionRequest(objective="采集A股政策"),
            completed_at=now,
        )

        self.assertEqual(prepared.candidate_counts["accepted"], 1)
        self.assertEqual(prepared.candidate_counts["exact_duplicate"], 1)
        self.assertEqual(prepared.candidate_counts["irrelevant"], 1)
        ledger_path = Path(prepared.staging_root) / "runs/run-title-policy/candidates.jsonl"
        ledger = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
        by_id = {item["candidate_id"]: item for item in ledger}
        duplicate_rows = [item for item in ledger if item["disposition"] == "exact_duplicate"]
        self.assertEqual([item["reason"] for item in duplicate_rows], ["normalized_title_sha256_already_indexed"])
        self.assertEqual(by_id["sports-noise"]["reason"], "title_irrelevant")
        self.assertEqual(by_id["sports-noise"]["title_relevance"], "irrelevant")
        self.assertNotIn("title_relevance_reason", by_id["sports-noise"])
        self.assertIsNone(by_id["sports-noise"]["document_path"])

    def test_title_dedup_keeps_legacy_index_immutable_and_uses_its_urls(self) -> None:
        now = datetime(2026, 8, 13, 2, tzinfo=UTC)
        legacy_url = "https://example.com/legacy"
        canonical_hash = hashlib.sha256(legacy_url.encode()).hexdigest()
        legacy = artifact_root() / "indexes/dedup-index.tsv"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy_payload = (
            "url_sha256\tcontent_sha256\tsimhash64\tdocument_path\n"
            f"{canonical_hash}\t{'e' * 64}\t0000000000000000\tdocuments/legacy.md\n"
        )
        legacy.write_text(legacy_payload, encoding="utf-8")
        candidate = Candidate(
            candidate_id="legacy-url",
            connector="bocha",
            query="政策",
            title="新政策标题",
            url=legacy_url,
            content="新正文",
            source_name="媒体",
            published_at=now,
            collected_at=now,
        )
        write_tool_batch(
            collection_id="run-legacy-index",
            connector="bocha",
            query="政策",
            requested_after=now - timedelta(hours=1),
            requested_before=now + timedelta(minutes=1),
            agent_component_id="raw-collector",
            agent_config_version=8,
            instructions_sha256="f" * 64,
            candidates=[candidate],
        )
        write_title_curation(
            "run-legacy-index",
            TitleCurationDraft(
                decisions=[
                    TitleCurationDecision(
                        candidate_id="legacy-url",
                        is_relevant=True,
                    )
                ]
            ),
        )
        prepared = build_artifact_set(
            "run-legacy-index",
            CollectionRequest(objective="采集政策"),
            completed_at=now,
        )
        publish_artifact_set(prepared, document_store=RecordingRawDocumentStore())

        self.assertEqual(prepared.candidate_counts["known_url"], 1)
        self.assertEqual(legacy.read_text(encoding="utf-8"), legacy_payload)
        self.assertTrue((artifact_root() / "indexes/title-dedup-index.tsv").is_file())

    async def test_title_curation_input_is_title_only_and_requires_exact_id_coverage(self) -> None:
        now = datetime(2026, 8, 13, 3, tzinfo=UTC)
        write_tool_batch(
            collection_id="run-curation-validation",
            connector="bocha",
            query="政策",
            requested_after=now - timedelta(hours=1),
            requested_before=now + timedelta(minutes=1),
            agent_component_id="raw-collector",
            agent_config_version=8,
            instructions_sha256="d" * 64,
            candidates=[
                Candidate(
                    candidate_id="candidate-a",
                    connector="bocha",
                    query="政策",
                    title="政策A",
                    url="https://example.com/a",
                    content="SECRET_BODY_A",
                    source_name="媒体",
                    published_at=now,
                    collected_at=now,
                ),
                Candidate(
                    candidate_id="candidate-b",
                    connector="bocha",
                    query="政策",
                    title="政策B",
                    url="https://example.com/b",
                    content="SECRET_BODY_B",
                    source_name="媒体",
                    published_at=now,
                    collected_at=now,
                ),
            ],
        )
        context = RunContext(run_id="run-curation-validation", session_id="session")
        prepared = await prepare_title_curation(StepInput(input="ignored"), context)
        encoded = cast(TitleCurationRequest, prepared.content).model_dump_json()
        self.assertNotIn("SECRET_BODY", encoded)
        self.assertNotIn("content", encoded)

        malformed = TitleCurationDraft(
            decisions=[
                TitleCurationDecision(
                    candidate_id="candidate-a",
                    is_relevant=True,
                ),
                TitleCurationDecision(
                    candidate_id="candidate-unknown",
                    is_relevant=True,
                ),
            ]
        )
        validation_input = StepInput(
            previous_step_outputs={
                "prepare-title-curation": prepared,
                "curate-collection-titles": StepOutput(content=malformed),
            }
        )
        with self.assertRaisesRegex(ValueError, "coverage mismatch"):
            await validate_title_curation(validation_input, context)

        duplicate = TitleCurationDraft(
            decisions=[
                TitleCurationDecision(
                    candidate_id="candidate-a",
                    is_relevant=True,
                ),
                TitleCurationDecision(
                    candidate_id="candidate-a",
                    is_relevant=False,
                ),
            ]
        )
        duplicate_input = StepInput(
            previous_step_outputs={
                "prepare-title-curation": prepared,
                "curate-collection-titles": StepOutput(content=duplicate),
            }
        )
        with self.assertRaisesRegex(ValueError, "duplicate Candidate IDs"):
            await validate_title_curation(duplicate_input, context)

    def test_title_curator_contract_migration_publishes_reviewed_binary_prompt(self) -> None:
        db = MagicMock()
        db.get_component.return_value = {"current_version": 8}
        current = MagicMock()
        current.metadata = {"title_curator_contract_version": 3}
        current.instructions = "reason_code 使用简短稳定的小写英文下划线代码"
        current.save.return_value = 9

        with (
            patch("agents.title_curator.get_postgres_db", return_value=db),
            patch("agents.title_curator.Agent.load", return_value=current),
        ):
            version = ensure_title_curator_agent(MagicMock())

        self.assertEqual(version, 9)
        self.assertIn("is_relevant", current.instructions)
        self.assertNotIn("reason_code", current.instructions)
        self.assertNotIn("uncertain", current.instructions)
        self.assertIs(current.output_schema, TitleCurationDraft)
        self.assertEqual(current.metadata["title_curator_contract_version"], 4)

    def test_studio_workflow_seed_round_trips_registered_functions(self) -> None:
        collector = Agent(id="raw-collector", name="Collection Query Planner")
        curator = Agent(id="title-curator", name="Collection Title Curator")
        seeded = _seed_workflow(collector, curator)
        serialized_steps = cast(list[dict[str, object]], seeded.to_dict()["steps"])
        self.assertEqual(
            [item.get("agent_id") for item in serialized_steps if item.get("agent_id") is not None],
            ["raw-collector", "title-curator"],
        )
        self.assertEqual(
            [step.agent.name for step in cast(list[Step], seeded.steps) if step.agent is not None],
            ["Collection Query Planner", "Collection Title Curator"],
        )
        self.assertIsInstance(seeded.steps, list)
        steps = cast(list[Step], seeded.steps)
        self.assertTrue(all(isinstance(step, Step) for step in steps))
        self.assertEqual(
            [step.agent.id if step.agent is not None else getattr(step.executor, "__name__", None) for step in steps],
            [
                "prepare_collection_context",
                "raw-collector",
                "execute_collection_channels_step",
                "prepare_title_curation",
                "title-curator",
                "validate_title_curation",
                "build_artifact_step",
                "publish_collection_step",
            ],
        )
        self.assertTrue(all(step.max_retries == 0 for step in steps))
        self.assertTrue(all(str(step.on_error) == "fail" for step in steps))

    def test_live_non_streaming_workflow_result_has_visible_agent_steps(self) -> None:
        """Optional REST seam: set RUN_LIVE_AGENTOS_TESTS=1 after starting local AgentOS."""
        if os.getenv("RUN_LIVE_AGENTOS_TESTS") != "1":
            self.skipTest("RUN_LIVE_AGENTOS_TESTS=1 is required for the local REST acceptance seam")
        response = subprocess.run(
            [
                "curl",
                "-fsS",
                "--max-time",
                "900",
                "-X",
                "POST",
                "http://localhost:8000/workflows/raw-collection/runs",
                "-H",
                "Content-Type: application/x-www-form-urlencoded",
                "--data-urlencode",
                "message=采集最近48小时影响中国A股的重要政策与上市公司经营事件。",
                "--data",
                "stream=false",
                "--data",
                "background=false",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(response.stdout)
        result = payload[0] if isinstance(payload, list) else payload
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["content"]["candidate_counts"]["results_pending"], 0)
        steps = {item["step_name"]: item for item in result["step_results"]}
        self.assertEqual(steps["plan-collection-query"]["executor_type"], "agent")
        self.assertEqual(steps["curate-collection-titles"]["executor_type"], "agent")


if __name__ == "__main__":
    unittest.main()
