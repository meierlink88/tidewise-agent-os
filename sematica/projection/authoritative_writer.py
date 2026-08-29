"""Deterministic Graphiti projection writes for Data-owned canonical facts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from graphiti_core import Graphiti
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode

from sematica.projection.runtime import GRAPHITI_GROUP_ID, ProjectionError

GROUP_ID = GRAPHITI_GROUP_ID
EMBEDDING_BATCH_SIZE = 10
WRITE_BATCH_SIZE = 100


def node_uuid(data_object_id: str) -> str:
    """Derive one stable Graphiti identity from an authoritative Data object ID."""

    return str(uuid5(NAMESPACE_URL, f"urn:tidewise:data-object:{data_object_id}"))


def edge_uuid(relation_name: str, source_id: str, target_id: str) -> str:
    """Derive one stable Graphiti relationship identity from logical type and endpoints."""

    return str(
        uuid5(
            NAMESPACE_URL,
            f"urn:tidewise:relation:{relation_name}:{source_id}:{target_id}",
        )
    )


def scoped_edge_uuid(
    relation_name: str,
    scope_id: str,
    source_id: str,
    target_id: str,
) -> str:
    """Derive a stable relationship identity when endpoints may repeat across scopes."""

    return str(
        uuid5(
            NAMESPACE_URL,
            f"urn:tidewise:relation:{relation_name}:{scope_id}:{source_id}:{target_id}",
        )
    )


async def _embed_nodes(
    graphiti: Graphiti,
    nodes: list[EntityNode],
    *,
    progress: Callable[[int, int], None] | None,
    total: int,
) -> int:
    embedded = 0
    for start in range(0, len(nodes), EMBEDDING_BATCH_SIZE):
        batch = nodes[start : start + EMBEDDING_BATCH_SIZE]
        embeddings = await graphiti.embedder.create_batch([node.name for node in batch])
        for node, embedding in zip(batch, embeddings, strict=True):
            node.name_embedding = embedding
        embedded += len(batch)
        if progress is not None:
            progress(embedded, total)
    return embedded


async def _embed_edges(
    graphiti: Graphiti,
    edges: list[EntityEdge],
    *,
    embedded: int,
    progress: Callable[[int, int], None] | None,
    total: int,
) -> None:
    for start in range(0, len(edges), EMBEDDING_BATCH_SIZE):
        batch = edges[start : start + EMBEDDING_BATCH_SIZE]
        embeddings = await graphiti.embedder.create_batch([edge.fact for edge in batch])
        for edge, embedding in zip(batch, embeddings, strict=True):
            edge.fact_embedding = embedding
        embedded += len(batch)
        if progress is not None:
            progress(embedded, total)


async def _save_edges_with_creation_time(
    graphiti: Graphiti,
    edges: list[EntityEdge],
) -> None:
    """Upsert relations while preserving the first successful graph creation time."""

    missing_embedding = next((edge.uuid for edge in edges if edge.fact_embedding is None), None)
    if missing_embedding is not None:
        raise ProjectionError(f"relationship embedding is missing: {missing_embedding}")

    for start in range(0, len(edges), WRITE_BATCH_SIZE):
        batch = edges[start : start + WRITE_BATCH_SIZE]
        created_at = datetime.now(UTC)
        prepared: list[dict[str, object]] = []
        for edge in batch:
            edge_data: dict[str, object] = {
                "uuid": edge.uuid,
                "source_node_uuid": edge.source_node_uuid,
                "target_node_uuid": edge.target_node_uuid,
                "name": edge.name,
                "fact": edge.fact,
                "fact_embedding": edge.fact_embedding,
                "group_id": edge.group_id,
                "episodes": edge.episodes,
                "created_at": created_at,
                "expired_at": edge.expired_at,
                "valid_at": edge.valid_at,
                "invalid_at": edge.invalid_at,
            }
            edge_data.update(edge.attributes or {})
            prepared.append(edge_data)

        await graphiti.driver.execute_query(
            """
            UNWIND $entity_edges AS edge
            MATCH (source:Entity {uuid: edge.source_node_uuid})
            MATCH (target:Entity {uuid: edge.target_node_uuid})
            MERGE (source)-[e:RELATES_TO {uuid: edge.uuid}]->(target)
            WITH e, edge, coalesce(e.created_at, edge.created_at) AS first_created_at
            SET e = edge
            SET e.created_at = first_created_at
            WITH e, edge
            CALL db.create.setRelationshipVectorProperty(
                e,
                "fact_embedding",
                edge.fact_embedding
            )
            RETURN e.uuid AS uuid
            """,
            entity_edges=prepared,
        )


def _unique_nodes(nodes: Sequence[EntityNode]) -> list[EntityNode]:
    nodes_by_uuid: dict[str, EntityNode] = {}
    for node in nodes:
        previous = nodes_by_uuid.get(node.uuid)
        if previous is not None and (
            previous.name != node.name or previous.labels != node.labels or previous.attributes != node.attributes
        ):
            raise ProjectionError(f"conflicting planned node for UUID: {node.uuid}")
        nodes_by_uuid[node.uuid] = node
    return list(nodes_by_uuid.values())


async def _remove_stale_projection_facts(
    graphiti: Graphiti,
    *,
    nodes: list[EntityNode],
    edges: list[EntityEdge],
    owned_node_labels: frozenset[str],
    owned_edge_names: frozenset[str],
) -> dict[str, int]:
    expected_node_uuids = {node.uuid for node in nodes}
    expected_edge_uuids = {edge.uuid for edge in edges}
    existing_nodes = await graphiti.nodes.entity.get_by_group_ids([GROUP_ID])
    stale_nodes = [
        node
        for node in existing_nodes
        if owned_node_labels.intersection(node.labels) and node.uuid not in expected_node_uuids
    ]
    existing_edges = await graphiti.edges.entity.get_by_group_ids([GROUP_ID])
    stale_edges = [
        edge for edge in existing_edges if edge.name in owned_edge_names and edge.uuid not in expected_edge_uuids
    ]

    if stale_edges:
        await graphiti.edges.entity.delete_by_uuids([edge.uuid for edge in stale_edges])
    for node in stale_nodes:
        await graphiti.nodes.entity.delete(node)
    return {"nodes": len(stale_nodes), "relationships": len(stale_edges)}


async def write_projection(
    graphiti: Graphiti,
    *,
    nodes: Sequence[EntityNode],
    edges: Sequence[EntityEdge],
    owned_node_labels: frozenset[str],
    owned_edge_names: frozenset[str],
    replace: bool,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int, dict[str, int]]:
    """Embed and upsert one owned projection scope without LLM identity resolution."""

    unique_nodes = _unique_nodes(nodes)
    unique_edges = list(edges)
    if len({edge.uuid for edge in unique_edges}) != len(unique_edges):
        raise ProjectionError("projection contains duplicate relationship UUIDs")
    for node in unique_nodes:
        if len(owned_node_labels.intersection(node.labels)) != 1:
            raise ProjectionError(f"node {node.uuid} is outside the owned projection labels")
    for edge in unique_edges:
        if edge.name not in owned_edge_names:
            raise ProjectionError(f"edge {edge.uuid} is outside the owned projection relations")

    total = len(unique_nodes) + len(unique_edges)
    embedded = await _embed_nodes(graphiti, unique_nodes, progress=progress, total=total)
    await _embed_edges(
        graphiti,
        unique_edges,
        embedded=embedded,
        progress=progress,
        total=total,
    )

    removed = {"nodes": 0, "relationships": 0}
    if replace:
        removed = await _remove_stale_projection_facts(
            graphiti,
            nodes=unique_nodes,
            edges=unique_edges,
            owned_node_labels=owned_node_labels,
            owned_edge_names=owned_edge_names,
        )
    if unique_nodes:
        await graphiti.nodes.entity.save_bulk(unique_nodes, batch_size=100)
    if unique_edges:
        await _save_edges_with_creation_time(graphiti, unique_edges)
    return len(unique_nodes), len(unique_edges), removed
