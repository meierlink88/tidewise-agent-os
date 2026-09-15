"""Bind frozen story Event Evidence to v6 summary scopes; never rewrite prose."""

import argparse
import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone_required")
    return result


def bind(report, pages, snapshot):
    result = copy.deepcopy(report)
    window = result["analysis_window"]
    start, end = instant(window["start"]), instant(window["end"])
    frozen_events = {event["id"]: event for event in snapshot["events"]}
    audit = []
    for unit in result["geopolitical_stories"]:
        sid = unit["source_id"]
        selected = [p for p in pages if p["query"]["story_id"] == sid]
        if not selected:
            raise ValueError(f"{sid}: missing_pages")
        seen, evidence, edges = set(), set(), []
        total = selected[0]["total"]
        previous = None
        for index, page in enumerate(selected):
            q = page["query"]
            if (
                page["schema_version"] != "story-events/v1"
                or page["storyline"]["data_object_id"] != sid
                or q["selection_time_field"] != "created_at"
                or instant(q["start"]) != start
                or instant(q["end"]) != end
                or page["total"] != total
            ):
                raise ValueError(f"{sid}: inconsistent_scope")
            for event in page["events"]:
                eid = event["id"]
                if eid in seen or (previous is not None and eid <= previous):
                    raise ValueError(f"{sid}: duplicate_or_unordered_event")
                previous = eid
                seen.add(eid)
                frozen = frozen_events.get(eid)
                if (
                    frozen is None
                    or not start <= instant(event["created_at"]) < end
                    or not start <= instant(frozen["created_at"]) < end
                ):
                    raise ValueError(f"{eid}: outside_frozen_input")
                ids = event["evidence_ids"]
                if (
                    not event.get("publication_complete")
                    or event.get("missing_evidence_ids")
                    or not ids
                    or set(ids) != set(frozen["evidence_ids"])
                ):
                    raise ValueError(f"{eid}: missing_or_changed_evidence")
                for evidence_id in ids:
                    if (
                        not re.fullmatch(r"EVD[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", evidence_id)
                        or evidence_id not in snapshot["evidences"]
                    ):
                        raise ValueError(f"{eid}: unknown_evidence")
                evidence.update(ids)
                edges.append({"event_id": eid, "evidence_ids": sorted(set(ids))})
            cursor = page["next_after_event_id"]
            if (index < len(selected) - 1 and (not cursor or cursor != previous)) or (
                index == len(selected) - 1 and cursor is not None
            ):
                raise ValueError(f"{sid}: incomplete_pagination")
        if len(seen) != total or not evidence:
            raise ValueError(f"{sid}: incomplete_story")
        unit["summary"]["evidence_ids"] = sorted(evidence)
        audit.append({"story_id": sid, "event_evidence_edges": edges, "evidence_ids": sorted(evidence)})
    return result, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["report", "pages", "snapshot", "output", "audit"]:
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    if args.output.resolve() == args.audit.resolve():
        raise ValueError("output_and_audit_must_differ")
    paths = sorted(args.pages.glob("*.json"))

    def load(path):
        return json.loads(path.read_text())

    original = load(args.report)
    wrapped = "report" in original
    report, mapping = bind(original["report"] if wrapped else original, [load(p) for p in paths], load(args.snapshot))
    result = dict(original, report=report) if wrapped else report
    if args.output.exists() or args.audit.exists():
        raise ValueError("output_exists_use_new_revision")
    raw = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode()
    args.output.write_bytes(raw)
    record = {
        "output_sha256": hashlib.sha256(raw).hexdigest(),
        "input_hashes": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in [args.report, args.snapshot, *paths]
        },
        "stories": mapping,
    }
    args.audit.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "stories": len(mapping),
                "unique_evidence": len({e for row in mapping for e in row["evidence_ids"]}),
                "output_sha256": record["output_sha256"],
            }
        )
    )


if __name__ == "__main__":
    main()
