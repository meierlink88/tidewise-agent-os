"""Event identity resolution and authoritative Data publication stage."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from sematica.ingestion.episcode.event.contracts import (
    AtomicityAssessment,
    EventResolutionOutcome,
    HistoricalEvent,
    PairComparison,
)


class HistoryRetriever(Protocol):
    async def retrieve(self, candidate) -> list[HistoricalEvent]: ...


class EventComparator(Protocol):
    async def assess_atomicity(self, candidate) -> AtomicityAssessment: ...
    async def compare(self, candidate, historical: HistoricalEvent) -> PairComparison: ...


class DataPublisher(Protocol):
    async def publish(self, submission) -> HistoricalEvent: ...


class ComparisonUnavailable(RuntimeError):
    """The bounded semantic decision could not produce a safe structured result."""


class EventHistoryUnavailable(RuntimeError):
    """The authoritative Data Event history could not be recalled safely."""


class PublicationRejected(RuntimeError):
    """Data rejected a publication with a permanent 4xx contract response."""


@dataclass(frozen=True)
class EventResolution:
    """One resolution decision and the formal Event requiring downstream stages."""

    outcome: EventResolutionOutcome
    published_event: HistoricalEvent | None = None


def _term(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _terms(values: list[str]) -> set[str]:
    return {_term(value) for value in values}


def _time(event):
    return event.occurred_at or event.announced_at or event.semantic.effective_at


def same_occurrence(candidate, historical) -> bool:
    left, right = candidate.semantic, historical.semantic
    return (
        _terms(left.actors) == _terms(right.actors)
        and _term(left.action) == _term(right.action)
        and _terms(left.objects) == _terms(right.objects)
        and left.stage == right.stage
        and _time(candidate) == _time(historical)
    )


def potentially_same(candidate, historical) -> bool:
    # Different stages are safely distinct. Actor/object string mismatch is not
    # a safe veto because aliases and translations are common; the constrained
    # comparator resolves those cases after retrieval has bounded the set.
    return candidate.semantic.stage == historical.semantic.stage


class EventResolver:
    def __init__(
        self,
        history: HistoryRetriever,
        comparator: EventComparator,
        publisher: DataPublisher,
    ) -> None:
        self._history = history
        self._comparator = comparator
        self._publisher = publisher

    async def _evaluate_history(self, candidate, history: list[HistoricalEvent]) -> EventResolutionOutcome | None:
        exact_ids = {item.id for item in history if same_occurrence(candidate, item.event)}
        if len(exact_ids) > 1:
            return EventResolutionOutcome(
                decision="IGNORED",
                event_id=None,
                event_created=False,
                evidence_link_result="NOT_ATTEMPTED",
                graph_projection_status="NOT_ATTEMPTED",
                reason_codes=["MULTIPLE_STRONG_EVENT_MATCHES"],
                matched_event_ids=sorted(exact_ids),
            )
        if len(exact_ids) == 1:
            event_id = next(iter(exact_ids))
            return EventResolutionOutcome(
                decision="SAME_EVENT",
                event_id=event_id,
                event_created=False,
                evidence_link_result="IGNORED",
                graph_projection_status="IGNORED",
                reason_codes=["SAME_REAL_WORLD_OCCURRENCE"],
                matched_event_ids=[event_id],
            )

        same_ids: set[str] = set()
        review_ids: set[str] = set()
        reason_codes: list[str] = []

        for item in history:
            if item.id in exact_ids or not potentially_same(candidate, item.event):
                continue
            try:
                comparison = await self._comparator.compare(candidate, item)
            except Exception as exc:
                raise ComparisonUnavailable("Event comparison did not complete") from exc
            reason_codes.extend(comparison.reason_codes)
            all_identity_dimensions_match = all(
                (
                    comparison.same_actor,
                    comparison.same_action,
                    comparison.same_object,
                    comparison.same_stage,
                    comparison.same_occurrence_time,
                )
            )
            plausible_same_occurrence = (
                sum(
                    (
                        comparison.same_actor,
                        comparison.same_action,
                        comparison.same_object,
                        comparison.same_occurrence_time,
                    )
                )
                >= 2
            )
            if comparison.decision == "SAME_EVENT":
                consistent = all_identity_dimensions_match and not comparison.material_conflicts
                if consistent:
                    same_ids.add(item.id)
                elif plausible_same_occurrence:
                    review_ids.add(item.id)
            elif comparison.decision in {"NEEDS_REVIEW", "SAME_EVENT_REVISION"} and plausible_same_occurrence:
                review_ids.add(item.id)
            elif all_identity_dimensions_match and not comparison.material_conflicts:
                review_ids.add(item.id)
                reason_codes.append("INCONSISTENT_DISTINCT_EVENT_COMPARISON")

        if len(same_ids) > 1 or (same_ids and review_ids):
            matched = sorted(same_ids | review_ids)
            return EventResolutionOutcome(
                decision="IGNORED",
                event_id=None,
                event_created=False,
                evidence_link_result="NOT_ATTEMPTED",
                graph_projection_status="NOT_ATTEMPTED",
                reason_codes=["MULTIPLE_STRONG_EVENT_MATCHES"],
                matched_event_ids=matched,
            )
        if len(same_ids) == 1:
            event_id = next(iter(same_ids))
            return EventResolutionOutcome(
                decision="SAME_EVENT",
                event_id=event_id,
                event_created=False,
                evidence_link_result="IGNORED",
                graph_projection_status="IGNORED",
                reason_codes=reason_codes or ["SAME_REAL_WORLD_OCCURRENCE"],
                matched_event_ids=[event_id],
            )
        if review_ids:
            return EventResolutionOutcome(
                decision="IGNORED",
                event_id=None,
                event_created=False,
                evidence_link_result="NOT_ATTEMPTED",
                graph_projection_status="NOT_ATTEMPTED",
                reason_codes=reason_codes or ["EVENT_IDENTITY_UNCERTAIN"],
                matched_event_ids=sorted(review_ids),
            )
        return None

    async def resolve(
        self,
        submission,
        on_published: Callable[[EventResolutionOutcome, HistoricalEvent], None] | None = None,
        on_publication_started: Callable[[str], None] | None = None,
    ) -> EventResolution:
        published_event = getattr(submission, "published_event", None)
        if published_event is not None:
            decision = getattr(submission, "pending_decision", None) or "NEW_EVENT"
            outcome = EventResolutionOutcome(
                decision=decision,
                event_id=published_event.id,
                event_created=True,
                evidence_link_result="CREATED",
                graph_projection_status="SUCCEEDED",
                reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                matched_event_ids=[],
            )
            return EventResolution(outcome, published_event)

        if getattr(submission, "publication_started", False):
            decision = getattr(submission, "pending_decision", None) or "NEW_EVENT"
            published = await self._publisher.publish(submission)
            outcome = EventResolutionOutcome(
                decision=decision,
                event_id=published.id,
                event_created=True,
                evidence_link_result="CREATED",
                graph_projection_status="SUCCEEDED",
                reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                matched_event_ids=[],
            )
            if on_published is not None:
                on_published(outcome, published)
            return EventResolution(outcome, published)

        try:
            atomicity = await self._comparator.assess_atomicity(submission.event)
        except Exception as exc:
            raise ComparisonUnavailable("Event atomicity assessment did not complete") from exc
        if not atomicity.atomic:
            return EventResolution(
                EventResolutionOutcome(
                    decision="IGNORED",
                    event_id=None,
                    event_created=False,
                    evidence_link_result="NOT_ATTEMPTED",
                    graph_projection_status="NOT_ATTEMPTED",
                    reason_codes=atomicity.reason_codes,
                    matched_event_ids=[],
                )
            )

        history = await self._history.retrieve(submission.event)
        if historical_outcome := await self._evaluate_history(submission.event, history):
            return EventResolution(historical_outcome)

        # A second recall immediately before the external write closes the
        # single-worker queue race between initial comparison and publication.
        final_history = await self._history.retrieve(submission.event)
        initial_ids = {item.id for item in history}
        newly_visible_history = [item for item in final_history if item.id not in initial_ids]
        if final_outcome := await self._evaluate_history(submission.event, newly_visible_history):
            return EventResolution(final_outcome)

        decision = "RELATED_BUT_DISTINCT" if history or final_history else "NEW_EVENT"
        if on_publication_started is not None:
            on_publication_started(decision)
        published = await self._publisher.publish(submission)
        outcome = EventResolutionOutcome(
            decision=decision,
            event_id=published.id,
            event_created=True,
            evidence_link_result="CREATED",
            graph_projection_status="SUCCEEDED",
            reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
            matched_event_ids=[],
        )
        if on_published is not None:
            on_published(outcome, published)
        return EventResolution(outcome, published)
