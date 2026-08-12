"""Tests for incremental Evidence extraction and publication."""

import inspect
import json
import os
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
from capabilities.evidence_extraction.functions import (
    prepare_raw_document,
    publish_evidences,
    validate_evidence_draft,
)
from capabilities.evidence_extraction.models import (
    AtomicEvidenceDraft,
    EvidenceExtractionDraft,
    EvidenceExtractionIdle,
    EvidencePublicationResult,
    PreparedEvidencePublication,
    PreparedRawDocument,
    RawEvidenceEnrichment,
)
from capabilities.evidence_extraction.storage import checkpoint_path, evidence_artifact_root, read_checkpoint
from capabilities.raw_collection.artifacts import build_artifact_set, publish_artifact_set
from capabilities.raw_collection.buffer import write_tool_batch
from capabilities.raw_collection.models import Candidate, CollectionRequest
from workflows.evidence_extraction import _seed_workflow


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
        prepared = build_artifact_set(
            "collection-evidence",
            CollectionRequest(objective="采集最近2小时服务器订单"),
            completed_at=now + timedelta(minutes=2),
        )
        publish_artifact_set(prepared)

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

    def test_validation_adds_stable_id_order_and_expression_key(self) -> None:
        self._publish_raw_fixture()
        publication = self._validated(self._prepared())
        self.assertEqual(len(publication.raw_evidence.raw_evidence_id), 32)
        self.assertEqual(publication.evidences[0].split_order, 0)
        self.assertEqual(len(publication.evidences[0].evidence_id), 32)
        self.assertEqual(len(publication.evidences[0].expression_key), 64)
        self.assertEqual(publication.evidences[0].fingerprint_version, "evidence-expression.v1")

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
        responses = [None, None]
        with patch(
            "capabilities.evidence_extraction.functions.extraction.post_publication",
            side_effect=responses,
        ) as mocked:
            output = await publish_evidences(step_input)
        result = EvidencePublicationResult.model_validate(output.content)
        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(Path(result.artifact_manifest_path).is_file())
        manifest = json.loads(Path(result.artifact_manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["artifacts"], {"prepared": "prepared.json"})
        self.assertEqual(
            {path.name for path in Path(result.artifact_manifest_path).parent.iterdir()},
            {"manifest.json", "prepared.json"},
        )
        self.assertFalse((evidence_artifact_root() / ".pending" / result.raw_evidence_id).exists())
        self.assertGreater(result.checkpoint.manifest_offset, 0)
        self.assertEqual(result.checkpoint, read_checkpoint())

        idle = prepare_raw_document(StepInput(input="再运行"))
        self.assertTrue(idle.stop)
        self.assertIsInstance(idle.content, EvidenceExtractionIdle)

        with patch("capabilities.evidence_extraction.functions.extraction.post_publication") as repeated:
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
                "capabilities.evidence_extraction.functions.extraction.post_publication",
                side_effect=[None, ValueError("evidence rejected")],
            ),
            self.assertRaisesRegex(ValueError, "evidence rejected"),
        ):
            await publish_evidences(step_input)
        self.assertEqual(read_checkpoint().manifest_offset, 0)
        final_manifest = (
            evidence_artifact_root() / "documents" / publication.raw_evidence.raw_evidence_id / "manifest.json"
        )
        self.assertFalse(final_manifest.exists())

    def test_workflow_round_trips_agent_and_three_functions(self) -> None:
        agent = build_evidence_extractor_agent()
        registry = Registry(
            name="Evidence Test Registry",
            agents=[agent],
            functions=[prepare_raw_document, validate_evidence_draft, publish_evidences],
        )
        workflow = _seed_workflow(agent)
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
