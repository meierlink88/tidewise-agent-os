"""Single durable Pipeline for Event resolution, publication, and analysis handoff."""

from __future__ import annotations

import logging
import traceback
from datetime import UTC, datetime
from functools import partial
from uuid import uuid4

from sematica.analysis.event.contracts import EventAnalysisInput
from sematica.ingestion.episcode.event.contracts import (
    EventCandidateAcceptance,
    EventCandidateRequest,
    EventCandidateStatus,
)
from sematica.ingestion.episcode.event.resolver import (
    ComparisonUnavailable,
    EventHistoryUnavailable,
    PublicationRejected,
)
from sematica.ingestion.episcode.event.store import EventCandidateStore

logger = logging.getLogger(__name__)


def _safe_exception_diagnostics(exc: BaseException) -> tuple[str, str]:
    """Return useful type and frame data without messages or arguments."""

    error_types: list[str] = []
    frames: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        error_types.append(f"{type(current).__module__}.{type(current).__qualname__}")
        frames.extend(
            f"{frame.filename}:{frame.lineno}:{frame.name}"
            for frame in traceback.extract_tb(current.__traceback__)[-3:]
        )
        current = current.__cause__ or current.__context__
    return "<-".join(error_types), "|".join(frames)


def _log_pipeline_failure(submission, exc: BaseException, *, stage: str) -> None:
    error_types, frames = _safe_exception_diagnostics(exc)
    logger.error(
        "event_candidate_pipeline_failed submission_id=%s attempt=%s "
        "stage=%s diagnostic_id=%s error_types=%s frames=%s",
        submission.submission_id,
        submission.attempt_count,
        stage,
        uuid4(),
        error_types,
        frames,
    )


class EventCandidatePipeline:
    """Only business interface for accepting and executing Event Candidates."""

    def __init__(
        self,
        store: EventCandidateStore,
        resolver,
        episode_stage=None,
        analysis_pipeline=None,
        *,
        max_attempts: int = 5,
        retry_delay_seconds: int = 30,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._episode_stage = episode_stage
        self._analysis_pipeline = analysis_pipeline
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds

    def submit(self, request: EventCandidateRequest) -> EventCandidateAcceptance:
        accepted = self._store.accept(request)
        return EventCandidateAcceptance(
            submission_id=accepted.submission_id,
            status="ACCEPTED",
            status_url=f"/api/reason/v1/event-candidates/{accepted.submission_id}",
            replayed=accepted.replayed,
        )

    def get_status(self, submission_id: str) -> EventCandidateStatus | None:
        return self._store.get(submission_id)

    async def process_pending(self, *, limit: int) -> int:
        if self._resolver is None:
            raise RuntimeError("Event resolution stage is not configured")
        processed = 0
        for _ in range(limit):
            submission = self._store.claim()
            if submission is None:
                break
            try:
                resolution = await self._resolver.resolve(
                    submission,
                    on_published=partial(self._store.published, submission.submission_id),
                    on_publication_started=partial(self._store.publication_started, submission.submission_id),
                )
            except ComparisonUnavailable as exc:
                _log_pipeline_failure(submission, exc, stage="EVENT_SEMANTIC_COMPARISON")
                if submission.attempt_count >= self._max_attempts:
                    self._store.needs_review(
                        submission.submission_id,
                        "EVENT_SEMANTIC_COMPARISON_UNAVAILABLE",
                    )
                else:
                    self._store.fail(
                        submission.submission_id,
                        "EVENT_SEMANTIC_COMPARISON_UNAVAILABLE",
                        terminal=False,
                        retry_delay_seconds=self._retry_delay_seconds,
                    )
                continue
            except EventHistoryUnavailable as exc:
                _log_pipeline_failure(submission, exc, stage="DATA_EVENT_HISTORY_RECALL")
                self._store.fail(
                    submission.submission_id,
                    "DATA_EVENT_HISTORY_UNAVAILABLE",
                    terminal=submission.attempt_count >= self._max_attempts,
                    retry_delay_seconds=self._retry_delay_seconds,
                )
                continue
            except PublicationRejected as exc:
                _log_pipeline_failure(submission, exc, stage="DATA_EVENT_PUBLICATION")
                self._store.fail(
                    submission.submission_id,
                    "DATA_EVENT_PUBLICATION_REJECTED",
                    terminal=True,
                    retry_delay_seconds=self._retry_delay_seconds,
                )
                continue
            except Exception as exc:
                _log_pipeline_failure(submission, exc, stage="EVENT_RESOLUTION")
                self._store.fail(
                    submission.submission_id,
                    "EVENT_RESOLUTION_FAILED",
                    terminal=submission.attempt_count >= self._max_attempts,
                    retry_delay_seconds=self._retry_delay_seconds,
                )
                continue

            outcome = resolution.outcome
            published_event = resolution.published_event
            if published_event is not None:
                try:
                    if self._episode_stage is None:
                        raise RuntimeError("Graphiti Episode stage is not configured")
                    episode_uuid = await self._episode_stage.execute(published_event)
                except Exception as exc:
                    _log_pipeline_failure(submission, exc, stage="GRAPHITI_EVENT_EPISODE")
                    self._store.projection_pending(
                        submission.submission_id,
                        outcome,
                        published_event,
                        terminal=submission.attempt_count >= self._max_attempts,
                        retry_delay_seconds=self._retry_delay_seconds,
                    )
                    continue

                try:
                    if self._analysis_pipeline is None:
                        raise RuntimeError("Event Analysis Pipeline is not configured")
                    self._analysis_pipeline.enqueue(
                        EventAnalysisInput(
                            event=published_event,
                            episode_uuid=episode_uuid,
                            reference_time=datetime.now(UTC),
                        )
                    )
                except Exception as exc:
                    _log_pipeline_failure(submission, exc, stage="EVENT_ANALYSIS_SCHEDULING")
                    self._store.analysis_scheduling_pending(
                        submission.submission_id,
                        outcome,
                        published_event,
                        terminal=submission.attempt_count >= self._max_attempts,
                        retry_delay_seconds=self._retry_delay_seconds,
                    )
                    continue

            self._store.complete(submission.submission_id, outcome)
            processed += 1
        return processed

    async def ready(self) -> bool:
        return bool(self._episode_stage is not None and await self._episode_stage.ready())

    async def close(self) -> None:
        if self._episode_stage is not None:
            await self._episode_stage.close()
