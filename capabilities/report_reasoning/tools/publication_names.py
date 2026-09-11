"""Freeze graph short names and project display fields in a separate v5 publication package."""

import argparse
import copy
import json
import os
from pathlib import Path
from typing import Any

from capabilities.report_reasoning.internal.storage import digest, now, read, write

PREFIXES = ("GPR", "MEC", "ICH", "CND")


def export_catalog() -> dict[str, Any]:
    from neo4j import GraphDatabase

    from sematica.projection.runtime import GRAPHITI_GROUP_ID

    query = """MATCH (n:Entity {group_id:$group})
        WITH n, coalesce(n.data_object_id,n.demo_catalog_key,n.policy_key) AS id
        WHERE substring(id,0,3) IN $prefixes
        RETURN id, n.name AS name, n.short_name AS short_name ORDER BY id"""
    with (
        GraphDatabase.driver(
            os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"])
        ) as driver,
        driver.session() as session,
    ):
        rows = session.run(query, group=GRAPHITI_GROUP_ID, prefixes=list(PREFIXES)).data()
    return {"observed_at": now(), "group_id": GRAPHITI_GROUP_ID, "entities": rows}


def project(report: dict[str, Any], catalog: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if report.get("schema_version") != "report-publication/v5":
        raise ValueError("Project names only after preparing the separate v5 publication package")
    names = {}
    for row in catalog["entities"]:
        identity = row["id"]
        if identity in names:
            raise ValueError(f"Duplicate catalog identity: {identity}")
        names[identity] = row
    result = copy.deepcopy(report)
    changes, missing = [], set()

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            identity = value.get("source_id", "")
            if isinstance(identity, str) and identity.startswith(PREFIXES):
                row = names.get(identity, {})
                short = row.get("short_name")
                fields = [key for key in ("name", "title") if key in value]
                if fields and (not isinstance(short, str) or not short.strip()):
                    missing.add(identity)
                elif fields:
                    for key in fields:
                        changes.append(
                            {"path": f"{path}/{key}", "source_id": identity, "before": value[key], "after": short}
                        )
                        value[key] = short
            for key, child in value.items():
                visit(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}/{index}")

    visit(result, "")
    if missing:
        raise ValueError("Missing graph short_name for: " + ", ".join(sorted(missing)))
    return result, {
        "source_report_hash": digest(report),
        "catalog_hash": digest(catalog),
        "report_hash": digest(result),
        "changes": changes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export-catalog")
    export.add_argument("--output", type=Path, required=True)
    projection = sub.add_parser("project")
    projection.add_argument("--report", type=Path, required=True)
    projection.add_argument("--catalog", type=Path, required=True)
    projection.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output already exists; preserve frozen artifacts")
    if args.command == "export-catalog":
        write(args.output, export_catalog())
    else:
        report, receipt = project(read(args.report), read(args.catalog))
        args.output.mkdir(parents=True)
        write(args.output / "report.json", report)
        write(args.output / "name-projection-receipt.json", receipt)
        print(json.dumps({"changed_fields": len(receipt["changes"]), "report_hash": receipt["report_hash"]}))


if __name__ == "__main__":
    main()
