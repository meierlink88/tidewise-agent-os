"""Local runtime seam used by the deterministic Event Workflow functions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from capabilities.event.internal.models import (
    EventCandidateSubmission,
    EventExtractionBatch,
    EventPublicationRecord,
    EventSignalRecord,
)

PublicationCheckpoint = Callable[[EventPublicationRecord], None]


class EventWorkflowRuntime(Protocol):
    """Deep interface hiding resolution, Data publication and Graphiti mechanics."""

    async def extract(self, batch: EventExtractionBatch) -> Any: ...

    async def publish(
        self,
        candidate: EventCandidateSubmission,
        candidate_key: str,
        *,
        existing: EventPublicationRecord | None,
        checkpoint: PublicationCheckpoint,
    ) -> EventPublicationRecord: ...

    async def construct_signals(self, publication: EventPublicationRecord) -> EventSignalRecord: ...

    async def close(self) -> None: ...


_runtime: EventWorkflowRuntime | None = None


def configure_event_workflow_runtime(runtime: EventWorkflowRuntime | None) -> None:
    """Install the app-owned runtime or clear it during deterministic tests."""

    global _runtime
    _runtime = runtime


def event_workflow_runtime() -> EventWorkflowRuntime:
    if _runtime is None:
        raise RuntimeError("Event Workflow runtime is not configured")
    return _runtime
