"""Read-only, group-scoped Event/storyline records for a fixed creation window."""

import os
from datetime import datetime
from typing import Any

from neo4j import AsyncGraphDatabase

from sematica.projection.runtime import GRAPHITI_GROUP_ID

# A cap rejects the entire selection rather than silently dropping stories/events.
MAX_ASSOCIATIONS = 10_000
WINDOW_QUERY = """
MATCH (e:Episodic {group_id:$group})-[:MENTIONS]->(g:Entity:GeopoliticRivalry {group_id:$group})
WHERE e.episode_kind='EVENT' AND e.created_at >= datetime($start)
  AND e.created_at < datetime($end)
RETURN DISTINCT g {.uuid, .data_object_id, .name, .core_proposition} AS story,
       e {.uuid, .domain_object_id, .content, .created_at, .valid_at} AS event
ORDER BY story.data_object_id, event.domain_object_id
LIMIT $limit
"""


async def load_geopolitical_event_window(start: datetime, end: datetime) -> list[dict[str, Any]]:
    async with AsyncGraphDatabase.driver(
        os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"])
    ) as driver:
        async with driver.session(default_access_mode="READ") as session:

            async def read(tx):  # type: ignore[no-untyped-def]
                result = await tx.run(
                    WINDOW_QUERY,
                    group=GRAPHITI_GROUP_ID,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    limit=MAX_ASSOCIATIONS + 1,
                )
                return [dict(record) async for record in result]

            rows = await session.execute_read(read)
    if len(rows) > MAX_ASSOCIATIONS:
        raise ValueError("Geopolitical Event window exceeds association capacity")
    return rows
