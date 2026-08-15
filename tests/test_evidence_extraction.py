"""Tests for incremental Evidence extraction and publication."""

import hashlib
import inspect
import json
import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import patch

from agno.registry import Registry
from agno.workflow import Loop, Step, StepInput, StepOutput, Workflow
from pydantic import ValidationError

from agents.evidence_extractor import build_evidence_extractor_agent
from capabilities.collection.internal.artifacts import build_artifact_set, publish_artifact_set
from capabilities.collection.internal.buffer import write_title_curation, write_tool_batch
from capabilities.collection.internal.models import (
    Candidate,
    CollectionRequest,
    SourceLevel,
    TitleCurationDecision,
    TitleCurationDraft,
    TitleRelevance,
)
from capabilities.evidence.functions import (
    prepare_raw_document,
    publish_evidences,
    validate_evidence_draft,
)
from capabilities.evidence.internal.models import (
    AtomicEvidenceDraft,
    EvidenceExtractionDraft,
    EvidenceExtractionIdle,
    EvidencePublicationResult,
    PreparedEvidencePublication,
    PreparedRawDocument,
    RawEvidenceEnrichment,
)
from capabilities.evidence.internal.storage import checkpoint_path, evidence_artifact_root, read_checkpoint
from workflows.evidence_extraction import EVIDENCE_EXTRACTION_CONTRACT_VERSION, _seed_workflow


class AcceptingRawDocumentStore:
    def publish_markdown(self, *, bucket: str, object_key: str, content: bytes, sha256: str) -> None:
        del bucket, object_key, content, sha256


class EvidenceExtractionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {
                "COLLECTOR_ARTIFACT_ROOT": str(root / "collector"),
                "EVIDENCE_ARTIFACT_ROOT": str(root / "evidence"),
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

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
            requested_after=now - timedelta(hours=1),
            requested_before=now + timedelta(hours=1),
            agent_component_id="raw-collector",
            agent_config_version=1,
            instructions_sha256="a" * 64,
            candidates=[candidate],
        )
        write_title_curation(
            "collection-evidence",
            TitleCurationDraft(
                decisions=[
                    TitleCurationDecision(
                        candidate_id=candidate.candidate_id,
                        relevance=TitleRelevance.RELEVANT,
                        reason_code="company_operation",
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
                keywords=["服务器", "订单"],
                is_original=False,
                quoted_source_name="示例公司公告",
            ),
            evidences=[
                AtomicEvidenceDraft(
                    layer_type="DOUBLE",
                    source_who="财联社",
                    source_what="财联社报道示例公司签署服务器订单",
                    source_when=None,
                    source_when_raw=None,
                    source_where=None,
                    source_why=None,
                    source_how=None,
                    source_who_core="示例公司",
                    source_what_core="示例公司签署10亿元服务器订单",
                    source_when_core=None,
                    source_when_raw_core=None,
                    source_where_core=None,
                    source_why_core=None,
                    source_how_core="合同金额10亿元，期限三年",
                    expression_fingerprint="示例公司签署10亿元三年期服务器订单",
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
                "extract-evidences": StepOutput(content=self._draft()),
            }
        )
        output = validate_evidence_draft(step_input)
        return PreparedEvidencePublication.model_validate(output.content)

    def test_prepare_reads_manifest_index_and_strips_artifact_wrapper(self) -> None:
        self._publish_raw_fixture()
        prepared = self._prepared()
        self.assertEqual(prepared.collection_id, "collection-evidence")
        self.assertEqual(prepared.source_level, "L2_WIRE")
        self.assertEqual(prepared.raw_text, "示例公司公告签署10亿元服务器订单，合同期限为三年。")
        self.assertFalse(checkpoint_path().exists())

    def test_semantic_contract_rejects_invalid_keywords_and_layers(self) -> None:
        with self.assertRaises(ValidationError):
            RawEvidenceEnrichment(keywords=["超过五个字符"], is_original=True)
        with self.assertRaises(ValidationError):
            AtomicEvidenceDraft(
                layer_type="SINGLE",
                source_what="示例事实",
                source_what_core="不允许的核心层",
                expression_fingerprint="示例事实",
            )

    def test_validation_adds_stable_publication_key_order_and_expression_key(self) -> None:
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
        self.assertEqual(publication.evidences[0].split_order, 0)
        self.assertNotIn("evidence_id", publication.evidences[0].model_dump(mode="json"))
        self.assertEqual(len(publication.evidences[0].expression_key), 64)
        self.assertEqual(publication.evidences[0].fingerprint_version, "evidence-expression.v1")

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

    def test_validation_preserves_fuzzy_fact_time_as_raw_expression(self) -> None:
        self._publish_raw_fixture()
        prepared = self._prepared()
        draft = self._draft().model_dump(mode="json")
        draft["evidences"][0]["source_when"] = "十五五期间"
        step_input = StepInput(
            previous_step_outputs={
                "prepare-raw-document": StepOutput(content=prepared),
                "extract-evidences": StepOutput(content=json.dumps(draft, ensure_ascii=False)),
            }
        )

        publication = PreparedEvidencePublication.model_validate(validate_evidence_draft(step_input).content)

        self.assertIsNone(publication.evidences[0].source_when)
        self.assertEqual(publication.evidences[0].source_when_raw, "十五五期间")

    def test_validation_discards_invalid_keywords_when_valid_ones_remain(self) -> None:
        enrichment = RawEvidenceEnrichment.model_validate(
            {
                "keywords": ["伊朗", "天然气", "9500万立方米", "伊朗"],
                "is_original": True,
                "quoted_source_name": None,
            }
        )

        self.assertEqual(enrichment.keywords, ["伊朗", "天然气"])

    async def test_publication_writes_manifest_last_and_advances_checkpoint(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        step_input = StepInput(previous_step_outputs={"validate-evidence-draft": StepOutput(content=publication)})
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
        responses = [
            {"raw_evidence_id": raw_evidence_id},
            {"raw_evidence_id": raw_evidence_id, "evidence_ids": [evidence_id]},
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
        self.assertEqual(
            raw_payload["raw_evidence"]["raw_text"],
            "/raw-evidence/documents/2026/08/11/c6fe9177b96308182802eb456d47768b06d890fa96b9e08f159a0f6fd2470128.md",
        )
        self.assertNotIn("10亿元服务器订单", raw_payload["raw_evidence"]["raw_text"])
        evidence_endpoint, evidence_payload = mocked.call_args_list[1].args
        self.assertEqual(evidence_endpoint, "evidence-publications")
        self.assertEqual(evidence_payload["raw_evidence_id"], raw_evidence_id)
        self.assertNotIn("evidence_id", evidence_payload["evidences"][0])
        self.assertTrue(Path(result.artifact_manifest_path).is_file())
        manifest = json.loads(Path(result.artifact_manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "evidence_extraction_manifest.v2")
        self.assertEqual(manifest["publication_key"], publication.raw_evidence.publication_key)
        self.assertEqual(manifest["raw_evidence_id"], raw_evidence_id)
        self.assertEqual(manifest["evidences"], [{"split_order": 0, "evidence_id": evidence_id}])
        self.assertEqual(manifest["artifacts"], {"prepared": "prepared.json"})
        self.assertEqual(
            {path.name for path in Path(result.artifact_manifest_path).parent.iterdir()},
            {"manifest.json", "prepared.json"},
        )
        self.assertEqual(result.raw_evidence_id, raw_evidence_id)
        self.assertEqual(result.evidence_ids, [evidence_id])
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

    async def test_failed_evidence_publication_does_not_advance_checkpoint(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        step_input = StepInput(previous_step_outputs={"validate-evidence-draft": StepOutput(content=publication)})
        with (
            patch(
                "capabilities.evidence.functions.extraction.post_publication",
                side_effect=[
                    {"raw_evidence_id": "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"},
                    ValueError("evidence rejected"),
                ],
            ),
            self.assertRaisesRegex(ValueError, "evidence rejected"),
        ):
            await publish_evidences(step_input)
        self.assertEqual(read_checkpoint().manifest_offset, 0)
        self.assertEqual(list((evidence_artifact_root() / "documents").glob("*/manifest.json")), [])

    async def test_retry_reuses_frozen_payload_after_partial_publication(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        step_input = StepInput(previous_step_outputs={"validate-evidence-draft": StepOutput(content=publication)})
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
        with (
            patch(
                "capabilities.evidence.functions.extraction.post_publication",
                side_effect=[{"raw_evidence_id": raw_evidence_id}, ValueError("evidence rejected")],
            ),
            self.assertRaisesRegex(ValueError, "evidence rejected"),
        ):
            await publish_evidences(step_input)

        changed = publication.model_copy(deep=True)
        changed.raw_evidence.keywords = ["变更"]
        changed.evidences[0].source_what = "不应在重试中发布的变更事实"
        retry_input = StepInput(previous_step_outputs={"validate-evidence-draft": StepOutput(content=changed)})
        with patch(
            "capabilities.evidence.functions.extraction.post_publication",
            side_effect=[
                {"raw_evidence_id": raw_evidence_id},
                {"raw_evidence_id": raw_evidence_id, "evidence_ids": [evidence_id]},
            ],
        ) as retried:
            output = await publish_evidences(retry_input)

        raw_payload = retried.call_args_list[0].args[1]["raw_evidence"]
        evidence_payload = retried.call_args_list[1].args[1]["evidences"][0]
        self.assertEqual(raw_payload["keywords"], publication.raw_evidence.keywords)
        self.assertEqual(evidence_payload["source_what"], publication.evidences[0].source_what)
        self.assertEqual(EvidencePublicationResult.model_validate(output.content).evidence_ids, [evidence_id])

    async def test_final_manifest_recovers_checkpoint_despite_new_agent_draft(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        step_input = StepInput(previous_step_outputs={"validate-evidence-draft": StepOutput(content=publication)})
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
        with (
            patch(
                "capabilities.evidence.functions.extraction.post_publication",
                side_effect=[
                    {"raw_evidence_id": raw_evidence_id},
                    {"raw_evidence_id": raw_evidence_id, "evidence_ids": [evidence_id]},
                ],
            ),
            patch(
                "capabilities.evidence.functions.extraction.advance_checkpoint",
                side_effect=RuntimeError("checkpoint interrupted"),
            ),
            self.assertRaisesRegex(RuntimeError, "checkpoint interrupted"),
        ):
            await publish_evidences(step_input)

        changed = publication.model_copy(deep=True)
        second = changed.evidences[0].model_copy(deep=True)
        second.split_order = 1
        second.source_what = "后续 Agent 输出的额外事实"
        changed.evidences.append(second)
        retry_input = StepInput(previous_step_outputs={"validate-evidence-draft": StepOutput(content=changed)})
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
                    {"raw_evidence_id": raw_evidence_id},
                    {
                        "raw_evidence_id": "RAWec95a292-d513-5aa6-a54c-a9e3926add1a",
                        "evidence_ids": [evidence_id],
                    },
                ],
                "Raw Evidence identity mismatch",
            ),
            (
                [
                    {"raw_evidence_id": raw_evidence_id},
                    {"raw_evidence_id": raw_evidence_id, "evidence_ids": [evidence_id, evidence_id]},
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
                    previous_step_outputs={"validate-evidence-draft": StepOutput(content=publication)}
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

    def test_workflow_round_trips_agent_and_three_functions(self) -> None:
        agent = build_evidence_extractor_agent()
        registry = Registry(
            name="Evidence Test Registry",
            agents=[agent],
            functions=[prepare_raw_document, validate_evidence_draft, publish_evidences],
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
            ["prepare-raw-document", "extract-evidences", "validate-evidence-draft", "publish-evidences"],
        )
        self.assertEqual(steps[1].agent.id, "evidence-extractor")  # type: ignore[union-attr]
        self.assertEqual(
            [getattr(step.executor, "__name__", None) for step in (steps[0], steps[2], steps[3])],
            ["prepare_raw_document", "validate_evidence_draft", "publish_evidences"],
        )
        self.assertTrue(inspect.iscoroutinefunction(steps[3].executor))


if __name__ == "__main__":
    unittest.main()
