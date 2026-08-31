"""Internal native Graphiti Episode stage for formal Tidewise Events."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType, EpisodicNode
from pydantic import BaseModel

from sematica.ingestion.episcode.event.contracts import HistoricalEvent, event_time_anchor
from sematica.ingestion.episcode.event.provenance import (
    EVENT_SOURCE_DESCRIPTION,
    PENDING_EVENT_SOURCE_DESCRIPTION,
    event_episode_uuid,
    formal_event_id_from_content,
)
from sematica.ontology import ENTITY_TYPES
from sematica.projection.runtime import GRAPHITI_GROUP_ID

EVENT_EPISODE_KIND = "EVENT"
EVENT_ENTITY_TYPE_NAMES = (
    "Country",
    "Region",
    "Organization",
    "Industry",
    "Concept",
    "IndustryChain",
    "ChainNode",
)


def event_entity_types() -> dict[str, type[BaseModel]]:
    """Return a fresh approved extraction registry for one Event Episode."""

    return {name: ENTITY_TYPES[name] for name in EVENT_ENTITY_TYPE_NAMES}


EXTRACTION_INSTRUCTIONS = """
The JSON is one canonical investment Event published by Tidewise Data. Extract entities and factual
relationships explicitly supported by this Event. Reuse existing entities when they resolve to the
same real-world identity; otherwise Graphiti may create a contextual Entity. Never invent a
data_object_id or promote a contextual Entity to an authoritative Tidewise identity. Region means a
reviewed global or cross-country region, not a province or city. Organization means an international
alliance or multilateral organization, not a company, issuer, media company or government department.
Do not turn forecasts, investment impacts, Variables, Signals, Storylines or inferred causal effects
into Event facts.
""".strip()

FIND_EVENT = """
/* graphiti_event_projection_identity */
MATCH (episode:Episodic {group_id: $group_id})
WHERE episode.uuid = $episode_uuid
OPTIONAL MATCH (episode)-[mention:MENTIONS]->()
RETURN episode.uuid AS uuid, episode.name AS name, episode.content AS content,
       episode.source_description AS source_description,
       episode.episode_kind AS episode_kind,
       episode.domain_object_id AS domain_object_id,
       count(mention) AS mention_count
LIMIT 1
""".strip()

MARK_EVENT = """
/* graphiti_native_event_metadata */
MATCH (episode:Episodic {uuid: $episode_uuid, group_id: $group_id})
WHERE episode.content = $content
SET episode.name = $title,
    episode.source_description = $source_description,
    episode.episode_kind = 'EVENT',
    episode.domain_object_id = $event_id
RETURN episode.uuid AS uuid, episode.episode_kind AS episode_kind,
       episode.domain_object_id AS domain_object_id
""".strip()

ISOLATE_CONTEXTUAL_ENTITIES = """
/* graphiti_event_contextual_entity_isolation */
MATCH (episode:Episodic {uuid: $episode_uuid, group_id: $group_id})
      -[:MENTIONS]->(entity:Entity {group_id: $group_id})
WHERE coalesce(trim(toString(entity.data_object_id)), '') = ''
  AND coalesce(trim(toString(entity.demo_catalog_key)), '') = ''
  AND coalesce(trim(toString(entity.policy_key)), '') = ''
  AND any(label IN labels(entity) WHERE label IN $controlled_labels)
SET entity:ContextualEntity,
    entity.contextual_entity_type = head(
        [label IN labels(entity) WHERE label IN $controlled_labels]
    )
REMOVE entity:Country:Region:Concept:IndustryChain:ChainNode
RETURN count(entity) AS isolated_count
""".strip()

CONTROLLED_EVENT_ANCHOR_LABELS = ["Country", "Region", "Concept", "IndustryChain", "ChainNode"]


class GraphitiEpisodeStage:
    """Internal Pipeline stage; never a standalone Event publication interface."""

    def __init__(self, graphiti: Graphiti):
        self._graphiti = graphiti

    async def execute(self, historical: HistoricalEvent) -> str:
        episode_uuid = event_episode_uuid(historical.id)
        content = json.dumps(
            {
                "id": historical.id,
                **historical.event.model_dump(mode="json"),
                "status": "ACTIVE",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        records, _, _ = await self._graphiti.driver.execute_query(
            FIND_EVENT,
            episode_uuid=episode_uuid,
            group_id=GRAPHITI_GROUP_ID,
            routing_="r",
        )
        if len(records) > 1:
            raise RuntimeError("multiple Graphiti Episodes share one Event identity")
        projected_uuid = episode_uuid
        native_projection_required = True
        if records:
            row = records[0]
            projected_uuid = str(row["uuid"])
            if projected_uuid != episode_uuid:
                raise RuntimeError("Graphiti Event Episode has a non-deterministic identity")
            if row["content"] != content:
                raise RuntimeError("Graphiti Event Episode conflicts with Data Event")
            if formal_event_id_from_content(str(row["content"])) != historical.id:
                raise RuntimeError("Graphiti Event Episode has a conflicting domain identity")
            source_description = str(row["source_description"])
            if source_description not in {
                EVENT_SOURCE_DESCRIPTION,
                PENDING_EVENT_SOURCE_DESCRIPTION,
            }:
                raise RuntimeError("Graphiti Event Episode has a conflicting source identity")
            # Graphiti persists Episode, MENTIONS, entities and ordinary Facts in
            # one bulk write. MENTIONS on a pending Episode therefore proves the
            # native write completed even if its acknowledgement was lost before
            # MARK_EVENT could finalize Tidewise metadata.
            if source_description == EVENT_SOURCE_DESCRIPTION or int(row["mention_count"]) > 0:
                native_projection_required = False

        valid_at = event_time_anchor(historical.event.semantic.time)
        assert valid_at is not None
        if native_projection_required:
            if not records:
                await EpisodicNode(
                    uuid=projected_uuid,
                    name=historical.event.title,
                    group_id=GRAPHITI_GROUP_ID,
                    labels=[],
                    source=EpisodeType.json,
                    content=content,
                    source_description=PENDING_EVENT_SOURCE_DESCRIPTION,
                    created_at=datetime.now(UTC),
                    valid_at=valid_at,
                    entity_edges=[],
                ).save(self._graphiti.driver)
            result = await self._graphiti.add_episode(
                name=historical.event.title,
                episode_body=content,
                source_description=EVENT_SOURCE_DESCRIPTION,
                reference_time=valid_at,
                source=EpisodeType.json,
                group_id=GRAPHITI_GROUP_ID,
                uuid=projected_uuid,
                update_communities=False,
                entity_types=event_entity_types(),
                custom_extraction_instructions=EXTRACTION_INSTRUCTIONS,
            )
            if result.episode.uuid != projected_uuid:
                raise RuntimeError("Graphiti returned an unexpected Event Episode identity")

        # Native extraction is allowed to create contextual entities, but only
        # catalog-backed entities may retain labels used as controlled Anchors.
        # Facts and MENTIONS remain connected to the generic Entity node.
        await self._graphiti.driver.execute_query(
            ISOLATE_CONTEXTUAL_ENTITIES,
            episode_uuid=projected_uuid,
            group_id=GRAPHITI_GROUP_ID,
            controlled_labels=CONTROLLED_EVENT_ANCHOR_LABELS,
        )

        written, _, _ = await self._graphiti.driver.execute_query(
            MARK_EVENT,
            episode_uuid=projected_uuid,
            group_id=GRAPHITI_GROUP_ID,
            event_id=historical.id,
            title=historical.event.title,
            content=content,
            source_description=EVENT_SOURCE_DESCRIPTION,
        )
        if (
            len(written) != 1
            or str(written[0]["uuid"]) != projected_uuid
            or written[0]["episode_kind"] != EVENT_EPISODE_KIND
            or written[0]["domain_object_id"] != historical.id
        ):
            raise RuntimeError("Graphiti Event Episode metadata was not persisted")
        return projected_uuid

    async def ready(self) -> bool:
        records, _, _ = await self._graphiti.driver.execute_query(
            "RETURN 1 AS ready",
            routing_="r",
        )
        return bool(records and records[0]["ready"] == 1)

    async def close(self) -> None:
        await self._graphiti.close()
