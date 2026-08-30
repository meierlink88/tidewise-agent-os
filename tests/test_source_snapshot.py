"""Consumer contract tests for the Data Service Source Snapshot boundary."""

import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch
from urllib.error import URLError

from agno.run import RunContext
from agno.workflow import StepInput

from capabilities.collection.functions import collect_raw_evidence
from capabilities.collection.internal.models import RawEvidenceFilterProgress
from capabilities.collection.internal.source_snapshot import DataServiceSourceSnapshotProvider

_PROVIDER_FIXTURE = Path(__file__).parent / "fixtures" / "source-snapshot.v1.json"


class _SnapshotHandler(BaseHTTPRequestHandler):
    body: ClassVar[bytes] = b"{}"
    status: ClassVar[int] = 200
    redirect_to: ClassVar[str | None] = None
    chunk_delay_seconds: ClassVar[float] = 0
    requests: ClassVar[list[tuple[str, str | None]]] = []

    def do_GET(self) -> None:
        type(self).requests.append((self.path, self.headers.get("Authorization")))
        self.send_response(type(self).status)
        redirect_to = type(self).redirect_to
        if redirect_to is not None:
            self.send_header("Location", redirect_to)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        try:
            if type(self).chunk_delay_seconds:
                for byte in type(self).body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(type(self).chunk_delay_seconds)
            else:
                self.wfile.write(type(self).body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class SourceSnapshotConsumerContractTest(unittest.TestCase):
    server: ThreadingHTTPServer
    thread: threading.Thread

    def setUp(self) -> None:
        _SnapshotHandler.body = _PROVIDER_FIXTURE.read_bytes()
        _SnapshotHandler.status = 200
        _SnapshotHandler.redirect_to = None
        _SnapshotHandler.chunk_delay_seconds = 0
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

    def test_wire_validation_accepts_values_allowed_by_openapi_without_adding_domain_rules(self) -> None:
        fixture = json.loads(_PROVIDER_FIXTURE.read_text())
        sources = self._sources(fixture)
        sources[0]["app_key"] = ""
        sources[0]["config"] = {
            "max_bytes": 1,
            "source_levels": {"": "L1_OFFICIAL"},
        }
        sources[0]["updated_at"] = "2026-08-18T00:00:00Z"
        _SnapshotHandler.body = json.dumps({"request_id": "openapi-valid", "result": {"sources": sources}}).encode()

        channels = self._provider().load_active_snapshot()

        self.assertEqual(channels[0].app_key, "")
        self.assertEqual(
            channels[0].config,
            {"max_bytes": 1, "source_levels": {"": "L1_OFFICIAL"}},
        )

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

        numeric_timestamp = self._sources(fixture)
        numeric_timestamp[0]["created_at"] = 0
        cases["numeric timestamp"] = numeric_timestamp

        space_timestamp = self._sources(fixture)
        space_timestamp[0]["created_at"] = "2026-08-19 00:00:00Z"
        cases["space-separated timestamp"] = space_timestamp

        compact_offset = self._sources(fixture)
        compact_offset[0]["created_at"] = "2026-08-19T00:00:00+0000"
        cases["non-RFC3339 offset"] = compact_offset

        unsupported_execution_endpoint = self._sources(fixture)
        unsupported_execution_endpoint[0]["endpoint"] = "ftp://example.com/source"
        cases["URI unsupported by collection execution"] = unsupported_execution_endpoint

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

        _SnapshotHandler.body = json.dumps({"request_id": "x" * 129, "result": {"sources": []}}).encode()
        with self.assertRaisesRegex(ValueError, "violates the complete snapshot contract"):
            self._provider().load_active_snapshot()

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
                "request_id": "data-20260819T142600.123456789",
                "error": {
                    "code": "SOURCE_TIMEOUT",
                    "message": "provider credential must-not-escape",
                    "details": {},
                },
            }
        ).encode()

        with self.assertRaisesRegex(
            ValueError,
            r"HTTP 503: SOURCE_TIMEOUT \(request_id=data-20260819T142600.123456789\)",
        ) as caught:
            self._provider().load_active_snapshot()
        self.assertNotIn("must-not-escape", str(caught.exception))

        _SnapshotHandler.body = json.dumps(
            {
                "request_id": "provider-credential-must-not-escape",
                "error": {
                    "code": "CREDENTIAL_MUST_NOT_ESCAPE",
                    "message": "provider credential must-not-escape",
                    "details": {},
                },
            }
        ).encode()
        with self.assertRaises(ValueError) as caught:
            self._provider().load_active_snapshot()
        self.assertNotIn("MUST_NOT_ESCAPE", str(caught.exception))
        self.assertNotIn("credential-must-not-escape", str(caught.exception))

        with patch(
            "capabilities.collection.internal.source_snapshot._open_request",
            side_effect=URLError("provider credential must-not-escape"),
        ):
            with self.assertRaisesRegex(ValueError, "unavailable within the request budget") as caught:
                self._provider().load_active_snapshot()
        self.assertNotIn("must-not-escape", str(caught.exception))

    def test_cross_origin_redirect_is_rejected_without_forwarding_the_token(self) -> None:
        class RedirectTargetHandler(_SnapshotHandler):
            requests: ClassVar[list[tuple[str, str | None]]] = []
            status = 200
            redirect_to = None
            chunk_delay_seconds = 0
            body = _PROVIDER_FIXTURE.read_bytes()

        target = ThreadingHTTPServer(("127.0.0.1", 0), RedirectTargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()
        _SnapshotHandler.status = 302
        _SnapshotHandler.redirect_to = f"http://127.0.0.1:{target.server_port}/stolen"
        try:
            with self.assertRaisesRegex(ValueError, "HTTP 302"):
                self._provider().load_active_snapshot()
            self.assertEqual(RedirectTargetHandler.requests, [])
        finally:
            target.shutdown()
            target.server_close()
            target_thread.join(timeout=2)

    def test_slow_drip_response_cannot_exceed_the_end_to_end_budget(self) -> None:
        _SnapshotHandler.chunk_delay_seconds = 0.02
        started = time.monotonic()

        with self.assertRaisesRegex(ValueError, "request budget"):
            DataServiceSourceSnapshotProvider(
                base_url=f"http://127.0.0.1:{self.server.server_port}",
                token="service-token",
                timeout_seconds=0.05,
            ).load_active_snapshot()

        self.assertLess(time.monotonic() - started, 0.3)

    def test_slow_drip_error_response_uses_the_same_end_to_end_budget(self) -> None:
        _SnapshotHandler.status = 503
        _SnapshotHandler.chunk_delay_seconds = 0.02
        _SnapshotHandler.body = json.dumps(
            {
                "request_id": "data-20260819T142600.123456789",
                "error": {
                    "code": "SOURCE_TIMEOUT",
                    "message": "timeout",
                    "details": {},
                },
            }
        ).encode()
        started = time.monotonic()

        with self.assertRaisesRegex(ValueError, "request budget"):
            DataServiceSourceSnapshotProvider(
                base_url=f"http://127.0.0.1:{self.server.server_port}",
                token="service-token",
                timeout_seconds=0.05,
            ).load_active_snapshot()

        self.assertLess(time.monotonic() - started, 0.3)

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

    async def test_raw_collection_acquisition_loads_one_snapshot_without_cross_step_state(self) -> None:
        class EmptyAdapter:
            async def fetch(self, channel: object, request: object) -> list[object]:
                del channel, request
                return []

        context = RunContext(
            run_id="source-snapshot-run",
            session_id="session",
            dependencies={
                "kept": "value",
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
            execution = await collect_raw_evidence(step_input, context)

        self.assertEqual(len(_SnapshotHandler.requests), 1)
        dependencies = context.dependencies
        self.assertIsNotNone(dependencies)
        assert dependencies is not None
        self.assertEqual(dependencies["kept"], "value")
        self.assertNotIn("collection_channel_snapshot", dependencies)
        execution_content = RawEvidenceFilterProgress.model_validate(execution.content)
        self.assertEqual(execution_content.total_candidates, 0)
        self.assertEqual(len(_SnapshotHandler.requests), 1)
