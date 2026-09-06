"""Per-article routing, durable recovery and a real Agno orchestration regression."""

import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.models.response import ModelResponse
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.workflow import Step, StepInput, StepOutput

from agents.title_curator import build_title_curator_agent
from app.workflow_runtime import install_raw_collection_session_compatibility
from capabilities.collection.functions import review
from capabilities.collection.internal import article_queue as queue
from capabilities.collection.internal.buffer import write_title_curation, write_tool_batch
from capabilities.collection.internal.models import Candidate, TitleCurationDecision, TitleCurationDraft
from capabilities.evidence import ArticleReviewDraft, ArticleReviewRequest
from capabilities.evidence.functions import publish_evidence, transfer_legacy_raw_documents
from capabilities.evidence.internal.storage import checkpoint_path
from tests import test_evidence_extraction as fixtures
from workflows.raw_collection import _seed_workflow


class ArticleReviewTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {
                "COLLECTOR_ARTIFACT_ROOT": str(root / "collector"),
                "EVIDENCE_ARTIFACT_ROOT": str(root / "evidence"),
                "EVENT_ARTIFACT_ROOT": str(root / "event"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        catalog = patch(
            "capabilities.evidence.functions.extraction.get_evidence_categories",
            return_value=fixtures.EvidenceExtractionTest._catalog_result(),
        )
        catalog.start()
        self.addCleanup(catalog.stop)
        self.context = RunContext(run_id="review-run", session_id="review-session", dependencies={})

    @staticmethod
    def candidate(
        url: str = "https://example.test/article", content: str = "示例公司公告签署10亿元服务器订单，合同期限为三年。"
    ) -> Candidate:
        return Candidate(
            candidate_id=url,
            connector="fixture",
            query="订单",
            title="示例公司签署服务器订单",
            url=url,
            content=content,
            source_name="财联社",
            collected_at=datetime(2026, 9, 5, tzinfo=UTC),
        )

    async def prepare(self, *, relevant: bool = True, empty: bool = False) -> str:
        key, _ = queue.enqueue_candidate(self.candidate(), "collection")
        output = await review.prepare_next_article(StepInput(), self.context)
        self.assertIsInstance(output.content, ArticleReviewRequest)
        draft = fixtures.EvidenceExtractionTest._draft() if relevant else None
        if empty and draft:
            draft.evidences = []
        review.save_article_review(
            StepInput(
                previous_step_content=ArticleReviewDraft(
                    article_key=key,
                    is_relevant=relevant,
                    extraction=draft,
                )
            ),
            self.context,
        )
        return key

    def test_identity_uses_url_and_body_not_title(self) -> None:
        key, created = queue.enqueue_candidate(self.candidate(), "first")
        self.assertTrue(created)
        duplicate, created = queue.enqueue_candidate(
            self.candidate("https://example.test/article?utm_source=x"), "second"
        )
        self.assertEqual(duplicate, key)
        self.assertFalse(created)
        changed, created = queue.enqueue_candidate(self.candidate(content="新订单变成20亿元"), "third")
        self.assertTrue(created)
        self.assertNotEqual(changed, key)
        other, created = queue.enqueue_candidate(self.candidate("https://other.test/article"), "fourth")
        self.assertTrue(created)
        self.assertNotEqual(other, key)

    def test_claim_fences_expired_workers_and_restores_lost_pending_marker(self) -> None:
        key, _ = queue.enqueue_candidate(self.candidate(), "first")
        (queue.queue_root() / "pending" / f"{key}.json").unlink()
        queue.enqueue_candidate(self.candidate(), "again")
        first = queue.claim_next("one")
        assert first is not None
        self.assertIsNone(queue.claim_next("two"))
        with patch.object(queue.time, "time", return_value=10**12):
            second = queue.claim_next("two")
            assert second is not None
            with self.assertRaisesRegex(ValueError, "superseded"):
                queue.save_claimed(first, "review.json", {})
        queue.fail_claim(first, "stale")
        self.assertEqual(queue.read_state(key)["token"], second["token"])

    async def test_irrelevant_is_terminal_and_never_published(self) -> None:
        key = await self.prepare(relevant=False)
        with patch.object(review, "publish_evidence") as publish, patch.object(review, "upload_article") as upload:
            result = review.validate_article_review(StepInput(), self.context)
        self.assertFalse(review.article_has_evidence(StepInput(previous_step_content=result.content)))
        publish.assert_not_called()
        upload.assert_not_called()
        self.assertEqual(queue.read_state(key)["result"]["reason"], "IRRELEVANT")
        self.assertTrue((queue.item_root(key) / "original.md").exists())
        queue.enqueue_candidate(self.candidate(), "again")
        self.assertIsNone(queue.claim_next("again"))

    async def test_no_valid_evidence_is_excluded(self) -> None:
        key = await self.prepare(empty=True)
        review.validate_article_review(StepInput(), self.context)
        self.assertEqual(queue.read_state(key)["result"]["reason"], "NO_VALID_EVIDENCE")
        self.assertFalse((queue.item_root(key) / "publication.json").exists())

    async def test_protocol_errors_retry_instead_of_exclusion(self) -> None:
        key, _ = queue.enqueue_candidate(self.candidate(), "collection")
        await review.prepare_next_article(StepInput(), self.context)
        with self.assertRaises(ValueError):
            review.save_article_review(StepInput(previous_step_content='{"article_key":'), self.context)
        self.assertEqual(queue.read_state(key)["status"], "pending")
        output = await review.prepare_next_article(StepInput(), self.context)
        self.assertTrue(review.article_needs_review(StepInput(previous_step_content=output.content)))
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            review.save_article_review(
                StepInput(
                    previous_step_content=ArticleReviewDraft(
                        article_key="wrong",
                        is_relevant=False,
                        extraction=None,
                    )
                ),
                self.context,
            )

    async def test_bad_category_archives_review_and_allows_fresh_attempt(self) -> None:
        key, _ = queue.enqueue_candidate(self.candidate(), "collection")
        await review.prepare_next_article(StepInput(), self.context)
        draft = fixtures.EvidenceExtractionTest._draft()
        draft.raw_evidence.category_code = "UNKNOWN"
        review.save_article_review(
            StepInput(
                previous_step_content=ArticleReviewDraft(
                    article_key=key,
                    is_relevant=True,
                    extraction=draft,
                )
            ),
            self.context,
        )
        with self.assertRaisesRegex(ValueError, "UNKNOWN_CATEGORY"):
            review.validate_article_review(StepInput(), self.context)
        self.assertEqual(queue.read_state(key)["status"], "pending")
        self.assertEqual(len(list((queue.item_root(key) / "rejected-reviews").glob("*.json"))), 1)
        output = await review.prepare_next_article(StepInput(), self.context)
        self.assertIsInstance(output.content, ArticleReviewRequest)

    async def test_publish_failure_resumes_frozen_payload_and_event_failure_replays_without_api(self) -> None:
        key = await self.prepare()
        review.validate_article_review(StepInput(), self.context)
        frozen = (queue.item_root(key) / "publication.json").read_bytes()
        raw_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
        replies = [
            {"id": raw_id},
            {"raw_evidence_id": raw_id, "ids": [evidence_id], "items": [{"input_index": 0, "id": evidence_id}]},
        ]
        with (
            patch.object(review, "upload_article"),
            patch(
                "capabilities.evidence.functions.extraction.post_publication",
                side_effect=[replies[0], RuntimeError("unavailable")],
            ),
        ):
            with self.assertRaises(RuntimeError):
                await review.publish_reviewed_article(StepInput(), self.context)
        prepared = await review.prepare_next_article(StepInput(), self.context)
        self.assertFalse(review.article_needs_review(StepInput(previous_step_content=prepared.content)))
        self.assertEqual((queue.item_root(key) / "publication.json").read_bytes(), frozen)
        with (
            patch.object(review, "upload_article"),
            patch("capabilities.evidence.functions.extraction.post_publication", side_effect=replies),
            patch(
                "capabilities.evidence.functions.extraction._enqueue_for_event",
                side_effect=RuntimeError("queue unavailable"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await review.publish_reviewed_article(StepInput(), self.context)
        await review.prepare_next_article(StepInput(), self.context)
        with (
            patch.object(review, "upload_article"),
            patch("capabilities.evidence.functions.extraction.post_publication") as post,
        ):
            await review.publish_reviewed_article(StepInput(), self.context)
        post.assert_not_called()
        self.assertEqual(queue.read_state(key)["status"], "completed")
        self.assertFalse(checkpoint_path().exists())

    async def test_agno_workflow_publishes_one_article_before_reading_next(self) -> None:
        first, _ = queue.enqueue_candidate(self.candidate(), "first")
        second, _ = queue.enqueue_candidate(self.candidate("https://example.test/second"), "second")
        agent = build_title_curator_agent()
        agent.db = None
        workflow = _seed_workflow(agent)
        workflow.db = None
        order = []

        async def analyze(**kwargs):
            request = ArticleReviewRequest.model_validate(kwargs["input"])
            order.append(("review", request.article_key))
            return RunOutput(
                agent_id="title-curator",
                content=ArticleReviewDraft(
                    article_key=request.article_key,
                    is_relevant=request.article_key == first,
                    extraction=fixtures.EvidenceExtractionTest._draft() if request.article_key == first else None,
                ),
            )

        raw_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"

        def post(endpoint, payload):
            order.append((endpoint, first))
            if endpoint == "raw-evidence-publications":
                return {"id": raw_id}
            return {"raw_evidence_id": raw_id, "ids": [evidence_id], "items": [{"input_index": 0, "id": evidence_id}]}

        with (
            patch.object(agent, "arun", new=AsyncMock(side_effect=analyze)),
            patch.object(review, "collect_raw_evidence", new=AsyncMock(return_value=StepOutput(content={}))),
            patch.object(review, "upload_article"),
            patch("capabilities.evidence.functions.extraction.post_publication", side_effect=post),
        ):
            result = await workflow.arun(input="采集并逐篇处理", run_id="whole", session_id="whole")
        self.assertEqual(result.status, RunStatus.completed)
        self.assertEqual(
            order,
            [
                ("review", first),
                ("raw-evidence-publications", first),
                ("evidence-publications", first),
                ("review", second),
            ],
        )
        self.assertEqual(queue.queue_counts(), {"pending": 0, "completed": 1, "excluded": 1})

    def test_cutover_imports_staged_batches_and_preserves_irrelevance(self) -> None:
        candidate = self.candidate()
        write_tool_batch(collection_id="old", connector="fixture", query="订单", candidates=[candidate])
        write_title_curation(
            "old",
            TitleCurationDraft(
                decisions=[TitleCurationDecision(candidate_id=candidate.candidate_id, is_relevant=False)]
            ),
        )
        first = review.import_legacy_articles()
        second = review.import_legacy_articles()
        self.assertEqual(first["transferred_staged_articles"], 1)
        self.assertEqual(second["transferred_staged_articles"], 0)
        self.assertEqual(queue.queue_counts()["excluded"], 1)

    def test_legacy_cursor_does_not_advance_before_durable_enqueue(self) -> None:
        helper = fixtures.EvidenceExtractionTest()
        helper._publish_raw_fixture()
        with self.assertRaisesRegex(RuntimeError, "disk unavailable"):
            transfer_legacy_raw_documents(lambda prepared: (_ for _ in ()).throw(RuntimeError("disk unavailable")))
        self.assertFalse(checkpoint_path().exists())
        self.assertEqual(transfer_legacy_raw_documents(queue.enqueue_legacy_document), 1)
        self.assertEqual(transfer_legacy_raw_documents(queue.enqueue_legacy_document), 0)
        self.assertEqual(queue.queue_counts()["pending"], 1)

    async def test_reacquired_published_article_retains_original_object_and_skips_model(self) -> None:
        helper = fixtures.EvidenceExtractionTest()
        helper._publish_raw_fixture()
        prepared = helper._prepared()
        publication = helper._validated(prepared)
        raw_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence_id = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
        with patch(
            "capabilities.evidence.functions.extraction.post_publication",
            side_effect=[
                {"id": raw_id},
                {"raw_evidence_id": raw_id, "ids": [evidence_id], "items": [{"input_index": 0, "id": evidence_id}]},
            ],
        ):
            await publish_evidence(StepInput(previous_step_content=publication))
        key, _ = queue.enqueue_candidate(self.candidate(prepared.source_url), "reacquired")
        self.assertNotEqual(queue.read_prepared(key).document_sha256, prepared.document_sha256)
        result = await review.prepare_next_article(StepInput(), self.context)
        self.assertFalse(review.article_needs_review(StepInput(previous_step_content=result.content)))
        with (
            patch.object(queue, "configured_raw_document_store") as store,
            patch("capabilities.evidence.functions.extraction.post_publication") as post,
        ):
            await review.publish_reviewed_article(StepInput(), self.context)
        post.assert_not_called()
        uploaded = store.return_value.publish_markdown.call_args.kwargs
        self.assertEqual(uploaded["object_key"], prepared.document_path)
        self.assertEqual(uploaded["sha256"], prepared.document_sha256)
        self.assertEqual(queue.read_state(key)["status"], "completed")

    async def test_workflow_owns_session_even_when_pinned_agent_restores_db(self) -> None:
        key, _ = queue.enqueue_candidate(self.candidate(), "session-test")
        db = SqliteDb(db_file=str(queue.queue_root() / "session-test.db"))
        agent = build_title_curator_agent()
        agent.db = db  # Simulate Agno's pinned component rehydration.
        install_raw_collection_session_compatibility()
        self.assertIsNone(agent.workflow_id)  # Standalone REST calls keep persistence.
        workflow = _seed_workflow(agent)
        workflow.db = db
        assert isinstance(workflow.steps, list)
        workflow.steps[0] = Step(name="existing-queue", executor=lambda step_input: StepOutput(content={}))
        response = ArticleReviewDraft(article_key=key, is_relevant=False, extraction=None)
        with patch.object(
            agent.model,
            "aresponse",
            new=AsyncMock(
                return_value=ModelResponse(
                    content=response.model_dump_json(),
                )
            ),
        ):
            result = await workflow.arun(input="session check", run_id="owner-test", session_id="owner-test")
        self.assertEqual(result.status, RunStatus.completed)
        self.assertIs(agent.db, db)
        self.assertEqual(agent.workflow_id, "raw-collection")
        session = db.get_session("owner-test", session_type=SessionType.WORKFLOW)
        assert session is not None and not isinstance(session, dict)
        self.assertEqual(session.workflow_id, "raw-collection")

    async def test_publication_condition_fails_workflow_instead_of_silently_continuing(self) -> None:
        key, _ = queue.enqueue_candidate(self.candidate(), "failure-test")
        agent = build_title_curator_agent()
        agent.db = None
        workflow = _seed_workflow(agent)
        workflow.db = None
        assert isinstance(workflow.steps, list)
        workflow.steps[0] = Step(name="existing-queue", executor=lambda step_input: StepOutput(content={}))
        draft = ArticleReviewDraft(
            article_key=key, is_relevant=True, extraction=fixtures.EvidenceExtractionTest._draft()
        )
        with (
            patch.object(agent, "arun", new=AsyncMock(return_value=RunOutput(content=draft))) as analyze,
            patch.object(review, "upload_article"),
            patch("capabilities.evidence.functions.extraction.post_publication", side_effect=RuntimeError("offline")),
            self.assertRaisesRegex(RuntimeError, "offline"),
        ):
            await workflow.arun(input="fail closed", run_id="failure-test", session_id="failure-test")
        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(queue.read_state(key)["status"], "pending")
        self.assertTrue((queue.item_root(key) / "publication.json").exists())


if __name__ == "__main__":
    unittest.main()
