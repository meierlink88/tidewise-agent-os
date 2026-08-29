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
from agno.workflow import Loop, Step, StepInput, StepOutput, Workflow
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
from capabilities.evidence import read_resolved_evidences, reconcile_evidence_bindings
from capabilities.evidence.functions import (
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
                keywords=["服务器", "订单"],
                is_original=False,
                quoted_source_name="示例公司公告",
            ),
            evidences=[
                AtomicEvidenceDraft(
                    summary="示例公司签署10亿元三年期服务器订单",
                    semantic={
                        "who": "示例公司",
                        "what": "签署10亿元服务器订单",
                        "when": None,
                        "where": None,
                        "why": None,
                        "how": "合同期限为三年",
                    },
                )
            ],
        )

    def _prepared(self) -> PreparedRawDocument:
        output = prepare_raw_document(StepInput(input="处理未提取文档"))
        self.assertFalse(output.stop)
        return PreparedRawDocument.model_validate(output.content)

    def _validated(self, prepared: PreparedRawDocument) -> PreparedEvidencePublication:
        step_input = StepInput(
            previous_step_outputs={
                "prepare-raw-document": StepOutput(content=prepared),
                "analyze-raw-evidence": StepOutput(content=self._draft()),
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
            RawEvidenceEnrichment(
                category_code="EVENT_BRIEF",
                keywords=["超过五个字符"],
                is_original=True,
            )
        with self.assertRaises(ValidationError):
            AtomicEvidenceDraft(
                summary="示例事实",
                semantic={
                    "who": None,
                    "what": " ",
                    "when": None,
                    "where": None,
                    "why": None,
                    "how": None,
                },
            )
        with self.assertRaises(ValidationError):
            AtomicEvidenceDraft.model_validate(
                {
                    "summary": "示例事实",
                    "semantic": {
                        "who": None,
                        "what": "示例事实",
                        "when": None,
                        "where": None,
                        "why": None,
                        "how": None,
                    },
                    "expression_fingerprint": "旧字段不允许",
                }
            )

    def test_provider_openapi_atomic_evidence_fixture_round_trips_exactly(self) -> None:
        fixture = {
            "raw_evidence_id": "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf",
            "evidences": [
                {
                    "summary": "Example Corp expands production",
                    "semantic": {
                        "who": "Example Corp",
                        "what": "expanded production",
                        "when": "August 10, 2026",
                        "where": None,
                        "why": None,
                        "how": "by adding a new production line",
                    },
                },
                {
                    "summary": "Example Corp secures additional capacity",
                    "semantic": {
                        "who": "Example Corp",
                        "what": "secured additional capacity",
                        "when": "August 10, 2026",
                        "where": None,
                        "why": "to meet rising demand",
                        "how": None,
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
        semantic = {
            "who": None,
            "what": "expanded production",
            "when": None,
            "where": None,
            "why": None,
            "how": None,
        }
        invalid = [
            {"summary": "x" * 201, "semantic": semantic},
            {"summary": "summary", "semantic": {key: value for key, value in semantic.items() if key != "why"}},
            {"summary": "summary", "semantic": {**semantic, "confidence": "high"}},
            {"summary": "summary", "semantic": {**semantic, "who": " "}},
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
            {"summary", "semantic"},
        )
        self.assertEqual(
            set(publication.evidences[0].semantic.model_dump()),
            {"who", "what", "when", "where", "why", "how"},
        )
        self.assertEqual(publication.schema_version, "prepared_evidence_publication.v4")

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
        draft["evidences"][0]["semantic"]["when"] = "十五五期间"
        step_input = StepInput(
            previous_step_outputs={
                "prepare-raw-document": StepOutput(content=prepared),
                "analyze-raw-evidence": StepOutput(content=json.dumps(draft, ensure_ascii=False)),
            }
        )
        publication = PreparedEvidencePublication.model_validate(
            validate_evidence_analysis(step_input, self._run_context("run-fuzzy-time")).content
        )

        self.assertEqual(publication.evidences[0].semantic.when, "十五五期间")

    def test_validation_discards_invalid_keywords_when_valid_ones_remain(self) -> None:
        enrichment = RawEvidenceEnrichment.model_validate(
            {
                "category_code": "EVENT_BRIEF",
                "keywords": ["伊朗", "天然气", "9500万立方米", "伊朗"],
                "is_original": True,
                "quoted_source_name": None,
            }
        )

        self.assertEqual(enrichment.keywords, ["伊朗", "天然气"])

    async def test_publication_writes_manifest_last_and_advances_checkpoint(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        step_input = StepInput(previous_step_outputs={"validate-evidence-analysis": StepOutput(content=publication)})
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
        responses = [
            {"id": raw_evidence_id},
            {"raw_evidence_id": raw_evidence_id, "ids": [evidence_id]},
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
        self.assertEqual(set(evidence_payload["evidences"][0]), {"summary", "semantic"})
        self.assertTrue(Path(result.artifact_manifest_path).is_file())
        manifest = json.loads(Path(result.artifact_manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "evidence_extraction_manifest.v4")
        self.assertEqual(manifest["publication_key"], publication.raw_evidence.publication_key)
        self.assertEqual(manifest["raw_evidence_id"], raw_evidence_id)
        self.assertEqual(manifest["evidence_ids"], [evidence_id])
        self.assertEqual(manifest["artifacts"], {"prepared": "prepared.json"})
        self.assertEqual(
            {path.name for path in Path(result.artifact_manifest_path).parent.iterdir()},
            {"manifest.json", "prepared.json"},
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
        second.semantic.what = "公告服务器订单合同期限为三年"
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
        second.semantic.what = "公告服务器订单合同期限为三年"
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
            [item.semantic.what for item in resolved],
            [item.semantic.what for item in publication.evidences],
        )

    async def test_reconciliation_replays_frozen_multi_evidence_and_preserves_legacy_manifest(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        second = publication.evidences[0].model_copy(deep=True)
        second.summary = "示例公司公告合同期限为三年"
        second.semantic.what = "公告服务器订单合同期限为三年"
        publication.evidences.append(second)
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        first_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
        second_id = "EVD15bec7e3-998c-5434-aa5d-29712c4c67cf"
        sorted_ids = sorted([first_id, second_id])
        step_input = StepInput(previous_step_outputs={"validate-evidence-analysis": StepOutput(content=publication)})
        with patch(
            "capabilities.evidence.functions.extraction.post_publication",
            side_effect=[
                {"id": raw_evidence_id},
                {
                    "raw_evidence_id": raw_evidence_id,
                    "ids": sorted_ids,
                    "items": [
                        {"input_index": 0, "id": first_id},
                        {"input_index": 1, "id": second_id},
                    ],
                },
            ],
        ):
            output = await publish_evidences(step_input)

        manifest_path = Path(EvidencePublicationResult.model_validate(output.content).artifact_manifest_path)
        (manifest_path.parent / "bindings.json").unlink()
        legacy_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        legacy_manifest["schema"] = "evidence_extraction_manifest.v4"
        legacy_manifest["artifacts"] = {"prepared": "prepared.json"}
        manifest_path.write_text(json.dumps(legacy_manifest), encoding="utf-8")
        original_manifest = manifest_path.read_bytes()
        with patch(
            "capabilities.evidence.functions.reconciliation.post_publication",
            return_value={
                "raw_evidence_id": raw_evidence_id,
                "ids": sorted_ids,
                "items": [
                    {"input_index": 0, "id": first_id},
                    {"input_index": 1, "id": second_id},
                ],
            },
        ) as replayed:
            result = reconcile_evidence_bindings()

        self.assertEqual(result.remotely_bound, 1)
        self.assertEqual(result.locally_bound, 0)
        self.assertEqual(result.already_bound, 0)
        self.assertEqual(result.ineligible, [])
        replayed.assert_called_once_with(
            "evidence-publications",
            {
                "raw_evidence_id": raw_evidence_id,
                "evidences": [item.model_dump(mode="json") for item in publication.evidences],
            },
        )
        self.assertEqual(manifest_path.read_bytes(), original_manifest)
        self.assertEqual([item.id for item in read_resolved_evidences(manifest_path)], [first_id, second_id])

        with patch("capabilities.evidence.functions.reconciliation.post_publication") as repeated:
            repeated_result = reconcile_evidence_bindings()
        repeated.assert_not_called()
        self.assertEqual(repeated_result.already_bound, 1)
        self.assertEqual(repeated_result.remotely_bound, 0)

    async def test_reconciliation_maps_single_evidence_locally_without_remote_call(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
        step_input = StepInput(previous_step_outputs={"validate-evidence-analysis": StepOutput(content=publication)})
        with patch(
            "capabilities.evidence.functions.extraction.post_publication",
            side_effect=[
                {"id": raw_evidence_id},
                {"raw_evidence_id": raw_evidence_id, "ids": [evidence_id]},
            ],
        ):
            output = await publish_evidences(step_input)

        manifest_path = Path(EvidencePublicationResult.model_validate(output.content).artifact_manifest_path)
        with patch("capabilities.evidence.functions.reconciliation.post_publication") as remote:
            result = reconcile_evidence_bindings()

        remote.assert_not_called()
        self.assertEqual(result.locally_bound, 1)
        self.assertEqual(result.remotely_bound, 0)
        self.assertEqual(result.ineligible, [])
        resolved = read_resolved_evidences(manifest_path)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].id, evidence_id)
        self.assertEqual(resolved[0].semantic, publication.evidences[0].semantic)

    def test_reconciliation_reports_malformed_history_without_aborting_the_pass(self) -> None:
        manifest_path = evidence_artifact_root() / "documents" / "malformed" / "manifest.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text("[]\n", encoding="utf-8")

        with patch("capabilities.evidence.functions.reconciliation.post_publication") as remote:
            result = reconcile_evidence_bindings()

        remote.assert_not_called()
        self.assertEqual(result.already_bound, 0)
        self.assertEqual(result.locally_bound, 0)
        self.assertEqual(result.remotely_bound, 0)
        self.assertEqual(len(result.ineligible), 1)
        self.assertEqual(result.ineligible[0].artifact_manifest_path, str(manifest_path))
        self.assertEqual(result.ineligible[0].reason, "historical Evidence Artifact is invalid")

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
                second.semantic.what = "公告服务器订单合同期限为三年"
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
        second.semantic.what = "公告服务器订单合同期限为三年"
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
        changed.raw_evidence.keywords = ["变更"]
        changed.raw_evidence.category_ids = ["EVCec95a292-d513-5aa6-a54c-a9e3926add1a"]
        changed.evidences[0].semantic.what = "不应在重试中发布的变更事实"
        retry_input = StepInput(previous_step_outputs={"validate-evidence-analysis": StepOutput(content=changed)})
        with patch(
            "capabilities.evidence.functions.extraction.post_publication",
            side_effect=[
                {"id": raw_evidence_id},
                {"raw_evidence_id": raw_evidence_id, "ids": [evidence_id]},
            ],
        ) as retried:
            output = await publish_evidences(retry_input)

        raw_payload = retried.call_args_list[0].args[1]["raw_evidence"]
        evidence_payload = retried.call_args_list[1].args[1]["evidences"][0]
        self.assertEqual(raw_payload["keywords"], publication.raw_evidence.keywords)
        self.assertEqual(raw_payload["category_ids"], [self.CATEGORY_ID])
        self.assertEqual(evidence_payload["semantic"]["what"], publication.evidences[0].semantic.what)
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
                    {"raw_evidence_id": raw_evidence_id, "ids": [evidence_id]},
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
        second.semantic.what = "后续 Agent 输出的额外事实"
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
                    {"raw_evidence_id": raw_evidence_id, "ids": [evidence_id]},
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
        self.assertEqual(
            publication_call.call_args_list[1].args[1]["evidences"],
            [
                {
                    "summary": "示例公司签署10亿元三年期服务器订单",
                    "semantic": {
                        "who": "示例公司",
                        "what": "签署10亿元服务器订单",
                        "when": None,
                        "where": None,
                        "why": None,
                        "how": "合同期限为三年",
                    },
                }
            ],
        )
        self.assertGreater(read_checkpoint().manifest_offset, 0)
        manifests = list((evidence_artifact_root() / "documents").glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        frozen = json.loads((manifests[0].parent / "prepared.json").read_text(encoding="utf-8"))
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(frozen["schema_version"], "prepared_evidence_publication.v4")
        self.assertEqual(manifest["schema"], "evidence_extraction_manifest.v4")
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
        steps = cast(list[Step], loop.steps)
        self.assertEqual(
            [step.name for step in steps],
            [
                "prepare-raw-document",
                "prepare-evidence-analysis",
                "analyze-raw-evidence",
                "validate-evidence-analysis",
                "publish-evidences",
            ],
        )
        self.assertEqual(steps[2].agent.id, "evidence-extractor")  # type: ignore[union-attr]
        self.assertEqual(agent.tools, [])
        self.assertIn("exactly one category", str(agent.additional_context))
        self.assertEqual(
            [getattr(step.executor, "__name__", None) for step in (steps[0], steps[1], steps[3], steps[4])],
            [
                "prepare_raw_document",
                "prepare_evidence_analysis",
                "validate_evidence_analysis",
                "publish_evidences",
            ],
        )
        self.assertTrue(inspect.iscoroutinefunction(steps[1].executor))
        self.assertTrue(inspect.iscoroutinefunction(steps[4].executor))

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
        analyze = cast(list[Step], loop.steps)[2]
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
        analyze = cast(list[Step], loop.steps)[2]
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
