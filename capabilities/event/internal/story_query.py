"""Live, read-only storyline queries. No historical expansion or stored snapshots."""

import json
import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from neo4j import GraphDatabase

from sematica.projection.runtime import GRAPHITI_GROUP_ID

MATCH_EVENTS = """
MATCH (g:Entity:GeopoliticRivalry {group_id:$group, data_object_id:$story_id})
MATCH (e:Episodic {group_id:$group})-[:MENTIONS]->(g)
WHERE e.episode_kind='EVENT' AND e.created_at >= datetime($start)
  AND e.created_at < datetime($end)
"""


def query_window(story_id: str, research_date: str) -> dict[str, str]:
    if not story_id.startswith("GPR") or len(story_id) > 100:
        raise ValueError("story_id must be an authoritative GPR business ID")
    day = date.fromisoformat(research_date)
    if day.isoformat() != research_date or day > datetime.now(ZoneInfo("Asia/Shanghai")).date():
        raise ValueError("research_date must be YYYY-MM-DD and not in the future")
    start = datetime.combine(day, time.min, ZoneInfo("Asia/Shanghai"))
    return {
        "story_id": story_id,
        "research_date": research_date,
        "timezone": "Asia/Shanghai",
        "selection_time_field": "created_at",
        "start": start.isoformat(),
        "end": (start + timedelta(days=1)).isoformat(),
    }


def _read(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with GraphDatabase.driver(
        os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"])
    ) as driver:
        with driver.session(default_access_mode="READ") as session:
            rows = session.execute_read(lambda tx: [dict(row) for row in tx.run(query, **params)])
    return json.loads(json.dumps(rows, default=str))


def _params(story_id: str, research_date: str) -> dict[str, Any]:
    return {**query_window(story_id, research_date), "group": GRAPHITI_GROUP_ID}


def _story(params: dict[str, Any]) -> dict[str, Any]:
    rows = _read(
        """MATCH (g:Entity:GeopoliticRivalry {group_id:$group, data_object_id:$story_id})
        RETURN g {.data_object_id, .uuid, .name, .core_proposition} AS story""",
        params,
    )
    if len(rows) != 1:
        raise ValueError("storyline not found or identity is ambiguous")
    return rows[0]["story"]


def _provenance(event_ids: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(os.getenv("EVENT_ARTIFACT_ROOT", "data/event"))
    if not root.is_dir():
        raise ValueError("Event journal root unavailable")
    refs: dict[str, Any] = {eid: {"evidence_ids": [], "publication_complete": None} for eid in event_ids}
    evidence: dict[str, Any] = {}
    for sub in ("batches", ".pending"):
        for path in sorted((root / sub).glob("*/storyline_journal.json")):
            journal = json.loads(path.read_text())
            matches = [
                c
                for c in journal.get("candidates", {}).values()
                if (c.get("publication") or {}).get("event_id") in event_ids
            ]
            if not matches:
                continue
            inputs = json.loads((path.parent / "input.json").read_text())
            wanted = set()
            for c in matches:
                ref = refs[c["publication"]["event_id"]]
                complete = bool(c.get("done"))
                ref["publication_complete"] = (
                    complete if ref["publication_complete"] is None else (ref["publication_complete"] and complete)
                )
                ids = c["identity_request"]["candidate"]["evidence_ids"]
                wanted.update(ids)
                ref["evidence_ids"] = sorted(set(ref["evidence_ids"]) | set(ids))
            for item in inputs["evidences"]:
                if item["id"] in wanted:
                    # Evidence is semantic source material, not a promise of full article text.
                    evidence[item["id"]] = {
                        k: v
                        for k, v in item.items()
                        if k
                        in {
                            "id",
                            "title",
                            "summary",
                            "semantic",
                            "published_at",
                            "collected_at",
                            "raw_evidence_id",
                            "url",
                            "source_url",
                        }
                    }
    for ref in refs.values():
        ref["missing_evidence_ids"] = sorted(set(ref["evidence_ids"]) - evidence.keys())
        ref["provenance_status"] = "available" if ref["evidence_ids"] and not ref["missing_evidence_ids"] else "missing"
    return refs, evidence


def query_events(story_id: str, research_date: str, after_event_id: str = "", limit: int = 1) -> dict[str, Any]:
    if isinstance(limit, bool) or not 1 <= limit <= 5:
        raise ValueError("limit must be between 1 and 5; use 1 to avoid tool truncation")
    # Research workers impose a 10k-character result limit. Bound server pages
    # regardless of the model requesting larger batches; preserve the cursor.
    limit = 1
    params = _params(story_id, research_date)
    story = _story(params)
    counts = _read(MATCH_EVENTS + " RETURN count(DISTINCT e) AS total", params)
    rows = _read(
        MATCH_EVENTS
        + """ AND e.domain_object_id > $after
        RETURN DISTINCT properties(e) AS event ORDER BY event.domain_object_id LIMIT $limit""",
        {**params, "after": after_event_id, "limit": limit + 1},
    )
    selected = rows[:limit]
    ids = {row["event"]["domain_object_id"] for row in selected}
    refs, _ = _provenance(ids) if ids else ({}, {})
    # Source closure is tested against the entire current day/story set, not just this page.
    all_ids = {r["id"] for r in _read(MATCH_EVENTS + " RETURN DISTINCT e.domain_object_id AS id", params)}
    signals = (
        _read(
            """MATCH (v:Entity:Variable {group_id:$group})
        -[s:RELATES_TO {group_id:$group, name:'SIGNAL_ON'}]->(a:Entity {group_id:$group})
        WHERE any(id IN coalesce(s.source_event_ids,[]) WHERE id IN $ids)
        RETURN s {.uuid,.fact,.direction,.source_event_ids,.assertion_modality,.magnitude,
                  .valid_at,.invalid_at,.expected_end_earliest,.expected_end_latest,
                  .confidence,.assumptions,.invalidation_conditions,.review_status} AS signal,
               v {.uuid,.data_object_id,.name,.variable_id} AS variable,
               a {.uuid,.data_object_id,.name} AS anchor, labels(a) AS anchor_types
        ORDER BY s.uuid""",
            {"group": GRAPHITI_GROUP_ID, "ids": sorted(ids)},
        )
        if ids
        else []
    )
    for item in signals:
        missing = sorted(set(item["signal"].get("source_event_ids") or []) - all_ids)
        item["missing_source_event_ids"] = missing
        item["usable_as_complete_fact"] = not missing
        if missing:
            # Do not deliver a mixed-source assertion as an in-scope fact.
            item["signal"] = {"uuid": item["signal"]["uuid"], "source_event_ids": item["signal"]["source_event_ids"]}
    events = []
    for row in selected:
        data = row["event"]
        content = json.loads(data["content"])
        if content["id"] != data["domain_object_id"]:
            raise ValueError("Event identity/content mismatch")
        events.append(
            {**content, "created_at": data["created_at"], "valid_at": data.get("valid_at"), **refs[content["id"]]}
        )
    result = {
        "schema_version": "story-events/v1",
        "query": query_window(story_id, research_date),
        "retrieved_at": datetime.now(UTC).isoformat(),
        "consistency": "live_not_snapshot",
        "storyline": story,
        "events": events,
        "variable_signals": signals,
        "total": counts[0]["total"],
        "next_after_event_id": selected[-1]["event"]["domain_object_id"] if len(rows) > limit else None,
    }

    if len(json.dumps(result, ensure_ascii=False)) > 8500:
        raise ValueError("Event bundle exceeds the research result budget; cannot deliver complete content")
    return result


def query_evidence(story_id: str, research_date: str, event_id: str, evidence_id: str) -> dict[str, Any]:
    params = _params(story_id, research_date)
    rows = _read(
        MATCH_EVENTS + " AND e.domain_object_id=$event_id RETURN DISTINCT e.domain_object_id AS id",
        {**params, "event_id": event_id},
    )
    if len(rows) != 1:
        raise ValueError("Event is outside the requested storyline/day")
    refs, evidence = _provenance({event_id})
    if evidence_id not in refs[event_id]["evidence_ids"] or evidence_id not in evidence:
        raise ValueError("Evidence is missing or is not linked to this Event")
    return {
        "schema_version": "story-events/v1",
        "event_id": event_id,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "evidence": evidence[evidence_id],
        "content_kind": "atomic_evidence_not_full_article",
    }
