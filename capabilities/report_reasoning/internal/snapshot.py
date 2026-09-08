"""Read-only adapter for AgentOS graph/journal exports; never imports old conclusions."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .contracts import BRANCHES
from .storage import digest, read

TOPOLOGY = {"ChainNodeInputTo": "投入", "ChainNodeIsComponentOf": "组成", "ChainNodeDependsOn": "依赖"}
STRUCTURE = {*TOPOLOGY, "ChainNodeBelongsToIndustryChain", "CompanyParticipatesInChainNode"}
ENTITY_TYPES = {"GeopoliticRivalry", "MacroEconomic", "IndustryChain", "ChainNode", "Company", "Variable"}


def import_export(path: Path) -> dict[str, Any]:
    return normalize_export(read(path))


def normalize_export(raw: dict[str, Any]) -> dict[str, Any]:
    if not raw["events"]:
        raise ValueError("export has no Events; no inference input can be prepared")
    entities = {}
    uuids = {}
    for item in raw["entities"]:
        kind = next((label for label in item["labels"] if label in ENTITY_TYPES), None)
        if kind:
            identity = item.get("id") or item["uuid"]
            # Names and authoritative IDs are reusable. Graph summaries may contain historical Event facts.
            entities[identity] = {"id": identity, "name": item["name"], "type": kind}
            uuids[item["uuid"]] = identity
    events = {}
    for item in raw["events"]:
        data = item["data"]
        content = json.loads(data["content"])
        if content["id"] in events:
            raise ValueError(f"duplicate Event: {content['id']}")
        events[content["id"]] = {
            "id": content["id"],
            "title": content["title"],
            "summary": content["summary"],
            "semantic": content["semantic"],
            "classes": [],
            "evidence_ids": [],
            "valid_at": data["valid_at"],
        }
    evidences = {}
    for batch in raw["journals"]:
        for evidence in batch["input"]["evidences"]:
            evidences[evidence["id"]] = {
                key: evidence[key] for key in ("id", "title", "summary", "semantic") if key in evidence
            }
        for candidate in batch["journal"]["candidates"].values():
            publication = candidate.get("publication") or {}
            event = events.get(publication.get("event_id"))
            if event is None:
                continue
            classification = candidate.get("classification") or {}
            category = classification.get("event_class")
            if category and category not in event["classes"]:
                event["classes"].append(category)
            for eid in candidate["identity_request"]["candidate"]["evidence_ids"]:
                if eid not in event["evidence_ids"]:
                    event["evidence_ids"].append(eid)
    for event in events.values():
        if not event["classes"] or not event["evidence_ids"]:
            raise ValueError(f"unresolved Event classification/provenance: {event['id']}")
        if not set(event["classes"]) <= set(BRANCHES["industry"]):
            raise ValueError(f"unknown Event class: {event['id']}")
        if not set(event["evidence_ids"]) <= evidences.keys():
            raise ValueError(f"missing Evidence: {event['id']}")
    signals, structure = [], []
    for relation in raw["relations"]:
        data = relation["data"]
        source, target = uuids.get(relation["source"]), uuids.get(relation["target"])
        if source is None or target is None:
            if data["name"] == "SIGNAL_ON":
                raise ValueError(f"Signal anchor missing from export: {data['uuid']}")
            continue
        if data["name"] in STRUCTURE:
            structure.append({"source": source, "target": target, "type": data["name"], "id": data["uuid"]})
        if data["name"] != "SIGNAL_ON":
            continue
        event_ids = data.get("source_event_ids") or []
        if not event_ids or not set(event_ids) <= events.keys():
            raise ValueError(f"Signal source closure missing: {data['uuid']}")
        if entities[source]["type"] != "Variable":
            raise ValueError("Signal source is not a Variable")
        signals.append(
            {
                "id": data["uuid"],
                "entity_id": target,
                "variable_id": source,
                "variable_name": entities[source]["name"],
                "signal": data["fact"],
                "source_direction": data.get("direction") or "UNKNOWN",
                "event_ids": event_ids,
                "evidence_ids": sorted({e for i in event_ids for e in events[i]["evidence_ids"]}),
                "modality": data.get("assertion_modality"),
                "valid_at": data.get("valid_at"),
                "invalid_at": data.get("invalid_at"),
                "expected_end_latest": data.get("expected_end_latest"),
            }
        )
    if len({s["id"] for s in signals}) != len(signals):
        raise ValueError("duplicate Signal identity in export")
    times = sorted(
        (e["valid_at"] for e in events.values()), key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    return {
        "source_kind": raw.get("source_kind", "agentos_graph_journal_export"),
        "source_sha256": digest(raw),
        "observed_at": raw["retrieved_at"],
        "events": list(events.values()),
        "signals": signals,
        "entities": entities,
        "structure": structure,
        "evidences": evidences,
        "window": raw.get("requested_window", {"start": times[0], "end": times[-1]}),
    }


def branch_input(snapshot: dict[str, Any], branch: str) -> dict[str, Any]:
    events = [e for e in snapshot["events"] if set(e["classes"]) & set(BRANCHES[branch])]
    ids = {e["id"] for e in events}
    signals = [s for s in snapshot["signals"] if ids & set(s["event_ids"])]
    # Do not silently trim aggregate Signals whose text uses out-of-scope Event sources.
    for signal in signals:
        if not set(signal["event_ids"]) <= ids:
            raise ValueError(f"mixed-scope Signal requires source decomposition: {signal['id']}")
    evidence_ids = {eid for event in events for eid in event["evidence_ids"]}
    signal_entities = {s["entity_id"] for s in signals}
    catalog = [
        e
        for e in snapshot["entities"].values()
        if e["type"] in {"GeopoliticRivalry", "MacroEconomic", "IndustryChain"} or e["id"] in signal_entities
    ]
    memberships = [r for r in snapshot["structure"] if r["source"] in signal_entities]
    return {
        "branch": branch,
        "allowed_classes": BRANCHES[branch],
        "window": snapshot["window"],
        "observed_at": snapshot["observed_at"],
        "events": events,
        "signals": signals,
        "evidences": [snapshot["evidences"][eid] for eid in sorted(evidence_ids)],
        "catalog": catalog,
        "direct_structure": memberships,
    }
