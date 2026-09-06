"""Atomic formal Event projection to selected existing entities; no LLM extraction."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sematica.ingestion.episcode.event.contracts import HistoricalEvent, event_time_anchor
from sematica.ingestion.episcode.event.provenance import EVENT_SOURCE_DESCRIPTION, event_episode_uuid
from sematica.projection.runtime import GRAPHITI_GROUP_ID


class SelectedEventEpisodeStage:
    def __init__(self, graphiti: Any):
        self._driver = graphiti.driver

    async def execute(self, historical: HistoricalEvent, associations: list[dict[str, str]]) -> str:
        episode_uuid = event_episode_uuid(historical.id)
        content = json.dumps(
            {"id": historical.id, **historical.event.model_dump(mode="json"), "status": "ACTIVE"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        selected = sorted(associations, key=lambda item: item["uuid"])
        if len({item["uuid"] for item in selected}) != len(selected):
            raise ValueError("duplicate selected entity")
        allowed = {"GeopoliticRivalry", "MacroEconomic", "IndustryChain", "ChainNode", "Company"}
        if any(item["entity_type"] not in allowed or not item["business_id"] for item in selected):
            raise ValueError("invalid selected entity identity")
        digest = hashlib.sha256(json.dumps(selected, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        rows = [
            {**item, "mention_uuid": str(uuid5(NAMESPACE_URL, f"{episode_uuid}:mentions:{item['uuid']}"))}
            for item in selected
        ]
        records, _, _ = await self._driver.execute_query(
            """
            /* event_selected_projection_atomic */
            WITH $selected AS selected
            WHERE all(item IN selected WHERE EXISTS {
                MATCH (n:Entity {uuid: item.uuid, group_id: $group_id})
                WHERE item.entity_type IN labels(n) AND n.data_object_id = item.business_id
            })
            MERGE (e:Episodic {uuid: $episode_uuid, group_id: $group_id})
            ON CREATE SET e.content = $content, e.association_digest = $digest,
                          e.created_at = $created_at, e.entity_edges = [], e.source = 'json',
                          e.valid_at = $valid_at, e.name = $title,
                          e.source_description = $source_description,
                          e.episode_kind = 'EVENT', e.domain_object_id = $event_id
            WITH e, selected
            WHERE e.content = $content AND e.association_digest = $digest
              AND e.domain_object_id = $event_id AND e.source_description = $source_description
            CALL (e, selected) {
                UNWIND selected AS item
                MATCH (n:Entity {uuid: item.uuid, group_id: $group_id})
                MERGE (e)-[m:MENTIONS {uuid: item.mention_uuid}]->(n)
                ON CREATE SET m.group_id = $group_id, m.created_at = $created_at,
                              m.association_reason = item.reason
                RETURN count(m) AS mentions
            }
            RETURN e.uuid AS uuid, mentions
            """,
            selected=rows,
            episode_uuid=episode_uuid,
            group_id=GRAPHITI_GROUP_ID,
            content=content,
            digest=digest,
            created_at=datetime.now(UTC),
            valid_at=event_time_anchor(historical.event.semantic.time),
            title=historical.event.title,
            source_description=EVENT_SOURCE_DESCRIPTION,
            event_id=historical.id,
        )
        if len(records) != 1 or records[0]["uuid"] != episode_uuid or records[0]["mentions"] != len(selected):
            raise ValueError("selected Event projection failed identity or replay validation")
        return episode_uuid
