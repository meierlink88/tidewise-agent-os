"""Read-only data tools called by the analyst, never by a model orchestrator."""

from pathlib import Path
from typing import Any

from capabilities.report_reasoning.internal.snapshot import branch_input, import_export
from capabilities.report_reasoning.internal.storage import digest, read, write


def prepare(source: Path, run: Path) -> dict[str, Any]:
    snapshot = import_export(source)
    return freeze(snapshot, run)


def freeze(snapshot: dict[str, Any], run: Path) -> dict[str, Any]:
    run.mkdir(parents=True, exist_ok=False)
    manifest = {
        "format": "codex-analyst-input/v1",
        "snapshot_hash": digest(snapshot),
        "source_kind": snapshot["source_kind"],
        "window": snapshot["window"],
    }
    write(run / "snapshot.json", snapshot)
    write(run / "manifest.json", manifest)
    return manifest


def prepare_live(event_root: Path, start: str, end: str, run: Path) -> dict[str, Any]:
    from capabilities.report_reasoning.internal.live import export_live
    from capabilities.report_reasoning.internal.snapshot import normalize_export

    return freeze(normalize_export(export_live(event_root, start, end)), run)


def load_snapshot(run: Path) -> dict[str, Any]:
    manifest, snapshot = read(run / "manifest.json"), read(run / "snapshot.json")
    if manifest.get("format") != "codex-analyst-input/v1":
        raise ValueError("not an analyst input workspace; prepare a new workspace")
    if digest(snapshot) != manifest["snapshot_hash"]:
        raise ValueError("frozen input changed")
    return snapshot


def query(
    run: Path,
    branch: str,
    resource: str,
    identity: str | None = None,
    text: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    if offset < 0 or not 1 <= limit <= 500:
        raise ValueError("offset >= 0 and limit 1..500 required")
    snapshot = load_snapshot(run)
    packet = branch_input(snapshot, branch)
    if resource == "entities":
        rows = list(snapshot["entities"].values())
    elif resource == "structure":
        rows = snapshot["structure"]
    else:
        rows = packet[resource]
    if identity:
        fields = ("id", "entity_id", "source", "target")
        rows = [
            r
            for r in rows
            if any(r.get(k) == identity for k in fields)
            or identity in r.get("event_ids", [])
            or identity in r.get("evidence_ids", [])
        ]
    if text:
        rows = [
            r
            for r in rows
            if any(
                text.casefold() in str(r.get(k, "")).casefold()
                for k in ("name", "title", "summary", "signal", "variable_name", "type")
            )
        ]
    rows = sorted(rows, key=lambda r: r["id"])
    end = min(offset + limit, len(rows))
    return {
        "branch": branch,
        "resource": resource,
        "snapshot_hash": digest(snapshot),
        "total": len(rows),
        "offset": offset,
        "next_offset": end if end < len(rows) else None,
        "items": rows[offset:end],
    }
