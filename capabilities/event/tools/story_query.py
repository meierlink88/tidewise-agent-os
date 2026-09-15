"""Public read-only tool boundary for research consumers."""

import os
from typing import Any

from capabilities.event.internal.story_query import query_events, query_evidence


def query_story_events(
    story_id: str,
    research_date: str = "",
    after_event_id: str = "",
    limit: int = 100,
    user_id: str | None = None,
    event_window_start: str = "",
    event_window_end: str = "",
) -> dict[str, Any]:
    """Read live Events linked to GPR story_id within [event_window_start, event_window_end).

    Supply both timezone-aware ISO timestamps, or legacy research_date (YYYY-MM-DD,
    Asia/Shanghai), never both modes. Time selects Event.created_at, not occurrence.

    Includes associated variable signals and Evidence IDs. No historical expansion or snapshot.
    Call with next_after_event_id until null. Default limit=100; server fits complete Events
    and Signals within 28,000 characters.
    Signals missing source Events are marked unusable; publication/provenance gaps are explicit.
    """
    _authorize(user_id)
    return query_events(
        story_id,
        research_date,
        after_event_id,
        limit,
        event_window_start=event_window_start,
        event_window_end=event_window_end,
    )


def get_story_evidence(
    story_id: str,
    research_date: str = "",
    event_id: str = "",
    evidence_id: str = "",
    user_id: str | None = None,
    event_window_start: str = "",
    event_window_end: str = "",
) -> dict[str, Any]:
    """Read atomic Evidence in the same storyline/window or legacy day; not full article text."""
    _authorize(user_id)
    return query_evidence(
        story_id,
        research_date,
        event_id,
        evidence_id,
        event_window_start=event_window_start,
        event_window_end=event_window_end,
    )


def _authorize(user_id: str | None) -> None:
    # Agno injects verified user_id and removes it from the MCP schema.
    if os.getenv("RUNTIME_ENV", "prd") != "dev" and not user_id:
        raise PermissionError("Authenticated AgentOS identity required")
