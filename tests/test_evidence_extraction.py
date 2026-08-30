"""Tests for incremental Evidence extraction and publication."""

import hashlib
import inspect
import json
import os
import shutil
import tempfile
import unittest
from copy import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

from agno.agent import Agent
from agno.registry import Registry
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.workflow import Loop, Step, StepInput, StepOutput, Steps, Workflow
from pydantic import ValidationError

from agents.evidence_extractor import (
    build_evidence_extractor_agent,
    ensure_evidence_extractor_agent,
    load_evidence_extractor_agent,
)
from app.registry import TidewiseRegistry
from capabilities.collection.internal.artifacts import build_artifact_set, publish_artifact_set
from capabilities.collection.internal.buffer import write_title_curation, write_tool_batch
from capabilities.collection.internal.models import (
    Candidate,
    CollectionRequest,
    SourceLevel,
    TitleCurationDecision,
    TitleCurationDraft,
)
from capabilities.evidence import read_resolved_evidences
from capabilities.evidence.functions import (
    evidence_extraction_complete,
    prepare_evidence_analysis,
    prepare_raw_document,
    publish_evidences,
    validate_evidence_analysis,
)
from capabilities.evidence.internal.models import (
    AtomicEvidenceDraft,
    EvidenceAnalysisRequest,
    EvidenceCategoryCatalog,
    EvidenceExtractionDraft,
    EvidenceExtractionIdle,
    EvidenceMetric,
    EvidencePublicationResult,
    EvidenceSetPublicationResponse,
    PreparedEvidencePublication,
    PreparedRawDocument,
    RawEvidenceEnrichment,
)
from capabilities.evidence.internal.storage import checkpoint_path, evidence_artifact_root, read_checkpoint
from workflows.evidence_extraction import (
    EVIDENCE_EXTRACTION_CONTRACT_VERSION,
    _seed_workflow,
    ensure_evidence_extraction_workflow,
)


class AcceptingRawDocumentStore:
    def publish_markdown(self, *, bucket: str, object_key: str, content: bytes, sha256: str) -> None:
        del bucket, object_key, content, sha256


class EvidenceExtractionTest(unittest.IsolatedAsyncioTestCase):
    CATEGORY_ID = "EVC15bec7e3-998c-5434-aa5d-29712c4c67cf"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {
                "COLLECTOR_ARTIFACT_ROOT": str(root / "collector"),
                "EVIDENCE_ARTIFACT_ROOT": str(root / "evidence"),
                "EVENT_ARTIFACT_ROOT": str(root / "event"),
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    @classmethod
    def _category_item(cls) -> dict[str, str]:
        return {
            "id": cls.CATEGORY_ID,
            "code": "EVENT_BRIEF",
            "name": "事件快讯",
            "description": "事件发生后的简要事实更新。",
        }

    @classmethod
    def _catalog_result(cls) -> dict[str, list[dict[str, str]]]:
        return {"categories": [cls._category_item()]}

    @classmethod
    def _catalog(cls) -> EvidenceCategoryCatalog:
        return EvidenceCategoryCatalog.model_validate(cls._catalog_result())

    @classmethod
    def _catalog_sha256(cls) -> str:
        payload = json.dumps(
            cls._catalog_result(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @classmethod
    def _run_context(cls, run_id: str) -> RunContext:
        return RunContext(
            run_id=run_id,
            session_id=f"session-{run_id}",
            dependencies={"evidence_category_catalog": cls._catalog()},
        )

    def _publish_raw_fixture(self) -> None:
        now = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
        candidate = Candidate(
            candidate_id="candidate-evidence",
            connector="cls_telegraph",
            query="AI服务器订单",
            title="示例公司签署服务器订单",
            url="https://example.test/evidence-source",
            content="示例公司公告签署10亿元服务器订单，合同期限为三年。",
            source_name="财联社",
            source_level=SourceLevel.L2_WIRE,
            published_at=now,
            collected_at=now + timedelta(minutes=1),
        )
        write_tool_batch(
            collection_id="collection-evidence",
            connector="cls_telegraph",
            query="AI服务器订单",
            candidates=[candidate],
        )
        write_title_curation(
            "collection-evidence",
            TitleCurationDraft(
                decisions=[
                    TitleCurationDecision(
                        candidate_id=candidate.candidate_id,
                        is_relevant=True,
                    )
                ]
            ),
        )
        prepared = build_artifact_set(
            "collection-evidence",
            CollectionRequest(objective="采集最近2小时服务器订单"),
            completed_at=now + timedelta(minutes=2),
        )
        publish_artifact_set(prepared, document_store=AcceptingRawDocumentStore())

    @staticmethod
    def _draft() -> EvidenceExtractionDraft:
        return EvidenceExtractionDraft(
            raw_evidence=RawEvidenceEnrichment(
                category_code="EVENT_BRIEF",
                is_original=False,
                quoted_source_name="示例公司公告",
            ),
            evidences=[
                AtomicEvidenceDraft(
                    summary="示例公司签署10亿元三年期服务器订单",
                    keywords=["服务器", "订单"],
                    semantic={
                        "actors": ["示例公司"],
                        "action": "签署",
                        "objects": ["服务器订单"],
                        "stage": "ANNOUNCED",
                        "modality": "FACT",
                        "time": {
                            "raw": "2026-08-11",
                            "start_at": None,
                            "end_at": None,
                            "precision": "DAY",
                        },
                        "jurisdictions": ["中国"],
                        "reason": None,
                        "method": "合同金额10亿元，期限三年",
                        "metrics": [
                            {"name": "合同金额", "value": "10", "unit": "亿元", "change": None, "period": "三年"}
                        ],
                        "attribution": {"reported_by": "财联社", "claimed_by": "示例公司公告"},
                    },
                )
            ],
        )

    def _prepared(self) -> PreparedRawDocument:
        output = prepare_raw_document(StepInput(input="处理未提取文档"))
        self.assertFalse(output.stop)
        return PreparedRawDocument.model_validate(output.content)

    def _validated(self, prepared: PreparedRawDocument) -> PreparedEvidencePublication:
        return self._validated_draft(prepared, self._draft())

    def _validated_draft(
        self,
        prepared: PreparedRawDocument,
        draft: EvidenceExtractionDraft,
    ) -> PreparedEvidencePublication:
        draft = EvidenceExtractionDraft.model_validate(draft.model_dump(mode="json"))
        step_input = StepInput(
            previous_step_outputs={
                "prepare-raw-document": StepOutput(content=prepared),
                "analyze-raw-evidence": StepOutput(content=draft),
            }
        )
        output = validate_evidence_analysis(step_input, self._run_context("run-evidence"))
        return PreparedEvidencePublication.model_validate(output.content)

    async def test_analysis_fetches_catalog_once_per_run_and_hides_ids_from_agent(self) -> None:
        self._publish_raw_fixture()
        prepared = self._prepared()
        context = RunContext(run_id="run-catalog", session_id="session-catalog", dependencies={})
        step_input = StepInput(previous_step_outputs={"prepare-raw-document": StepOutput(content=prepared)})

        with patch(
            "capabilities.evidence.functions.extraction.get_evidence_categories",
            return_value=self._catalog_result(),
        ) as mocked:
            first = await prepare_evidence_analysis(step_input, context)
            second = await prepare_evidence_analysis(step_input, context)

        self.assertEqual(mocked.call_count, 1)
        request = EvidenceAnalysisRequest.model_validate(first.content)
        self.assertEqual(EvidenceAnalysisRequest.model_validate(second.content), request)
        self.assertEqual(request.document, prepared)
        self.assertEqual(request.categories[0].code, "EVENT_BRIEF")
        self.assertNotIn("id", request.categories[0].model_dump())
        snapshot = context.dependencies["evidence_category_catalog"]  # type: ignore[index]
        self.assertEqual(EvidenceCategoryCatalog.model_validate(snapshot).categories[0].id, self.CATEGORY_ID)

    async def test_analysis_initializes_missing_run_dependencies(self) -> None:
        self._publish_raw_fixture()
        prepared = self._prepared()
        context = RunContext(
            run_id="run-missing-dependencies",
            session_id="session-missing-dependencies",
            session_state={},
        )
        step_input = StepInput(previous_step_outputs={"prepare-raw-document": StepOutput(content=prepared)})

        with patch(
            "capabilities.evidence.functions.extraction.get_evidence_categories",
            return_value=self._catalog_result(),
        ) as mocked:
            first = await prepare_evidence_analysis(step_input, copy(context))
            second = await prepare_evidence_analysis(step_input, copy(context))

        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(
            EvidenceAnalysisRequest.model_validate(first.content),
            EvidenceAnalysisRequest.model_validate(second.content),
        )
        self.assertIsNotNone(context.session_state)
        assert context.session_state is not None
        self.assertIn("evidence_extraction", context.session_state)
        validation_input = StepInput(
            previous_step_outputs={
                "prepare-raw-document": StepOutput(content=prepared),
                "analyze-raw-evidence": StepOutput(content=self._draft()),
            }
        )
        validated = validate_evidence_analysis(validation_input, copy(context))
        self.assertIsInstance(validated.content, PreparedEvidencePublication)

    async def test_invalid_catalog_fails_before_agent_analysis(self) -> None:
        self._publish_raw_fixture()
        prepared = self._prepared()
        context = RunContext(run_id="run-invalid-catalog", session_id="session-invalid-catalog", dependencies={})
        step_input = StepInput(previous_step_outputs={"prepare-raw-document": StepOutput(content=prepared)})
        duplicate_code = {
            "categories": [
                self._category_item(),
                {
                    "id": "EVC5cb71bef-5b1d-5995-add0-7408eaa2be15",
                    "code": "EVENT_BRIEF",
                    "name": "重复分类",
                    "description": "不应被接受。",
                },
            ]
        }

        with (
            patch(
                "capabilities.evidence.functions.extraction.get_evidence_categories",
                return_value=duplicate_code,
            ),
            self.assertRaisesRegex(ValueError, "Category Catalog is invalid"),
        ):
            await prepare_evidence_analysis(step_input, context)

        self.assertFalse(context.dependencies)

    def test_category_catalog_rejects_incomplete_empty_duplicate_and_unstable_values(self) -> None:
        second_id = "EVC5cb71bef-5b1d-5995-add0-7408eaa2be15"
        valid = self._category_item()
        cases = [
            {"categories": []},
            {
                "categories": [
                    valid,
                    {
                        **valid,
                        "code": "IN_DEPTH_REPORT",
                    },
                ]
            },
            {
                "categories": [
                    {
                        **valid,
                        "id": second_id,
                        "code": "IN_DEPTH_REPORT",
                    },
                    valid,
                ]
            },
        ]
        cases.extend(
            {"categories": [{key: value for key, value in valid.items() if key != missing}]}
            for missing in ("id", "code", "name", "description")
        )
        cases.extend({"categories": [{**valid, field: " "}]} for field in ("id", "code", "name", "description"))

        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                EvidenceCategoryCatalog.model_validate(value)

    def test_no_work_stops_before_category_catalog_is_needed(self) -> None:
        with patch("capabilities.evidence.functions.extraction.get_evidence_categories") as mocked:
            output = prepare_raw_document(StepInput(input="没有待处理文档"))

        self.assertTrue(output.stop)
        self.assertIsInstance(output.content, EvidenceExtractionIdle)
        mocked.assert_not_called()

    def test_unknown_category_code_fails_before_publication_is_prepared(self) -> None:
        self._publish_raw_fixture()
        prepared = self._prepared()
        draft = self._draft().model_copy(deep=True)
        draft.raw_evidence.category_code = "UNKNOWN_CATEGORY"
        step_input = StepInput(
            previous_step_outputs={
                "prepare-raw-document": StepOutput(content=prepared),
                "analyze-raw-evidence": StepOutput(content=draft),
            }
        )

        with self.assertRaisesRegex(ValueError, "unknown Evidence Category code"):
            validate_evidence_analysis(step_input, self._run_context("run-unknown-category"))

    def test_prepare_reads_manifest_index_and_strips_artifact_wrapper(self) -> None:
        self._publish_raw_fixture()
        prepared = self._prepared()
        self.assertEqual(prepared.collection_id, "collection-evidence")
        self.assertEqual(prepared.source_level, "L2_WIRE")
        self.assertEqual(prepared.raw_text, "示例公司公告签署10亿元服务器订单，合同期限为三年。")
        self.assertFalse(checkpoint_path().exists())

    def test_semantic_contract_rejects_invalid_keywords_blanks_and_legacy_fields(self) -> None:
        with self.assertRaises(ValidationError):
            AtomicEvidenceDraft(
                summary="示例事实",
                keywords=["超过六个字符标签"],
                semantic=self._draft().evidences[0].semantic,
            )
        with self.assertRaises(ValidationError):
            AtomicEvidenceDraft(
                summary="示例事实",
                keywords=["示例"],
                semantic={
                    **self._draft().evidences[0].semantic.model_dump(mode="json"),
                    "action": " ",
                },
            )
        with self.assertRaises(ValidationError):
            AtomicEvidenceDraft.model_validate(
                {
                    "summary": "示例事实",
                    "keywords": ["示例"],
                    "semantic": self._draft().evidences[0].semantic.model_dump(mode="json"),
                    "expression_fingerprint": "旧字段不允许",
                }
            )

    def test_provider_openapi_atomic_evidence_fixture_round_trips_exactly(self) -> None:
        fixture = {
            "raw_evidence_id": "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf",
            "evidences": [
                {
                    "summary": "Example Corp expands production",
                    "keywords": ["扩产", "产能"],
                    "semantic": {
                        "actors": ["Example Corp"],
                        "action": "expanded production",
                        "objects": ["production capacity"],
                        "stage": "OCCURRED",
                        "modality": "FACT",
                        "time": {"raw": "August 10, 2026", "start_at": None, "end_at": None, "precision": "DAY"},
                        "jurisdictions": [],
                        "reason": None,
                        "method": "by adding a new production line",
                        "metrics": [],
                        "attribution": {"reported_by": None, "claimed_by": "Example Corp"},
                    },
                },
                {
                    "summary": "Example Corp secures additional capacity",
                    "keywords": ["产能"],
                    "semantic": {
                        "actors": ["Example Corp"],
                        "action": "secured",
                        "objects": ["additional capacity"],
                        "stage": "OCCURRED",
                        "modality": "FACT",
                        "time": {"raw": "August 10, 2026", "start_at": None, "end_at": None, "precision": "DAY"},
                        "jurisdictions": [],
                        "reason": "to meet rising demand",
                        "method": None,
                        "metrics": [],
                        "attribution": {"reported_by": None, "claimed_by": "Example Corp"},
                    },
                },
            ],
        }

        parsed = [AtomicEvidenceDraft.model_validate(item).model_dump(mode="json") for item in fixture["evidences"]]
        self.assertEqual(
            {"raw_evidence_id": fixture["raw_evidence_id"], "evidences": parsed},
            fixture,
        )

    def test_provider_evidence_publication_response_fixture_preserves_request_mapping(self) -> None:
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "evidence-publication-response.v1.json").read_text(encoding="utf-8")
        )

        response = EvidenceSetPublicationResponse.model_validate(fixture["result"])

        self.assertEqual(response.ids, sorted(response.ids))
        self.assertIsNotNone(response.items)
        assert response.items is not None
        self.assertEqual([item.input_index for item in response.items], [0, 1])
        self.assertEqual([item.id for item in response.items], [response.ids[1], response.ids[0]])

    def test_atomic_evidence_rejects_summary_overflow_and_invalid_semantic_shape(self) -> None:
        semantic = self._draft().evidences[0].semantic.model_dump(mode="json")
        invalid = [
            {"summary": "x" * 201, "keywords": ["示例"], "semantic": semantic},
            {
                "summary": "summary",
                "keywords": ["示例"],
                "semantic": {key: value for key, value in semantic.items() if key != "reason"},
            },
            {"summary": "summary", "keywords": ["示例"], "semantic": {**semantic, "confidence": "high"}},
            {"summary": "summary", "keywords": ["示例"], "semantic": {**semantic, "actors": [" "]}},
        ]

        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                AtomicEvidenceDraft.model_validate(payload)

    def test_validation_adds_stable_publication_key_and_exact_atomic_contract(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        self.assertEqual(
            publication.raw_evidence.raw_text,
            "/raw-evidence/documents/2026/08/11/c6fe9177b96308182802eb456d47768b06d890fa96b9e08f159a0f6fd2470128.md",
        )
        self.assertNotIn("示例公司公告签署10亿元服务器订单", publication.raw_evidence.raw_text)
        self.assertTrue(publication.raw_evidence.publication_key.startswith("agentos.raw-evidence.v1:"))
        self.assertLessEqual(len(publication.raw_evidence.publication_key), 128)
        self.assertNotIn("raw_evidence_id", publication.raw_evidence.model_dump(mode="json"))
        self.assertEqual(publication.category_catalog_sha256, self._catalog_sha256())
        self.assertEqual(publication.selected_category_code, "EVENT_BRIEF")
        self.assertEqual(publication.raw_evidence.category_ids, [self.CATEGORY_ID])
        self.assertNotIn("evidence_id", publication.evidences[0].model_dump(mode="json"))
        self.assertEqual(
            set(publication.evidences[0].model_dump(mode="json")),
            {"summary", "keywords", "semantic"},
        )
        self.assertEqual(
            set(publication.evidences[0].semantic.model_dump()),
            {
                "actors",
                "action",
                "objects",
                "stage",
                "modality",
                "time",
                "jurisdictions",
                "reason",
                "method",
                "metrics",
                "attribution",
            },
        )
        self.assertEqual(publication.schema_version, "prepared_evidence_publication.v5")

    def test_reuters_attribution_does_not_replace_business_actor_and_relative_time_is_not_guessed(self) -> None:
        self._publish_raw_fixture()
        draft = self._draft().model_copy(deep=True)
        evidence = draft.evidences[0]
        evidence.summary = "路透社称美国计划周末攻击伊朗"
        evidence.keywords = ["美伊冲突", "军事行动"]
        evidence.semantic.actors = ["美国"]
        evidence.semantic.action = "计划攻击"
        evidence.semantic.objects = ["伊朗"]
        evidence.semantic.stage = "EXPECTED"
        evidence.semantic.modality = "PLAN"
        evidence.semantic.time.raw = "周末"
        evidence.semantic.time.precision = "UNKNOWN"
        evidence.semantic.jurisdictions = ["伊朗"]
        evidence.semantic.method = None
        evidence.semantic.metrics = []
        evidence.semantic.attribution.reported_by = "路透社"
        evidence.semantic.attribution.claimed_by = None

        publication = self._validated_draft(self._prepared(), draft)
        semantic = publication.evidences[0].semantic

        self.assertEqual(semantic.actors, ["美国"])
        self.assertNotIn("路透社", semantic.actors)
        self.assertEqual(semantic.attribution.reported_by, "路透社")
        self.assertEqual(semantic.time.raw, "周末")
        self.assertIsNone(semantic.time.start_at)
        self.assertIsNone(semantic.time.end_at)

    def test_company_metrics_stay_in_one_business_proposition_and_guidance_can_split(self) -> None:
        self._publish_raw_fixture()
        draft = self._draft().model_copy(deep=True)
        actual = draft.evidences[0]
        actual.summary = "英伟达披露季度收入、毛利率和数据中心收入"
        actual.keywords = ["英伟达", "财报", "数据中心"]
        actual.semantic.actors = ["英伟达"]
        actual.semantic.action = "披露季度业绩"
        actual.semantic.objects = ["季度经营结果"]
        actual.semantic.stage = "OCCURRED"
        actual.semantic.metrics = [
            EvidenceMetric(name="收入", value="30", unit="亿美元", change="+20%", period="2026Q2"),
            EvidenceMetric(name="毛利率", value="75", unit="%", change=None, period="2026Q2"),
            EvidenceMetric(name="数据中心收入", value="26", unit="亿美元", change="+25%", period="2026Q2"),
        ]
        guidance = actual.model_copy(deep=True)
        guidance.summary = "英伟达预计下一季度收入继续增长"
        guidance.keywords = ["英伟达", "业绩指引"]
        guidance.semantic.action = "发布收入指引"
        guidance.semantic.objects = ["下一季度收入"]
        guidance.semantic.stage = "EXPECTED"
        guidance.semantic.modality = "PLAN"
        guidance.semantic.metrics = [
            EvidenceMetric(name="收入指引", value="32", unit="亿美元", change=None, period="2026Q3")
        ]
        draft.evidences = [actual, guidance]

        publication = self._validated_draft(self._prepared(), draft)

        self.assertEqual(len(publication.evidences), 2)
        self.assertEqual(len(publication.evidences[0].semantic.metrics), 3)
        self.assertEqual({item.semantic.stage for item in publication.evidences}, {"OCCURRED", "EXPECTED"})

    def test_exact_repetition_collapses_but_divergent_same_identity_fails_closed(self) -> None:
        self._publish_raw_fixture()
        prepared = self._prepared()
        draft = self._draft().model_copy(deep=True)
        draft.evidences.append(draft.evidences[0].model_copy(deep=True))

        publication = self._validated_draft(prepared, draft)
        self.assertEqual(len(publication.evidences), 1)

        divergent = self._draft().model_copy(deep=True)
        duplicate_identity = divergent.evidences[0].model_copy(deep=True)
        duplicate_identity.summary = "相同动作但出现不一致的补充摘要"
        divergent.evidences.append(duplicate_identity)
        with self.assertRaisesRegex(ValueError, "identity collision"):
            self._validated_draft(prepared, divergent)

    def test_legacy_manifest_is_audited_and_skipped_without_body_publication(self) -> None:
        self._publish_raw_fixture()
        collector_root = Path(os.environ["COLLECTOR_ARTIFACT_ROOT"])
        index_path = collector_root / "indexes/manifest-index.jsonl"
        index_entry = json.loads(index_path.read_text(encoding="utf-8"))
        manifest_path = collector_root / index_entry["manifest_path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema"] = "raw_collection_manifest.v1"
        manifest["accepted_documents"][0].pop("url_path")
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        manifest_path.write_bytes(manifest_bytes)
        index_entry["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
        index_path.write_text(
            json.dumps(index_entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        output = prepare_raw_document(StepInput(input="处理未提取文档"))

        self.assertTrue(output.stop)
        self.assertIsInstance(output.content, EvidenceExtractionIdle)
        self.assertGreater(read_checkpoint().manifest_offset, 0)
        audits = list((evidence_artifact_root() / "legacy-skips").glob("*.json"))
        self.assertEqual(len(audits), 1)
        audit = json.loads(audits[0].read_text(encoding="utf-8"))
        self.assertEqual(audit["reason"], "archived_url_path_unavailable")
        self.assertEqual(audit["skipped_documents"], 1)
        self.assertNotIn("示例公司公告签署10亿元服务器订单", audits[0].read_text(encoding="utf-8"))

    def test_validation_preserves_supported_fuzzy_fact_time_in_semantic(self) -> None:
        self._publish_raw_fixture()
        prepared = self._prepared()
        draft = self._draft().model_dump(mode="json")
        draft["evidences"][0]["semantic"]["time"] = {
            "raw": "十五五期间",
            "start_at": None,
            "end_at": None,
            "precision": "RANGE",
        }
        step_input = StepInput(
            previous_step_outputs={
                "prepare-raw-document": StepOutput(content=prepared),
                "analyze-raw-evidence": StepOutput(content=json.dumps(draft, ensure_ascii=False)),
            }
        )
        publication = PreparedEvidencePublication.model_validate(
            validate_evidence_analysis(step_input, self._run_context("run-fuzzy-time")).content
        )

        self.assertEqual(publication.evidences[0].semantic.time.raw, "十五五期间")
        self.assertEqual(publication.evidences[0].semantic.time.precision, "RANGE")
        self.assertIsNone(publication.evidences[0].semantic.time.start_at)
        self.assertIsNone(publication.evidences[0].semantic.time.end_at)

    def test_evidence_keywords_preserve_order_while_deduplicating_and_reject_oversized_values(self) -> None:
        draft = AtomicEvidenceDraft(
            summary="伊朗天然气产量发生变化",
            keywords=["伊朗", "天然气", "伊朗", "伊朗", "伊朗", "伊朗"],
            semantic=self._draft().evidences[0].semantic,
        )
        self.assertEqual(draft.keywords, ["伊朗", "天然气"])
        with self.assertRaises(ValidationError):
            AtomicEvidenceDraft(
                summary="伊朗天然气产量发生变化",
                keywords=["伊朗", "天然气", "产量", "供给", "能源", "价格"],
                semantic=self._draft().evidences[0].semantic,
            )
        with self.assertRaises(ValidationError):
            AtomicEvidenceDraft(
                summary="伊朗天然气产量发生变化",
                keywords=["9500万立方米"],
                semantic=self._draft().evidences[0].semantic,
            )

    async def test_publication_writes_manifest_last_and_advances_checkpoint(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        step_input = StepInput(previous_step_outputs={"validate-evidence-analysis": StepOutput(content=publication)})
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
        responses = [
            {"id": raw_evidence_id},
            {
                "raw_evidence_id": raw_evidence_id,
                "ids": [evidence_id],
                "items": [{"input_index": 0, "id": evidence_id}],
            },
        ]
        with patch(
            "capabilities.evidence.functions.extraction.post_publication",
            side_effect=responses,
        ) as mocked:
            output = await publish_evidences(step_input)
        result = EvidencePublicationResult.model_validate(output.content)
        self.assertEqual(mocked.call_count, 2)
        raw_endpoint, raw_payload = mocked.call_args_list[0].args
        self.assertEqual(raw_endpoint, "raw-evidence-publications")
        self.assertEqual(raw_payload["raw_evidence"]["publication_key"], publication.raw_evidence.publication_key)
        self.assertNotIn("raw_evidence_id", raw_payload["raw_evidence"])
        self.assertEqual(raw_payload["raw_evidence"]["category_ids"], [self.CATEGORY_ID])
        self.assertEqual(
            raw_payload["raw_evidence"]["raw_text"],
            "/raw-evidence/documents/2026/08/11/c6fe9177b96308182802eb456d47768b06d890fa96b9e08f159a0f6fd2470128.md",
        )
        self.assertNotIn("10亿元服务器订单", raw_payload["raw_evidence"]["raw_text"])
        evidence_endpoint, evidence_payload = mocked.call_args_list[1].args
        self.assertEqual(evidence_endpoint, "evidence-publications")
        self.assertEqual(evidence_payload["raw_evidence_id"], raw_evidence_id)
        self.assertNotIn("evidence_id", evidence_payload["evidences"][0])
        self.assertEqual(set(evidence_payload["evidences"][0]), {"summary", "keywords", "semantic"})
        self.assertTrue(Path(result.artifact_manifest_path).is_file())
        manifest = json.loads(Path(result.artifact_manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "evidence_extraction_manifest.v5")
        self.assertEqual(manifest["publication_key"], publication.raw_evidence.publication_key)
        self.assertEqual(manifest["raw_evidence_id"], raw_evidence_id)
        self.assertEqual(manifest["evidence_ids"], [evidence_id])
        self.assertEqual(manifest["artifacts"], {"prepared": "prepared.json", "bindings": "bindings.json"})
        self.assertEqual(
            {path.name for path in Path(result.artifact_manifest_path).parent.iterdir()},
            {"manifest.json", "prepared.json", "bindings.json"},
        )
        frozen = json.loads((Path(result.artifact_manifest_path).parent / "prepared.json").read_text(encoding="utf-8"))
        self.assertNotIn("category_catalog", frozen)
        self.assertEqual(frozen["category_catalog_sha256"], self._catalog_sha256())
        self.assertEqual(frozen["selected_category_code"], "EVENT_BRIEF")
        self.assertEqual(frozen["raw_evidence"]["category_ids"], [self.CATEGORY_ID])
        self.assertEqual(result.raw_evidence_id, raw_evidence_id)
        self.assertEqual(result.evidence_ids, [evidence_id])
        queue_marker = Path(os.environ["EVENT_ARTIFACT_ROOT"]) / "evidence-queue" / "pending" / f"{evidence_id}.json"
        self.assertTrue(queue_marker.is_file())
        self.assertEqual(list((evidence_artifact_root() / ".pending").glob("*")), [])
        self.assertGreater(result.checkpoint.manifest_offset, 0)
        self.assertEqual(result.checkpoint, read_checkpoint())

        idle = prepare_raw_document(StepInput(input="再运行"))
        self.assertTrue(idle.stop)
        self.assertIsInstance(idle.content, EvidenceExtractionIdle)

        with patch("capabilities.evidence.functions.extraction.post_publication") as repeated:
            repeated_output = await publish_evidences(step_input)
        self.assertEqual(repeated.call_count, 0)
        self.assertEqual(
            EvidencePublicationResult.model_validate(repeated_output.content).checkpoint, read_checkpoint()
        )
        self.assertEqual(len(list(queue_marker.parent.glob("*.json"))), 1)

    async def test_failed_evidence_publication_does_not_advance_checkpoint(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        step_input = StepInput(previous_step_outputs={"validate-evidence-analysis": StepOutput(content=publication)})
        with (
            patch(
                "capabilities.evidence.functions.extraction.post_publication",
                side_effect=[
                    {"id": "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"},
                    ValueError("evidence rejected"),
                ],
            ),
            self.assertRaisesRegex(ValueError, "evidence rejected"),
        ):
            await publish_evidences(step_input)

        self.assertEqual(read_checkpoint().manifest_offset, 0)
        self.assertEqual(
            list((Path(os.environ["EVENT_ARTIFACT_ROOT"]) / "evidence-queue" / "pending").glob("*.json")),
            [],
        )
        self.assertEqual(list((evidence_artifact_root() / "documents").glob("*/manifest.json")), [])

    async def test_multi_evidence_response_persists_request_indexed_bindings(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        second = publication.evidences[0].model_copy(deep=True)
        second.summary = "示例公司公告合同期限为三年"
        second.semantic.action = "公告服务器订单合同期限为三年"
        publication.evidences.append(second)
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence_ids = [
            "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15",
            "EVD15bec7e3-998c-5434-aa5d-29712c4c67cf",
        ]
        step_input = StepInput(previous_step_outputs={"validate-evidence-analysis": StepOutput(content=publication)})

        with patch(
            "capabilities.evidence.functions.extraction.post_publication",
            side_effect=[
                {"id": raw_evidence_id},
                {
                    "raw_evidence_id": raw_evidence_id,
                    "ids": sorted(evidence_ids),
                    "items": [
                        {"input_index": 0, "id": evidence_ids[0]},
                        {"input_index": 1, "id": evidence_ids[1]},
                    ],
                },
            ],
        ):
            output = await publish_evidences(step_input)

        result = EvidencePublicationResult.model_validate(output.content)
        manifest = json.loads(Path(result.artifact_manifest_path).read_text(encoding="utf-8"))
        bindings = json.loads(
            (Path(result.artifact_manifest_path).parent / "bindings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(result.evidence_ids, sorted(evidence_ids))
        self.assertEqual(manifest["schema"], "evidence_extraction_manifest.v5")
        self.assertEqual(manifest["evidence_ids"], sorted(evidence_ids))
        self.assertEqual(manifest["artifacts"], {"bindings": "bindings.json", "prepared": "prepared.json"})
        self.assertEqual(
            bindings,
            {
                "schema_version": "evidence_identity_bindings.v1",
                "publication_key": publication.raw_evidence.publication_key,
                "raw_evidence_id": raw_evidence_id,
                "document_sha256": publication.prepared_raw.document_sha256,
                "evidence_count": 2,
                "items": [
                    {"input_index": 0, "id": evidence_ids[0]},
                    {"input_index": 1, "id": evidence_ids[1]},
                ],
            },
        )

        checkpoint_path().unlink()
        with patch("capabilities.evidence.functions.extraction.post_publication") as repeated:
            recovered = await publish_evidences(step_input)

        repeated.assert_not_called()
        self.assertEqual(
            EvidencePublicationResult.model_validate(recovered.content).evidence_ids,
            sorted(evidence_ids),
        )
        self.assertEqual(read_checkpoint().manifest_offset, publication.prepared_raw.next_manifest_offset)

    async def test_public_capability_resolves_formal_ids_with_matching_local_semantics(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        second = publication.evidences[0].model_copy(deep=True)
        second.summary = "示例公司公告合同期限为三年"
        second.semantic.action = "公告服务器订单合同期限为三年"
        publication.evidences.append(second)
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        first_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
        second_id = "EVD15bec7e3-998c-5434-aa5d-29712c4c67cf"
        step_input = StepInput(previous_step_outputs={"validate-evidence-analysis": StepOutput(content=publication)})
        with patch(
            "capabilities.evidence.functions.extraction.post_publication",
            side_effect=[
                {"id": raw_evidence_id},
                {
                    "raw_evidence_id": raw_evidence_id,
                    "ids": sorted([first_id, second_id]),
                    "items": [
                        {"input_index": 0, "id": first_id},
                        {"input_index": 1, "id": second_id},
                    ],
                },
            ],
        ):
            output = await publish_evidences(step_input)

        resolved = read_resolved_evidences(
            EvidencePublicationResult.model_validate(output.content).artifact_manifest_path
        )

        self.assertEqual([item.id for item in resolved], [first_id, second_id])
        self.assertEqual([item.raw_evidence_id for item in resolved], [raw_evidence_id, raw_evidence_id])
        self.assertEqual([item.summary for item in resolved], [item.summary for item in publication.evidences])
        self.assertEqual(
            [item.semantic.action for item in resolved],
            [item.semantic.action for item in publication.evidences],
        )

    async def test_duplicate_response_ids_fail_with_matching_evidence_count(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        publication.evidences.append(publication.evidences[0].model_copy(deep=True))
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        duplicate_id = "EVD15bec7e3-998c-5434-aa5d-29712c4c67cf"
        step_input = StepInput(previous_step_outputs={"validate-evidence-analysis": StepOutput(content=publication)})

        with (
            patch(
                "capabilities.evidence.functions.extraction.post_publication",
                side_effect=[
                    {"id": raw_evidence_id},
                    {"raw_evidence_id": raw_evidence_id, "ids": [duplicate_id, duplicate_id]},
                ],
            ),
            self.assertRaisesRegex(ValueError, "Evidence publication response is invalid"),
        ):
            await publish_evidences(step_input)

        self.assertEqual(read_checkpoint().manifest_offset, 0)

    async def test_invalid_request_indexed_mappings_fail_before_checkpoint_or_manifest(self) -> None:
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        first_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
        second_id = "EVD15bec7e3-998c-5434-aa5d-29712c4c67cf"
        other_id = "EVDec95a292-d513-5aa6-a54c-a9e3926add1a"
        cases = {
            "missing index": {
                "raw_evidence_id": raw_evidence_id,
                "ids": [first_id],
                "items": [{"input_index": 0, "id": first_id}],
            },
            "duplicate index": {
                "raw_evidence_id": raw_evidence_id,
                "ids": [first_id, second_id],
                "items": [
                    {"input_index": 0, "id": first_id},
                    {"input_index": 0, "id": second_id},
                ],
            },
            "out of range index": {
                "raw_evidence_id": raw_evidence_id,
                "ids": [first_id, second_id],
                "items": [
                    {"input_index": 0, "id": first_id},
                    {"input_index": 2, "id": second_id},
                ],
            },
            "identity set disagreement": {
                "raw_evidence_id": raw_evidence_id,
                "ids": [first_id, second_id],
                "items": [
                    {"input_index": 0, "id": first_id},
                    {"input_index": 1, "id": other_id},
                ],
            },
        }

        for name, evidence_response in cases.items():
            with self.subTest(name=name):
                shutil.rmtree(os.environ["COLLECTOR_ARTIFACT_ROOT"], ignore_errors=True)
                shutil.rmtree(os.environ["EVIDENCE_ARTIFACT_ROOT"], ignore_errors=True)
                self._publish_raw_fixture()
                publication = self._validated(self._prepared())
                second = publication.evidences[0].model_copy(deep=True)
                second.summary = "示例公司公告合同期限为三年"
                second.semantic.action = "公告服务器订单合同期限为三年"
                publication.evidences.append(second)
                step_input = StepInput(
                    previous_step_outputs={"validate-evidence-analysis": StepOutput(content=publication)}
                )
                with (
                    patch(
                        "capabilities.evidence.functions.extraction.post_publication",
                        side_effect=[{"id": raw_evidence_id}, evidence_response],
                    ),
                    self.assertRaises(ValueError),
                ):
                    await publish_evidences(step_input)

                self.assertEqual(read_checkpoint().manifest_offset, 0)
                self.assertEqual(list((evidence_artifact_root() / "documents").glob("*/manifest.json")), [])

    async def test_duplicate_ids_in_final_manifest_fail_closed_before_checkpoint_recovery(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        second = publication.evidences[0].model_copy(deep=True)
        second.summary = "示例公司公告合同期限为三年"
        second.semantic.action = "公告服务器订单合同期限为三年"
        publication.evidences.append(second)
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence_ids = [
            "EVD15bec7e3-998c-5434-aa5d-29712c4c67cf",
            "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15",
        ]
        step_input = StepInput(previous_step_outputs={"validate-evidence-analysis": StepOutput(content=publication)})
        with patch(
            "capabilities.evidence.functions.extraction.post_publication",
            side_effect=[
                {"id": raw_evidence_id},
                {
                    "raw_evidence_id": raw_evidence_id,
                    "ids": evidence_ids,
                    "items": [
                        {"input_index": 0, "id": evidence_ids[0]},
                        {"input_index": 1, "id": evidence_ids[1]},
                    ],
                },
            ],
        ):
            output = await publish_evidences(step_input)

        result = EvidencePublicationResult.model_validate(output.content)
        manifest_path = Path(result.artifact_manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["evidence_ids"] = [evidence_ids[0], evidence_ids[0]]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        checkpoint_path().unlink()

        with (
            patch("capabilities.evidence.functions.extraction.post_publication") as repeated,
            self.assertRaisesRegex(ValueError, "published Evidence Artifact"),
        ):
            await publish_evidences(step_input)

        repeated.assert_not_called()
        self.assertEqual(read_checkpoint().manifest_offset, 0)

    async def test_retry_reuses_frozen_payload_after_partial_publication(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        step_input = StepInput(previous_step_outputs={"validate-evidence-analysis": StepOutput(content=publication)})
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
        with (
            patch(
                "capabilities.evidence.functions.extraction.post_publication",
                side_effect=[{"id": raw_evidence_id}, ValueError("evidence rejected")],
            ),
            self.assertRaisesRegex(ValueError, "evidence rejected"),
        ):
            await publish_evidences(step_input)

        changed = publication.model_copy(deep=True)
        changed.raw_evidence.category_ids = ["EVCec95a292-d513-5aa6-a54c-a9e3926add1a"]
        changed.evidences[0].keywords = ["变更"]
        changed.evidences[0].semantic.action = "不应在重试中发布的变更事实"
        retry_input = StepInput(previous_step_outputs={"validate-evidence-analysis": StepOutput(content=changed)})
        with patch(
            "capabilities.evidence.functions.extraction.post_publication",
            side_effect=[
                {"id": raw_evidence_id},
                {
                    "raw_evidence_id": raw_evidence_id,
                    "ids": [evidence_id],
                    "items": [{"input_index": 0, "id": evidence_id}],
                },
            ],
        ) as retried:
            output = await publish_evidences(retry_input)

        raw_payload = retried.call_args_list[0].args[1]["raw_evidence"]
        evidence_payload = retried.call_args_list[1].args[1]["evidences"][0]
        self.assertNotIn("keywords", raw_payload)
        self.assertEqual(raw_payload["category_ids"], [self.CATEGORY_ID])
        self.assertEqual(evidence_payload["semantic"]["action"], publication.evidences[0].semantic.action)
        self.assertEqual(evidence_payload["keywords"], publication.evidences[0].keywords)
        self.assertEqual(EvidencePublicationResult.model_validate(output.content).evidence_ids, [evidence_id])

    async def test_final_manifest_recovers_checkpoint_despite_new_agent_draft(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        step_input = StepInput(previous_step_outputs={"validate-evidence-analysis": StepOutput(content=publication)})
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
        with (
            patch(
                "capabilities.evidence.functions.extraction.post_publication",
                side_effect=[
                    {"id": raw_evidence_id},
                    {
                        "raw_evidence_id": raw_evidence_id,
                        "ids": [evidence_id],
                        "items": [{"input_index": 0, "id": evidence_id}],
                    },
                ],
            ),
            patch(
                "capabilities.evidence.functions.extraction.advance_checkpoint",
                side_effect=RuntimeError("checkpoint interrupted"),
            ),
            self.assertRaisesRegex(RuntimeError, "checkpoint interrupted"),
        ):
            await publish_evidences(step_input)

        queue_marker = Path(os.environ["EVENT_ARTIFACT_ROOT"]) / "evidence-queue" / "pending" / f"{evidence_id}.json"
        self.assertTrue(queue_marker.is_file())

        changed = publication.model_copy(deep=True)
        second = changed.evidences[0].model_copy(deep=True)
        second.summary = "后续 Agent 输出的额外事实"
        second.semantic.action = "后续 Agent 输出的额外事实"
        changed.evidences.append(second)
        retry_input = StepInput(previous_step_outputs={"validate-evidence-analysis": StepOutput(content=changed)})
        with patch("capabilities.evidence.functions.extraction.post_publication") as repeated:
            output = await publish_evidences(retry_input)

        result = EvidencePublicationResult.model_validate(output.content)
        self.assertEqual(repeated.call_count, 0)
        self.assertEqual(result.evidence_count, 1)
        self.assertEqual(result.evidence_ids, [evidence_id])
        self.assertEqual(result.checkpoint, read_checkpoint())

    async def test_invalid_data_service_id_responses_do_not_advance_checkpoint(self) -> None:
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
        cases = [
            ([{}, {}], "Raw Evidence publication response"),
            (
                [
                    {"id": raw_evidence_id},
                    {
                        "raw_evidence_id": "RAWec95a292-d513-5aa6-a54c-a9e3926add1a",
                        "ids": [evidence_id],
                        "items": [{"input_index": 0, "id": evidence_id}],
                    },
                ],
                "Raw Evidence identity mismatch",
            ),
            (
                [
                    {"id": raw_evidence_id},
                    {
                        "raw_evidence_id": raw_evidence_id,
                        "ids": [evidence_id, "EVD15bec7e3-998c-5434-aa5d-29712c4c67cf"],
                        "items": [
                            {"input_index": 0, "id": evidence_id},
                            {"input_index": 1, "id": "EVD15bec7e3-998c-5434-aa5d-29712c4c67cf"},
                        ],
                    },
                ],
                "Evidence identity count mismatch",
            ),
        ]
        for responses, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                shutil.rmtree(os.environ["COLLECTOR_ARTIFACT_ROOT"], ignore_errors=True)
                shutil.rmtree(os.environ["EVIDENCE_ARTIFACT_ROOT"], ignore_errors=True)
                self._publish_raw_fixture()
                publication = self._validated(self._prepared())
                step_input = StepInput(
                    previous_step_outputs={"validate-evidence-analysis": StepOutput(content=publication)}
                )
                with (
                    patch(
                        "capabilities.evidence.functions.extraction.post_publication",
                        side_effect=responses,
                    ),
                    self.assertRaisesRegex(ValueError, expected_error),
                ):
                    await publish_evidences(step_input)
                self.assertEqual(read_checkpoint().manifest_offset, 0)

    async def test_complete_workflow_classifies_extracts_publishes_and_checkpoints(self) -> None:
        self._publish_raw_fixture()
        agent = build_evidence_extractor_agent()
        agent.db = None
        workflow = _seed_workflow(agent)
        workflow.db = None
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
        controlled_agent = AsyncMock(
            return_value=RunOutput(
                agent_id="evidence-extractor",
                content=self._draft(),
                content_type="EvidenceExtractionDraft",
            )
        )
        with (
            patch.object(agent, "arun", new=controlled_agent),
            patch(
                "capabilities.evidence.functions.extraction.get_evidence_categories",
                return_value=self._catalog_result(),
            ) as catalog_call,
            patch(
                "capabilities.evidence.functions.extraction.post_publication",
                side_effect=[
                    {"id": raw_evidence_id},
                    {
                        "raw_evidence_id": raw_evidence_id,
                        "ids": [evidence_id],
                        "items": [{"input_index": 0, "id": evidence_id}],
                    },
                ],
            ) as publication_call,
        ):
            response = await workflow.arun(
                input="处理所有尚未提取的 Raw Document",
                run_id="run-complete-workflow",
                session_id="session-complete-workflow",
            )  # type: ignore[misc]

        self.assertEqual(response.status, RunStatus.completed)
        self.assertEqual(catalog_call.call_count, 1)
        self.assertEqual(controlled_agent.call_count, 1)
        analysis = EvidenceAnalysisRequest.model_validate(controlled_agent.call_args.kwargs["input"])
        self.assertEqual(analysis.categories[0].code, "EVENT_BRIEF")
        self.assertNotIn("id", analysis.categories[0].model_dump())
        self.assertEqual(publication_call.call_count, 2)
        self.assertEqual(publication_call.call_args_list[0].args[1]["raw_evidence"]["category_ids"], [self.CATEGORY_ID])
        evidence_payload = publication_call.call_args_list[1].args[1]["evidences"][0]
        self.assertEqual(evidence_payload["summary"], "示例公司签署10亿元三年期服务器订单")
        self.assertEqual(evidence_payload["keywords"], ["服务器", "订单"])
        self.assertEqual(evidence_payload["semantic"]["actors"], ["示例公司"])
        self.assertEqual(evidence_payload["semantic"]["action"], "签署")
        self.assertEqual(evidence_payload["semantic"]["time"]["precision"], "DAY")
        self.assertIsNotNone(evidence_payload["semantic"]["time"]["start_at"])
        self.assertGreater(read_checkpoint().manifest_offset, 0)
        manifests = list((evidence_artifact_root() / "documents").glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        frozen = json.loads((manifests[0].parent / "prepared.json").read_text(encoding="utf-8"))
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(frozen["schema_version"], "prepared_evidence_publication.v5")
        self.assertEqual(manifest["schema"], "evidence_extraction_manifest.v5")
        self.assertEqual(manifest["evidence_ids"], [evidence_id])
        self.assertNotIn("evidences", manifest)
        self.assertNotIn("category_catalog", frozen)
        self.assertEqual(frozen["category_catalog_sha256"], self._catalog_sha256())
        self.assertEqual(frozen["selected_category_code"], "EVENT_BRIEF")

    def test_workflow_round_trips_one_agent_and_four_functions(self) -> None:
        agent = build_evidence_extractor_agent()
        registry = Registry(
            name="Evidence Test Registry",
            agents=[agent],
            functions=[
                evidence_extraction_complete,
                prepare_raw_document,
                prepare_evidence_analysis,
                validate_evidence_analysis,
                publish_evidences,
            ],
        )
        workflow = _seed_workflow(agent)
        self.assertIsNotNone(workflow.metadata)
        assert workflow.metadata is not None
        self.assertEqual(
            workflow.metadata["evidence_extraction_contract_version"], EVIDENCE_EXTRACTION_CONTRACT_VERSION
        )
        restored = Workflow.from_dict(workflow.to_dict(), registry=registry)
        restored_steps = restored.steps
        self.assertIsInstance(restored_steps, list)
        loop = cast(Loop, cast(list[object], restored_steps)[0])
        self.assertIsInstance(loop, Loop)
        stages = cast(list[Steps], loop.steps)
        self.assertEqual(
            [stage.name for stage in stages],
            ["extract-evidences", "publish-evidences"],
        )
        extract_steps = cast(list[Step], stages[0].steps)
        publish_steps = cast(list[Step], stages[1].steps)
        self.assertEqual(
            [step.name for step in extract_steps],
            ["prepare-raw-document", "prepare-evidence-analysis", "analyze-raw-evidence", "validate-evidence-analysis"],
        )
        self.assertEqual([step.name for step in publish_steps], ["publish-evidence-set"])
        self.assertEqual(extract_steps[2].agent.id, "evidence-extractor")  # type: ignore[union-attr]
        self.assertEqual(agent.tools, [])
        self.assertIn("exactly one category", str(agent.additional_context))
        self.assertEqual(
            [
                getattr(step.executor, "__name__", None)
                for step in (extract_steps[0], extract_steps[1], extract_steps[3], publish_steps[0])
            ],
            [
                "prepare_raw_document",
                "prepare_evidence_analysis",
                "validate_evidence_analysis",
                "publish_evidences",
            ],
        )
        self.assertTrue(inspect.iscoroutinefunction(extract_steps[1].executor))
        self.assertTrue(inspect.iscoroutinefunction(publish_steps[0].executor))

    def test_workflow_agent_loads_component_without_runtime_session_db(self) -> None:
        db = MagicMock()
        db.get_component.return_value = {"current_version": 7}
        loaded = Agent(id="evidence-extractor", instructions="published instructions", db=db)
        registry = MagicMock()

        with (
            patch("agents.evidence_extractor.get_postgres_db", return_value=db),
            patch("agents.evidence_extractor.Agent.load", return_value=loaded) as agent_load,
        ):
            agent = load_evidence_extractor_agent(registry)

        agent_load.assert_called_once_with(
            "evidence-extractor",
            db=db,
            registry=registry,
            version=7,
        )
        self.assertIsNone(agent.db)

    def test_database_workflow_rehydrates_a_sessionless_agent_runtime_copy(self) -> None:
        component_db = MagicMock()
        published = Agent(id="evidence-extractor", instructions="published instructions", db=component_db)
        workflow = _seed_workflow(published)
        runtime = Agent(id="evidence-extractor", instructions="published instructions", db=None)
        registry = TidewiseRegistry(
            functions=[
                evidence_extraction_complete,
                prepare_raw_document,
                prepare_evidence_analysis,
                validate_evidence_analysis,
                publish_evidences,
            ]
        )

        with patch("app.registry.load_evidence_extractor_agent", return_value=runtime) as runtime_load:
            restored = Workflow.from_dict(workflow.to_dict(), registry=registry)

        runtime_load.assert_called_once_with(registry)
        loop = cast(Loop, cast(list[object], restored.steps)[0])
        extract_stage = cast(list[Steps], loop.steps)[0]
        analyze = cast(list[Step], extract_stage.steps)[2]
        self.assertIsNotNone(restored.db)
        self.assertIsNotNone(analyze.agent)
        assert analyze.agent is not None
        self.assertIsNone(analyze.agent.db)

    def test_workflow_contract_migration_keeps_workflow_db_and_detaches_agent_db(self) -> None:
        db = MagicMock()
        db.get_component.return_value = {"current_version": 12}
        db.get_config.return_value = {
            "config": {
                "id": "evidence-extraction",
                "name": "Evidence Extraction",
                "metadata": {"evidence_extraction_contract_version": 5},
            }
        }
        published_agent = Agent(id="evidence-extractor", instructions="published instructions", db=db)

        with (
            patch("workflows.evidence_extraction.get_postgres_db", return_value=db),
            patch("agents.evidence_extractor.get_postgres_db", return_value=db),
            patch("agents.evidence_extractor.Agent.load", return_value=published_agent),
            patch.object(Workflow, "save", autospec=True, return_value=13) as workflow_save,
        ):
            version = ensure_evidence_extraction_workflow(MagicMock())

        self.assertEqual(version, 13)
        migrated = cast(Workflow, workflow_save.call_args.args[0])
        self.assertIs(migrated.db, db)
        loop = cast(Loop, cast(list[object], migrated.steps)[0])
        extract_stage = cast(list[Steps], loop.steps)[0]
        analyze = cast(list[Step], extract_stage.steps)[2]
        self.assertIsNotNone(analyze.agent)
        assert analyze.agent is not None
        self.assertIsNone(analyze.agent.db)
        self.assertIsNotNone(migrated.metadata)
        assert migrated.metadata is not None
        self.assertEqual(
            migrated.metadata["evidence_extraction_contract_version"],
            EVIDENCE_EXTRACTION_CONTRACT_VERSION,
        )

    def test_agent_contract_migration_publishes_reviewed_atomic_evidence_prompt(self) -> None:
        db = MagicMock()
        db.get_component.return_value = {"current_version": 7}
        current = MagicMock()
        current.metadata = {"evidence_extractor_contract_version": 3}
        current.instructions = "retired SINGLE/DOUBLE and source_* contract"
        current.save.return_value = 8

        with (
            patch("agents.evidence_extractor.get_postgres_db", return_value=db),
            patch("agents.evidence_extractor.Agent.load", return_value=current),
        ):
            version = ensure_evidence_extractor_agent(MagicMock())

        self.assertEqual(version, 8)
        self.assertIn("`summary`", current.instructions)
        self.assertIn("`semantic`", current.instructions)
        self.assertNotIn("SINGLE/DOUBLE", current.instructions)


if __name__ == "__main__":
    unittest.main()
