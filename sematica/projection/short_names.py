"""Explicit, audited property-only updates from an operator-owned Data snapshot."""

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from neo4j import GraphDatabase
from pydantic import BaseModel, ConfigDict, Field, model_validator

from sematica.ontology.entities.base import ShortName
from sematica.projection.authoritative_writer import GROUP_ID, node_uuid

EntityType = Literal["ChainNode", "IndustryChain", "MacroEconomic", "GeopoliticRivalry"]
PREFIXES = {"ChainNode": "CND", "IndustryChain": "ICH", "MacroEconomic": "MEC", "GeopoliticRivalry": "GPR"}


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entity_type: EntityType
    id: str = Field(
        pattern=r"^(CND|ICH|MEC|GPR)[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    name: str = Field(min_length=1)
    short_name: ShortName | None

    @model_validator(mode="after")
    def typed_id(self):
        if not self.id.startswith(PREFIXES[self.entity_type]):
            raise ValueError("entity type and ID prefix differ")
        return self


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["entity-short-names.v1"] = "entity-short-names.v1"
    source: str = Field(min_length=1)
    exported_at: datetime
    rows: list[Record] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        if self.exported_at.tzinfo is None:
            raise ValueError("export time must include timezone")
        if len({r.id for r in self.rows}) != len(self.rows):
            raise ValueError("duplicate source ID")
        if {r.entity_type for r in self.rows} != set(PREFIXES):
            raise ValueError("snapshot must cover all four entity types")
        return self


class Change(Record):
    uuid: str
    before: ShortName | None
    fingerprint: str


class Package(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["graph-short-names-plan.v1"] = "graph-short-names-plan.v1"
    target: str = Field(min_length=1)
    group_id: Literal["neo4j"] = "neo4j"
    prepared_at: datetime
    source: Snapshot
    rows: list[Change]
    missing: list[Record]

    @model_validator(mode="after")
    def coverage(self):
        combined = [*self.rows, *self.missing]
        if len({r.id for r in combined}) != len(combined):
            raise ValueError("duplicate package ID")
        expected = {r.id: r.model_dump() for r in self.source.rows}
        actual = {
            r.id: Record.model_validate(r.model_dump(include=set(Record.model_fields))).model_dump() for r in combined
        }
        if expected != actual:
            raise ValueError("package differs from source snapshot")
        if any(r.uuid != node_uuid(r.id) for r in self.rows):
            raise ValueError("noncanonical graph UUID")
        return self


def fingerprint(props):
    """Exclude only the property this tool owns; retain vectors and all other attributes."""
    return hashlib.sha256(
        json.dumps(
            {k: v for k, v in props.items() if k != "short_name"}, sort_keys=True, default=str, ensure_ascii=False
        ).encode()
    ).hexdigest()


def inspect(tx, rows, *, lock=False):
    # Native labels are from a closed enum, never user-controlled Cypher fragments.
    result = {}
    for label in PREFIXES:
        ids = [r.id for r in rows if r.entity_type == label]
        query = """
            MATCH (n:Entity {group_id: $group_id}) WHERE n.data_object_id IN $ids
        """
        if lock:
            # A dependent property SET obtains the node write lock before reading current values.
            query += " SET n.short_name = n.short_name "
        query += " RETURN labels(n) AS labels, properties(n) AS props "
        for row in tx.run(query, group_id=GROUP_ID, ids=ids):
            p = dict(row["props"])
            key = p["data_object_id"]
            if key in result:
                raise ValueError(f"duplicate graph identity: {key}")
            if set(row["labels"]) != {"Entity", label} or p.get("uuid") != node_uuid(key):
                raise ValueError(f"graph type/UUID mismatch: {key}")
            result[key] = p
    return result


def prepare(snapshot, state, target):
    rows, missing = [], []
    for r in snapshot.rows:
        p = state.get(r.id)
        if p is None:
            missing.append(r)
            continue
        if p.get("name") != r.name:
            raise ValueError(f"canonical name differs: {r.id}")
        rows.append(Change(**r.model_dump(), uuid=p["uuid"], before=p.get("short_name"), fingerprint=fingerprint(p)))
    return Package(target=target, prepared_at=datetime.now(UTC), source=snapshot, rows=rows, missing=missing)


def check(package, state, *, after):
    if set(state) != {r.id for r in package.rows}:
        raise ValueError("graph coverage changed; prepare a fresh package")
    for r in package.rows:
        p = state[r.id]
        if fingerprint(p) != r.fingerprint or p.get("short_name") != (r.short_name if after else r.before):
            raise ValueError(f"graph changed or value differs: {r.id}")


def apply(tx, package, *, rollback=False):
    # All locks, preconditions, writes and verification share one transaction.
    state = inspect(tx, package.source.rows, lock=True)
    check(package, state, after=rollback)
    tx.run(
        """
        UNWIND $rows AS row
        MATCH (n:Entity {group_id: $group_id, uuid: row.uuid, data_object_id: row.id})
        SET n.short_name = row.value
    """,
        group_id=GROUP_ID,
        rows=[{"uuid": r.uuid, "id": r.id, "value": r.before if rollback else r.short_name} for r in package.rows],
    ).consume()
    check(package, inspect(tx, package.source.rows), after=not rollback)


def write_new(path, value):
    with path.open("x", encoding="utf-8") as f:
        f.write(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "apply", "verify", "rollback"])
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--target", required=True, help="Operator target identity, e.g. dgx-uat")
    parser.add_argument("--output", type=Path, help="New plan file (plan only)")
    parser.add_argument("--allow-missing", action="store_true", help="Acknowledge explicitly listed source-only IDs")
    args = parser.parse_args()
    source = Snapshot.model_validate_json(args.input.read_text()) if args.command == "plan" else None
    package = None if source else Package.model_validate_json(args.input.read_text())
    if package and (package.target != args.target or (package.missing and not args.allow_missing)):
        raise ValueError("target mismatch or unacknowledged missing nodes")
    uri = os.environ["NEO4J_URI"]
    with GraphDatabase.driver(uri, auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"])) as driver:
        with driver.session(database=os.getenv("NEO4J_DATABASE", "neo4j")) as session:
            if source:
                if args.output is None:
                    raise ValueError("plan requires --output")
                package = prepare(source, session.execute_read(inspect, source.rows), args.target)
                write_new(args.output, package.model_dump(mode="json"))
            else:
                assert package is not None
                if args.command == "verify":
                    check(package, session.execute_read(inspect, package.source.rows), after=True)
                else:
                    session.execute_write(apply, package, rollback=args.command == "rollback")
    assert package is not None
    print(
        json.dumps(
            {
                "command": args.command,
                "target": args.target,
                "matched": len(package.rows),
                "missing": len(package.missing),
                "changed": sum(r.before != r.short_name for r in package.rows),
            }
        )
    )


if __name__ == "__main__":
    main()
