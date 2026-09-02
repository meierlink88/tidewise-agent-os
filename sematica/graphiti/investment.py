"""Low-level Graphiti reads used by the investment capability.

This module deliberately contains no investment selection, Signal eligibility, or
reasoning policy. It only exposes bounded Graphiti-native search and group-scoped
graph records; ``capabilities.investment`` owns their business interpretation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from graphiti_core import Graphiti
from graphiti_core.search.search_config import (
    NodeReranker,
    NodeSearchConfig,
    NodeSearchMethod,
    SearchConfig,
)
from graphiti_core.search.search_filters import SearchFilters
from neo4j.exceptions import ClientError

from sematica.projection.runtime import GRAPHITI_GROUP_ID

TOPOLOGY_NAMES = ("ChainNodeInputTo", "ChainNodeIsComponentOf", "ChainNodeDependsOn")
SEARCH_BATCH_SIZE = 20
ANCHOR_SEARCH_CONCURRENCY = 4
ANCHOR_SEARCH_RESULT_LIMIT = 20
INVESTMENT_ANCHOR_LABELS = frozenset(
    {
        "GeopoliticRivalry",
        "MacroEconomic",
        "IndustryChain",
        "ChainNode",
    }
)


class GraphitiInvestmentReader:
    """Return bounded raw graph records without applying investment policy."""

    def __init__(self, graphiti: Graphiti, *, group_id: str = GRAPHITI_GROUP_ID) -> None:
        self._graphiti = graphiti
        self._group_id = group_id

    async def search_anchor_nodes(
        self,
        queries: list[str],
        labels: set[str],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Recall existing investment anchors through Graphiti's native node search.

        Search is deliberately restricted to the four analysis-anchor labels used by
        the layered investment workflow.  The returned nodes are existing graph
        identities; this method never creates or mutates an Entity.
        """

        if limit < 1:
            return []
        unsupported = labels - INVESTMENT_ANCHOR_LABELS
        if unsupported:
            raise ValueError(f"unsupported investment anchor labels: {sorted(unsupported)}")
        if not labels:
            return []
        normalized = list(dict.fromkeys(item.strip()[:2000] for item in queries if item.strip()))
        if not normalized:
            return []

        config = SearchConfig(
            node_config=NodeSearchConfig(
                search_methods=[NodeSearchMethod.bm25, NodeSearchMethod.cosine_similarity],
                reranker=NodeReranker.rrf,
            ),
            limit=min(limit, ANCHOR_SEARCH_RESULT_LIMIT),
        )
        vector_only_config = SearchConfig(
            node_config=NodeSearchConfig(
                search_methods=[NodeSearchMethod.cosine_similarity],
                reranker=NodeReranker.rrf,
            ),
            limit=min(limit, ANCHOR_SEARCH_RESULT_LIMIT),
        )
        search_filter = SearchFilters(node_labels=sorted(labels))
        semaphore = asyncio.Semaphore(ANCHOR_SEARCH_CONCURRENCY)

        async def search(query: str):
            async with semaphore:
                try:
                    return await self._graphiti.search_(
                        query,
                        config=config,
                        group_ids=[self._group_id],
                        search_filter=search_filter,
                    )
                except ClientError as exc:
                    if "TooManyClauses" not in str(exc):
                        raise
                    return await self._graphiti.search_(
                        query,
                        config=vector_only_config,
                        group_ids=[self._group_id],
                        search_filter=search_filter,
                    )

        batches = await asyncio.gather(*(search(query) for query in normalized))
        nodes_by_uuid = {
            node.uuid: node
            for batch in batches
            for node in batch.nodes
            if node.group_id == self._group_id and labels.intersection(node.labels)
        }
        result: list[dict[str, Any]] = []
        for node in nodes_by_uuid.values():
            business_id = (
                node.attributes.get("data_object_id")
                or node.attributes.get("demo_catalog_key")
                or node.attributes.get("policy_key")
            )
            if not isinstance(business_id, str) or not business_id.strip():
                continue
            result.append(
                {
                    "uuid": node.uuid,
                    "business_id": business_id,
                    "name": node.name,
                    "labels": list(node.labels),
                    "summary": node.summary or "",
                }
            )
            if len(result) == limit:
                break
        return result

    async def search_fact_ids(self, queries: list[str], scoped_fact_ids: set[str]) -> list[str]:
        semaphore = asyncio.Semaphore(4)

        async def search(query: str):
            async with semaphore:
                return await self._graphiti.search(query, group_ids=[self._group_id], num_results=30)

        normalized = list(dict.fromkeys(item.strip()[:2000] for item in queries if item.strip()))
        batches = []
        for offset in range(0, len(normalized), SEARCH_BATCH_SIZE):
            batches.extend(
                await asyncio.gather(*(search(query) for query in normalized[offset : offset + SEARCH_BATCH_SIZE]))
            )
        return list(dict.fromkeys(edge.uuid for batch in batches for edge in batch if edge.uuid in scoped_fact_ids))

    async def load_events(self, start: datetime, decision_at: datetime, *, limit: int) -> list[dict[str, Any]]:
        records, _, _ = await self._graphiti.driver.execute_query(
            """
            MATCH (event:Episodic {group_id: $group_id})
            WHERE event.episode_kind = 'EVENT'
              AND event.valid_at >= $start AND event.valid_at <= $decision_at
            RETURN event.uuid AS episode_uuid, event.domain_object_id AS event_id,
                   event.name AS name, event.content AS content, event.valid_at AS valid_at
            ORDER BY event.valid_at, event.uuid
            LIMIT $limit
            """,
            group_id=self._group_id,
            start=start,
            decision_at=decision_at,
            limit=limit,
            routing_="r",
        )
        return [dict(record) for record in records]

    async def load_mentions(self, episode_ids: list[str]) -> list[dict[str, Any]]:
        records, _, _ = await self._graphiti.driver.execute_query(
            """
            MATCH (event:Episodic)-[mention:MENTIONS]->(entity:Entity)
            WHERE event.uuid IN $episode_ids
              AND event.group_id = $group_id
              AND entity.group_id = $group_id
              AND mention.group_id = $group_id
            RETURN event.uuid AS episode_uuid, entity.uuid AS uuid,
                   coalesce(entity.data_object_id, entity.demo_catalog_key, entity.policy_key) AS business_id,
                   entity.name AS name, labels(entity) AS labels
            """,
            episode_ids=episode_ids,
            group_id=self._group_id,
            routing_="r",
        )
        return [dict(record) for record in records]

    async def load_facts(
        self,
        event_ids: list[str],
        episode_ids: list[str],
        decision_at: datetime,
        latest_considered: datetime,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        # Keep the compatibility parameter for callers that already pass a
        # forward horizon. Facts used for an as-of decision must never be read
        # from the future, regardless of that analysis horizon.
        del latest_considered
        records, _, _ = await self._graphiti.driver.execute_query(
            """
            MATCH (source:Entity)-[fact:RELATES_TO]->(target:Entity)
            WHERE fact.group_id = $group_id
              AND source.group_id = $group_id AND target.group_id = $group_id
              AND (fact.invalid_at IS NULL OR fact.invalid_at > $decision_at)
              AND (fact.valid_at IS NULL OR fact.valid_at <= $decision_at)
              AND (fact.created_at IS NULL OR fact.created_at <= $decision_at)
              AND (
                any(episode IN coalesce(fact.episodes, []) WHERE episode IN $episode_ids)
                OR any(event_id IN coalesce(fact.source_event_ids, []) WHERE event_id IN $event_ids)
              )
            RETURN source.uuid AS source_uuid, source.name AS source_name,
                   coalesce(source.data_object_id, source.demo_catalog_key, source.policy_key)
                       AS source_business_id,
                   labels(source) AS source_labels,
                   target.uuid AS target_uuid, target.name AS target_name,
                   coalesce(target.data_object_id, target.demo_catalog_key, target.policy_key)
                       AS target_business_id,
                   labels(target) AS target_labels,
                   fact.uuid AS uuid, fact.name AS name, fact.fact AS text,
                   fact.episodes AS source_episode_ids, fact.source_event_ids AS source_event_ids,
                   fact.variable_id AS variable_id, source.variable_role AS variable_role,
                   source.variable_group AS variable_group, source.definition AS variable_definition,
                   source.measurement_basis AS variable_measurement_basis,
                   fact.direction AS direction, fact.magnitude AS magnitude,
                   fact.event_class AS event_class, fact.anchor_type AS anchor_type,
                   fact.horizon_tags AS horizon_tags, fact.valid_at AS valid_at,
                   fact.invalid_at AS invalid_at, fact.expected_end_latest AS expected_end_latest,
                   fact.assertion_modality AS assertion_modality, fact.mechanism AS mechanism,
                   fact.mechanism_confidence AS mechanism_confidence,
                   fact.provenance_confidence AS provenance_confidence,
                   fact.temporal_confidence AS temporal_confidence
            ORDER BY fact.valid_at, fact.uuid
            LIMIT $limit
            """,
            group_id=self._group_id,
            event_ids=event_ids,
            episode_ids=episode_ids,
            decision_at=decision_at,
            limit=limit,
            routing_="r",
        )
        return [dict(record) for record in records]

    async def load_anchor_facts(
        self,
        anchor_uuids: list[str],
        decision_at: datetime,
        latest_considered: datetime,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Read temporally eligible Facts whose source or target is a candidate anchor.

        Native node search finds the semantic candidates; this exact endpoint read is
        the bounded second half of retrieval.  It preserves Graphiti's ``valid_at`` /
        ``invalid_at`` semantics and returns the same record contract as
        :meth:`load_facts`.
        """

        normalized = list(dict.fromkeys(item for item in anchor_uuids if item))
        if not normalized or limit < 1:
            return []
        del latest_considered
        records, _, _ = await self._graphiti.driver.execute_query(
            """
            MATCH (source:Entity)-[fact:RELATES_TO]->(target:Entity)
            WHERE fact.group_id = $group_id
              AND source.group_id = $group_id AND target.group_id = $group_id
              AND (source.uuid IN $anchor_uuids OR target.uuid IN $anchor_uuids)
              AND (fact.invalid_at IS NULL OR fact.invalid_at > $decision_at)
              AND (fact.valid_at IS NULL OR fact.valid_at <= $decision_at)
              AND (fact.created_at IS NULL OR fact.created_at <= $decision_at)
            RETURN source.uuid AS source_uuid, source.name AS source_name,
                   coalesce(source.data_object_id, source.demo_catalog_key, source.policy_key)
                       AS source_business_id,
                   labels(source) AS source_labels,
                   target.uuid AS target_uuid, target.name AS target_name,
                   coalesce(target.data_object_id, target.demo_catalog_key, target.policy_key)
                       AS target_business_id,
                   labels(target) AS target_labels,
                   fact.uuid AS uuid, fact.name AS name, fact.fact AS text,
                   fact.episodes AS source_episode_ids, fact.source_event_ids AS source_event_ids,
                   fact.variable_id AS variable_id, source.variable_role AS variable_role,
                   source.variable_group AS variable_group, source.definition AS variable_definition,
                   source.measurement_basis AS variable_measurement_basis,
                   fact.direction AS direction, fact.magnitude AS magnitude,
                   fact.event_class AS event_class, fact.anchor_type AS anchor_type,
                   fact.horizon_tags AS horizon_tags, fact.valid_at AS valid_at,
                   fact.invalid_at AS invalid_at, fact.expected_end_latest AS expected_end_latest,
                   fact.assertion_modality AS assertion_modality, fact.mechanism AS mechanism,
                   fact.mechanism_confidence AS mechanism_confidence,
                   fact.provenance_confidence AS provenance_confidence,
                   fact.temporal_confidence AS temporal_confidence
            ORDER BY fact.valid_at, fact.uuid
            LIMIT $limit
            """,
            group_id=self._group_id,
            anchor_uuids=normalized,
            decision_at=decision_at,
            limit=limit,
            routing_="r",
        )
        return [dict(record) for record in records]

    async def load_chain_candidates(
        self,
        anchor_node_ids: set[str],
        direct_chain_ids: set[str],
        *,
        limit: int,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1 or offset < 0:
            return []
        records, _, _ = await self._graphiti.driver.execute_query(
            """
            MATCH (node:ChainNode)-[membership:RELATES_TO]->(chain:IndustryChain)
            WHERE membership.name = 'ChainNodeBelongsToIndustryChain'
              AND membership.group_id = $group_id
              AND node.group_id = $group_id AND chain.group_id = $group_id
              AND (node.data_object_id IN $anchor_node_ids OR chain.data_object_id IN $direct_chain_ids)
            RETURN chain.uuid AS uuid, chain.data_object_id AS business_id, chain.name AS name,
                   chain.data_object_id IN $direct_chain_ids AS direct_match,
                   collect(DISTINCT CASE WHEN node.data_object_id IN $anchor_node_ids
                                        THEN node.data_object_id ELSE null END) AS matched_node_ids
            ORDER BY direct_match DESC, size(matched_node_ids) DESC, name, business_id
            SKIP $offset
            LIMIT $limit
            """,
            group_id=self._group_id,
            anchor_node_ids=sorted(anchor_node_ids),
            direct_chain_ids=sorted(direct_chain_ids),
            offset=offset,
            limit=limit,
            routing_="r",
        )
        return [dict(record) for record in records]

    async def load_chain_nodes(self, chain_ids: list[str], *, limit: int) -> list[dict[str, Any]]:
        records, _, _ = await self._graphiti.driver.execute_query(
            """
            MATCH (node:ChainNode)-[membership:RELATES_TO]->(chain:IndustryChain)
            WHERE membership.name = 'ChainNodeBelongsToIndustryChain'
              AND membership.group_id = $group_id
              AND node.group_id = $group_id AND chain.group_id = $group_id
              AND chain.data_object_id IN $chain_ids
            RETURN chain.data_object_id AS chain_id, node.uuid AS uuid,
                   node.data_object_id AS business_id, node.name AS name,
                   membership.contextual_stage AS stage, membership.position AS position
            ORDER BY chain_id, position, name
            LIMIT $limit
            """,
            group_id=self._group_id,
            chain_ids=chain_ids,
            limit=limit,
            routing_="r",
        )
        return [dict(record) for record in records]

    async def load_topology_edges(self, chain_ids: list[str], *, limit: int) -> list[dict[str, Any]]:
        records, _, _ = await self._graphiti.driver.execute_query(
            """
            MATCH (source:ChainNode)-[edge:RELATES_TO]->(target:ChainNode)
            WHERE edge.name IN $topology_names
              AND edge.group_id = $group_id
              AND source.group_id = $group_id AND target.group_id = $group_id
              AND edge.industry_chain_id IN $chain_ids
            RETURN edge.industry_chain_id AS chain_id, edge.uuid AS uuid,
                   edge.data_object_id AS business_id, edge.name AS name, edge.fact AS fact,
                   source.data_object_id AS source_node_id, source.name AS source_name,
                   target.data_object_id AS target_node_id, target.name AS target_name
            ORDER BY chain_id, name, source_name, target_name
            LIMIT $limit
            """,
            group_id=self._group_id,
            topology_names=list(TOPOLOGY_NAMES),
            chain_ids=chain_ids,
            limit=limit,
            routing_="r",
        )
        return [dict(record) for record in records]
