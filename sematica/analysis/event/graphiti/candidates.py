"""Bounded retrieval of existing eligible Analysis Anchors and Variables."""

from __future__ import annotations

import logging
from time import perf_counter

from graphiti_core import Graphiti
from graphiti_core.nodes import EntityNode
from graphiti_core.search.search_config import (
    NodeReranker,
    NodeSearchConfig,
    NodeSearchMethod,
    SearchConfig,
)
from graphiti_core.search.search_filters import SearchFilters

from sematica.analysis.event.contracts import (
    AnchorCandidate,
    CandidateSet,
    EventAnalysisInput,
    EventClass,
    EventClassification,
    VariableCandidate,
)
from sematica.ontology.enums import AnalysisAnchorType, VariableGroup
from sematica.projection.runtime import GRAPHITI_GROUP_ID

logger = logging.getLogger(__name__)

MAX_ANCHORS_PER_TYPE = 8
MAX_ANCHOR_CANDIDATES = 30
MAX_SEARCH_INTENTS = 5
SEARCH_LIMIT = 8
RETRIEVAL_SOURCE_RANK = {
    "EXACT": 0,
    "MENTION": 1,
    "FACT": 2,
    "SEMANTIC": 3,
    "TOPOLOGY": 4,
}
CLASS_ANCHOR_TYPES = {
    EventClass.GEOPOLITICAL: frozenset(
        {
            AnalysisAnchorType.GEOPOLITIC_RIVALRY,
            AnalysisAnchorType.COUNTRY,
            AnalysisAnchorType.REGION,
            AnalysisAnchorType.MACRO_ECONOMIC,
            AnalysisAnchorType.INDUSTRY_CHAIN,
            AnalysisAnchorType.CHAIN_NODE,
        }
    ),
    EventClass.MACRO_ECONOMIC: frozenset(
        {
            AnalysisAnchorType.MACRO_ECONOMIC,
            AnalysisAnchorType.COUNTRY,
            AnalysisAnchorType.INDUSTRY_CHAIN,
            AnalysisAnchorType.CHAIN_NODE,
        }
    ),
    EventClass.INDUSTRY_CHAIN: frozenset({AnalysisAnchorType.INDUSTRY_CHAIN, AnalysisAnchorType.CHAIN_NODE}),
    EventClass.CHAIN_NODE: frozenset({AnalysisAnchorType.CHAIN_NODE, AnalysisAnchorType.INDUSTRY_CHAIN}),
    EventClass.COMPANY: frozenset({AnalysisAnchorType.INDUSTRY_CHAIN, AnalysisAnchorType.CHAIN_NODE}),
}
TOPOLOGY_RELATION_NAMES = [
    "ChainNodeBelongsToIndustryChain",
    "ChainNodeInputTo",
    "ChainNodeIsComponentOf",
    "ChainNodeDependsOn",
]


class GraphitiCandidateRetriever:
    """Recall candidates without ever constructing an EntityNode."""

    def __init__(self, graphiti: Graphiti) -> None:
        self._graphiti = graphiti

    async def retrieve(self, event: EventAnalysisInput, classification: EventClassification) -> CandidateSet:
        started_at = perf_counter()
        allowed = CLASS_ANCHOR_TYPES[classification.event_class]
        if not allowed:
            return CandidateSet(anchors=[], variables=[])
        labels = sorted(item.value for item in allowed)
        nodes_by_uuid: dict[str, EntityNode] = {}
        retrieval_sources: dict[str, set[str]] = {}
        semantic_relevance: dict[str, float] = {}
        search_intents = self._search_intents(event, classification)

        exact_records, _, _ = await self._graphiti.driver.execute_query(
            """
            /* event_analysis_exact_anchor_candidates */
            MATCH (anchor:Entity {group_id: $group_id})
            WHERE any(label IN $labels WHERE label IN labels(anchor))
              AND (
                toLower(anchor.name) IN $terms
                OR any(alias IN coalesce(anchor.aliases, []) WHERE toLower(alias) IN $terms)
              )
            RETURN DISTINCT anchor.uuid AS uuid
            ORDER BY uuid
            LIMIT $limit
            """,
            group_id=GRAPHITI_GROUP_ID,
            labels=labels,
            terms=[item.casefold() for item in search_intents],
            limit=MAX_ANCHOR_CANDIDATES,
            routing_="r",
        )
        self._record_sources(exact_records, retrieval_sources, source="EXACT")

        records, _, _ = await self._graphiti.driver.execute_query(
            """
            /* event_analysis_mentioned_anchor_candidates */
            UNWIND $labels AS candidate_label
            CALL (candidate_label) {
                MATCH (episode:Episodic {uuid: $episode_uuid, group_id: $group_id})
                      -[:MENTIONS]->(anchor:Entity)
                WHERE anchor.group_id = $group_id
                  AND candidate_label IN labels(anchor)
                RETURN DISTINCT anchor.uuid AS uuid
                ORDER BY uuid
                LIMIT $per_type_limit
            }
            RETURN DISTINCT uuid
            """,
            episode_uuid=event.episode_uuid,
            group_id=GRAPHITI_GROUP_ID,
            labels=labels,
            per_type_limit=MAX_ANCHORS_PER_TYPE,
            routing_="r",
        )
        self._record_sources(records, retrieval_sources, source="MENTION")

        fact_records, _, _ = await self._graphiti.driver.execute_query(
            """
            /* event_analysis_fact_endpoint_candidates */
            UNWIND $labels AS candidate_label
            CALL (candidate_label) {
                MATCH (episode:Episodic {uuid: $episode_uuid, group_id: $group_id})
                MATCH (source:Entity)-[fact:RELATES_TO]->(target:Entity)
                WHERE episode.uuid IN coalesce(fact.episodes, [])
                  AND fact.group_id = $group_id
                  AND source.group_id = $group_id
                  AND target.group_id = $group_id
                WITH candidate_label, collect(source) + collect(target) AS endpoints
                UNWIND endpoints AS anchor
                WITH DISTINCT candidate_label, anchor
                WHERE candidate_label IN labels(anchor)
                RETURN anchor.uuid AS uuid
                ORDER BY uuid
                LIMIT $per_type_limit
            }
            RETURN DISTINCT uuid
            """,
            episode_uuid=event.episode_uuid,
            group_id=GRAPHITI_GROUP_ID,
            labels=labels,
            per_type_limit=MAX_ANCHORS_PER_TYPE,
            routing_="r",
        )
        self._record_sources(fact_records, retrieval_sources, source="FACT")

        config = SearchConfig(
            node_config=NodeSearchConfig(
                search_methods=[
                    NodeSearchMethod.bm25,
                    NodeSearchMethod.cosine_similarity,
                ],
                reranker=NodeReranker.rrf,
            ),
            limit=SEARCH_LIMIT,
        )
        for query in search_intents:
            result = await self._graphiti.search_(
                query,
                config=config,
                group_ids=[GRAPHITI_GROUP_ID],
                search_filter=SearchFilters(node_labels=labels),
            )
            scores = (
                result.node_reranker_scores
                if len(result.node_reranker_scores) == len(result.nodes)
                else [0.0] * len(result.nodes)
            )
            for node, score in zip(result.nodes, scores, strict=True):
                nodes_by_uuid[node.uuid] = node
                retrieval_sources.setdefault(node.uuid, set()).add("SEMANTIC")
                semantic_relevance[node.uuid] = max(semantic_relevance.get(node.uuid, float("-inf")), float(score))

        if retrieval_sources:
            expanded, _, _ = await self._graphiti.driver.execute_query(
                """
                /* event_analysis_topology_anchor_candidates */
                UNWIND $labels AS candidate_label
                CALL (candidate_label) {
                    UNWIND $anchor_uuids AS anchor_uuid
                    MATCH (anchor:Entity {uuid: anchor_uuid, group_id: $group_id})
                          -[relation:RELATES_TO]-(neighbor:Entity)
                    WHERE relation.name IN $relation_names
                      AND neighbor.group_id = $group_id
                      AND candidate_label IN labels(neighbor)
                    RETURN DISTINCT neighbor.uuid AS uuid
                    ORDER BY uuid
                    LIMIT $per_type_limit
                }
                RETURN DISTINCT uuid
                """,
                anchor_uuids=sorted(retrieval_sources),
                group_id=GRAPHITI_GROUP_ID,
                relation_names=TOPOLOGY_RELATION_NAMES,
                labels=labels,
                per_type_limit=MAX_ANCHORS_PER_TYPE,
                routing_="r",
            )
            self._record_sources(expanded, retrieval_sources, source="TOPOLOGY")

        missing_uuids = sorted(set(retrieval_sources) - set(nodes_by_uuid))
        if missing_uuids:
            loaded = await EntityNode.get_by_uuids(
                self._graphiti.driver,
                missing_uuids,
                group_id=GRAPHITI_GROUP_ID,
            )
            nodes_by_uuid.update((node.uuid, node) for node in loaded)

        eligible_anchors = [
            (
                candidate.model_copy(
                    update={
                        "retrieval_sources": sorted(
                            retrieval_sources.get(node.uuid, set()),
                            key=lambda item: (RETRIEVAL_SOURCE_RANK[item], item),
                        )
                    }
                ),
                min(
                    (RETRIEVAL_SOURCE_RANK[item] for item in retrieval_sources.get(node.uuid, {"TOPOLOGY"})),
                    default=4,
                ),
            )
            for node in nodes_by_uuid.values()
            if (candidate := self._anchor_candidate(node, allowed)) is not None
        ]
        hint_rank = {entity_type: index for index, entity_type in enumerate(classification.anchor_type_hints)}
        eligible_anchors.sort(
            key=lambda item: (
                item[1],
                -semantic_relevance.get(item[0].uuid, 0.0),
                hint_rank.get(item[0].entity_type, len(hint_rank)),
                item[0].name,
                item[0].uuid,
            )
        )
        anchors: list[AnchorCandidate] = []
        counts: dict[AnalysisAnchorType, int] = {}
        for candidate, _rank in eligible_anchors:
            if len(anchors) >= MAX_ANCHOR_CANDIDATES:
                break
            count = counts.get(candidate.entity_type, 0)
            if count >= MAX_ANCHORS_PER_TYPE:
                continue
            counts[candidate.entity_type] = count + 1
            anchors.append(candidate)
        variables = await self._variables(classification, search_intents)
        logger.info(
            "event_signal_candidate_retrieval event_id=%s intents=%d anchor_searches=%d "
            "variable_searches=1 anchors=%d variables=%d elapsed_ms=%.1f",
            event.event.id,
            len(search_intents),
            len(search_intents),
            len(anchors),
            len(variables),
            (perf_counter() - started_at) * 1000,
        )
        return CandidateSet(anchors=anchors, variables=variables)

    async def _variables(
        self,
        classification: EventClassification,
        search_intents: list[str],
    ) -> list[VariableCandidate]:
        semantic: dict[str, VariableCandidate] = {}
        config = SearchConfig(
            node_config=NodeSearchConfig(
                search_methods=[
                    NodeSearchMethod.bm25,
                    NodeSearchMethod.cosine_similarity,
                ],
                reranker=NodeReranker.rrf,
            ),
            limit=SEARCH_LIMIT,
        )
        result = await self._graphiti.search_(
            " ".join(search_intents),
            config=config,
            group_ids=[GRAPHITI_GROUP_ID],
            search_filter=SearchFilters(node_labels=["Variable"]),
        )
        for node in result.nodes:
            if candidate := self._variable_candidate(node):
                semantic[candidate.uuid] = candidate

        hints = set(classification.variable_group_hints)

        records, _, _ = await self._graphiti.driver.execute_query(
            """
            /* event_analysis_fundamental_variable_candidates */
            MATCH (variable:Variable {group_id: $group_id})
            WHERE variable.variable_role = 'FUNDAMENTAL'
              AND (size($group_hints) = 0 OR variable.variable_group IN $group_hints)
            RETURN variable.uuid AS uuid, variable.name AS name,
                   variable.variable_id AS variable_id,
                   variable.variable_group AS variable_group,
                   variable.allowed_anchor_types AS allowed_anchor_types,
                   variable.definition AS definition
            ORDER BY CASE WHEN variable.variable_group IN $group_hints THEN 0 ELSE 1 END,
                     variable.variable_id
            """,
            group_id=GRAPHITI_GROUP_ID,
            group_hints=[item.value for item in classification.variable_group_hints],
            routing_="r",
        )
        fallback = [
            VariableCandidate(
                uuid=str(row["uuid"]),
                variable_id=str(row["variable_id"]),
                name=str(row["name"]),
                variable_group=VariableGroup(str(row["variable_group"])),
                allowed_anchor_types=list(row["allowed_anchor_types"]),
                definition=str(row["definition"]),
            )
            for row in records
            if not hints or VariableGroup(str(row["variable_group"])) in hints
        ]
        ordered = [*semantic.values(), *fallback]
        unique: dict[str, VariableCandidate] = {}
        for candidate in ordered:
            unique.setdefault(candidate.uuid, candidate)
        return sorted(
            unique.values(),
            key=lambda item: (
                0 if item.uuid in semantic else 1,
                0 if item.variable_group in hints else 1,
                item.variable_id,
            ),
        )

    @staticmethod
    def _search_intents(event: EventAnalysisInput, classification: EventClassification) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        semantic = event.event.event.semantic
        raw_candidates = [
            *classification.retrieval_queries,
            *semantic.objects,
            *semantic.actors,
            *semantic.jurisdictions,
        ]
        for raw in raw_candidates:
            value = " ".join(raw.split()).strip()
            key = value.casefold()
            if not value or key in seen:
                continue
            unique.append(value)
            seen.add(key)
            if len(unique) >= MAX_SEARCH_INTENTS:
                break
        if unique:
            return unique
        fallback = " ".join([semantic.action, *semantic.objects]).strip() or event.event.event.title
        return [fallback]

    @staticmethod
    def _record_sources(
        records,
        retrieval_sources: dict[str, set[str]],
        *,
        source: str,
    ) -> None:
        for record in records:
            retrieval_sources.setdefault(str(record["uuid"]), set()).add(source)

    @staticmethod
    def _variable_candidate(node: EntityNode) -> VariableCandidate | None:
        attributes = node.attributes
        if "Variable" not in node.labels or attributes.get("variable_role") != "FUNDAMENTAL":
            return None
        try:
            return VariableCandidate(
                uuid=node.uuid,
                variable_id=str(attributes["variable_id"]),
                name=node.name,
                variable_group=VariableGroup(str(attributes["variable_group"])),
                allowed_anchor_types=list(attributes["allowed_anchor_types"]),
                definition=str(attributes["definition"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _anchor_candidate(
        node: EntityNode, allowed: set[AnalysisAnchorType] | frozenset[AnalysisAnchorType]
    ) -> AnchorCandidate | None:
        if node.group_id != GRAPHITI_GROUP_ID:
            return None
        entity_type = next((item for item in allowed if item.value in node.labels), None)
        if entity_type is None or entity_type == AnalysisAnchorType.INDUSTRY_CHAIN:
            return None
        business_id = (
            node.attributes.get("data_object_id")
            or node.attributes.get("demo_catalog_key")
            or node.attributes.get("policy_key")
        )
        if not isinstance(business_id, str) or not business_id.strip():
            return None
        return AnchorCandidate(
            uuid=node.uuid,
            name=node.name,
            entity_type=entity_type,
            business_id=business_id,
            summary=node.summary or "",
        )
