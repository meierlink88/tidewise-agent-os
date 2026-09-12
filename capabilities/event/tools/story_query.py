"""Public read-only tool boundary for research consumers."""

import os
from typing import Any

from capabilities.event.internal.story_query import query_events, query_evidence


def query_story_events(
    story_id: str, research_date: str, after_event_id: str = "", limit: int = 1, user_id: str | None = None
) -> dict[str, Any]:
    """Read live Events created on research_date (YYYY-MM-DD, Asia/Shanghai) linked to GPR story_id.

    Includes associated variable signals and Evidence IDs. No historical expansion or snapshot.
    Call with next_after_event_id until null. Server caps each page at one Event to avoid truncation.
    Signals missing source Events are marked unusable; publication/provenance gaps are explicit.
    """
    _authorize(user_id)
    return query_events(story_id, research_date, after_event_id, limit)


def get_story_evidence(
    story_id: str, research_date: str, event_id: str, evidence_id: str, user_id: str | None = None
) -> dict[str, Any]:
    """Read an Event's supporting atomic Evidence within the same storyline/day; not full article text."""
    _authorize(user_id)
    return query_evidence(story_id, research_date, event_id, evidence_id)


def _authorize(user_id: str | None) -> None:
    # Agno injects verified user_id and removes it from the MCP schema.
    if os.getenv("RUNTIME_ENV", "prd") != "dev" and not user_id:
        raise PermissionError("Authenticated AgentOS identity required")
