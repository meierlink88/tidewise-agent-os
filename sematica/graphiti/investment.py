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

from sematica.projection.runtime import GRAPHITI_GROUP_ID

TOPOLOGY_NAMES = ("ChainNodeInputTo", "ChainNodeIsComponentOf", "ChainNodeDependsOn")
SEARCH_BATCH_SIZE = 20


class GraphitiInvestmentReader:
    """Return bounded raw graph records without applying investment policy."""

    def __init__(self, graphiti: Graphiti, *, group_id: str = GRAPHITI_GROUP_ID) -> None:
        self._graphiti = graphiti
        self._group_id = group_id

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
                   entity.data_object_id AS business_id, entity.name AS name, labels(entity) AS labels
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
        records, _, _ = await self._graphiti.driver.execute_query(
            """
            MATCH (source:Entity)-[fact:RELATES_TO]->(target:Entity)
            WHERE fact.group_id = $group_id
              AND source.group_id = $group_id AND target.group_id = $group_id
              AND (fact.invalid_at IS NULL OR fact.invalid_at > $decision_at)
              AND (fact.valid_at IS NULL OR fact.valid_at <= $latest_considered)
              AND (
                any(episode IN coalesce(fact.episodes, []) WHERE episode IN $episode_ids)
                OR any(event_id IN coalesce(fact.source_event_ids, []) WHERE event_id IN $event_ids)
              )
            RETURN source.uuid AS source_uuid, source.name AS source_name,
                   source.data_object_id AS source_business_id, labels(source) AS source_labels,
                   target.uuid AS target_uuid, target.name AS target_name,
                   target.data_object_id AS target_business_id, labels(target) AS target_labels,
                   fact.uuid AS uuid, fact.name AS name, fact.fact AS text,
                   fact.episodes AS source_episode_ids, fact.source_event_ids AS source_event_ids,
                   fact.variable_id AS variable_id, source.variable_role AS variable_role,
                   source.variable_group AS variable_group, source.definition AS variable_definition,
                   source.measurement_basis AS variable_measurement_basis,
                   fact.direction AS direction, fact.magnitude AS magnitude,
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
            latest_considered=latest_considered,
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
    ) -> list[dict[str, Any]]:
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
            LIMIT $limit
            """,
            group_id=self._group_id,
            anchor_node_ids=sorted(anchor_node_ids),
            direct_chain_ids=sorted(direct_chain_ids),
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
