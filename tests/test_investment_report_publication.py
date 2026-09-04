"""Canonical publication projection and mock publisher tests."""

import asyncio
import json
import tempfile
import traceback
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx
from agno.run import RunContext
from agno.workflow import StepInput
from pydantic import ValidationError

from capabilities.investment import configure_investment_workflow_runtime
from capabilities.investment.functions.reasoning import publish_investment_report
from capabilities.investment.internal.models import (
    InvestmentReportPublicationOutput,
    InvestmentReportWorkflowOutput,
)
from capabilities.investment.internal.report_contract import InvestmentReportArtifact
from capabilities.investment.internal.report_publication import (
    DataServiceReportPublisher,
    MockReportPublisher,
    PublicationConclusionBasis,
    PublicationConfidence,
    PublicationEvidenceRole,
    PublicationResult,
    PublicationTargetType,
    PublicationTimeWindow,
    PublicationTransmissionKind,
    PublicationValidationStatus,
    ReportPublicationConflict,
    ReportPublicationRejected,
    ReportPublicationRequest,
    ReportPublicationUnavailable,
    build_report_publication,
)


class InvestmentReportPublicationTest(unittest.IsolatedAsyncioTestCase):
    EVIDENCE_ID = "EVD11111111-1111-4111-8111-111111111111"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        configure_investment_workflow_runtime(None)
        self.temporary.cleanup()

    def _source_report(self) -> InvestmentReportArtifact:
        evidence = {"evidence_id": self.EVIDENCE_ID, "role": "直接依据", "display_order": 1}
        confidence = {"label": "中", "score": None}
        result = {"code": "warming", "label": "升温"}
        uncertainty = {
            "counterevidence": "替代供应可能缓冲冲击。",
            "evidence_gap": "仍需价格数据验证。",
            "boundary": "影响取决于冲突持续时间。",
            "reversal_condition": "若运输恢复则影响减弱。",
            "checkpoints": [],
        }
        geo_path_macro = {
            "key": "geo-to-macro-01",
            "display_order": 1,
            "source_conclusion": "海湾安全风险上升",
            "target_refs": [
                {
                    "ref": {"type": "anchor", "key": "macro-anchor-01"},
                    "label": "宏观经济 · 能源供应与进口安全",
                    "result": {"code": "cooling", "label": "降温"},
                }
            ],
            "logic": "运输风险增加能源供应不确定性。",
            "relation_nature": "跨层推理",
            "evidence_role": "推导背景",
            "confidence": confidence,
            "status": "已形成解释闭环",
            "evidence_refs": [],
        }
        geo_path_chain = {
            "key": "geo-to-chain-01",
            "display_order": 2,
            "source_conclusion": "海湾运输风险上升",
            "target_refs": [
                {
                    "ref": {"type": "industry_chain", "key": "chain-01"},
                    "label": "产业链",
                    "result": result,
                },
                {
                    "ref": {"type": "industry_chain_node", "key": "node-01"},
                    "label": "油气勘探",
                    "result": result,
                },
            ],
            "logic": "供应风险提高资源保障需求。",
            "relation_nature": "同源信号",
            "evidence_role": "推导背景",
            "confidence": confidence,
            "status": "目标节点已有直接 Signal",
            "evidence_refs": [],
        }
        anchor = {
            "key": "geo-anchor-01",
            "display_order": 1,
            "name": "伊朗—美国及海湾安全对抗",
            "current_state": "军事与航运风险上升。",
            "result": result,
            "nature": {"code": "direct_evidence", "label": "直接证据"},
            "reasoning": "冲突和航运受阻共同推高地区风险。",
            "time_window": "短期–中期",
            "confidence": confidence,
            "evidence_refs": [evidence],
        }
        macro_anchor = {
            "key": "macro-anchor-01",
            "display_order": 1,
            "name": "能源供应与进口安全",
            "current_state": "进口供应稳定性下降。",
            "result": {"code": "cooling", "label": "降温"},
            "nature": {"code": "reasoning_hypothesis", "label": "推理假设"},
            "reasoning": "运输风险可能增加进口成本。",
            "time_window": "中期",
            "confidence": confidence,
            "evidence_refs": [],
        }
        reasoning = {
            "key": "reasoning-01",
            "display_order": 1,
            "input": "冲突和运输受阻。",
            "mechanism": "关键航道风险上升。",
            "output": "地区风险升温。",
            "type": "事件 → 变量信号 → 锚点评估",
            "confidence": confidence,
            "evidence_refs": [evidence],
        }
        downward = {
            "summary": "风险继续向下传导。",
            "published_paths": [geo_path_macro, geo_path_chain],
            "candidate_mechanisms": [],
            "boundary_notes": [],
        }
        macro_downward = {
            "summary": "成本压力向产业链传导。",
            "published_paths": [geo_path_chain],
            "candidate_mechanisms": [],
            "boundary_notes": [],
        }
        statistics = {
            "event_count": 1,
            "ordinary_fact_count": 0,
            "signal_fact_count": 1,
            "transmission_hypothesis_count": 1,
            "remaining_topology_pending_count": 1,
            "adaptive_inclusion_threshold": 0.4,
            "adaptive_continuation_threshold": 0.5,
            "adaptive_hard_max_hops": 5,
            "adaptive_observed_max_hops": 1,
            "adaptive_stopped_by_confidence": 0,
            "adaptive_stopped_by_no_unvisited_neighbor": 0,
            "adaptive_rejected_below_inclusion": 0,
            "geopolitic_anchor_count": 1,
            "macroeconomic_anchor_count": 1,
            "signaled_chain_node_count": 1,
            "industry_chain_count": 1,
            "unmapped_chain_node_count": 0,
        }
        return InvestmentReportArtifact.model_validate(
            {
                "schema_version": "investment-report-artifact/v1",
                "source_report_id": "agentos-investment-run-001",
                "content": {
                    "report_type": "investment_reasoning",
                    "title": "每日投研推理报告",
                    "status": "generated",
                    "simulation": False,
                    "generated_at": "2026-09-03T10:30:00+08:00",
                    "timezone": "Asia/Shanghai",
                    "included_layers": ["geopolitics", "macroeconomics", "industry_chain"],
                    "statistics": statistics,
                    "report_cards": [],
                    "geopolitics": {
                        "key": "geopolitics",
                        "display_order": 1,
                        "title": "地缘政治面",
                        "conclusion": "海湾安全风险上升。",
                        "result": result,
                        "confidence": confidence,
                        "time_window": "短期–中期",
                        "anchors": [anchor],
                        "reasoning_steps": [reasoning],
                        "related_anchor_keys": ["geo-anchor-01"],
                        "related_chain_keys": ["chain-01"],
                        "downward_transmission": downward,
                        "uncertainty": uncertainty,
                        "evidence_refs": [evidence],
                    },
                    "macroeconomics": {
                        "key": "macroeconomics",
                        "display_order": 2,
                        "title": "宏观经济面",
                        "conclusion": "能源输入成本承压。",
                        "result": {"code": "diverging", "label": "分化"},
                        "confidence": confidence,
                        "time_window": "中期",
                        "anchors": [macro_anchor],
                        "reasoning_steps": [],
                        "related_anchor_keys": ["macro-anchor-01"],
                        "related_chain_keys": ["chain-01"],
                        "downward_transmission": macro_downward,
                        "uncertainty": uncertainty,
                        "evidence_refs": [],
                    },
                    "industry_chains": [
                        {
                            "key": "chain-01",
                            "claim_key": "claim-01",
                            "display_order": 1,
                            "name": "油气勘探开发产业链",
                            "conclusion": "上游资源保障需求升温。",
                            "status": "已形成链路结论",
                            "result": result,
                            "confidence": confidence,
                            "time_window": "中期",
                            "path_summary": "供应风险向勘探开发传导。",
                            "accepted_hypothesis_summary": "设备需求可能随后改善。",
                            "evidence_refs": [evidence],
                            "nodes": [
                                {
                                    "key": "node-01",
                                    "display_order": 1,
                                    "name": "油气勘探",
                                    "impact": "资源保障需求增强。",
                                    "result": result,
                                    "nature": {"code": "direct_evidence", "label": "直接证据"},
                                    "reasoning": "供应风险提高新增储量价值。",
                                    "time_window": "中期",
                                    "confidence": confidence,
                                    "evidence_refs": [evidence],
                                },
                                {
                                    "key": "node-02",
                                    "display_order": 2,
                                    "name": "油服设备",
                                    "impact": "设备订单可能增加。",
                                    "result": result,
                                    "nature": {"code": "reasoning_hypothesis", "label": "推理假设"},
                                    "reasoning": "勘探活动增加可能带动设备需求。",
                                    "time_window": "中期–长期",
                                    "confidence": {"label": "低", "score": None},
                                    "evidence_refs": [],
                                },
                            ],
                            "edges": [
                                {
                                    "key": "edge-01",
                                    "display_order": 1,
                                    "from_node_key": "node-01",
                                    "to_node_key": "node-02",
                                    "relation_label": "需求传导",
                                }
                            ],
                            "uncertainty": {
                                "counterevidence_and_gap": "仍需订单和价格数据验证。",
                                "stop_condition": "若供应恢复则停止传导。",
                                "checkpoints": [],
                            },
                        }
                    ],
                    "company": {
                        "key": "company",
                        "display_order": 4,
                        "title": "公司层面",
                        "included": False,
                        "boundary": "公司层面尚未纳入本期推理与报告范围。",
                    },
                },
            }
        )

    def test_projection_matches_the_canonical_publication_fixture(self) -> None:
        expected = ReportPublicationRequest.model_validate_json(
            (Path(__file__).parent / "fixtures" / "investment-report-publication-request.json").read_text(
                encoding="utf-8"
            )
        )

        actual = build_report_publication(self._source_report())

        self.assertEqual(actual, expected)
        payload = actual.model_dump(mode="json")
        self.assertNotIn("analysis_window", payload["report"])
        self.assertNotIn("statistics", payload["report"])
        self.assertNotIn("source_report_id", payload["report"])

    def test_code_and_label_cannot_diverge(self) -> None:
        with self.assertRaises(ValidationError):
            PublicationResult(code="warming", label="降温")

    def test_every_published_enum_uses_a_fixed_code_and_chinese_label_catalog(self) -> None:
        catalogs = [
            (PublicationResult, {"warming": "升温", "cooling": "降温", "diverging": "分化", "pending": "待验证"}),
            (PublicationConfidence, {"low": "低", "medium": "中", "high": "高"}),
            (
                PublicationTimeWindow,
                {
                    "short": "短期",
                    "medium": "中期",
                    "long": "长期",
                    "short_medium": "短期–中期",
                    "short_long": "短期–长期",
                    "medium_long": "中期–长期",
                    "short_medium_long": "短期–中期–长期",
                    "follow_up": "后续周期",
                },
            ),
            (
                PublicationConclusionBasis,
                {
                    "direct_evidence": "直接证据",
                    "reasoning_hypothesis": "推理假设",
                    "no_directional_conclusion": "无方向性结论",
                },
            ),
            (PublicationValidationStatus, {"confirmed": "已确认", "pending_validation": "待验证"}),
            (
                PublicationEvidenceRole,
                {"direct_support": "直接依据", "reasoning_support": "推导依据", "summary_support": "核心依据"},
            ),
            (
                PublicationTransmissionKind,
                {"cross_layer_reasoning": "跨层推理", "same_source_signal": "同源信号"},
            ),
            (
                PublicationTargetType,
                {
                    "macro_anchor": "宏观经济锚点",
                    "industry_chain": "产业链",
                    "industry_chain_node": "产业链节点",
                },
            ),
        ]
        for model, catalog in catalogs:
            for code, label in catalog.items():
                with self.subTest(model=model.__name__, code=code):
                    self.assertEqual(model(code=code, label=label).model_dump(), {"code": code, "label": label})

    async def test_mock_publisher_creates_then_replays_the_same_request(self) -> None:
        request = build_report_publication(self._source_report())
        publisher = MockReportPublisher(self.root)

        first = await publisher.publish(request)
        second = await publisher.publish(request)

        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.report_id, second.report_id)
        self.assertTrue(publisher.publication_path(request.publisher_report_id).is_file())

    async def test_mock_publisher_rejects_divergent_content_for_the_same_identity(self) -> None:
        request = build_report_publication(self._source_report())
        publisher = MockReportPublisher(self.root)
        await publisher.publish(request)
        changed = request.model_copy(
            update={
                "report": request.report.model_copy(
                    update={"generated_at": datetime.fromisoformat("2026-09-04T10:30:00+08:00")}
                )
            }
        )

        with self.assertRaisesRegex(ReportPublicationConflict, "divergent"):
            await publisher.publish(changed)

    async def test_data_service_publisher_posts_the_strict_request_and_accepts_create_or_replay(self) -> None:
        request = build_report_publication(self._source_report())
        cases = ((201, False), (200, True))

        for status_code, replayed in cases:
            with self.subTest(status_code=status_code):
                observed: list[httpx.Request] = []

                async def respond(http_request: httpx.Request) -> httpx.Response:
                    observed.append(http_request)
                    return httpx.Response(
                        status_code,
                        request=http_request,
                        json={
                            "request_id": "request-1",
                            "result": {
                                "report_id": "RPT11111111-1111-4111-8111-111111111111",
                                "published_at": "2026-09-04T08:00:00Z",
                                "replayed": replayed,
                            },
                        },
                    )

                publisher = DataServiceReportPublisher(
                    "https://data.example.test/",
                    "service-token",
                    transport=httpx.MockTransport(respond),
                    retry_delay_seconds=0,
                )
                receipt = await publisher.publish(request)

                self.assertEqual(receipt.replayed, replayed)
                self.assertEqual(receipt.report_id, "RPT11111111-1111-4111-8111-111111111111")
                self.assertEqual(len(observed), 1)
                self.assertEqual(observed[0].method, "POST")
                self.assertEqual(
                    str(observed[0].url),
                    "https://data.example.test/api/data/v1/report-publications",
                )
                self.assertEqual(observed[0].headers["Authorization"], "Bearer service-token")
                self.assertEqual(observed[0].headers["Content-Type"], "application/json")
                self.assertEqual(json.loads(observed[0].content), request.model_dump(mode="json"))

    async def test_data_service_publisher_retries_transient_failure_with_identical_payload(self) -> None:
        request = build_report_publication(self._source_report())
        observed: list[dict[str, object]] = []

        async def respond(http_request: httpx.Request) -> httpx.Response:
            observed.append(json.loads(http_request.content))
            if len(observed) == 1:
                return httpx.Response(503, request=http_request, json={"error": {"code": "DATA_SERVICE_NOT_READY"}})
            return httpx.Response(
                200,
                request=http_request,
                json={
                    "request_id": "request-2",
                    "result": {
                        "report_id": "RPT11111111-1111-4111-8111-111111111111",
                        "published_at": "2026-09-04T08:00:00Z",
                        "replayed": True,
                    },
                },
            )

        publisher = DataServiceReportPublisher(
            "https://data.example.test",
            "service-token",
            transport=httpx.MockTransport(respond),
            retry_delay_seconds=0,
        )

        receipt = await publisher.publish(request)

        self.assertTrue(receipt.replayed)
        self.assertEqual(observed, [request.model_dump(mode="json"), request.model_dump(mode="json")])

    async def test_data_service_publisher_fails_closed_for_permanent_rejection(self) -> None:
        request = build_report_publication(self._source_report())
        attempts = 0

        async def respond(http_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(
                422,
                request=http_request,
                json={"error": {"code": "REPORT_EVIDENCE_REFERENCE_INVALID", "message": "secret body"}},
            )

        publisher = DataServiceReportPublisher(
            "https://data.example.test",
            "service-token",
            transport=httpx.MockTransport(respond),
            retry_delay_seconds=0,
        )

        with self.assertRaises(ReportPublicationRejected) as raised:
            await publisher.publish(request)

        self.assertEqual(attempts, 1)
        self.assertNotIn("secret body", str(raised.exception))
        self.assertNotIn("service-token", str(raised.exception))

    async def test_data_service_publisher_maps_conflict_without_retry(self) -> None:
        request = build_report_publication(self._source_report())
        attempts = 0

        async def respond(http_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(409, request=http_request)

        publisher = DataServiceReportPublisher(
            "https://data.example.test",
            "service-token",
            transport=httpx.MockTransport(respond),
            retry_delay_seconds=0,
        )

        with self.assertRaises(ReportPublicationConflict):
            await publisher.publish(request)

        self.assertEqual(attempts, 1)

    async def test_data_service_publisher_rejects_malformed_success_envelope(self) -> None:
        request = build_report_publication(self._source_report())

        async def respond(http_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201,
                request=http_request,
                json={
                    "request_id": "request-3",
                    "result": {
                        "report_id": "secret-response-body",
                        "published_at": "2026-09-04T08:00:00Z",
                        "replayed": False,
                    },
                },
            )

        publisher = DataServiceReportPublisher(
            "https://data.example.test",
            "service-token",
            transport=httpx.MockTransport(respond),
            retry_delay_seconds=0,
        )

        with self.assertRaisesRegex(ReportPublicationUnavailable, "invalid success envelope") as raised:
            await publisher.publish(request)

        rendered = "".join(traceback.format_exception(raised.exception))
        self.assertNotIn("secret-response-body", rendered)

    async def test_data_service_publisher_rejects_coerced_replay_flag(self) -> None:
        request = build_report_publication(self._source_report())

        async def respond(http_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201,
                request=http_request,
                json={
                    "request_id": "request-4",
                    "result": {
                        "report_id": "RPT11111111-1111-4111-8111-111111111111",
                        "published_at": "2026-09-04T08:00:00Z",
                        "replayed": "false",
                    },
                },
            )

        publisher = DataServiceReportPublisher(
            "https://data.example.test",
            "service-token",
            transport=httpx.MockTransport(respond),
            retry_delay_seconds=0,
        )

        with self.assertRaisesRegex(ReportPublicationUnavailable, "invalid success envelope"):
            await publisher.publish(request)

    async def test_data_service_publisher_rejects_noncanonical_receipt_fields(self) -> None:
        request = build_report_publication(self._source_report())
        cases = (
            ("RPT------------------------------------", "2026-09-04T08:00:00Z"),
            ("RPT11111111-1111-4111-8111-111111111111", 1788508800),
            ("RPT11111111-1111-4111-8111-111111111111", "1788508800"),
            ("RPT11111111-1111-4111-8111-111111111111", "2026-09-04T08:00:00+00:00"),
        )

        for report_id, published_at in cases:
            with self.subTest(report_id=report_id, published_at=published_at):

                async def respond(http_request: httpx.Request) -> httpx.Response:
                    return httpx.Response(
                        201,
                        request=http_request,
                        json={
                            "request_id": "request-5",
                            "result": {
                                "report_id": report_id,
                                "published_at": published_at,
                                "replayed": False,
                            },
                        },
                    )

                publisher = DataServiceReportPublisher(
                    "https://data.example.test",
                    "service-token",
                    transport=httpx.MockTransport(respond),
                    retry_delay_seconds=0,
                )

                with self.assertRaisesRegex(ReportPublicationUnavailable, "invalid success envelope"):
                    await publisher.publish(request)

    async def test_data_service_publisher_retries_http_request_timeout(self) -> None:
        request = build_report_publication(self._source_report())
        attempts = 0

        async def respond(http_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(408, request=http_request)
            return httpx.Response(
                200,
                request=http_request,
                json={
                    "request_id": "request-408",
                    "result": {
                        "report_id": "RPT11111111-1111-4111-8111-111111111111",
                        "published_at": "2026-09-04T08:00:00Z",
                        "replayed": True,
                    },
                },
            )

        publisher = DataServiceReportPublisher(
            "https://data.example.test",
            "service-token",
            transport=httpx.MockTransport(respond),
            retry_delay_seconds=0,
        )

        receipt = await publisher.publish(request)

        self.assertTrue(receipt.replayed)
        self.assertEqual(attempts, 2)

    async def test_data_service_publisher_retries_transport_failures_then_fails_safely(self) -> None:
        request = build_report_publication(self._source_report())
        for error_type in (httpx.ConnectError, httpx.ReadTimeout):
            with self.subTest(error_type=error_type.__name__):
                attempts = 0

                async def respond(http_request: httpx.Request) -> httpx.Response:
                    nonlocal attempts
                    attempts += 1
                    raise error_type("sensitive provider diagnostic", request=http_request)

                publisher = DataServiceReportPublisher(
                    "https://data.example.test",
                    "service-token",
                    transport=httpx.MockTransport(respond),
                    retry_delay_seconds=0,
                )

                with self.assertRaises(ReportPublicationUnavailable) as raised:
                    await publisher.publish(request)

                self.assertEqual(attempts, 2)
                rendered = "".join(traceback.format_exception(raised.exception))
                self.assertNotIn("sensitive provider diagnostic", rendered)
                self.assertNotIn("service-token", rendered)

    async def test_data_service_publisher_enforces_one_total_timeout_across_retries(self) -> None:
        request = build_report_publication(self._source_report())
        attempts = 0

        async def respond(http_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503, request=http_request)

        publisher = DataServiceReportPublisher(
            "https://data.example.test",
            "service-token",
            timeout_seconds=0.01,
            total_timeout_seconds=0.01,
            max_attempts=2,
            retry_delay_seconds=1,
            transport=httpx.MockTransport(respond),
        )

        with self.assertRaisesRegex(ReportPublicationUnavailable, "total timeout"):
            await publisher.publish(request)

        self.assertEqual(attempts, 1)

    async def test_data_service_publisher_can_retry_after_one_request_timeout(self) -> None:
        request = build_report_publication(self._source_report())
        attempts = 0

        async def respond(http_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await asyncio.sleep(0.02)
            return httpx.Response(
                200,
                request=http_request,
                json={
                    "request_id": "request-6",
                    "result": {
                        "report_id": "RPT11111111-1111-4111-8111-111111111111",
                        "published_at": "2026-09-04T08:00:00Z",
                        "replayed": True,
                    },
                },
            )

        publisher = DataServiceReportPublisher(
            "https://data.example.test",
            "service-token",
            timeout_seconds=0.01,
            total_timeout_seconds=0.05,
            retry_delay_seconds=0,
            transport=httpx.MockTransport(respond),
        )

        receipt = await publisher.publish(request)

        self.assertTrue(receipt.replayed)
        self.assertEqual(attempts, 2)

    def test_data_service_publisher_rejects_unsafe_or_incomplete_configuration(self) -> None:
        cases = (
            ("data.example.test", "service-token", "BASE_URL"),
            ("https://user:password@data.example.test", "service-token", "BASE_URL"),
            ("https://data.example.test?token=unsafe", "service-token", "BASE_URL"),
            ("https://data.example.test", "", "TOKEN"),
            ("https://data.example.test", "service-token\nleak", "TOKEN"),
        )

        for base_url, token, message in cases:
            with self.subTest(base_url=base_url, token=bool(token)):
                with self.assertRaisesRegex(ValueError, message):
                    DataServiceReportPublisher(base_url, token)

    async def test_publish_step_uses_direct_predecessor_and_returns_the_mock_receipt(self) -> None:
        report = self._source_report()
        artifact_path = self.root / "report.json"
        artifact_path.write_text(report.model_dump_json(), encoding="utf-8")
        publisher = MockReportPublisher(self.root / "published")
        configure_investment_workflow_runtime(SimpleNamespace(publish_report=publisher.publish))
        generated = InvestmentReportWorkflowOutput(
            source_report_id=report.source_report_id,
            report_artifact_path=str(artifact_path),
            audit_artifact_path=str(self.root / "audit.json"),
            generation_status="GENERATED",
        )

        output = await publish_investment_report(
            StepInput(previous_step_content=generated),
            cast(RunContext, SimpleNamespace(run_id="run-001")),
        )

        result = InvestmentReportPublicationOutput.model_validate(output.content)
        self.assertEqual(result.publication_status, "PUBLISHED")
        self.assertEqual(result.publisher_report_id, report.source_report_id)
        self.assertFalse(result.replayed)

    async def test_skipped_generation_does_not_invoke_the_publisher(self) -> None:
        publish_report = AsyncMock()
        configure_investment_workflow_runtime(SimpleNamespace(publish_report=publish_report))
        generated = InvestmentReportWorkflowOutput(
            source_report_id="agentos-investment-run-skipped",
            report_artifact_path="",
            audit_artifact_path="audit.json",
            generation_status="SKIPPED",
            reason="没有可发布结论",
        )

        output = await publish_investment_report(
            StepInput(previous_step_content=generated),
            cast(RunContext, SimpleNamespace(run_id="run-skipped")),
        )

        result = InvestmentReportPublicationOutput.model_validate(output.content)
        self.assertEqual(result.publication_status, "SKIPPED")
        self.assertEqual(result.reason, "没有可发布结论")
        publish_report.assert_not_awaited()
