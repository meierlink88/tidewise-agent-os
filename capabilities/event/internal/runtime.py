"""Local runtime seam used by deterministic Event Workflow Functions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from capabilities.event.internal.models import (
    EventCandidateSubmission,
    EventPublicationRecord,
    EventResolutionRecord,
)
from sematica.analysis.event.contracts import (
    AnchorCandidate,
    CandidateSet,
    EventAnalysisInput,
    EventClassification,
    SignalProposal,
    VariableCandidate,
)
from sematica.ingestion.episcode.event.contracts import HistoricalEvent

PublicationCheckpoint = Callable[[EventPublicationRecord], None]


class EventWorkflowRuntime(Protocol):
    """Deep interface containing only deterministic I/O and Graphiti-native operations."""

    async def retrieve_history(self, candidate: EventCandidateSubmission) -> list[HistoricalEvent]: ...

    async def publish(
        self,
        candidate: EventCandidateSubmission,
        candidate_key: str,
        resolution: EventResolutionRecord,
        *,
        existing: EventPublicationRecord | None,
        checkpoint: PublicationCheckpoint,
    ) -> EventPublicationRecord: ...

    async def retrieve_signal_candidates(
        self,
        event: EventAnalysisInput,
        classification: EventClassification,
    ) -> CandidateSet: ...

    async def project_signal(
        self,
        event: EventAnalysisInput,
        classification: EventClassification,
        variable: VariableCandidate,
        anchor: AnchorCandidate,
        proposal: SignalProposal,
    ) -> str: ...

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
