"""Consumer contract tests for the Data Service Source Snapshot boundary."""

import json
import os
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar, cast
from unittest.mock import patch
from urllib.error import URLError

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput

from capabilities.collection.functions import execute_collection_channels_step, prepare_collection_context
from capabilities.collection.internal.models import CollectionQueryPlan
from capabilities.collection.internal.source_snapshot import DataServiceSourceSnapshotProvider

_PROVIDER_FIXTURE = Path(__file__).parent / "fixtures" / "source-snapshot.v1.json"


class _SnapshotHandler(BaseHTTPRequestHandler):
    body: ClassVar[bytes] = b"{}"
    status: ClassVar[int] = 200
    requests: ClassVar[list[tuple[str, str | None]]] = []

    def do_GET(self) -> None:
        type(self).requests.append((self.path, self.headers.get("Authorization")))
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class SourceSnapshotConsumerContractTest(unittest.TestCase):
    server: ThreadingHTTPServer
    thread: threading.Thread

    def setUp(self) -> None:
        _SnapshotHandler.body = _PROVIDER_FIXTURE.read_bytes()
        _SnapshotHandler.status = 200
        _SnapshotHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _SnapshotHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_complete_provider_fixture_becomes_the_frozen_execution_model(self) -> None:
        provider = DataServiceSourceSnapshotProvider(
            base_url=f"http://127.0.0.1:{self.server.server_port}",
            token="service-token",
        )

        channels = provider.load_active_snapshot()

        self.assertEqual(_SnapshotHandler.requests, [("/api/data/v1/source-snapshot", "Bearer service-token")])
        self.assertEqual([channel.code for channel in channels], ["cls_telegraph", "bocha"])
        self.assertEqual(channels[0].adapter_key.value, "cls")
        self.assertEqual(str(channels[0].endpoint), "https://www.cls.cn/v1/roll/get_roll_list")
        self.assertEqual(channels[1].app_key, "plaintext-provider-key")
        self.assertEqual(json.loads(_PROVIDER_FIXTURE.read_text())["request_id"], "source-provider-fixture-v1")

    def test_empty_snapshot_is_a_valid_complete_snapshot(self) -> None:
        _SnapshotHandler.body = json.dumps({"request_id": "empty-snapshot", "result": {"sources": []}}).encode()

        channels = self._provider().load_active_snapshot()

        self.assertEqual(channels, ())

    def test_contract_integrity_failures_reject_the_whole_snapshot_without_leaking_credentials(self) -> None:
        fixture = json.loads(_PROVIDER_FIXTURE.read_text())
        cases: dict[str, list[dict[str, object]]] = {}

        disabled = self._sources(fixture)
        disabled[0]["enabled"] = False
        cases["inactive Source"] = disabled

        duplicate_id = self._sources(fixture)
        duplicate_id[1]["id"] = duplicate_id[0]["id"]
        cases["duplicate Source ID"] = duplicate_id

        duplicate_code = self._sources(fixture)
        duplicate_code[1]["code"] = duplicate_code[0]["code"]
        cases["duplicate Source code"] = duplicate_code

        cases["unstable order"] = list(reversed(self._sources(fixture)))

        two_search = self._sources(fixture)
        two_search[0].update(
            {
                "channel_type": "web_search",
                "adapter_key": "tavily",
                "code": "tavily",
            }
        )
        cases["multiple web-search Sources"] = sorted(
            two_search,
            key=lambda source: (source["channel_type"], source["priority"], source["code"], source["id"]),
        )

        incompatible = self._sources(fixture)
        incompatible[0]["adapter_key"] = "bocha"
        cases["adapter/channel mismatch"] = incompatible

        unknown_adapter = self._sources(fixture)
        unknown_adapter[0]["adapter_key"] = "unknown-adapter"
        cases["unknown adapter"] = unknown_adapter

        additional_field = self._sources(fixture)
        additional_field[0]["credential"] = "must-not-escape"
        cases["additional Source field"] = additional_field

        wrong_type = self._sources(fixture)
        wrong_type[0]["priority"] = "1"
        cases["wrong JSON type"] = wrong_type

        invalid_config = self._sources(fixture)
        invalid_config[0]["config"] = {
            "source_levels": {"example.com": "INVALID"},
            "credential": "must-not-escape",
        }
        cases["invalid config"] = invalid_config

        for name, sources in cases.items():
            with self.subTest(name=name):
                _SnapshotHandler.body = json.dumps(
                    {"request_id": "invalid-snapshot", "result": {"sources": sources}}
                ).encode()
                with self.assertRaisesRegex(ValueError, "violates the complete snapshot contract") as caught:
                    self._provider().load_active_snapshot()
                self.assertNotIn("must-not-escape", str(caught.exception))

        _SnapshotHandler.body = json.dumps(
            {
                "request_id": "invalid-envelope",
                "result": {"sources": []},
                "credential": "must-not-escape",
            }
        ).encode()
        with self.assertRaisesRegex(ValueError, "violates the complete snapshot contract") as caught:
            self._provider().load_active_snapshot()
        self.assertNotIn("must-not-escape", str(caught.exception))

    def test_snapshot_source_count_is_bounded(self) -> None:
        source = self._sources(json.loads(_PROVIDER_FIXTURE.read_text()))[0]
        sources = []
        for index in range(201):
            source_id = uuid.uuid5(uuid.NAMESPACE_URL, f"https://example.com/source/{index}")
            sources.append(
                {
                    **source,
                    "id": f"SRC{source_id}",
                    "code": f"source-{index:03d}",
                }
            )
        _SnapshotHandler.body = json.dumps({"request_id": "too-many-sources", "result": {"sources": sources}}).encode()

        with self.assertRaisesRegex(ValueError, "violates the complete snapshot contract"):
            self._provider().load_active_snapshot()

    def test_response_size_limit_is_enforced_before_parsing(self) -> None:
        _SnapshotHandler.body = b"{" + b"x" * 500_000

        with self.assertRaisesRegex(ValueError, "500000-byte contract limit"):
            self._provider().load_active_snapshot()

    def test_http_and_transport_failures_are_sanitized(self) -> None:
        _SnapshotHandler.status = 503
        _SnapshotHandler.body = json.dumps(
            {
                "request_id": "data-request-42",
                "error": {
                    "code": "SOURCE_TIMEOUT",
                    "message": "provider credential must-not-escape",
                },
            }
        ).encode()

        with self.assertRaisesRegex(
            ValueError,
            r"HTTP 503: SOURCE_TIMEOUT \(request_id=data-request-42\)",
        ) as caught:
            self._provider().load_active_snapshot()
        self.assertNotIn("must-not-escape", str(caught.exception))

        with patch(
            "capabilities.collection.internal.source_snapshot.urlopen",
            side_effect=URLError("provider credential must-not-escape"),
        ):
            with self.assertRaisesRegex(ValueError, "unavailable within the request budget") as caught:
                self._provider().load_active_snapshot()
        self.assertNotIn("must-not-escape", str(caught.exception))

    def _provider(self) -> DataServiceSourceSnapshotProvider:
        return DataServiceSourceSnapshotProvider(
            base_url=f"http://127.0.0.1:{self.server.server_port}",
            token="service-token",
        )

    @staticmethod
    def _sources(fixture: dict[str, object]) -> list[dict[str, object]]:
        result = fixture["result"]
        assert isinstance(result, dict)
        sources = result["sources"]
        assert isinstance(sources, list)
        return [dict(source) for source in sources]


class SourceSnapshotWorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        _SnapshotHandler.body = _PROVIDER_FIXTURE.read_bytes()
        _SnapshotHandler.status = 200
        _SnapshotHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _SnapshotHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    async def asyncTearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    async def test_raw_collection_preparation_freezes_exactly_one_remote_snapshot_before_planning(self) -> None:
        class EmptyAdapter:
            async def fetch(self, channel: object, request: object) -> list[object]:
                del channel, request
                return []

        context = RunContext(
            run_id="source-snapshot-run",
            session_id="session",
            dependencies={
                "kept": "value",
                "collector_agent_component_id": "raw-collector",
                "collector_agent_config_version": 1,
                "collector_instructions_sha256": "a" * 64,
                "collection_adapter_registry": {"bocha": EmptyAdapter(), "cls": EmptyAdapter()},
            },
        )
        step_input = StepInput(input={"objective": "采集最近政策信号"})

        with patch.dict(
            os.environ,
            {
                "DATA_SERVICE_BASE_URL": f"http://127.0.0.1:{self.server.server_port}",
                "DATA_SERVICE_TOKEN": "service-token",
                "COLLECTOR_ARTIFACT_ROOT": str(Path(self.temporary.name) / "collector"),
            },
        ):
            output = await prepare_collection_context(step_input, context)
            _SnapshotHandler.body = b"not-a-snapshot"
            execution = await execute_collection_channels_step(
                StepInput(
                    previous_step_outputs={
                        "plan-collection-query": StepOutput(
                            content=CollectionQueryPlan(query="政策信号", lookback_hours=48)
                        )
                    }
                ),
                context,
            )

        self.assertEqual(output.content, "采集最近政策信号")
        self.assertEqual(len(_SnapshotHandler.requests), 1)
        dependencies = context.dependencies
        self.assertIsNotNone(dependencies)
        assert dependencies is not None
        self.assertEqual(dependencies["kept"], "value")
        snapshot = dependencies["collection_channel_snapshot"]
        self.assertIsInstance(snapshot, tuple)
        self.assertEqual([channel.code for channel in snapshot], ["cls_telegraph", "bocha"])
        execution_content = cast(dict[str, object], execution.content)
        receipts = cast(list[dict[str, object]], execution_content["receipts"])
        self.assertEqual([receipt["outcome"] for receipt in receipts], ["succeeded", "succeeded", "no_channels"])
        self.assertEqual(len(_SnapshotHandler.requests), 1)
