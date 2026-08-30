"""Concrete Data and Graphiti adapters hidden by Event resolution."""

from __future__ import annotations

import json
import re
from typing import Literal

import httpx
from graphiti_core import Graphiti
from graphiti_core.search.search_filters import SearchFilters
from pydantic import BaseModel, ConfigDict, field_validator

from sematica.ingestion.episcode.event.contracts import (
    EventCandidateDTO,
    HistoricalEvent,
)
from sematica.ingestion.episcode.event.provenance import EVENT_SOURCE_DESCRIPTION
from sematica.projection.runtime import GRAPHITI_GROUP_ID

EVENTS_PATH = "/api/data/v1/events"
MAX_RESOLUTION_CANDIDATES = 30
PERMANENT_PUBLICATION_REJECTION_STATUSES = frozenset({400, 409, 422})
EVENT_ID_PATTERN = re.compile(r"^EVT[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class EventRecallUnavailable(RuntimeError):
    """The complete Graphiti Event recall set could not be loaded safely."""


class PublicationRejected(RuntimeError):
    """Data rejected a publication with a permanent 4xx contract response."""


class DataEventDTO(EventCandidateDTO):
    id: str
    status: Literal["ACTIVE", "DEPRECATED", "ARCHIVED"]

    @field_validator("id")
    @classmethod
    def id_is_a_formal_data_identity(cls, value: str) -> str:
        if EVENT_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("id must be a formal Data Event identity")
        return value

    def historical(self) -> HistoricalEvent:
        return HistoricalEvent(
            id=self.id, event=EventCandidateDTO.model_validate(self.model_dump(exclude={"id", "status"}))
        )


class DataEventPublicationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event: DataEventDTO
    evidence_link_ids: list[str]
    receipt_id: str
    payload_hash: str
    replayed: bool


class DataEventPublicationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    result: DataEventPublicationResult


class DataEventClient:
    def __init__(
        self,
        base_url: str,
        service_token: str,
        *,
        timeout_seconds: float = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._url = f"{base_url.rstrip('/')}{EVENTS_PATH}"
        self._headers = {"Authorization": f"Bearer {service_token}"}
        self._timeout = timeout_seconds
        self._transport = transport

    async def publish(self, submission) -> HistoricalEvent:
        payload = {
            "publication_key": f"{submission.submission_id}:create",
            "event": submission.event.model_dump(mode="json"),
            "evidence_ids": sorted(submission.evidence_ids),
        }
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers, transport=self._transport) as client:
            response = await client.post(self._url, json=payload)
        if response.status_code in PERMANENT_PUBLICATION_REJECTION_STATUSES:
            raise PublicationRejected(f"Data rejected Event publication with HTTP {response.status_code}")
        response.raise_for_status()
        envelope = DataEventPublicationEnvelope.model_validate(response.json())
        return envelope.result.event.historical()


ANCHOR_EVENTS = """
/* event_candidate_anchor_history */
UNWIND $mentions AS mention
MATCH (target:Entity {group_id: $group_id})
WHERE target.data_object_id IS NOT NULL
  AND (target.name = mention OR target.code = mention OR mention IN coalesce(target.aliases, []))
WITH collect(DISTINCT target.uuid) AS target_uuids
MATCH (episode:Episodic {group_id: $group_id})-[:MENTIONS]->(target:Entity)
WHERE target.uuid IN target_uuids
  AND episode.source_description = $source_description
RETURN DISTINCT episode.content AS content
LIMIT $limit
""".strip()


def _historical_from_content(content: str, *, expected_event_id: str | None = None) -> HistoricalEvent | None:
    try:
        event = DataEventDTO.model_validate(json.loads(content)).historical()
    except (ValueError, TypeError):
        return None
    if expected_event_id is not None and event.id != expected_event_id:
        return None
    return event


def _identity_rank(candidate: EventCandidateDTO, historical: HistoricalEvent) -> tuple:
    event = historical.event
    actors = {value.casefold() for value in candidate.semantic.actors}
    objects = {value.casefold() for value in candidate.semantic.objects}
    actor_overlap = len(actors & {value.casefold() for value in event.semantic.actors})
    object_overlap = len(objects & {value.casefold() for value in event.semantic.objects})
    action_match = candidate.semantic.action.casefold() == event.semantic.action.casefold()
    stage_match = candidate.semantic.stage == event.semantic.stage
    anchor = (
        candidate.semantic.time.occurred_at
        or candidate.semantic.time.announced_at
        or candidate.semantic.time.effective_at
    )
    other = event.semantic.time.occurred_at or event.semantic.time.announced_at or event.semantic.time.effective_at
    distance = abs((anchor - other).total_seconds()) if anchor and other else float("inf")
    return (-int(stage_match), -actor_overlap, -object_overlap, -int(action_match), distance, historical.id)


class GraphitiEventHistory:
    def __init__(self, graphiti: Graphiti):
        self._graphiti = graphiti

    async def retrieve(self, candidate: EventCandidateDTO) -> list[HistoricalEvent]:
        query = " ".join(
            [
                candidate.title,
                candidate.summary,
                *candidate.semantic.actors,
                candidate.semantic.action,
                *candidate.semantic.objects,
            ]
        )
        result: dict[str, HistoricalEvent] = {}
        search_ops = self._graphiti.driver.search_ops
        if search_ops is None:
            raise EventRecallUnavailable("Graphiti Event recall failed: full-text search is unavailable")
        try:
            episodes = await search_ops.episode_fulltext_search(
                self._graphiti.driver,
                query,
                SearchFilters(),
                [GRAPHITI_GROUP_ID],
                MAX_RESOLUTION_CANDIDATES,
            )
        except Exception as exc:
            raise EventRecallUnavailable("Graphiti Event recall failed: full-text query failed") from exc
        event_hits = 0
        malformed_event_hits = 0
        for episode in episodes:
            if getattr(episode, "source_description", None) != EVENT_SOURCE_DESCRIPTION:
                continue
            event_hits += 1
            content = getattr(episode, "content", None)
            if not isinstance(content, str):
                malformed_event_hits += 1
                continue
            if event := _historical_from_content(content):
                result[event.id] = event
            else:
                malformed_event_hits += 1

        try:
            records, _, _ = await self._graphiti.driver.execute_query(
                ANCHOR_EVENTS,
                mentions=[
                    *candidate.semantic.actors,
                    *candidate.semantic.objects,
                    *candidate.semantic.jurisdictions,
                ],
                group_id=GRAPHITI_GROUP_ID,
                source_description=EVENT_SOURCE_DESCRIPTION,
                limit=MAX_RESOLUTION_CANDIDATES,
                routing_="r",
            )
        except Exception as exc:
            raise EventRecallUnavailable("Graphiti Event recall failed: anchor query failed") from exc
        for record in records:
            event_hits += 1
            try:
                content = record["content"]
            except (KeyError, TypeError):
                malformed_event_hits += 1
                continue
            if not isinstance(content, str):
                malformed_event_hits += 1
                continue
            if event := _historical_from_content(content):
                result[event.id] = event
            else:
                malformed_event_hits += 1
        if event_hits > 0 and malformed_event_hits == event_hits:
            raise EventRecallUnavailable("Graphiti Event recall matched only malformed Event content")
        return sorted(result.values(), key=lambda item: _identity_rank(candidate, item))[:MAX_RESOLUTION_CANDIDATES]
