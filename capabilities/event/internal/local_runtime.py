"""Data and Graphiti runtime for the deterministic Event Workflow Functions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, cast

from capabilities.event.internal.models import (
    EventCandidateSubmission,
    EventPublicationRecord,
    EventResolutionRecord,
    PublishedEvent,
)
from capabilities.event.internal.runtime import PublicationCheckpoint
from sematica.analysis.event.contracts import (
    AnchorCandidate,
    CandidateSet,
    EventAnalysisInput,
    EventClassification,
    SignalProposal,
    VariableCandidate,
)
from sematica.analysis.event.graphiti import GraphitiCandidateRetriever, GraphitiSignalFactProjector
from sematica.graphiti.runtime import create_agentos_graphiti
from sematica.ingestion.episcode.event.adapters import CompositeEventHistory, DataEventClient
from sematica.ingestion.episcode.event.contracts import (
    EventCandidateDTO,
    EventCandidateRequest,
    HistoricalEvent,
)
from sematica.ingestion.episcode.event.stages.episode import GraphitiEpisodeStage


@dataclass
class _WorkflowSubmission:
    submission_id: str
    event: EventCandidateDTO
    evidence_ids: list[str]


def _historical(event: PublishedEvent) -> HistoricalEvent:
    return HistoricalEvent(
        id=event.id,
        event=EventCandidateDTO.model_validate(event.event.model_dump(mode="json")),
    )


def _published(event: HistoricalEvent) -> PublishedEvent:
    return PublishedEvent(id=event.id, event=event.event.model_dump(mode="json"))


class LocalEventWorkflowRuntime:
    """Expose native Graphiti ingestion/search and the existing Data contract only."""

    def __init__(self, graphiti: Any, data: DataEventClient) -> None:
        self._graphiti = graphiti
        self._data = data
        self._history = CompositeEventHistory(graphiti, data)
        self._episode_stage = GraphitiEpisodeStage(graphiti)
        self._candidate_retriever = GraphitiCandidateRetriever(graphiti)
        self._signal_projector = GraphitiSignalFactProjector(graphiti)

    async def retrieve_history(self, candidate: EventCandidateSubmission) -> list[HistoricalEvent]:
        request = EventCandidateRequest.model_validate(candidate.model_dump(mode="json"))
        return await self._history.retrieve(request.event)

    async def publish(
        self,
        candidate: EventCandidateSubmission,
        candidate_key: str,
        resolution: EventResolutionRecord,
        *,
        existing: EventPublicationRecord | None,
        checkpoint: PublicationCheckpoint,
    ) -> EventPublicationRecord:
        """Publish an already-resolved Candidate and checkpoint each irreversible boundary."""

        if resolution.decision not in {"NEW_EVENT", "RELATED_BUT_DISTINCT"}:
            raise ValueError("only a publishable Event resolution may reach the Data runtime")
        decision = cast(Literal["NEW_EVENT", "RELATED_BUT_DISTINCT"], resolution.decision)
        request = EventCandidateRequest.model_validate(candidate.model_dump(mode="json"))
        historical = (
            _historical(existing.published_event)
            if existing is not None and existing.published_event is not None
            else None
        )
        if historical is None:
            if existing is None or not existing.publication_started:
                checkpoint(
                    EventPublicationRecord(
                        candidate_key=candidate_key,
                        decision=decision,
                        publication_started=True,
                        event_id=None,
                        event_created=False,
                        evidence_link_result="NOT_ATTEMPTED",
                        graph_projection_status="NOT_ATTEMPTED",
                        reason_codes=["PUBLICATION_STARTED", *resolution.reason_codes],
                        matched_event_ids=resolution.matched_event_ids,
                    )
                )
            historical = await self._data.publish(
                _WorkflowSubmission(
                    submission_id=f"evt-workflow-{candidate_key}",
                    event=request.event,
                    evidence_ids=request.evidence_ids,
                )
            )
            checkpoint(
                EventPublicationRecord(
                    candidate_key=candidate_key,
                    decision=decision,
                    publication_started=True,
                    event_id=historical.id,
                    event_created=True,
                    evidence_link_result="CREATED",
                    graph_projection_status="NOT_ATTEMPTED",
                    reason_codes=resolution.reason_codes,
                    matched_event_ids=resolution.matched_event_ids,
                    published_event=_published(historical),
                )
            )

        episode_uuid = await self._episode_stage.execute(historical)
        return EventPublicationRecord(
            candidate_key=candidate_key,
            decision=decision,
            publication_started=True,
            event_id=historical.id,
            event_created=True,
            evidence_link_result="CREATED",
            graph_projection_status="SUCCEEDED",
            reason_codes=resolution.reason_codes,
            matched_event_ids=resolution.matched_event_ids,
            episode_uuid=episode_uuid,
            published_event=_published(historical),
        )

    async def retrieve_signal_candidates(
        self,
        event: EventAnalysisInput,
        classification: EventClassification,
    ) -> CandidateSet:
        return await self._candidate_retriever.retrieve(event, classification)

    async def project_signal(
        self,
        event: EventAnalysisInput,
        classification: EventClassification,
        variable: VariableCandidate,
        anchor: AnchorCandidate,
        proposal: SignalProposal,
    ) -> str:
        return await self._signal_projector.project(event, classification, variable, anchor, proposal)

    async def close(self) -> None:
        await self._graphiti.close()


def create_local_event_workflow_runtime(model: Any) -> LocalEventWorkflowRuntime:
    """Compose the app-owned runtime from environment and Graphiti-native model plumbing."""

    base_url = os.getenv("DATA_SERVICE_BASE_URL", "http://data:9011")
    service_token = os.getenv("DATA_SERVICE_TOKEN", "").strip()
    if not service_token:
        raise ValueError("DATA_SERVICE_TOKEN must be configured")
    graphiti = create_agentos_graphiti(model)
    data = DataEventClient(base_url, service_token)
    return LocalEventWorkflowRuntime(graphiti, data)
