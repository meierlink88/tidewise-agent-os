"""Deterministic selection: one authoritative storyline per set of new Events."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from capabilities.geopolitical_research.internal.models import MAX_STORIES, ResearchPlan, ResearchRequest, ResearchStory
from sematica.graphiti.geopolitical_research import load_geopolitical_event_window


def request(value: Any) -> ResearchRequest:
    if isinstance(value, str):
        if value.strip().startswith("{"):
            value = json.loads(value)
        else:
            value = {}  # Legacy Schedule prose cannot override the fixed 24-hour contract.
    if isinstance(value, dict):
        value = dict(value)
        # Existing Schedule seeds are operator-owned; accept their old envelope while
        # ignoring old reasoning controls. All new/unknown fields remain strict.
        for key in ("question", "event_window_hours", "include_company"):
            value.pop(key, None)
    return ResearchRequest.model_validate(value or {})


def select_stories(rows: list[dict[str, Any]], start: datetime, end: datetime) -> list[ResearchStory]:
    stories: dict[str, ResearchStory] = {}
    identities: dict[str, str] = {}
    events: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        graph_story, graph_event = row["story"], row["event"]
        sid, eid = graph_story["data_object_id"], graph_event["domain_object_id"]
        created = datetime.fromisoformat(str(graph_event["created_at"]).replace("Z", "+00:00"))
        if created.tzinfo is None or not start <= created < end:
            raise ValueError("Event is outside the frozen creation window")
        content = json.loads(graph_event["content"])
        if not isinstance(eid, str) or not eid or content.get("id") != eid:
            raise ValueError("Event identity/content mismatch")
        event = {**content, "created_at": created.isoformat(), "valid_at": str(graph_event.get("valid_at") or "")}
        if sid in identities and identities[sid] != graph_story["uuid"]:
            raise ValueError("Ambiguous geopolitical storyline identity")
        identities[sid] = graph_story["uuid"]
        previous = events.get((sid, eid))
        if previous is not None and previous != event:
            raise ValueError("Conflicting Event content")
        events[(sid, eid)] = event
        if sid not in stories:
            stories[sid] = ResearchStory(
                story_id=sid,
                name=graph_story["name"],
                core_proposition=graph_story.get("core_proposition") or "",
                events=[event],
            )
    for sid, story in stories.items():
        story.events = [event for (story_id, _), event in sorted(events.items()) if story_id == sid]
    if len(stories) > MAX_STORIES:
        raise ValueError("Storyline count exceeds Workflow Loop capacity")
    return [stories[sid] for sid in sorted(stories)]


async def prepare_plan(run_id: str, value: Any) -> ResearchPlan:
    parsed = request(value)
    cutoff = parsed.cutoff_at or datetime.now(UTC)
    if cutoff > datetime.now(UTC):
        raise ValueError("Research cutoff cannot be in the future")
    start = cutoff - timedelta(hours=24)
    rows = await load_geopolitical_event_window(start, cutoff)
    return ResearchPlan(
        workflow_run_id=run_id,
        start_at=start,
        cutoff_at=cutoff,
        market=parsed.market,
        stories=select_stories(rows, start, cutoff),
    )
