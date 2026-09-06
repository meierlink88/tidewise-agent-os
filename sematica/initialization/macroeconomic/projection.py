"""Project an operator-exported Data join snapshot without database access or extraction."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from graphiti_core.nodes import EntityNode
from pydantic import BaseModel, ConfigDict, Field, model_validator

from sematica.ontology.entities.base import NonBlankText
from sematica.ontology.entities.macro_economic import ID_SUFFIX, MacroEconomic, MacroEconomicTactic
from sematica.projection.authoritative_writer import GROUP_ID, node_uuid
from sematica.projection.runtime import ProjectionError

OWNER = "tidewise-agentos/macroeconomic-projection/v1"


class Storyline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern="^MEC" + ID_SUFFIX + "$")
    name: NonBlankText = Field(max_length=100)
    macro_economics_domain_id: str = Field(pattern="^MCD" + ID_SUFFIX + "$")
    domain_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,49}$")
    domain_name: NonBlankText = Field(max_length=50)
    domain_description: NonBlankText
    tactics: list[MacroEconomicTactic] = Field(min_length=1)
    core_proposition: NonBlankText
    candidate_assets: list[str] = Field(min_length=1)
    created_at: datetime
    updated_at: datetime
    domain_created_at: datetime
    domain_updated_at: datetime

    @model_validator(mode="after")
    def validate_record(self):
        for value in (self.created_at, self.updated_at, self.domain_created_at, self.domain_updated_at):
            if value.tzinfo is None:
                raise ValueError("Data timestamps require timezone")
        if self.updated_at < self.created_at or self.domain_updated_at < self.domain_created_at:
            raise ValueError("invalid timestamp order")
        self.ontology()
        return self

    def ontology(self):
        return MacroEconomic(
            data_object_id=self.id,
            core_proposition=self.core_proposition,
            domain_code=self.domain_code,
            domain_name=self.domain_name,
            domain_description=self.domain_description,
            tactics=json.dumps([t.model_dump() for t in self.tactics], ensure_ascii=False, separators=(",", ":")),
            candidate_assets=self.candidate_assets,
            updated_at=self.updated_at,
        )


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["macroeconomic-projection-snapshot.v1"]
    source_count: int = Field(gt=0)
    items: list[Storyline] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_snapshot(self):
        if len(self.items) != self.source_count:
            raise ValueError("snapshot join lost or duplicated rows")
        for key in ("id", "name"):
            if len({getattr(item, key) for item in self.items}) != len(self.items):
                raise ValueError("duplicate storyline identity or name")
        domains: dict[str, tuple] = {}
        codes: dict[str, str] = {}
        for item in self.items:
            profile = (
                item.domain_code,
                item.domain_name,
                item.domain_description,
                item.tactics,
                item.domain_created_at,
                item.domain_updated_at,
            )
            if domains.setdefault(item.macro_economics_domain_id, profile) != profile:
                raise ValueError("conflicting domain profiles")
            if codes.setdefault(item.domain_code, item.macro_economics_domain_id) != item.macro_economics_domain_id:
                raise ValueError("domain code maps to multiple IDs")
        return self


def load_snapshot(path: Path) -> Snapshot:
    return Snapshot.model_validate_json(path.read_text(encoding="utf-8"))


def build_plan(snapshot: Snapshot) -> list[EntityNode]:
    nodes = []
    for item in sorted(snapshot.items, key=lambda x: x.id):
        summary = "\n".join(
            [
                item.name,
                "领域：" + item.domain_name,
                "领域描述：" + item.domain_description,
                "核心命题：" + item.core_proposition,
                "手段：" + "；".join(t.name + "：" + t.description for t in item.tactics),
            ]
        )
        attrs = item.ontology().model_dump(mode="json", exclude_none=True)
        attrs.update(
            {
                "projection_owner": OWNER,
                "domain_created_at": item.domain_created_at.astimezone(UTC).isoformat(),
                "domain_updated_at": item.domain_updated_at.astimezone(UTC).isoformat(),
                "projection_fingerprint": hashlib.sha256(
                    json.dumps(item.model_dump(mode="json"), sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest(),
            }
        )
        nodes.append(
            EntityNode(
                uuid=node_uuid(item.id),
                name=item.name,
                group_id=GROUP_ID,
                labels=["Entity", "MacroEconomic"],
                summary=summary,
                created_at=item.created_at,
                attributes=attrs,
            )
        )
    return nodes


async def inspect_state(graphiti, nodes: list[EntityNode]) -> list[dict]:
    records, _, _ = await graphiti.driver.execute_query(
        """MATCH (n) WHERE n:MacroEconomic OR n.uuid IN $uuids OR n.data_object_id IN $ids
        RETURN n{.*,name_embedding:null} AS props, labels(n) AS labels,
        size(n.name_embedding) AS dimension ORDER BY n.uuid""",
        uuids=[n.uuid for n in nodes],
        ids=[n.attributes["data_object_id"] for n in nodes],
    )
    return [r.data() if hasattr(r, "data") else dict(r) for r in records]


def preflight(nodes: list[EntityNode], state: list[dict]) -> dict:
    expected = {n.uuid: n for n in nodes}
    actual = {}
    for row in state:
        p = row["props"]
        uuid = p.get("uuid")
        if uuid in actual or uuid not in expected:
            raise ProjectionError("unexpected or duplicate macroeconomic node; explicit cleanup required")
        n = expected[uuid]
        if (
            set(row["labels"]) != {"Entity", "MacroEconomic"}
            or p.get("group_id") != GROUP_ID
            or p.get("projection_owner") != OWNER
            or p.get("data_object_id") != n.attributes["data_object_id"]
        ):
            raise ProjectionError("macroeconomic namespace collision")
        actual[uuid] = row
    return actual


def matches(node: EntityNode, row: dict, dimension: int) -> bool:
    expected = {
        "uuid": node.uuid,
        "name": node.name,
        "summary": node.summary,
        "group_id": node.group_id,
        **node.attributes,
    }
    props = dict(row["props"])
    props.pop("name_embedding", None)
    # Graphiti save_bulk also persists a labels property in addition to native labels.
    if set(props.pop("labels", [])) != set(node.labels):
        return False
    created = props.pop("created_at", None)
    if hasattr(created, "to_native"):
        created = created.to_native()
    return props == expected and created == node.created_at and row["dimension"] == dimension


def verify(nodes: list[EntityNode], state: list[dict], dimension: int) -> dict:
    actual = preflight(nodes, state)
    if set(actual) != {n.uuid for n in nodes} or any(not matches(n, actual[n.uuid], dimension) for n in nodes):
        raise ProjectionError("macroeconomic projection differs from joined Data snapshot")
    return {"verified": True, "storylines": len(nodes), "embedding_dimension": dimension}


async def execute_plan(graphiti, nodes: list[EntityNode], dimension: int) -> dict:
    actual = preflight(nodes, await inspect_state(graphiti, nodes))
    changed = [n for n in nodes if n.uuid not in actual or not matches(n, actual[n.uuid], dimension)]
    # Complete all vectors before the first graph write.
    for start in range(0, len(changed), 10):
        batch = changed[start : start + 10]
        vectors = await graphiti.embedder.create_batch([n.name for n in batch])
        for n, vector in zip(batch, vectors, strict=True):
            if len(vector) != dimension:
                raise ProjectionError("embedding dimension mismatch")
            n.name_embedding = vector
    preflight(nodes, await inspect_state(graphiti, nodes))
    if changed:
        await graphiti.nodes.entity.save_bulk(changed, batch_size=len(changed))
    return {**verify(nodes, await inspect_state(graphiti, nodes), dimension), "nodes_written": len(changed)}
