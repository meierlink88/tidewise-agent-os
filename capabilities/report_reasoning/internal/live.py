"""Read-only AgentOS-local graph/journal export with drift and readiness gates."""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

from sematica.projection.runtime import GRAPHITI_GROUP_ID

from .storage import digest, now

QUERIES = {
    "entities": """MATCH (n:Entity {group_id:$group}) RETURN n.uuid AS uuid, labels(n) AS labels,
        coalesce(n.data_object_id,n.demo_catalog_key,n.policy_key) AS id,n.name AS name ORDER BY n.uuid""",
    "events": """MATCH (e:Episodic {group_id:$group}) WHERE e.episode_kind='EVENT'
        AND e.valid_at >= datetime($start) AND e.valid_at <= datetime($end)
        RETURN properties(e) AS data ORDER BY e.uuid""",
    "relations": """MATCH (a:Entity {group_id:$group})-[r:RELATES_TO {group_id:$group}]->(b:Entity {group_id:$group})
        WHERE r.name IN ['SIGNAL_ON','ChainNodeBelongsToIndustryChain','CompanyParticipatesInChainNode',
                        'ChainNodeInputTo','ChainNodeIsComponentOf','ChainNodeDependsOn']
        RETURN a.uuid AS source,b.uuid AS target,
        r {.uuid,.name,.fact,.direction,.source_event_ids,.event_class,.assertion_modality,
           .valid_at,.invalid_at,.expected_end_latest} AS data ORDER BY r.uuid""",
}


def journal_files(root: Path) -> dict[Path, bytes]:
    paths = sorted(p for sub in ("batches", ".pending") for p in (root / sub).glob("*/storyline_journal.json"))
    if not paths:
        raise ValueError("no AgentOS storyline journals found at event-root")
    result = {}
    for path in paths:
        result[path] = path.read_bytes()
        result[path.parent / "input.json"] = (path.parent / "input.json").read_bytes()
    return result


def export_live(event_root: Path, start: str, end: str) -> dict[str, Any]:
    lower, upper = (datetime.fromisoformat(v.replace("Z", "+00:00")) for v in (start, end))
    if lower.tzinfo is None or upper.tzinfo is None or lower >= upper:
        raise ValueError("live window requires timezone-aware start < end")
    if upper > datetime.fromisoformat(now()):
        raise ValueError("live cutoff cannot be in the future")
    names = ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD")
    if any(not os.environ.get(k) for k in names):
        raise ValueError("live export requires NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD in runtime environment")
    before_files = journal_files(event_root)
    journals = [
        {
            "batch": p.parent.name,
            "journal": json.loads(value),
            "input": json.loads(before_files[p.parent / "input.json"]),
        }
        for p, value in before_files.items()
        if p.name == "storyline_journal.json"
    ]
    with GraphDatabase.driver(
        os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"])
    ) as driver:
        with driver.session(default_access_mode="READ", fetch_size=1000) as session:

            def capture(tx):
                return {
                    name: [dict(row) for row in tx.run(query, group=GRAPHITI_GROUP_ID, start=start, end=end)]
                    for name, query in QUERIES.items()
                }

            before = session.execute_read(capture)
            after = session.execute_read(capture)
    # Neo4j temporal values require canonical serialization before comparing source captures.
    before = json.loads(json.dumps(before, default=str))
    after = json.loads(json.dumps(after, default=str))
    if digest(before) != digest(after) or before_files != journal_files(event_root):
        raise ValueError("source changed during capture; retry after extraction/catalog updates settle")
    event_ids = {row["data"]["domain_object_id"] for row in before["events"]}
    for batch in journals:
        for candidate in batch["journal"]["candidates"].values():
            if (candidate.get("publication") or {}).get("event_id") in event_ids and not candidate.get("done"):
                raise ValueError("selected Event extraction/Signal publication is not complete")
    # Only include Signal records wholly rooted in selected Events. A mixed source aggregate fails normalization.
    before["relations"] = [
        r
        for r in before["relations"]
        if r["data"]["name"] != "SIGNAL_ON" or event_ids.intersection(r["data"].get("source_event_ids") or [])
    ]
    return {
        **before,
        "mentions": [],
        "journals": journals,
        "retrieved_at": now(),
        "source_kind": "agentos_live_export",
        "requested_window": {"start": start, "end": end},
    }
