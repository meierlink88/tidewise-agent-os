"""Report checks only. Never generate, rewrite or claim semantic review of a report."""

from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

from .snapshot import TOPOLOGY, branch_input
from .storage import digest, read


def objects(value: Any, path: str = "$") -> list[tuple[str, dict[str, Any]]]:
    result = []
    if isinstance(value, dict):
        result.append((path, value))
        for key, item in value.items():
            result.extend(objects(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            result.extend(objects(item, f"{path}[{i}]"))
    return result


def validate_report(report: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    schema = read(Path(__file__).with_name("publication.schema.json"))
    issues = [
        {"path": str(list(e.absolute_path)), "message": e.message}
        for e in Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(report)
    ]

    def check(ok: bool, path: str, message: str) -> None:
        if not ok:
            issues.append({"path": path, "message": message})

    if not issues:
        sections = {
            "geopolitical_stories": ("geopolitics", "GeopoliticRivalry"),
            "macroeconomic_stories": ("macroeconomics", "MacroEconomic"),
            "industry_chain_analyses": ("industry", "IndustryChain"),
            "company_analyses": ("industry", "Company"),
        }
        entities = snapshot["entities"]
        check(
            not report["concept_analyses"],
            "$.concept_analyses",
            "Concept inference is outside this three-procedure contract",
        )
        check(any(report[s] for s in sections), "$", "at least one judgment required")
        for section, (branch, root_type) in sections.items():
            packet = branch_input(snapshot, branch)
            events = {e["id"]: e for e in packet["events"]}
            signals = {s["id"]: s for s in packet["signals"]}
            ev_ids = {e["id"] for e in packet["evidences"]}
            for i, unit in enumerate(report[section]):
                path = f"$.{section}[{i}]"
                check(entities.get(unit["source_id"], {}).get("type") == root_type, path, f"root must be {root_type}")
                if branch != "geopolitics" and "detail" in unit:
                    check(
                        not unit["detail"]["macro_impacts"], path, "macro_impacts only belongs in geopolitical detail"
                    )
                nodes = objects(unit, path)
                judgments = {o["local_key"]: o for _, o in nodes if "judgment_origin" in o}
                check(
                    len(judgments) == sum("judgment_origin" in o for _, o in nodes),
                    path,
                    "duplicate judgment local_key",
                )
                for ref in unit.get("summary", {}).get("affected_refs", []):
                    target = judgments.get(ref["local_key"])
                    check(target is not None, path, "summary affected reference not found in this unit")
                    if target is not None:
                        expected = {
                            "macroeconomic_story": "MacroEconomic",
                            "industry_chain": "IndustryChain",
                            "industry_chain_node": "ChainNode",
                        }
                        kind = entities.get(target["source_id"], {}).get("type")
                        check(
                            kind == expected.get(ref["target_type"]), path, "summary affected reference type mismatch"
                        )
                dependencies = {
                    key: [r["local_key"] for r in value["reasoning_sources"]["upstream_refs"]]
                    for key, value in judgments.items()
                }

                def cyclic(key: str, trail: set[str]) -> bool:
                    if key in trail:
                        return True
                    return any(cyclic(parent, trail | {key}) for parent in dependencies.get(key, []))

                check(not any(cyclic(key, set()) for key in dependencies), path, "circular upstream references")
                for p, obj in nodes:
                    if "evidence_ids" in obj:
                        check(set(obj["evidence_ids"]) <= ev_ids, p, "Evidence outside procedure scope")
                    if "source_id" in obj:
                        check(obj["source_id"] in entities, p, "entity not present in graph")
                    if "transmission_logic" in obj:
                        check("→" in obj["transmission_logic"], p, "causal logic must use arrows")
                    if "judgment_origin" in obj:
                        rows = obj.get("variable_signals", obj.get("detail", {}).get("variable_signals", []))
                        check(
                            obj["judgment_origin"] == ("direct" if rows else "inferred"),
                            p,
                            "direct/inferred inconsistent with own Signals",
                        )
                        sources = obj["reasoning_sources"]
                        event_ids = set(sources["event_ids"])
                        check(
                            bool(event_ids) and event_ids <= events.keys(), p, "missing or out-of-scope source Events"
                        )
                        allowed_ev = {e for eid in event_ids if eid in events for e in events[eid]["evidence_ids"]}
                        assessment = obj.get("assessment", obj.get("summary", {}))
                        check(
                            set(assessment.get("evidence_ids", [])) <= allowed_ev,
                            p,
                            "assessment Evidence not linked to source Events",
                        )
                        check(
                            set(sources["signal_ids"]) == {s["signal_id"] for s in rows},
                            p,
                            "source Signal IDs differ from own rows",
                        )
                        for ref in sources["upstream_refs"]:
                            parent = judgments.get(ref["local_key"])
                            check(
                                parent is not None and parent["source_id"] == ref["entity_id"] and parent is not obj,
                                p,
                                "upstream reference not closed within this unit",
                            )
                        for row in rows:
                            source = signals.get(row["signal_id"])
                            check(source is not None, p, "Signal outside procedure scope")
                            if source:
                                check(source["entity_id"] == obj["source_id"], p, "Signal belongs to another entity")
                                for key in (
                                    "variable_id",
                                    "variable_name",
                                    "signal",
                                    "source_direction",
                                    "event_ids",
                                    "evidence_ids",
                                ):
                                    check(row[key] == source[key], p, f"Signal source field changed: {key}")
                                check(set(source["event_ids"]) <= event_ids, p, "own Signal Events missing")
                    if "graph" in obj:
                        graph = obj["graph"]
                        lookup = {n["local_key"]: n["source_id"] for n in graph["nodes"]}
                        assessed = {n["node_local_key"]: n["source_id"] for n in obj["affected_nodes"]}
                        check(
                            bool(assessed) and lookup == assessed and obj["empty_state"] is None,
                            p,
                            "graph must contain exactly assessed nodes",
                        )
                        memberships = {
                            (r["source"], r["target"])
                            for r in snapshot["structure"]
                            if r["type"] == "ChainNodeBelongsToIndustryChain"
                        }
                        check(
                            all((identity, obj["source_id"]) in memberships for identity in assessed.values()),
                            p,
                            "node not member of this chain",
                        )
                        edges = {
                            (r["source"], r["target"], TOPOLOGY[r["type"]])
                            for r in snapshot["structure"]
                            if r["type"] in TOPOLOGY
                        }
                        for edge in graph["edges"]:
                            check(
                                (
                                    lookup.get(edge["from_node_local_key"]),
                                    lookup.get(edge["to_node_local_key"]),
                                    edge["relation_label"],
                                )
                                in edges,
                                p,
                                "edge not in retrieved graph",
                            )
    return {
        "passed": not issues,
        "issues": issues,
        "report_hash": digest(report),
        "snapshot_hash": digest(snapshot),
        "scope": "schema_and_reference_checks",
        "semantic_review": "Codex analyst responsibility; not certified by this command",
        "coverage_review": "Codex analyst responsibility; not certified by this command",
        "data_service_validation": "not_performed",
        "publication": "not_performed",
    }
