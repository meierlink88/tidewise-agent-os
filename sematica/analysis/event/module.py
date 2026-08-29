"""Deep interface coordinating durable Event Analysis execution."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from uuid import uuid4

from sematica.analysis.event.contracts import (
    EventAnalysisAcceptance,
    EventAnalysisInput,
    EventAnalysisStatus,
)
from sematica.analysis.event.errors import PermanentEventAnalysisFailure
from sematica.analysis.event.store import EventAnalysisStore

logger = logging.getLogger(__name__)


def _stage_callback(
    store: EventAnalysisStore,
    analysis_id: str,
    current_stage: list[str],
) -> Callable[[str], None]:
    def on_stage(stage: str) -> None:
        current_stage[0] = stage
        store.set_stage(analysis_id, stage)

    return on_stage


def _safe_diagnostics(exc: BaseException) -> tuple[str, str]:
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


class EventAnalysisModule:
    def __init__(
        self,
        store: EventAnalysisStore,
        pipeline,
        *,
        max_attempts: int = 5,
        retry_delay_seconds: int = 30,
    ) -> None:
        self._store = store
        self._pipeline = pipeline
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds

    def enqueue(self, input_: EventAnalysisInput) -> EventAnalysisAcceptance:
        return self._store.enqueue(input_)

    def get_status(self, analysis_id: str) -> EventAnalysisStatus | None:
        return self._store.get(analysis_id)

    async def process_pending(self, *, limit: int) -> int:
        processed = 0
        for _ in range(limit):
            claimed = self._store.claim()
            if claimed is None:
                break
            analysis_id = claimed.analysis_id
            current_stage = ["CLASSIFYING"]
            on_stage = _stage_callback(self._store, analysis_id, current_stage)

            try:
                outcome = await self._pipeline.analyze(claimed.input, on_stage=on_stage)
            except PermanentEventAnalysisFailure as exc:
                error_types, frames = _safe_diagnostics(exc)
                logger.error(
                    "event_analysis_validation_failed analysis_id=%s event_id=%s "
                    "stage=%s diagnostic_id=%s error_types=%s frames=%s",
                    analysis_id,
                    claimed.input.event.id,
                    current_stage[0],
                    uuid4(),
                    error_types,
                    frames,
                )
                self._store.fail(
                    analysis_id,
                    terminal=True,
                    retry_delay_seconds=self._retry_delay_seconds,
                    error_code="EVENT_ANALYSIS_VALIDATION_FAILED",
                )
                continue
            except Exception as exc:
                error_types, frames = _safe_diagnostics(exc)
                logger.error(
                    "event_analysis_failed analysis_id=%s event_id=%s attempt=%s "
                    "stage=%s diagnostic_id=%s error_types=%s frames=%s",
                    analysis_id,
                    claimed.input.event.id,
                    claimed.attempt_count,
                    current_stage[0],
                    uuid4(),
                    error_types,
                    frames,
                )
                self._store.fail(
                    analysis_id,
                    terminal=claimed.attempt_count >= self._max_attempts,
                    retry_delay_seconds=self._retry_delay_seconds,
                )
                continue
            self._store.complete(analysis_id, outcome)
            processed += 1
        return processed
