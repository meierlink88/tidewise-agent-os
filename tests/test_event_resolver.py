"""Behavior tests for the five-dimensional Event identity gate."""

import unittest
from unittest.mock import AsyncMock

from capabilities.event.internal.resolver import EventResolver
from sematica.ingestion.episcode.event.contracts import (
    AtomicityAssessment,
    EventCandidateDTO,
    HistoricalEvent,
    PairComparison,
)


def _event(*, actor: str, action: str, object_: str, announced_at: str) -> EventCandidateDTO:
    return EventCandidateDTO.model_validate(
        {
            "title": f"{actor}{action}{object_}",
            "summary": f"{actor}{action}{object_}",
            "semantic": {
                "actors": [actor],
                "action": action,
                "objects": [object_],
                "stage": "ANNOUNCED",
                "jurisdictions": [],
                "effective_at": None,
                "time_precision": "DAY",
            },
            "modality": "FACT",
            "occurred_at": None,
            "announced_at": announced_at,
        }
    )


class EventResolverTest(unittest.IsolatedAsyncioTestCase):
    async def test_non_atomic_candidate_is_ignored_without_human_review_or_publication(self) -> None:
        candidate = _event(
            actor="央行", action="发布报告并降息", object_="货币政策", announced_at="2026-08-29T00:00:00Z"
        )
        history = AsyncMock()
        comparator = AsyncMock()
        comparator.assess_atomicity.return_value = AtomicityAssessment(
            atomic=False,
            reason_codes=["MULTIPLE_ACTIONS"],
            summary="multiple actions",
        )
        publisher = AsyncMock()
        submission = type("Submission", (), {"event": candidate})()

        result = await EventResolver(history, comparator, publisher).resolve(submission)

        self.assertEqual(result.outcome.decision, "IGNORED")
        self.assertEqual(result.outcome.reason_codes, ["MULTIPLE_ACTIONS"])
        history.retrieve.assert_not_awaited()
        publisher.publish.assert_not_awaited()

    async def test_unrelated_uncertain_comparison_cannot_block_a_new_event(self) -> None:
        candidate = _event(
            actor="央行", action="发布货币政策报告", object_="适度宽松政策", announced_at="2026-08-29T00:00:00Z"
        )
        unrelated = HistoricalEvent(
            id="EVT15bec7e3-998c-4434-aa5d-29712c4c67cf",
            event=_event(
                actor="某公司", action="签署销售合同", object_="交换芯片", announced_at="2026-08-16T00:00:00Z"
            ),
        )
        published = HistoricalEvent(id="EVT5cb71bef-5b1d-4995-add0-7408eaa2be15", event=candidate)
        history = AsyncMock()
        history.retrieve.return_value = [unrelated]
        comparator = AsyncMock()
        comparator.assess_atomicity.return_value = AtomicityAssessment(
            atomic=True,
            reason_codes=["SINGLE_ACTION"],
            summary="one action",
        )
        comparator.compare.return_value = PairComparison(
            decision="NEEDS_REVIEW",
            same_actor=False,
            same_action=False,
            same_object=False,
            same_stage=True,
            same_occurrence_time=False,
            material_conflicts=[],
            reason_codes=["DIFFERENT_ACTOR", "DIFFERENT_ACTION", "DIFFERENT_OBJECT"],
            summary="unrelated",
        )
        publisher = AsyncMock()
        publisher.publish.return_value = published
        submission = type("Submission", (), {"event": candidate})()

        result = await EventResolver(history, comparator, publisher).resolve(submission)

        self.assertEqual(result.outcome.decision, "RELATED_BUT_DISTINCT")
        self.assertEqual(result.outcome.event_id, published.id)
        publisher.publish.assert_awaited_once()

    async def test_plausible_identity_uncertainty_is_ignored_without_publication(self) -> None:
        candidate = _event(
            actor="央行", action="发布货币政策报告", object_="适度宽松政策", announced_at="2026-08-29T00:00:00Z"
        )
        plausible = HistoricalEvent(
            id="EVT15bec7e3-998c-4434-aa5d-29712c4c67cf",
            event=_event(
                actor="中国人民银行",
                action="公布货币政策报告",
                object_="宽松货币政策",
                announced_at="2026-08-29T00:00:00Z",
            ),
        )
        history = AsyncMock()
        history.retrieve.return_value = [plausible]
        comparator = AsyncMock()
        comparator.assess_atomicity.return_value = AtomicityAssessment(
            atomic=True,
            reason_codes=["SINGLE_ACTION"],
            summary="one action",
        )
        comparator.compare.return_value = PairComparison(
            decision="NEEDS_REVIEW",
            same_actor=True,
            same_action=True,
            same_object=False,
            same_stage=True,
            same_occurrence_time=False,
            material_conflicts=[],
            reason_codes=["OBJECT_UNCERTAIN"],
            summary="plausibly the same occurrence",
        )
        publisher = AsyncMock()
        submission = type("Submission", (), {"event": candidate})()

        result = await EventResolver(history, comparator, publisher).resolve(submission)

        self.assertEqual(result.outcome.decision, "IGNORED")
        self.assertEqual(result.outcome.matched_event_ids, [plausible.id])
        publisher.publish.assert_not_awaited()

    async def test_started_publication_retries_the_idempotent_data_write_without_history(self) -> None:
        candidate = _event(
            actor="央行",
            action="发布货币政策报告",
            object_="适度宽松政策",
            announced_at="2026-08-29T00:00:00Z",
        )
        published = HistoricalEvent(id="EVT5cb71bef-5b1d-4995-add0-7408eaa2be15", event=candidate)
        history = AsyncMock()
        comparator = AsyncMock()
        publisher = AsyncMock()
        publisher.publish.return_value = published
        submission = type(
            "Submission",
            (),
            {
                "event": candidate,
                "publication_started": True,
                "pending_decision": "NEW_EVENT",
            },
        )()

        result = await EventResolver(history, comparator, publisher).resolve(submission)

        self.assertEqual(result.outcome.decision, "NEW_EVENT")
        self.assertEqual(result.outcome.event_id, published.id)
        publisher.publish.assert_awaited_once_with(submission)
        history.retrieve.assert_not_awaited()
        comparator.assess_atomicity.assert_not_awaited()

    async def test_second_history_guard_compares_only_events_new_since_initial_recall(self) -> None:
        candidate = _event(
            actor="央行", action="发布货币政策报告", object_="适度宽松政策", announced_at="2026-08-29T00:00:00Z"
        )
        first = HistoricalEvent(
            id="EVT15bec7e3-998c-4434-aa5d-29712c4c67cf",
            event=_event(
                actor="央行", action="发布金融稳定报告", object_="银行体系", announced_at="2026-08-28T00:00:00Z"
            ),
        )
        appeared_later = HistoricalEvent(
            id="EVT5cb71bef-5b1d-4995-add0-7408eaa2be15",
            event=_event(actor="央行", action="发布通胀报告", object_="物价水平", announced_at="2026-08-29T00:00:00Z"),
        )
        published = HistoricalEvent(id="EVT9cb71bef-5b1d-4995-add0-7408eaa2be15", event=candidate)
        history = AsyncMock()
        history.retrieve.side_effect = [[first], [first, appeared_later]]
        comparator = AsyncMock()
        comparator.assess_atomicity.return_value = AtomicityAssessment(
            atomic=True,
            reason_codes=["SINGLE_ACTION"],
            summary="one action",
        )
        comparator.compare.return_value = PairComparison(
            decision="RELATED_BUT_DISTINCT",
            same_actor=True,
            same_action=False,
            same_object=False,
            same_stage=True,
            same_occurrence_time=False,
            material_conflicts=[],
            reason_codes=["DIFFERENT_ACTION"],
            summary="distinct",
        )
        publisher = AsyncMock()
        publisher.publish.return_value = published
        submission = type("Submission", (), {"event": candidate})()

        result = await EventResolver(history, comparator, publisher).resolve(submission)

        self.assertEqual(result.outcome.decision, "RELATED_BUT_DISTINCT")
        self.assertEqual(comparator.compare.await_count, 2)
        compared_ids = [call.args[1].id for call in comparator.compare.await_args_list]
        self.assertEqual(compared_ids, [first.id, appeared_later.id])


if __name__ == "__main__":
    unittest.main()
