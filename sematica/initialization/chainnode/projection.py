"""Build and verify the authoritative ChainNode topology projection."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from graphiti_core import Graphiti
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from sematica.ontology import (
    EDGE_TYPE_MAP,
    ChainNode,
    ChainNodeBelongsToIndustryChain,
    ChainNodeDependsOn,
    ChainNodeInputTo,
    ChainNodeIsComponentOf,
)
from sematica.ontology.enums import ContextualStage, ReviewStatus
from sematica.projection.authoritative_writer import (
    GROUP_ID,
    edge_uuid,
    node_uuid,
    scoped_edge_uuid,
    write_projection,
)
from sematica.projection.runtime import ProjectionError


@dataclass(frozen=True)
class TopologyRelationSpec:
    name: str
    model: type[BaseModel]
    fact_template: str


TOPOLOGY_RELATIONS = {
    "input_to": TopologyRelationSpec(
        name="ChainNodeInputTo",
        model=ChainNodeInputTo,
        fact_template="在{chain}中，{source}向{target}提供投入",
    ),
    "is_component_of": TopologyRelationSpec(
        name="ChainNodeIsComponentOf",
        model=ChainNodeIsComponentOf,
        fact_template="在{chain}中，{source}是{target}的组成部分",
    ),
    "depends_on": TopologyRelationSpec(
        name="ChainNodeDependsOn",
        model=ChainNodeDependsOn,
        fact_template="在{chain}中，{source}依赖{target}",
    ),
}
TOPOLOGY_RELATION_NAMES = tuple(spec.name for spec in TOPOLOGY_RELATIONS.values())
STAGE_TEXT = {
    ContextualStage.UPSTREAM: "上游",
    ContextualStage.MIDSTREAM: "中游",
    ContextualStage.DOWNSTREAM: "下游",
}
OWNED_EDGE_NAMES = frozenset({"ChainNodeBelongsToIndustryChain", *TOPOLOGY_RELATION_NAMES})
GRAPHITI_RELATIONSHIP_PROPERTIES = frozenset(
    {
        "uuid",
        "source_node_uuid",
        "target_node_uuid",
        "name",
        "fact",
        "fact_embedding",
        "group_id",
        "episodes",
        "created_at",
    }
)
CUSTOM_RELATIONSHIP_PROPERTIES = {
    "ChainNodeBelongsToIndustryChain": frozenset({"position", "contextual_stage"}),
    **{name: frozenset({"data_object_id", "industry_chain_id"}) for name in TOPOLOGY_RELATION_NAMES},
}


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == UTC.utcoffset(value)


class DataChainNodeDTO(BaseModel):
    """Frozen snapshot of one approved Data-owned ChainNode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^CND[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    name: str = Field(min_length=1)
    aliases: list[str]
    definition: str | None
    review_status: ReviewStatus
    created_at: datetime
    updated_at: datetime

    @field_validator("aliases")
    @classmethod
    def aliases_must_be_nonblank_and_unique(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("aliases must not contain blank values")
        if len(values) != len(set(values)):
            raise ValueError("aliases must be unique")
        return values

    @model_validator(mode="after")
    def timestamps_must_be_consistent(self) -> DataChainNodeDTO:
        if not _is_utc(self.created_at) or not _is_utc(self.updated_at):
            raise ValueError("ChainNode timestamps must be explicit UTC")
        if self.updated_at < self.created_at:
            raise ValueError("ChainNode updated_at precedes created_at")
        return self


class DataMembershipDTO(BaseModel):
    """Minimal graph contract for a ChainNode membership."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    industry_chain_id: str = Field(
        pattern=r"^ICH[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    industry_chain_name: str = Field(min_length=1)
    chain_node_id: str = Field(pattern=r"^CND[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    chain_node_name: str = Field(min_length=1)
    position: int = Field(gt=0)
    contextual_stage: ContextualStage


class DataGraphEdgeDTO(BaseModel):
    """Minimal graph contract for one direct IndustryChainGraphEdge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^IGE[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    industry_chain_id: str = Field(
        pattern=r"^ICH[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    industry_chain_name: str = Field(min_length=1)
    from_chain_node_id: str = Field(
        pattern=r"^CND[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    from_node_name: str = Field(min_length=1)
    to_chain_node_id: str = Field(
        pattern=r"^CND[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    to_node_name: str = Field(min_length=1)
    relation_type: Literal["input_to", "is_component_of", "depends_on"]

    @model_validator(mode="after")
    def endpoints_must_differ(self) -> DataGraphEdgeDTO:
        if self.from_chain_node_id == self.to_chain_node_id:
            raise ValueError("ChainNode topology edge must not be a self edge")
        return self


class SnapshotLine(BaseModel):
    """Discriminated JSON Line emitted by the read-only PostgreSQL export."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["chain_node", "membership", "graph_edge"]
    payload: dict[str, object]


class ChainNodeFacts(BaseModel):
    model_config = ConfigDict(frozen=True)

    chain_nodes: tuple[DataChainNodeDTO, ...]
    memberships: tuple[DataMembershipDTO, ...]
    graph_edges: tuple[DataGraphEdgeDTO, ...]


class ChainNodePlan(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    chain_node_count: int
    membership_count: int
    relation_counts: dict[str, int]
    nodes: tuple[EntityNode, ...]
    edges: tuple[EntityEdge, ...]
    industry_chain_ids: frozenset[str]
    industry_chain_names: dict[str, str]

    def summary(self) -> dict[str, object]:
        return {
            "group_id": GROUP_ID,
            "chain_nodes": self.chain_node_count,
            "memberships": self.membership_count,
            **self.relation_counts,
        }


def parse_snapshot(lines: Iterable[str]) -> ChainNodeFacts:
    """Parse and validate the complete PostgreSQL JSONL snapshot before graph access."""

    nodes: list[DataChainNodeDTO] = []
    memberships: list[DataMembershipDTO] = []
    graph_edges: list[DataGraphEdgeDTO] = []
    for line_number, raw_line in enumerate(lines, 1):
        if not raw_line.strip():
            continue
        try:
            record = SnapshotLine.model_validate_json(raw_line)
            if record.kind == "chain_node":
                nodes.append(DataChainNodeDTO.model_validate(record.payload))
            elif record.kind == "membership":
                memberships.append(DataMembershipDTO.model_validate(record.payload))
            else:
                graph_edges.append(DataGraphEdgeDTO.model_validate(record.payload))
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise ProjectionError(f"invalid ChainNode snapshot record at line {line_number}: {exc}") from None
    if not nodes or not memberships:
        raise ProjectionError("ChainNode snapshot must contain nodes and memberships")
    return ChainNodeFacts(
        chain_nodes=tuple(nodes),
        memberships=tuple(memberships),
        graph_edges=tuple(graph_edges),
    )


def _topology_fact(edge: DataGraphEdgeDTO) -> str:
    return TOPOLOGY_RELATIONS[edge.relation_type].fact_template.format(
        chain=edge.industry_chain_name,
        source=edge.from_node_name,
        target=edge.to_node_name,
    )


def has_exact_relationship_properties(name: str, property_keys: Iterable[str]) -> bool:
    """Accept only Graphiti fields plus the custom allowlist for this relation type."""

    expected = GRAPHITI_RELATIONSHIP_PROPERTIES | CUSTOM_RELATIONSHIP_PROPERTIES[name]
    return set(property_keys) == expected


def build_plan(facts: ChainNodeFacts) -> ChainNodePlan:
    """Validate all identities and references before constructing Graphiti writes."""

    if EDGE_TYPE_MAP.get(("ChainNode", "IndustryChain")) != ["ChainNodeBelongsToIndustryChain"]:
        raise ProjectionError("ontology does not permit ChainNode membership")
    if EDGE_TYPE_MAP.get(("ChainNode", "ChainNode")) != list(TOPOLOGY_RELATION_NAMES):
        raise ProjectionError("ontology does not permit the required ChainNode topology links")

    nodes_by_id: dict[str, DataChainNodeDTO] = {}
    nodes: list[EntityNode] = []
    for node in facts.chain_nodes:
        if node.id in nodes_by_id:
            raise ProjectionError(f"duplicate ChainNode ID: {node.id}")
        if node.review_status != ReviewStatus.APPROVED:
            raise ProjectionError(f"unapproved ChainNode in snapshot: {node.id}")
        nodes_by_id[node.id] = node
        try:
            attributes = ChainNode(
                data_object_id=node.id,
                aliases=node.aliases,
                definition=node.definition,
                review_status=node.review_status,
                updated_at=node.updated_at,
            ).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            raise ProjectionError(f"ChainNode {node.id} violates ontology: {exc}") from None
        nodes.append(
            EntityNode(
                uuid=node_uuid(node.id),
                name=node.name,
                group_id=GROUP_ID,
                labels=["ChainNode"],
                created_at=node.created_at,
                summary=node.definition or f"产业链节点：{node.name}",
                attributes=attributes,
            )
        )

    planned_at = datetime.now(UTC)
    memberships: set[tuple[str, str]] = set()
    industry_chain_ids: set[str] = set()
    industry_chain_names: dict[str, str] = {}
    edges: list[EntityEdge] = []
    for membership in facts.memberships:
        key = (membership.industry_chain_id, membership.chain_node_id)
        if key in memberships:
            raise ProjectionError(f"duplicate ChainNode membership: {key}")
        memberships.add(key)
        industry_chain_ids.add(membership.industry_chain_id)
        previous_chain_name = industry_chain_names.setdefault(
            membership.industry_chain_id, membership.industry_chain_name
        )
        if previous_chain_name != membership.industry_chain_name:
            raise ProjectionError(f"IndustryChain has conflicting names: {membership.industry_chain_id}")
        node = nodes_by_id.get(membership.chain_node_id)
        if node is None:
            raise ProjectionError(f"membership references missing ChainNode: {key}")
        if node.name != membership.chain_node_name:
            raise ProjectionError(f"membership ChainNode name differs from node snapshot: {key}")
        try:
            attributes = ChainNodeBelongsToIndustryChain(
                position=membership.position,
                contextual_stage=membership.contextual_stage,
            ).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            raise ProjectionError(f"membership {key} violates ontology: {exc}") from None
        relation_name = "ChainNodeBelongsToIndustryChain"
        edges.append(
            EntityEdge(
                uuid=edge_uuid(
                    relation_name,
                    membership.chain_node_id,
                    membership.industry_chain_id,
                ),
                group_id=GROUP_ID,
                source_node_uuid=node_uuid(membership.chain_node_id),
                target_node_uuid=node_uuid(membership.industry_chain_id),
                created_at=planned_at,
                name=relation_name,
                fact=(
                    f"{membership.chain_node_name}是{membership.industry_chain_name}的"
                    f"{STAGE_TEXT[membership.contextual_stage]}第{membership.position}个节点"
                ),
                attributes=attributes,
            )
        )

    relation_counts: Counter[str] = Counter()
    graph_edge_ids: set[str] = set()
    graph_edge_keys: set[tuple[str, str, str, str]] = set()
    for topology in facts.graph_edges:
        if topology.id in graph_edge_ids:
            raise ProjectionError(f"duplicate IndustryChainGraphEdge ID: {topology.id}")
        graph_edge_ids.add(topology.id)
        key = (
            topology.industry_chain_id,
            topology.from_chain_node_id,
            topology.to_chain_node_id,
            topology.relation_type,
        )
        if key in graph_edge_keys:
            raise ProjectionError(f"duplicate IndustryChainGraphEdge endpoints: {key}")
        graph_edge_keys.add(key)
        previous_chain_name = industry_chain_names.setdefault(topology.industry_chain_id, topology.industry_chain_name)
        if previous_chain_name != topology.industry_chain_name:
            raise ProjectionError(f"IndustryChain has conflicting names: {topology.industry_chain_id}")
        for endpoint in (topology.from_chain_node_id, topology.to_chain_node_id):
            if endpoint not in nodes_by_id:
                raise ProjectionError(f"topology references missing ChainNode: {topology.id}")
            if (topology.industry_chain_id, endpoint) not in memberships:
                raise ProjectionError(f"topology endpoint lacks membership in its IndustryChain: {topology.id}")
        if nodes_by_id[topology.from_chain_node_id].name != topology.from_node_name:
            raise ProjectionError(f"topology source name differs from node snapshot: {topology.id}")
        if nodes_by_id[topology.to_chain_node_id].name != topology.to_node_name:
            raise ProjectionError(f"topology target name differs from node snapshot: {topology.id}")
        relation_spec = TOPOLOGY_RELATIONS[topology.relation_type]
        relation_name = relation_spec.name
        try:
            attributes = relation_spec.model(
                data_object_id=topology.id,
                industry_chain_id=topology.industry_chain_id,
            ).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            raise ProjectionError(f"topology {topology.id} violates ontology: {exc}") from None
        edges.append(
            EntityEdge(
                uuid=scoped_edge_uuid(
                    relation_name,
                    topology.industry_chain_id,
                    topology.from_chain_node_id,
                    topology.to_chain_node_id,
                ),
                group_id=GROUP_ID,
                source_node_uuid=node_uuid(topology.from_chain_node_id),
                target_node_uuid=node_uuid(topology.to_chain_node_id),
                created_at=planned_at,
                name=relation_name,
                fact=_topology_fact(topology),
                attributes=attributes,
            )
        )
        relation_counts[relation_name] += 1

    return ChainNodePlan(
        chain_node_count=len(nodes),
        membership_count=len(memberships),
        relation_counts={name: relation_counts[name] for name in TOPOLOGY_RELATION_NAMES},
        nodes=tuple(nodes),
        edges=tuple(edges),
        industry_chain_ids=frozenset(industry_chain_ids),
        industry_chain_names=industry_chain_names,
    )


async def _validate_industry_chains(graphiti: Graphiti, plan: ChainNodePlan) -> None:
    result = await graphiti.driver.execute_query(
        """
        MATCH (chain:Entity {group_id: $group_id})
        WHERE chain.data_object_id IN $industry_chain_ids
        RETURN chain.data_object_id AS data_object_id, chain.uuid AS uuid, chain.name AS name,
               labels(chain) AS labels
        ORDER BY data_object_id
        """,
        group_id=GROUP_ID,
        industry_chain_ids=list(plan.industry_chain_ids),
    )
    by_id: dict[str, list[dict[str, object]]] = {}
    for record in result.records:
        data = record.data()
        by_id.setdefault(str(data["data_object_id"]), []).append(data)
    for chain_id in plan.industry_chain_ids:
        matches = by_id.get(chain_id, [])
        if len(matches) != 1:
            raise ProjectionError(f"IndustryChain target preflight failed: {chain_id} resolves to {len(matches)} nodes")
        if set(matches[0]["labels"]) != {"Entity", "IndustryChain"}:
            raise ProjectionError(f"IndustryChain target has wrong labels: {chain_id}")
        if matches[0]["uuid"] != node_uuid(chain_id):
            raise ProjectionError(f"IndustryChain target has noncanonical UUID: {chain_id}")
        if matches[0]["name"] != plan.industry_chain_names[chain_id]:
            raise ProjectionError(f"IndustryChain target has stale canonical name: {chain_id}")


async def execute_plan(
    graphiti: Graphiti,
    plan: ChainNodePlan,
    *,
    replace: bool,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int, dict[str, int]]:
    await _validate_industry_chains(graphiti, plan)
    return await write_projection(
        graphiti,
        nodes=plan.nodes,
        edges=plan.edges,
        owned_node_labels=frozenset({"ChainNode"}),
        owned_edge_names=OWNED_EDGE_NAMES,
        replace=replace,
        progress=progress,
    )


async def inspect_graph_state(graphiti: Graphiti) -> dict[str, list[dict[str, object]]]:
    node_result = await graphiti.driver.execute_query(
        """
        MATCH (node:ChainNode {group_id: $group_id})
        RETURN node.uuid AS uuid, node.data_object_id AS data_object_id,
               labels(node) AS labels, size(node.name_embedding) AS embedding_dimension
        ORDER BY data_object_id
        """,
        group_id=GROUP_ID,
    )
    edge_result = await graphiti.driver.execute_query(
        """
        MATCH (source:Entity {group_id: $group_id})-[edge:RELATES_TO]->(target:Entity)
        WHERE edge.name IN $relation_names
        RETURN edge.uuid AS uuid, edge.name AS name,
               source.data_object_id AS source_id, target.data_object_id AS target_id,
               source.name AS source_name, target.name AS target_name,
               edge.data_object_id AS data_object_id,
               edge.industry_chain_id AS industry_chain_id,
               edge.position AS position, edge.contextual_stage AS contextual_stage,
               labels(source) AS source_labels, labels(target) AS target_labels,
               size(edge.fact_embedding) AS embedding_dimension,
               edge.created_at IS NOT NULL AS has_created_at,
               keys(edge) AS property_keys
        ORDER BY name, source_id, target_id, industry_chain_id
        """,
        group_id=GROUP_ID,
        relation_names=list(OWNED_EDGE_NAMES),
    )
    return {
        "nodes": [record.data() for record in node_result.records],
        "edges": [record.data() for record in edge_result.records],
    }


def verify_state(
    plan: ChainNodePlan,
    state: dict[str, list[dict[str, object]]],
) -> dict[str, object]:
    nodes = state["nodes"]
    edges = state["edges"]
    expected_node_ids = {node.attributes["data_object_id"] for node in plan.nodes}
    actual_node_ids = {record["data_object_id"] for record in nodes}
    expected_edges = {edge.uuid: edge for edge in plan.edges}
    actual_edges = {str(record["uuid"]): record for record in edges}
    nodes_by_graph_uuid = {node.uuid: node for node in plan.nodes}
    endpoint_ids = {node.uuid: str(node.attributes["data_object_id"]) for node in plan.nodes}
    endpoint_ids.update({node_uuid(chain_id): chain_id for chain_id in plan.industry_chain_ids})
    problems: list[str] = []

    if actual_node_ids != expected_node_ids or len(nodes) != len(expected_node_ids):
        problems.append("ChainNode ID set differs from Data snapshot")
    if any(set(record["labels"]) != {"Entity", "ChainNode"} for record in nodes):
        problems.append("ChainNode labels are not exclusive")
    if any(record["embedding_dimension"] != 1024 for record in nodes):
        problems.append("ChainNode embedding is missing or has wrong dimension")
    if set(actual_edges) != set(expected_edges) or len(edges) != len(expected_edges):
        problems.append("ChainNode relationship set differs from Data snapshot")

    for edge_uuid_value, planned in expected_edges.items():
        actual = actual_edges.get(edge_uuid_value)
        if actual is None:
            continue
        if actual["name"] != planned.name:
            problems.append(f"relationship type differs: {edge_uuid_value}")
            break
        if actual["source_id"] != endpoint_ids[planned.source_node_uuid]:
            problems.append(f"relationship source differs: {edge_uuid_value}")
            break
        if actual["target_id"] != endpoint_ids[planned.target_node_uuid]:
            problems.append(f"relationship target differs: {edge_uuid_value}")
            break
        if actual["embedding_dimension"] != 1024 or not actual["has_created_at"]:
            problems.append(f"relationship vector or created_at is invalid: {edge_uuid_value}")
            break
        if not has_exact_relationship_properties(planned.name, actual["property_keys"]):
            problems.append(f"relationship property set differs: {edge_uuid_value}")
            break
        if set(actual["source_labels"]) != {"Entity", "ChainNode"}:
            problems.append(f"relationship source is not ChainNode: {edge_uuid_value}")
            break
        if actual["source_name"] != nodes_by_graph_uuid[planned.source_node_uuid].name:
            problems.append(f"relationship source name differs: {edge_uuid_value}")
            break
        if planned.name == "ChainNodeBelongsToIndustryChain":
            if (
                actual["position"] != planned.attributes["position"]
                or actual["contextual_stage"] != planned.attributes["contextual_stage"]
            ):
                problems.append(f"membership attributes differ: {edge_uuid_value}")
                break
            if set(actual["target_labels"]) != {"Entity", "IndustryChain"}:
                problems.append(f"membership target is not IndustryChain: {edge_uuid_value}")
                break
            if actual["target_name"] != plan.industry_chain_names[actual["target_id"]]:
                problems.append(f"membership target name differs: {edge_uuid_value}")
                break
        else:
            if (
                actual["data_object_id"] != planned.attributes["data_object_id"]
                or actual["industry_chain_id"] != planned.attributes["industry_chain_id"]
            ):
                problems.append(f"topology attributes differ: {edge_uuid_value}")
                break
            if set(actual["target_labels"]) != {"Entity", "ChainNode"}:
                problems.append(f"topology target is not ChainNode: {edge_uuid_value}")
                break
            if actual["target_name"] != nodes_by_graph_uuid[planned.target_node_uuid].name:
                problems.append(f"topology target name differs: {edge_uuid_value}")
                break

    if problems:
        raise ProjectionError("; ".join(problems))
    return {
        **plan.summary(),
        "node_total": len(nodes),
        "relation_total": len(edges),
        "verified": True,
    }
