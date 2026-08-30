"""Concrete Data, Graphiti, and LLM adapters hidden by Event resolution."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Literal

import httpx
from graphiti_core import Graphiti
from graphiti_core.prompts.models import Message
from graphiti_core.search.search_filters import SearchFilters
from pydantic import BaseModel, ConfigDict, field_validator

from sematica.ingestion.episcode.event.contracts import (
    AtomicityAssessment,
    EventCandidateDTO,
    HistoricalEvent,
    PairComparison,
)
from sematica.ingestion.episcode.event.provenance import EVENT_SOURCE_DESCRIPTION
from sematica.projection.runtime import GRAPHITI_GROUP_ID

EVENTS_PATH = "/api/data/v1/events"
MAX_DATA_HISTORY = 1_000
MAX_RESOLUTION_CANDIDATES = 30
EVENT_ID_PATTERN = re.compile(r"^EVT[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class EventHistoryUnavailable(RuntimeError):
    """The authoritative Data Event history could not be recalled safely."""


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


class DataEventPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    items: list[DataEventDTO]
    total: int
    page: int
    page_size: int


class DataEventPageEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    result: DataEventPage


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

    async def ready(self) -> bool:
        """Validate connectivity and the authoritative Event page contract."""

        params: dict[str, str | int] = {"page": 1, "page_size": 1, "status": "ACTIVE"}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                headers=self._headers,
                transport=self._transport,
            ) as client:
                response = await client.get(self._url, params=params)
                response.raise_for_status()
                DataEventPageEnvelope.model_validate(response.json())
        except Exception:
            return False
        return True

    async def list_candidates(self, candidate: EventCandidateDTO) -> list[HistoricalEvent]:
        anchor = candidate.occurred_at or candidate.announced_at or candidate.semantic.effective_at
        assert anchor is not None
        lower, upper = anchor - timedelta(days=30), anchor + timedelta(days=30)
        params: dict[str, str | int] = {"page": 1, "page_size": 100, "status": "ACTIVE"}
        result: list[HistoricalEvent] = []
        async with httpx.AsyncClient(
            timeout=self._timeout,
            headers=self._headers,
            transport=self._transport,
        ) as client:
            while True:
                response = await client.get(self._url, params=params)
                response.raise_for_status()
                page = DataEventPageEnvelope.model_validate(response.json()).result
                if page.total > MAX_DATA_HISTORY:
                    raise RuntimeError("Event history window exceeds the safe retrieval bound")
                result.extend(item.historical() for item in page.items)
                if page.page * page.page_size >= page.total:
                    break
                params["page"] = page.page + 1
        return [
            event
            for event in result
            if (
                event_anchor := event.event.occurred_at or event.event.announced_at or event.event.semantic.effective_at
            )
            and lower <= event_anchor <= upper
        ]

    async def publish(self, submission) -> HistoricalEvent:
        payload = {
            "publication_key": f"{submission.submission_id}:create",
            "event": submission.event.model_dump(mode="json"),
            "evidence_ids": sorted(submission.evidence_ids),
        }
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers, transport=self._transport) as client:
            response = await client.post(self._url, json=payload)
        if 400 <= response.status_code < 500:
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
    anchor = candidate.occurred_at or candidate.announced_at or candidate.semantic.effective_at
    other = event.occurred_at or event.announced_at or event.semantic.effective_at
    distance = abs((anchor - other).total_seconds()) if anchor and other else float("inf")
    return (-int(stage_match), -actor_overlap, -object_overlap, -int(action_match), distance, historical.id)


class CompositeEventHistory:
    def __init__(self, graphiti: Graphiti, data: DataEventClient):
        self._graphiti, self._data = graphiti, data

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
        try:
            search_interface = self._graphiti.driver.search_interface
            if search_interface is not None:
                episodes = await search_interface.episode_fulltext_search(
                    self._graphiti.driver,
                    query,
                    SearchFilters(),
                    [GRAPHITI_GROUP_ID],
                    MAX_RESOLUTION_CANDIDATES,
                )
                for episode in episodes:
                    if event := _historical_from_content(episode.content):
                        result[event.id] = event
        except Exception:
            pass

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
            for record in records:
                if event := _historical_from_content(str(record["content"])):
                    result[event.id] = event
        except Exception:
            pass

        try:
            for event in await self._data.list_candidates(candidate):
                result[event.id] = event
        except Exception as exc:
            raise EventHistoryUnavailable("authoritative Data Event history retrieval failed") from exc
        return sorted(result.values(), key=lambda item: _identity_rank(candidate, item))[:MAX_RESOLUTION_CANDIDATES]


class GraphitiLLMComparator:
    def __init__(self, graphiti: Graphiti):
        self._client = graphiti.clients.llm_client

    async def assess_atomicity(self, candidate: EventCandidateDTO) -> AtomicityAssessment:
        messages = [
            Message(
                role="system",
                content=(
                    "Assess whether one investment Event record describes exactly one independently timed "
                    "real-world action. A compound announcement plus implementation, or unrelated actions "
                    "joined together, is not atomic. Multiple objects are allowed only when they are direct "
                    "objects of the same action at the same stage and time. Return only the structured result."
                ),
            ),
            Message(
                role="user",
                content=json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
            ),
        ]
        response = await self._client.generate_response(
            messages,
            response_model=AtomicityAssessment,
            group_id=GRAPHITI_GROUP_ID,
            prompt_name="tidewise_event_atomicity_v1",
        )
        return AtomicityAssessment.model_validate(response)

    async def compare(self, candidate: EventCandidateDTO, historical: HistoricalEvent) -> PairComparison:
        messages = [
            Message(
                role="system",
                content=(
                    "Compare two investment-research Event records. SAME_EVENT only when actor, one real-world action, "
                    "direct object, stage, and occurrence time identify the same occurrence. "
                    "Announcement, effectiveness, implementation, update, suspension, and termination are distinct. "
                    "Return only the structured result."
                ),
            ),
            Message(
                role="user",
                content=json.dumps(
                    {"candidate": candidate.model_dump(mode="json"), "historical": historical.model_dump(mode="json")},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        ]
        response = await self._client.generate_response(
            messages,
            response_model=PairComparison,
            group_id=GRAPHITI_GROUP_ID,
            prompt_name="tidewise_event_identity_v1",
        )
        return PairComparison.model_validate(response)
