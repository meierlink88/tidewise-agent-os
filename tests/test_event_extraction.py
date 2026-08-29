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
from capabilities.event import (
    EventExtractionBatch,
    EventExtractionDraft,
    EventExtractionResult,
    EventPublicationRecord,
    EventSignalRecord,
    configure_event_workflow_runtime,
)
from capabilities.event.functions import (
    construct_event_signals,
    event_batch_requires_analysis,
    freeze_event_analysis,
    prepare_event_batch,
    publish_event_candidates,
)
from capabilities.event.internal.local_runtime import LocalEventWorkflowRuntime
from capabilities.event.internal.queue import enqueue_evidence_artifact, queue_counts
from capabilities.evidence import ResolvedEvidence
from sematica.ingestion.episcode.event.contracts import AtomicityAssessment, HistoricalEvent
from sematica.ingestion.episcode.event.resolver import EventResolver
from workflows.event_extraction import (
    EVENT_EXTRACTION_CONTRACT_VERSION,
    _seed_workflow,
    ensure_event_extraction_workflow,
)


class GraphWriteTracer:
    """Stateful Episode writer seam exposing externally observable graph counts."""

    def __init__(self, *, counts: dict[str, int] | None = None, fail_after_first_write: bool = False) -> None:
        self.counts = (
            counts
            if counts is not None
            else {"event_episodes": 0, "mentions": 0, "ordinary_facts": 0, "signal_facts": 0}
        )
        self._projected_events: set[str] = set()
        self._fail_after_first_write = fail_after_first_write
        self.calls = 0

    async def execute(self, historical: HistoricalEvent) -> str:
        self.calls += 1
        if historical.id not in self._projected_events:
            self._projected_events.add(historical.id)
            self.counts["event_episodes"] += 1
            self.counts["mentions"] += 3
            self.counts["ordinary_facts"] += 2
        if self._fail_after_first_write:
            self._fail_after_first_write = False
            raise ConnectionError("Episode acknowledgement lost after graph side effects")
        return f"episode-{historical.id}"


class EventExtractionTest(unittest.IsolatedAsyncioTestCase):
    FIRST_ID = "EVD15bec7e3-998c-5434-aa5d-29712c4c67cf"
    SECOND_ID = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
    RAW_ID = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.evidence_root = root / "evidence"
        self.event_root = root / "event"
        self.evidence_manifest = self.evidence_root / "documents" / "published" / "manifest.json"
        self.evidence_manifest.parent.mkdir(parents=True)
        self.evidence_manifest.write_text("{}\n", encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {
                "EVIDENCE_ARTIFACT_ROOT": str(self.evidence_root),
                "EVENT_ARTIFACT_ROOT": str(self.event_root),
                "EVENT_EXTRACTION_BATCH_SIZE": "50",
            },
        )
        self.environment.start()
        self.runtime = AsyncMock()
        configure_event_workflow_runtime(self.runtime)

    def tearDown(self) -> None:
        configure_event_workflow_runtime(None)
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
        )

    def _prepare(self) -> EventExtractionBatch:
        with patch(
            "capabilities.event.internal.queue.read_resolved_evidences",
            return_value=self._evidences(),
        ):
            enqueue_evidence_artifact(
                str(self.evidence_manifest),
                [item.id for item in self._evidences()],
            )
            output = prepare_event_batch(StepInput(input="处理未提炼 Evidence"))
        self.assertFalse(output.stop)
        return EventExtractionBatch.model_validate(output.content)

    def _freeze(self, batch: EventExtractionBatch, draft: EventExtractionDraft) -> EventExtractionDraft:
        output = freeze_event_analysis(
            StepInput(
                previous_step_outputs={
                    "prepare-event-batch": StepOutput(content=batch),
                    "analyze-event-batch": StepOutput(content=draft),
                }
            )
        )
        self.assertEqual(EventExtractionBatch.model_validate(output.content), batch)
        frozen = json.loads((self.event_root / ".pending" / batch.batch_id / "draft.json").read_text())
        return EventExtractionDraft.model_validate(frozen)

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

    def test_missing_agent_assignment_becomes_no_event(self) -> None:
        batch = self._prepare()
        draft = self._draft().model_copy(deep=True)
        draft.candidates[0].evidence_ids = [self.FIRST_ID]

        self._freeze(batch, draft)
        frozen = EventExtractionDraft.model_validate_json(
            (self.event_root / ".pending" / batch.batch_id / "draft.json").read_text()
        )
        self.assertEqual(frozen.candidates[0].evidence_ids, [self.FIRST_ID])
        self.assertEqual(
            [(item.evidence_id, item.reason) for item in frozen.no_event],
            [(self.SECOND_ID, "unassigned_by_model")],
        )

    def test_candidate_wins_over_duplicate_no_event_assignment(self) -> None:
        batch = self._prepare()
        payload = self._draft().model_dump(mode="json")
        payload["no_event"] = [
            {"evidence_id": self.FIRST_ID, "reason": "duplicate_of_other_candidate"},
            {"evidence_id": self.SECOND_ID, "reason": "duplicate_of_other_candidate"},
        ]

        self._freeze(batch, EventExtractionDraft.model_validate(payload))
        frozen = EventExtractionDraft.model_validate_json(
            (self.event_root / ".pending" / batch.batch_id / "draft.json").read_text()
        )
        self.assertEqual(frozen.candidates[0].evidence_ids, [self.FIRST_ID, self.SECOND_ID])
        self.assertEqual(frozen.no_event, [])

    def test_timeless_agent_candidate_becomes_no_event_without_failing_batch(self) -> None:
        batch = self._prepare()
        payload = self._draft().model_dump(mode="json")
        payload["candidates"][0]["event"]["announced_at"] = None
        output = freeze_event_analysis(
            StepInput(
                previous_step_outputs={
                    "prepare-event-batch": StepOutput(content=batch),
                    "analyze-event-batch": StepOutput(content=payload),
                }
            )
        )

        self.assertEqual(EventExtractionBatch.model_validate(output.content), batch)
        frozen = EventExtractionDraft.model_validate_json(
            (self.event_root / ".pending" / batch.batch_id / "draft.json").read_text()
        )
        self.assertEqual(frozen.candidates, [])
        self.assertEqual(
            [(item.evidence_id, item.reason) for item in frozen.no_event],
            [(self.FIRST_ID, "missing_reliable_time"), (self.SECOND_ID, "missing_reliable_time")],
        )

    def test_incomplete_candidate_semantics_becomes_no_event_without_failing_batch(self) -> None:
        batch = self._prepare()
        payload = self._draft().model_dump(mode="json")
        payload["candidates"][0]["event"]["semantic"]["actors"] = []
        output = freeze_event_analysis(
            StepInput(
                previous_step_outputs={
                    "prepare-event-batch": StepOutput(content=batch),
                    "analyze-event-batch": StepOutput(content=payload),
                }
            )
        )

        self.assertEqual(EventExtractionBatch.model_validate(output.content), batch)
        frozen = EventExtractionDraft.model_validate_json(
            (self.event_root / ".pending" / batch.batch_id / "draft.json").read_text()
        )
        self.assertEqual(frozen.candidates, [])
        self.assertEqual(
            [(item.evidence_id, item.reason) for item in frozen.no_event],
            [(self.FIRST_ID, "invalid_event_semantics"), (self.SECOND_ID, "invalid_event_semantics")],
        )

    def test_missing_nullable_semantic_fields_are_defaulted_without_losing_candidate(self) -> None:
        batch = self._prepare()
        payload = self._draft().model_dump(mode="json")
        del payload["candidates"][0]["event"]["semantic"]["effective_at"]
        del payload["candidates"][0]["event"]["semantic"]["time_precision"]
        freeze_event_analysis(
            StepInput(
                previous_step_outputs={
                    "prepare-event-batch": StepOutput(content=batch),
                    "analyze-event-batch": StepOutput(content=payload),
                }
            )
        )

        frozen = EventExtractionDraft.model_validate_json(
            (self.event_root / ".pending" / batch.batch_id / "draft.json").read_text()
        )
        self.assertEqual(len(frozen.candidates), 1)
        self.assertIsNone(frozen.candidates[0].event.semantic.effective_at)
        self.assertEqual(frozen.candidates[0].event.semantic.time_precision, "UNKNOWN")

    def test_stage_output_can_carry_batch_to_downstream_steps(self) -> None:
        batch = self._prepare()

        self.assertTrue(
            event_batch_requires_analysis(
                StepInput(previous_step_outputs={"extract-event-candidates": StepOutput(content=batch)})
            )
        )

    def test_concurrent_schedule_callback_stops_without_reprocessing_pending_batch(self) -> None:
        batch = self._prepare()
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

    def test_claim_moves_pending_evidence_to_processing(self) -> None:
        batch = self._prepare()

        self.assertEqual(queue_counts(), {"pending": 0, "processing": 2, "completed": 0, "failed": 0})
        self.assertTrue((self.event_root / "evidence-queue" / "processing" / batch.batch_id).is_dir())

    def test_malformed_pending_queue_item_fails_closed(self) -> None:
        pending = self.event_root / "evidence-queue" / "pending" / f"{self.FIRST_ID}.json"
        pending.parent.mkdir(parents=True)
        pending.write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "queue item is invalid"):
            prepare_event_batch(StepInput(input="again"))

    async def test_publishes_and_analyzes_each_candidate_then_does_not_select_evidence_again(self) -> None:
        batch = self._prepare()
        self._freeze(batch, self._draft(separate=True))
        publications = [
            EventPublicationRecord(
                candidate_key=str(index) * 64,
                decision="NEW_EVENT",
                event_id=f"EVT15bec7e3-998c-5434-aa5d-29712c4c67c{index}",
                event_created=True,
                evidence_link_result="CREATED",
                graph_projection_status="SUCCEEDED",
                reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                matched_event_ids=[],
                episode_uuid=f"episode-{index}",
                published_event={
                    "id": f"EVT15bec7e3-998c-5434-aa5d-29712c4c67c{index}",
                    "event": self._draft(separate=True).candidates[index - 1].event,
                },
            )
            for index in (1, 2)
        ]

        async def publish(candidate, candidate_key, *, existing, checkpoint):
            del candidate, existing, checkpoint
            return publications[self.runtime.publish.await_count - 1].model_copy(
                update={"candidate_key": candidate_key}
            )

        self.runtime.publish.side_effect = publish
        self.runtime.construct_signals.side_effect = [
            EventSignalRecord(event_id=item.event_id, status="SUCCEEDED", signal_fact_uuids=[f"signal-{index}"])
            for index, item in enumerate(publications, 1)
        ]
        published = await publish_event_candidates(
            StepInput(previous_step_outputs={"prepare-event-batch": StepOutput(content=batch)})
        )
        output = await construct_event_signals(
            StepInput(
                previous_step_outputs={
                    "prepare-event-batch": StepOutput(content=batch),
                    "publish-event-candidates": published,
                }
            )
        )

        result = EventExtractionResult.model_validate(output.content)
        self.assertEqual(result.published_event_ids, [item.event_id for item in publications])
        self.assertEqual(result.signal_fact_uuids, ["signal-1", "signal-2"])
        self.assertEqual(self.runtime.publish.await_count, 2)
        self.assertEqual(self.runtime.construct_signals.await_count, 2)

        idle = prepare_event_batch(StepInput(input="again"))
        self.assertTrue(idle.stop)
        self.assertEqual(queue_counts(), {"pending": 0, "processing": 0, "completed": 2, "failed": 0})

    async def test_duplicate_event_is_terminal_without_projection_or_signal_analysis(self) -> None:
        batch = self._prepare()
        self._freeze(batch, self._draft())
        historical = HistoricalEvent(
            id="EVT15bec7e3-998c-5434-aa5d-29712c4c67cf",
            event=self._draft().candidates[0].event.model_dump(mode="json"),
        )
        history = AsyncMock()
        history.retrieve.return_value = [historical]
        comparator = AsyncMock()
        comparator.assess_atomicity.return_value = AtomicityAssessment(
            atomic=True,
            reason_codes=["ATOMIC_EVENT"],
            summary="one real-world action",
        )
        publisher = AsyncMock()
        runtime = object.__new__(LocalEventWorkflowRuntime)
        runtime._resolver = EventResolver(history, comparator, publisher)
        tracer = GraphWriteTracer(counts={"event_episodes": 1, "mentions": 12, "ordinary_facts": 10, "signal_facts": 3})
        runtime._episode_stage = tracer
        graph_counts_before = tracer.counts.copy()
        configure_event_workflow_runtime(runtime)
        published = await publish_event_candidates(
            StepInput(previous_step_outputs={"prepare-event-batch": StepOutput(content=batch)})
        )
        output = await construct_event_signals(
            StepInput(
                previous_step_outputs={
                    "prepare-event-batch": StepOutput(content=batch),
                    "publish-event-candidates": published,
                }
            )
        )
        result = EventExtractionResult.model_validate(output.content)
        self.assertEqual(result.duplicate_event_count, 1)
        self.assertEqual(result.published_event_ids, [])
        self.assertEqual(result.signal_fact_uuids, [])
        self.assertEqual(tracer.calls, 0)
        self.assertEqual(tracer.counts, graph_counts_before)
        publisher.publish.assert_not_awaited()
        configure_event_workflow_runtime(self.runtime)

    async def test_unresolved_candidate_moves_its_evidence_to_failed(self) -> None:
        batch = self._prepare()
        self._freeze(batch, self._draft())

        async def fail_resolution(candidate, candidate_key, *, existing, checkpoint):
            del candidate, existing, checkpoint
            return EventPublicationRecord(
                candidate_key=candidate_key,
                decision="FAILED",
                event_id=None,
                event_created=False,
                evidence_link_result="NOT_ATTEMPTED",
                graph_projection_status="NOT_ATTEMPTED",
                reason_codes=["EVENT_IDENTITY_UNCERTAIN"],
                matched_event_ids=[],
            )

        self.runtime.publish.side_effect = fail_resolution
        published = await publish_event_candidates(
            StepInput(previous_step_outputs={"prepare-event-batch": StepOutput(content=batch)})
        )
        output = await construct_event_signals(
            StepInput(
                previous_step_outputs={
                    "prepare-event-batch": StepOutput(content=batch),
                    "publish-event-candidates": published,
                }
            )
        )

        result = EventExtractionResult.model_validate(output.content)
        self.assertEqual(result.failed_candidate_count, 1)
        self.assertEqual(result.failed_evidence_ids, [self.FIRST_ID, self.SECOND_ID])
        self.assertEqual(queue_counts(), {"pending": 0, "processing": 0, "completed": 0, "failed": 2})

    async def test_publication_intent_is_checkpointed_before_the_data_write(self) -> None:
        runtime = object.__new__(LocalEventWorkflowRuntime)
        resolver = AsyncMock()

        async def fail_after_intent(submission, **callbacks):
            del submission
            callbacks["on_publication_started"]("NEW_EVENT")
            raise ConnectionError("response lost after the Data write may have committed")

        resolver.resolve.side_effect = fail_after_intent
        runtime._resolver = resolver
        checkpoints: list[EventPublicationRecord] = []

        with self.assertRaises(ConnectionError):
            await runtime.publish(
                self._draft().candidates[0],
                "a" * 64,
                existing=None,
                checkpoint=checkpoints.append,
            )

        self.assertEqual(len(checkpoints), 1)
        self.assertTrue(checkpoints[0].publication_started)
        self.assertEqual(checkpoints[0].decision, "NEW_EVENT")
        self.assertIsNone(checkpoints[0].event_id)
        self.assertEqual(checkpoints[0].reason_codes, ["PUBLICATION_STARTED"])

    async def test_resume_after_data_publication_skips_data_write_and_projects_episode(self) -> None:
        runtime = object.__new__(LocalEventWorkflowRuntime)
        history = AsyncMock()
        comparator = AsyncMock()
        publisher = AsyncMock()
        runtime._resolver = EventResolver(history, comparator, publisher)
        runtime._episode_stage = AsyncMock()
        runtime._episode_stage.execute.return_value = "episode-resumed"
        historical = HistoricalEvent(
            id="EVT15bec7e3-998c-5434-aa5d-29712c4c67cf",
            event=self._draft().candidates[0].event.model_dump(mode="json"),
        )
        existing = EventPublicationRecord(
            candidate_key="b" * 64,
            decision="NEW_EVENT",
            publication_started=True,
            event_id=historical.id,
            event_created=True,
            evidence_link_result="CREATED",
            graph_projection_status="NOT_ATTEMPTED",
            reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
            matched_event_ids=[],
            published_event={"id": historical.id, "event": historical.event.model_dump(mode="json")},
        )

        result = await runtime.publish(
            self._draft().candidates[0],
            "b" * 64,
            existing=existing,
            checkpoint=MagicMock(),
        )

        self.assertEqual(result.episode_uuid, "episode-resumed")
        publisher.publish.assert_not_awaited()
        comparator.assess_atomicity.assert_not_awaited()
        history.retrieve.assert_not_awaited()
        runtime._episode_stage.execute.assert_awaited_once_with(historical)

    async def test_retry_after_episode_side_effect_reuses_published_event_and_deterministic_projection(self) -> None:
        runtime = object.__new__(LocalEventWorkflowRuntime)
        history = AsyncMock()
        comparator = AsyncMock()
        publisher = AsyncMock()
        runtime._resolver = EventResolver(history, comparator, publisher)
        tracer = GraphWriteTracer(fail_after_first_write=True)
        runtime._episode_stage = tracer
        historical = HistoricalEvent(
            id="EVT15bec7e3-998c-5434-aa5d-29712c4c67cf",
            event=self._draft().candidates[0].event.model_dump(mode="json"),
        )
        existing = EventPublicationRecord(
            candidate_key="c" * 64,
            decision="NEW_EVENT",
            publication_started=True,
            event_id=historical.id,
            event_created=True,
            evidence_link_result="CREATED",
            graph_projection_status="NOT_ATTEMPTED",
            reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
            matched_event_ids=[],
            published_event={"id": historical.id, "event": historical.event.model_dump(mode="json")},
        )

        with self.assertRaises(ConnectionError):
            await runtime.publish(
                self._draft().candidates[0],
                "c" * 64,
                existing=existing,
                checkpoint=MagicMock(),
            )
        counts_after_lost_ack = tracer.counts.copy()
        result = await runtime.publish(
            self._draft().candidates[0],
            "c" * 64,
            existing=existing,
            checkpoint=MagicMock(),
        )

        self.assertEqual(tracer.calls, 2)
        self.assertEqual(result.episode_uuid, f"episode-{historical.id}")
        self.assertEqual(tracer.counts, counts_after_lost_ack)
        self.assertEqual(
            tracer.counts,
            {"event_episodes": 1, "mentions": 3, "ordinary_facts": 2, "signal_facts": 0},
        )
        publisher.publish.assert_not_awaited()

    async def test_publication_failure_exposes_safe_stage_and_diagnostic_id(self) -> None:
        batch = self._prepare()
        self._freeze(batch, self._draft())
        self.runtime.publish.side_effect = ConnectionError("secret provider payload")

        with self.assertLogs("capabilities.event.functions.extraction", level="ERROR") as logs:
            with self.assertRaisesRegex(RuntimeError, r"EVENT_PUBLICATION failed; diagnostic_id=") as raised:
                await publish_event_candidates(
                    StepInput(previous_step_outputs={"prepare-event-batch": StepOutput(content=batch)})
                )

        self.assertNotIn("secret provider payload", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertIn("error_types=ConnectionError", logs.output[0])

    async def test_signal_failure_exposes_safe_stage_and_diagnostic_id(self) -> None:
        batch = self._prepare()
        self._freeze(batch, self._draft())
        publication = EventPublicationRecord(
            candidate_key="d" * 64,
            decision="NEW_EVENT",
            event_id="EVT15bec7e3-998c-5434-aa5d-29712c4c67cf",
            event_created=True,
            evidence_link_result="CREATED",
            graph_projection_status="SUCCEEDED",
            reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
            matched_event_ids=[],
            episode_uuid="episode-signal",
            published_event={
                "id": "EVT15bec7e3-998c-5434-aa5d-29712c4c67cf",
                "event": self._draft().candidates[0].event,
            },
        )

        async def publish(candidate, candidate_key, *, existing, checkpoint):
            del candidate, existing, checkpoint
            return publication.model_copy(update={"candidate_key": candidate_key})

        self.runtime.publish.side_effect = publish
        self.runtime.construct_signals.side_effect = ConnectionError("secret signal payload")
        published = await publish_event_candidates(
            StepInput(previous_step_outputs={"prepare-event-batch": StepOutput(content=batch)})
        )

        with self.assertLogs("capabilities.event.functions.extraction", level="ERROR") as logs:
            with self.assertRaisesRegex(RuntimeError, r"SIGNAL_CONSTRUCTION failed; diagnostic_id=") as raised:
                await construct_event_signals(
                    StepInput(
                        previous_step_outputs={
                            "prepare-event-batch": StepOutput(content=batch),
                            "publish-event-candidates": published,
                        }
                    )
                )

        self.assertNotIn("secret signal payload", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertIn("error_types=ConnectionError", logs.output[0])

    async def test_no_event_is_terminal_without_posting(self) -> None:
        batch = self._prepare()
        draft = EventExtractionDraft(
            candidates=[],
            no_event=[
                {"evidence_id": self.FIRST_ID, "reason": "not_a_real_world_action"},
                {"evidence_id": self.SECOND_ID, "reason": "compound_atomic_evidence"},
            ],
        )
        self._freeze(batch, draft)

        published = await publish_event_candidates(
            StepInput(previous_step_outputs={"prepare-event-batch": StepOutput(content=batch)})
        )
        output = await construct_event_signals(
            StepInput(
                previous_step_outputs={
                    "prepare-event-batch": StepOutput(content=batch),
                    "publish-event-candidates": published,
                }
            )
        )

        self.runtime.publish.assert_not_awaited()
        result = EventExtractionResult.model_validate(output.content)
        self.assertEqual(result.candidate_count, 0)
        self.assertEqual(result.no_event_count, 2)
        self.assertEqual(result.failed_candidate_count, 0)

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
                publish_event_candidates,
                construct_event_signals,
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
        from agno.workflow import Steps

        self.assertIsInstance(steps[0], Steps)
        extraction = steps[0]
        assert isinstance(extraction, Steps)
        self.assertEqual(extraction.name, "extract-event-candidates")
        self.assertIsInstance(extraction.steps[0], Step)
        self.assertIsInstance(extraction.steps[1], Condition)
        condition = extraction.steps[1]
        assert isinstance(condition, Condition)
        self.assertEqual([step.name for step in condition.steps], ["analyze-event-batch", "freeze-event-analysis"])
        self.assertEqual(cast(Step, steps[1]).name, "publish-event-candidates")
        self.assertEqual(cast(Step, steps[2]).name, "construct-event-signals")

    async def test_complete_workflow_analyzes_publishes_projects_and_builds_signals(self) -> None:
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
        publication = EventPublicationRecord(
            candidate_key="1" * 64,
            decision="NEW_EVENT",
            event_id="EVT15bec7e3-998c-5434-aa5d-29712c4c67cf",
            event_created=True,
            evidence_link_result="CREATED",
            graph_projection_status="SUCCEEDED",
            reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
            matched_event_ids=[],
            episode_uuid="episode-complete",
            published_event={
                "id": "EVT15bec7e3-998c-5434-aa5d-29712c4c67cf",
                "event": self._draft().candidates[0].event,
            },
        )

        async def publish(candidate, candidate_key, *, existing, checkpoint):
            del candidate, existing, checkpoint
            return publication.model_copy(update={"candidate_key": candidate_key})

        self.runtime.publish.side_effect = publish
        self.runtime.construct_signals.return_value = EventSignalRecord(
            event_id=publication.event_id,
            status="SUCCEEDED",
            signal_fact_uuids=["signal-complete"],
        )
        with (
            patch.object(agent, "arun", new=controlled_agent),
            patch(
                "capabilities.event.internal.queue.read_resolved_evidences",
                return_value=self._evidences(),
            ),
        ):
            enqueue_evidence_artifact(
                str(self.evidence_manifest),
                [item.id for item in self._evidences()],
            )
            response = await workflow.arun(
                input="处理所有已发布且尚未提炼 Event 的 Evidence",
                run_id="run-event-workflow",
                session_id="session-event-workflow",
            )

        self.assertEqual(response.status, RunStatus.completed)
        self.assertEqual(controlled_agent.call_count, 1)
        analyzed = EventExtractionBatch.model_validate(controlled_agent.call_args.kwargs["input"])
        self.assertEqual([item.id for item in analyzed.evidences], [self.FIRST_ID, self.SECOND_ID])
        self.runtime.publish.assert_awaited_once()
        self.runtime.construct_signals.assert_awaited_once()

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
        from agno.workflow import Steps

        extraction = cast(Steps, cast(list[object], migrated.steps)[0])
        condition = cast(Condition, extraction.steps[1])
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
