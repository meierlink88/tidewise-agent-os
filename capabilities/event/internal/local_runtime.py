"""Synchronous-in-Workflow Event runtime composed from Data and Graphiti SDKs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from agno.agent import Agent
from agno.run.agent import RunOutput

from capabilities.event.internal.models import (
    EventCandidateSubmission,
    EventExtractionBatch,
    EventPublicationRecord,
    EventSignalRecord,
    PublishedEvent,
)
from capabilities.event.internal.runtime import PublicationCheckpoint
from sematica.analysis.event.adapters import GraphitiEventAnalysisLLM
from sematica.analysis.event.contracts import EventAnalysisInput
from sematica.analysis.event.graphiti import GraphitiCandidateRetriever, GraphitiSignalFactProjector
from sematica.analysis.event.pipeline import EventAnalysisPipeline
from sematica.analysis.event.review import ControlledSignalReviewer
from sematica.graphiti.runtime import create_agentos_graphiti
from sematica.ingestion.episcode.event.adapters import (
    CompositeEventHistory,
    DataEventClient,
    GraphitiLLMComparator,
)
from sematica.ingestion.episcode.event.contracts import (
    EventCandidateDTO,
    EventCandidateRequest,
    HistoricalEvent,
)
from sematica.ingestion.episcode.event.resolver import EventResolver
from sematica.ingestion.episcode.event.stages.episode import GraphitiEpisodeStage


@dataclass
class _WorkflowSubmission:
    submission_id: str
    event: EventCandidateDTO
    evidence_ids: list[str]
    published_event: HistoricalEvent | None = None
    pending_decision: str | None = None
    publication_started: bool = False


def _historical(event: PublishedEvent) -> HistoricalEvent:
    return HistoricalEvent(
        id=event.id,
        event=EventCandidateDTO.model_validate(event.event.model_dump(mode="json")),
    )


def _published(event: HistoricalEvent) -> PublishedEvent:
    return PublishedEvent(
        id=event.id,
        event=event.event.model_dump(mode="json"),
    )


class LocalEventWorkflowRuntime:
    """Run resolution, publication, Graphiti and Signal stages in the Agno run."""

    def __init__(self, graphiti, data: DataEventClient, extractor: Agent) -> None:
        self._graphiti = graphiti
        self._data = data
        self._extractor = extractor
        self._resolver = EventResolver(
            CompositeEventHistory(graphiti, data),
            GraphitiLLMComparator(graphiti),
            data,
        )
        self._episode_stage = GraphitiEpisodeStage(graphiti)
        analysis_llm = GraphitiEventAnalysisLLM(graphiti)
        self._analysis = EventAnalysisPipeline(
            analysis_llm,
            GraphitiCandidateRetriever(graphiti),
            analysis_llm,
            ControlledSignalReviewer(),
            GraphitiSignalFactProjector(graphiti),
        )

    async def extract(self, batch: EventExtractionBatch) -> Any:
        """Run the registered Agno semantic Agent behind the extraction module."""

        result = await self._extractor.arun(batch, stream=False)
        if not isinstance(result, RunOutput):
            raise RuntimeError("Event Extractor returned a streaming response")
        return result.content

    async def publish(
        self,
        candidate: EventCandidateSubmission,
        candidate_key: str,
        *,
        existing: EventPublicationRecord | None,
        checkpoint: PublicationCheckpoint,
    ) -> EventPublicationRecord:
        request = EventCandidateRequest.model_validate(candidate.model_dump(mode="json"))
        submission = _WorkflowSubmission(
            submission_id=f"evt-workflow-{candidate_key}",
            event=request.event,
            evidence_ids=request.evidence_ids,
            published_event=(
                _historical(existing.published_event)
                if existing is not None and existing.published_event is not None
                else None
            ),
            pending_decision=existing.decision if existing is not None else None,
            publication_started=existing is not None and existing.publication_started,
        )

        def on_publication_started(decision: str) -> None:
            if decision not in {"NEW_EVENT", "RELATED_BUT_DISTINCT"}:
                raise ValueError(f"invalid publication decision: {decision}")
            publication_decision = cast(Literal["NEW_EVENT", "RELATED_BUT_DISTINCT"], decision)
            checkpoint(
                EventPublicationRecord(
                    candidate_key=candidate_key,
                    decision=publication_decision,
                    publication_started=True,
                    event_id=None,
                    event_created=False,
                    evidence_link_result="NOT_ATTEMPTED",
                    graph_projection_status="NOT_ATTEMPTED",
                    reason_codes=["PUBLICATION_STARTED"],
                    matched_event_ids=[],
                )
            )

        def on_published(outcome, historical: HistoricalEvent) -> None:
            checkpoint(
                EventPublicationRecord(
                    candidate_key=candidate_key,
                    decision=outcome.decision,
                    publication_started=True,
                    event_id=historical.id,
                    event_created=True,
                    evidence_link_result=outcome.evidence_link_result,
                    graph_projection_status="NOT_ATTEMPTED",
                    reason_codes=outcome.reason_codes,
                    matched_event_ids=outcome.matched_event_ids,
                    published_event=_published(historical),
                )
            )

        resolution = await self._resolver.resolve(
            submission,
            on_published=on_published,
            on_publication_started=on_publication_started,
        )
        outcome = resolution.outcome
        historical = resolution.published_event
        if historical is None:
            return EventPublicationRecord(
                candidate_key=candidate_key,
                decision=outcome.decision,
                publication_started=False,
                event_id=outcome.event_id,
                event_created=False,
                evidence_link_result=outcome.evidence_link_result,
                graph_projection_status=outcome.graph_projection_status,
                reason_codes=outcome.reason_codes,
                matched_event_ids=outcome.matched_event_ids,
            )
        episode_uuid = await self._episode_stage.execute(historical)
        return EventPublicationRecord(
            candidate_key=candidate_key,
            decision=outcome.decision,
            publication_started=True,
            event_id=historical.id,
            event_created=True,
            evidence_link_result=outcome.evidence_link_result,
            graph_projection_status="SUCCEEDED",
            reason_codes=outcome.reason_codes,
            matched_event_ids=outcome.matched_event_ids,
            episode_uuid=episode_uuid,
            published_event=_published(historical),
        )

    async def construct_signals(self, publication: EventPublicationRecord) -> EventSignalRecord:
        if publication.published_event is None or publication.episode_uuid is None:
            raise ValueError("Signal construction requires a projected formal Event")
        outcome = await self._analysis.analyze(
            EventAnalysisInput(
                event=_historical(publication.published_event),
                episode_uuid=publication.episode_uuid,
                reference_time=datetime.now(UTC),
            )
        )
        return EventSignalRecord(
            event_id=publication.published_event.id,
            status=outcome.status,
            signal_fact_uuids=outcome.signal_fact_uuids,
            reason_codes=outcome.reason_codes,
        )

    async def close(self) -> None:
        await self._graphiti.close()


def create_local_event_workflow_runtime(model: Any, extractor: Agent) -> LocalEventWorkflowRuntime:
    """Compose the app-owned runtime from environment and the registered Agno model."""

    base_url = os.getenv("DATA_SERVICE_BASE_URL", "http://data:9011")
    service_token = os.getenv("DATA_SERVICE_TOKEN", "").strip()
    if not service_token:
        raise ValueError("DATA_SERVICE_TOKEN must be configured")
    graphiti = create_agentos_graphiti(model)
    data = DataEventClient(base_url, service_token)
    return LocalEventWorkflowRuntime(graphiti, data, extractor)
