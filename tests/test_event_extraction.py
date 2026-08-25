"""Behavior tests for local Evidence to Reason Event Candidate handoff."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

from agno.registry import Registry
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.workflow import Condition, Step, StepInput, StepOutput, Workflow

from agents.event_extractor import (
    EVENT_EXTRACTOR_CONTRACT_VERSION,
    build_event_extractor_agent,
    ensure_event_extractor_agent,
)
from app.registry import TidewiseRegistry
from capabilities.event import EventExtractionBatch, EventExtractionDraft, EventExtractionResult
from capabilities.event.functions import (
    event_batch_requires_analysis,
    freeze_event_analysis,
    prepare_event_batch,
    submit_event_candidates,
)
from capabilities.evidence import ResolvedEvidence
from workflows.event_extraction import (
    EVENT_EXTRACTION_CONTRACT_VERSION,
    _seed_workflow,
    ensure_event_extraction_workflow,
)


class EventExtractionTest(unittest.IsolatedAsyncioTestCase):
    FIRST_ID = "EVD15bec7e3-998c-5434-aa5d-29712c4c67cf"
    SECOND_ID = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
    RAW_ID = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.evidence_root = root / "evidence"
        self.event_root = root / "event"
        manifest = self.evidence_root / "documents" / "published" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}\n", encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {
                "EVIDENCE_ARTIFACT_ROOT": str(self.evidence_root),
                "EVENT_ARTIFACT_ROOT": str(self.event_root),
                "EVENT_EXTRACTION_BATCH_SIZE": "50",
                "REASON_SERVICE_BASE_URL": "http://reason.test:8890",
                "REASON_SERVICE_TOKEN": "secret-token",
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    @classmethod
    def _evidences(cls) -> list[ResolvedEvidence]:
        return [
            ResolvedEvidence(
                id=cls.FIRST_ID,
                raw_evidence_id=cls.RAW_ID,
                summary="示例公司宣布签署服务器订单",
                semantic={
                    "who": "示例公司",
                    "what": "签署服务器订单",
                    "when": "2026-08-25",
                    "where": "中国",
                    "why": None,
                    "how": "公告宣布",
                },
            ),
            ResolvedEvidence(
                id=cls.SECOND_ID,
                raw_evidence_id=cls.RAW_ID,
                summary="示例公司的同一服务器订单公告",
                semantic={
                    "who": "示例公司",
                    "what": "签署服务器订单",
                    "when": "2026-08-25",
                    "where": "中国",
                    "why": None,
                    "how": "媒体转述",
                },
            ),
        ]

    @classmethod
    def _draft(cls, *, separate: bool = False) -> EventExtractionDraft:
        base_event = {
            "title": "示例公司签署服务器订单",
            "summary": "示例公司于 2026 年 8 月 25 日宣布签署服务器订单。",
            "semantic": {
                "actors": ["示例公司"],
                "action": "签署",
                "objects": ["服务器订单"],
                "stage": "ANNOUNCED",
                "jurisdictions": ["中国"],
                "effective_at": None,
                "time_precision": "DAY",
            },
            "modality": "FACT",
            "occurred_at": None,
            "announced_at": "2026-08-25T00:00:00Z",
        }
        evidence_sets = [[cls.FIRST_ID], [cls.SECOND_ID]] if separate else [[cls.FIRST_ID, cls.SECOND_ID]]
        return EventExtractionDraft(
            candidates=[{"event": base_event, "evidence_ids": ids} for ids in evidence_sets],
            no_event=[],
            needs_review=[],
        )

    def _prepare(self) -> EventExtractionBatch:
        with patch(
            "capabilities.event.internal.storage.read_resolved_evidences",
            return_value=self._evidences(),
        ):
            output = prepare_event_batch(StepInput(input="处理未提炼 Evidence"))
        self.assertFalse(output.stop)
        return EventExtractionBatch.model_validate(output.content)

    def _freeze(self, batch: EventExtractionBatch, draft: EventExtractionDraft) -> None:
        output = freeze_event_analysis(
            StepInput(
                previous_step_outputs={
                    "prepare-event-batch": StepOutput(content=batch),
                    "analyze-event-batch": StepOutput(content=draft),
                }
            )
        )
        self.assertEqual(EventExtractionDraft.model_validate(output.content), draft)

    def test_freezes_one_grouped_candidate_and_partitions_every_evidence(self) -> None:
        batch = self._prepare()
        self.assertTrue(batch.needs_analysis)
        self.assertTrue(
            event_batch_requires_analysis(
                StepInput(previous_step_outputs={"prepare-event-batch": StepOutput(content=batch)})
            )
        )

        draft = self._draft()
        self._freeze(batch, draft)

        frozen = json.loads((self.event_root / ".pending" / batch.batch_id / "draft.json").read_text())
        self.assertEqual(frozen, draft.model_dump(mode="json"))

    def test_rejects_agent_output_that_does_not_partition_the_frozen_batch(self) -> None:
        batch = self._prepare()
        draft = self._draft().model_copy(deep=True)
        draft.candidates[0].evidence_ids = [self.FIRST_ID]

        with self.assertRaisesRegex(ValueError, "partition"):
            self._freeze(batch, draft)

    def test_concurrent_schedule_callback_stops_without_reprocessing_pending_batch(self) -> None:
        batch = self._prepare()
        with patch(
            "capabilities.event.internal.storage.read_resolved_evidences",
            return_value=self._evidences(),
        ):
            concurrent = prepare_event_batch(StepInput(input="concurrent"))

        self.assertTrue(concurrent.stop)
        self.assertEqual(concurrent.content.status, "busy")
        self.assertEqual(concurrent.content.batch_id, batch.batch_id)

    def test_expired_processing_lease_allows_crash_recovery_without_new_batch(self) -> None:
        batch = self._prepare()
        lease_path = self.event_root / ".pending" / batch.batch_id / "lease.json"
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
        lease["expires_at"] = "2000-01-01T00:00:00Z"
        lease_path.write_text(json.dumps(lease), encoding="utf-8")

        resumed = self._prepare()
        self.assertEqual(resumed.batch_id, batch.batch_id)
        self.assertNotEqual(resumed.lease_id, batch.lease_id)
        self.assertTrue(resumed.needs_analysis)

    async def test_posts_each_candidate_and_completed_evidence_is_not_selected_again(self) -> None:
        batch = self._prepare()
        self._freeze(batch, self._draft(separate=True))
        acceptances = [
            {
                "submission_id": f"evt-submission-{index}",
                "status": "ACCEPTED",
                "status_url": f"/api/reason/v1/event-candidates/evt-submission-{index}",
                "replayed": False,
            }
            for index in (1, 2)
        ]
        with patch(
            "capabilities.event.functions.extraction.post_event_candidate",
            side_effect=acceptances,
        ) as posted:
            output = await submit_event_candidates(
                StepInput(previous_step_outputs={"prepare-event-batch": StepOutput(content=batch)})
            )

        result = EventExtractionResult.model_validate(output.content)
        self.assertEqual(result.submission_ids, ["evt-submission-1", "evt-submission-2"])
        self.assertEqual(posted.call_count, 2)
        self.assertEqual(posted.call_args_list[0].args[0]["evidence_ids"], [self.FIRST_ID])
        self.assertEqual(posted.call_args_list[1].args[0]["evidence_ids"], [self.SECOND_ID])

        with patch(
            "capabilities.event.internal.storage.read_resolved_evidences",
            return_value=self._evidences(),
        ):
            idle = prepare_event_batch(StepInput(input="again"))
        self.assertTrue(idle.stop)

    async def test_partial_failure_reuses_frozen_draft_and_posts_only_missing_candidate(self) -> None:
        batch = self._prepare()
        self._freeze(batch, self._draft(separate=True))
        first = {
            "submission_id": "evt-submission-1",
            "status": "ACCEPTED",
            "status_url": "/api/reason/v1/event-candidates/evt-submission-1",
            "replayed": False,
        }
        with patch(
            "capabilities.event.functions.extraction.post_event_candidate",
            side_effect=[first, ValueError("Reasoning Server request failed")],
        ):
            with self.assertRaisesRegex(ValueError, "Reasoning Server"):
                await submit_event_candidates(
                    StepInput(previous_step_outputs={"prepare-event-batch": StepOutput(content=batch)})
                )

        resumed = self._prepare()
        self.assertEqual(resumed.batch_id, batch.batch_id)
        self.assertFalse(resumed.needs_analysis)
        self.assertFalse(
            event_batch_requires_analysis(
                StepInput(previous_step_outputs={"prepare-event-batch": StepOutput(content=resumed)})
            )
        )
        second = {
            "submission_id": "evt-submission-2",
            "status": "ACCEPTED",
            "status_url": "/api/reason/v1/event-candidates/evt-submission-2",
            "replayed": True,
        }
        with patch(
            "capabilities.event.functions.extraction.post_event_candidate",
            return_value=second,
        ) as posted:
            output = await submit_event_candidates(
                StepInput(previous_step_outputs={"prepare-event-batch": StepOutput(content=resumed)})
            )

        posted.assert_called_once()
        self.assertEqual(posted.call_args.args[0]["evidence_ids"], [self.SECOND_ID])
        result = EventExtractionResult.model_validate(output.content)
        self.assertEqual(result.submission_ids, ["evt-submission-1", "evt-submission-2"])

    async def test_no_event_and_needs_review_are_terminal_without_posting(self) -> None:
        batch = self._prepare()
        draft = EventExtractionDraft(
            candidates=[],
            no_event=[{"evidence_id": self.FIRST_ID, "reason": "not_a_real_world_action"}],
            needs_review=[{"evidence_id": self.SECOND_ID, "reason": "compound_atomic_evidence"}],
        )
        self._freeze(batch, draft)

        with patch("capabilities.event.functions.extraction.post_event_candidate") as posted:
            output = await submit_event_candidates(
                StepInput(previous_step_outputs={"prepare-event-batch": StepOutput(content=batch)})
            )

        posted.assert_not_called()
        result = EventExtractionResult.model_validate(output.content)
        self.assertEqual(result.candidate_count, 0)
        self.assertEqual(result.no_event_count, 1)
        self.assertEqual(result.needs_review_count, 1)

    def test_agent_and_workflow_round_trip_with_conditional_analysis(self) -> None:
        agent = build_event_extractor_agent()
        agent.db = None
        self.assertEqual(agent.id, "event-extractor")
        self.assertEqual(agent.output_schema, EventExtractionDraft)
        self.assertEqual(agent.tools, [])

        registry = Registry(
            name="Event Test Registry",
            agents=[agent],
            schemas=[EventExtractionBatch, EventExtractionDraft, EventExtractionResult],
            functions=[
                prepare_event_batch,
                event_batch_requires_analysis,
                freeze_event_analysis,
                submit_event_candidates,
            ],
        )
        workflow = _seed_workflow(agent)
        workflow.db = None
        self.assertEqual(
            workflow.metadata,
            {"event_extraction_contract_version": EVENT_EXTRACTION_CONTRACT_VERSION},
        )
        restored = Workflow.from_dict(workflow.to_dict(), registry=registry)
        steps = restored.steps
        self.assertIsInstance(steps, list)
        assert isinstance(steps, list)
        self.assertEqual(len(steps), 3)
        self.assertIsInstance(steps[0], Step)
        self.assertIsInstance(steps[1], Condition)
        condition = steps[1]
        assert isinstance(condition, Condition)
        self.assertEqual([step.name for step in condition.steps], ["analyze-event-batch", "freeze-event-analysis"])
        self.assertIsInstance(steps[2], Step)

    async def test_complete_workflow_analyzes_freezes_and_hands_off(self) -> None:
        agent = build_event_extractor_agent()
        agent.db = None
        workflow = _seed_workflow(agent)
        workflow.db = None
        controlled_agent = AsyncMock(
            return_value=RunOutput(
                agent_id="event-extractor",
                content=self._draft(),
                content_type="EventExtractionDraft",
            )
        )
        acceptance = {
            "submission_id": "evt-submission-complete",
            "status": "ACCEPTED",
            "status_url": "/api/reason/v1/event-candidates/evt-submission-complete",
            "replayed": False,
        }
        with (
            patch.object(agent, "arun", new=controlled_agent),
            patch(
                "capabilities.event.internal.storage.read_resolved_evidences",
                return_value=self._evidences(),
            ),
            patch(
                "capabilities.event.functions.extraction.post_event_candidate",
                return_value=acceptance,
            ) as posted,
        ):
            response = await workflow.arun(
                input="处理所有已发布且尚未提炼 Event 的 Evidence",
                run_id="run-event-workflow",
                session_id="session-event-workflow",
            )

        self.assertEqual(response.status, RunStatus.completed)
        self.assertEqual(controlled_agent.call_count, 1)
        analyzed = EventExtractionBatch.model_validate(controlled_agent.call_args.kwargs["input"])
        self.assertEqual([item.id for item in analyzed.evidences], [self.FIRST_ID, self.SECOND_ID])
        posted.assert_called_once()

    def test_agent_contract_migration_reapplies_reviewed_runtime_contract(self) -> None:
        db = MagicMock()
        db.get_component.return_value = {"current_version": 4}
        current = MagicMock()
        current.metadata = {"event_extractor_contract_version": 0}
        current.save.return_value = 5
        with (
            patch("agents.event_extractor.get_postgres_db", return_value=db),
            patch("agents.event_extractor.Agent.load", return_value=current),
        ):
            version = ensure_event_extractor_agent(MagicMock())

        self.assertEqual(version, 5)
        self.assertEqual(current.output_schema, EventExtractionDraft)
        self.assertEqual(current.tools, [])
        self.assertEqual(
            current.metadata["event_extractor_contract_version"],
            EVENT_EXTRACTOR_CONTRACT_VERSION,
        )
        current.save.assert_called_once_with(
            db=db,
            stage="published",
            notes=f"Event Extractor runtime contract migration {EVENT_EXTRACTOR_CONTRACT_VERSION}",
        )

    def test_workflow_contract_migration_uses_sessionless_published_agent(self) -> None:
        db = MagicMock()
        db.get_component.return_value = {"current_version": 7}
        db.get_config.return_value = {
            "config": {
                "id": "event-extraction",
                "name": "Event Extraction",
                "metadata": {"event_extraction_contract_version": 0},
            }
        }
        runtime_agent = build_event_extractor_agent()
        runtime_agent.db = None
        with (
            patch("workflows.event_extraction.get_postgres_db", return_value=db),
            patch("workflows.event_extraction.load_event_extractor_agent", return_value=runtime_agent),
            patch.object(Workflow, "save", autospec=True, return_value=8) as saved,
        ):
            version = ensure_event_extraction_workflow(MagicMock())

        self.assertEqual(version, 8)
        migrated = cast(Workflow, saved.call_args.args[0])
        self.assertEqual(
            migrated.metadata,
            {"event_extraction_contract_version": EVENT_EXTRACTION_CONTRACT_VERSION},
        )
        condition = cast(Condition, cast(list[object], migrated.steps)[1])
        analyze = cast(Step, condition.steps[0])
        self.assertIsNotNone(analyze.agent)
        assert analyze.agent is not None
        self.assertIsNone(analyze.agent.db)

    def test_tidewise_registry_resolves_published_event_extractor(self) -> None:
        runtime_agent = build_event_extractor_agent()
        runtime_agent.db = None
        registry = TidewiseRegistry(name="Event Registry Test")
        with patch("app.registry.load_event_extractor_agent", return_value=runtime_agent) as loaded:
            resolved = registry.get_agent("event-extractor")

        self.assertIs(resolved, runtime_agent)
        loaded.assert_called_once_with(registry)


if __name__ == "__main__":
    unittest.main()
