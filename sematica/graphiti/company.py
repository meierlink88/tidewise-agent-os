"""Read-only canonical target catalog used by Company inference."""

from __future__ import annotations

from graphiti_core import Graphiti

from capabilities.company import (
    CanonicalChainNode,
    CanonicalIndustry,
    CanonicalIndustryChain,
    ChainMembership,
    IndustryChainMapping,
    TargetCatalog,
)
from sematica.projection.authoritative_writer import edge_uuid, node_uuid
from sematica.projection.runtime import GRAPHITI_GROUP_ID, ProjectionError


async def _records(graphiti: Graphiti, query: str) -> list[dict[str, object]]:
    result = await graphiti.driver.execute_query(query, group_id=GRAPHITI_GROUP_ID)
    return [record.data() for record in result.records]


def _validate_node(record: dict[str, object], label: str) -> str:
    data_object_id = str(record["data_object_id"])
    labels = record["labels"]
    if not isinstance(labels, list) or any(not isinstance(item, str) for item in labels):
        raise ProjectionError(f"canonical {label} {data_object_id} has malformed labels")
    if set(labels) != {"Entity", label}:
        raise ProjectionError(f"canonical {label} {data_object_id} has wrong labels")
    if record["uuid"] != node_uuid(data_object_id):
        raise ProjectionError(f"canonical {label} {data_object_id} does not use its deterministic UUID")
    return data_object_id


async def load_company_target_catalog(graphiti: Graphiti) -> TargetCatalog:
    """Load canonical Industries and topology without invoking Graphiti episodes or extraction."""

    industry_records = await _records(
        graphiti,
        """
        MATCH (industry:Entity:Industry {group_id: $group_id})
        WHERE industry.data_object_id STARTS WITH 'IND'
        OPTIONAL MATCH (industry)-[parent_edge:RELATES_TO]->(parent:Entity:Industry {group_id: $group_id})
        WHERE parent_edge.name = 'IndustryHasParent' AND parent.data_object_id STARTS WITH 'IND'
        RETURN industry.data_object_id AS data_object_id, industry.uuid AS uuid,
               labels(industry) AS labels, industry.name AS name,
               industry.definition AS definition,
               collect(DISTINCT CASE WHEN parent IS NULL THEN NULL ELSE {
                   uuid: parent_edge.uuid,
                   parent_id: parent.data_object_id
               } END) AS parent_edges
        ORDER BY data_object_id
        """,
    )
    chain_records = await _records(
        graphiti,
        """
        MATCH (chain:Entity:IndustryChain {group_id: $group_id})
        WHERE chain.data_object_id STARTS WITH 'ICH'
        RETURN chain.data_object_id AS data_object_id, chain.uuid AS uuid,
               labels(chain) AS labels, chain.name AS name
        ORDER BY data_object_id
        """,
    )
    node_records = await _records(
        graphiti,
        """
        MATCH (node:Entity:ChainNode {group_id: $group_id})
        WHERE node.data_object_id STARTS WITH 'CND'
        RETURN node.data_object_id AS data_object_id, node.uuid AS uuid,
               labels(node) AS labels, node.name AS name, node.definition AS definition
        ORDER BY data_object_id
        """,
    )
    mapping_records = await _records(
        graphiti,
        """
        MATCH (chain:Entity:IndustryChain {group_id: $group_id})-[edge:RELATES_TO]->
              (industry:Entity:Industry {group_id: $group_id})
        WHERE edge.name = 'IndustryChainMappedToIndustry'
          AND chain.data_object_id STARTS WITH 'ICH'
          AND industry.data_object_id STARTS WITH 'IND'
        RETURN edge.uuid AS uuid, chain.data_object_id AS industry_chain_id,
               industry.data_object_id AS industry_id
        ORDER BY industry_chain_id, industry_id
        """,
    )
    membership_records = await _records(
        graphiti,
        """
        MATCH (node:Entity:ChainNode {group_id: $group_id})-[edge:RELATES_TO]->
              (chain:Entity:IndustryChain {group_id: $group_id})
        WHERE edge.name = 'ChainNodeBelongsToIndustryChain'
          AND node.data_object_id STARTS WITH 'CND'
          AND chain.data_object_id STARTS WITH 'ICH'
        RETURN edge.uuid AS uuid, chain.data_object_id AS industry_chain_id,
               node.data_object_id AS chain_node_id
        ORDER BY industry_chain_id, chain_node_id
        """,
    )

    industries: list[CanonicalIndustry] = []
    for record in industry_records:
        industry_id = _validate_node(record, "Industry")
        raw_parent_edges = record["parent_edges"]
        if not isinstance(raw_parent_edges, list) or any(not isinstance(item, dict) for item in raw_parent_edges):
            raise ProjectionError(f"canonical Industry {industry_id} has malformed parents")
        if len(raw_parent_edges) > 1:
            raise ProjectionError(f"canonical Industry {industry_id} has multiple parents")
        parent_id: str | None = None
        if raw_parent_edges:
            parent_edge = raw_parent_edges[0]
            raw_parent_id = parent_edge.get("parent_id")
            if not isinstance(raw_parent_id, str):
                raise ProjectionError(f"canonical Industry {industry_id} has malformed parents")
            if parent_edge.get("uuid") != edge_uuid("IndustryHasParent", industry_id, raw_parent_id):
                raise ProjectionError(f"canonical Industry {industry_id} parent edge UUID is not deterministic")
            parent_id = raw_parent_id
        industries.append(
            CanonicalIndustry(
                industry_id=industry_id,
                name=record["name"],
                definition=record["definition"],
                parent_id=parent_id,
            )
        )
    chains: list[CanonicalIndustryChain] = []
    for record in chain_records:
        chain_id = _validate_node(record, "IndustryChain")
        chains.append(CanonicalIndustryChain(industry_chain_id=chain_id, name=record["name"]))
    nodes: list[CanonicalChainNode] = []
    for record in node_records:
        chain_node_id = _validate_node(record, "ChainNode")
        nodes.append(
            CanonicalChainNode(
                chain_node_id=chain_node_id,
                name=record["name"],
                definition=record["definition"],
            )
        )
    mappings: list[IndustryChainMapping] = []
    for record in mapping_records:
        expected = edge_uuid(
            "IndustryChainMappedToIndustry",
            str(record["industry_chain_id"]),
            str(record["industry_id"]),
        )
        if record["uuid"] != expected:
            raise ProjectionError("canonical IndustryChain mapping UUID is not deterministic")
        mappings.append(
            IndustryChainMapping(
                industry_chain_id=record["industry_chain_id"],
                industry_id=record["industry_id"],
            )
        )
    memberships: list[ChainMembership] = []
    for record in membership_records:
        expected = edge_uuid(
            "ChainNodeBelongsToIndustryChain",
            str(record["chain_node_id"]),
            str(record["industry_chain_id"]),
        )
        if record["uuid"] != expected:
            raise ProjectionError("canonical ChainNode membership UUID is not deterministic")
        memberships.append(
            ChainMembership(
                industry_chain_id=record["industry_chain_id"],
                chain_node_id=record["chain_node_id"],
            )
        )
    if not industries or not chains or not nodes:
        raise ProjectionError("Company inference target catalog is incomplete")
    try:
        return TargetCatalog(
            industries=industries,
            industry_chains=chains,
            chain_nodes=nodes,
            industry_chain_mappings=mappings,
            chain_memberships=memberships,
        )
    except ValueError as exc:
        raise ProjectionError(f"Company inference target catalog is invalid: {exc}") from None


__all__ = ["load_company_target_catalog"]
