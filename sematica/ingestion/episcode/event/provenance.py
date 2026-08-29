"""Stable Graphiti provenance contract for formal Data Events."""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid5

EVENT_SOURCE_DESCRIPTION = "Published canonical Event from Data Service"
PENDING_EVENT_SOURCE_DESCRIPTION = "Pending canonical Event native projection"


def event_episode_uuid(event_id: str) -> str:
    """Return the durable Graphiti identity for one formal Data Event."""

    return str(uuid5(NAMESPACE_URL, f"urn:tidewise:event-episode:{event_id}"))


def formal_event_id_from_content(content: str) -> str | None:
    """Read formal Event identity from Graphiti's native Episode content."""

    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    event_id = payload.get("id") if isinstance(payload, dict) else None
    return event_id if isinstance(event_id, str) and event_id else None
