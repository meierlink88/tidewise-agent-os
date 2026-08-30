"""Bounded retrieval of existing eligible Analysis Anchors and Variables."""

from __future__ import annotations

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

MAX_ANCHORS_PER_TYPE = 4
SEARCH_LIMIT = 8
SUPPORTED_ANCHOR_TYPES = frozenset(
    {
        AnalysisAnchorType.COUNTRY,
        AnalysisAnchorType.REGION,
        AnalysisAnchorType.GEOPOLITIC_RIVALRY,
        AnalysisAnchorType.MACRO_ECONOMIC,
        AnalysisAnchorType.INDUSTRY_CHAIN,
        AnalysisAnchorType.CHAIN_NODE,
        AnalysisAnchorType.CONCEPT,
    }
)
CLASS_ANCHOR_TYPES = {
    EventClass.GEOPOLITICAL: frozenset(
        {
            AnalysisAnchorType.GEOPOLITIC_RIVALRY,
            AnalysisAnchorType.COUNTRY,
            AnalysisAnchorType.REGION,
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
    EventClass.COMPANY: frozenset(),
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
        class_allowed = CLASS_ANCHOR_TYPES[classification.event_class]
        hinted = set(classification.anchor_type_hints)
        allowed = class_allowed & hinted if hinted else class_allowed
        if not allowed:
            return CandidateSet(anchors=[], variables=[])
        labels = sorted(item.value for item in allowed)
        nodes_by_uuid: dict[str, EntityNode] = {}

        records, _, _ = await self._graphiti.driver.execute_query(
            """
            /* event_analysis_mentioned_anchor_candidates */
            UNWIND $labels AS candidate_label
            CALL (candidate_label) {
                MATCH (episode:Episodic {uuid: $episode_uuid, group_id: $group_id})
                      -[:MENTIONS]->(anchor:Entity)
                WHERE candidate_label IN labels(anchor)
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
        for record in records:
            node = await EntityNode.get_by_uuid(self._graphiti.driver, str(record["uuid"]))
            nodes_by_uuid[node.uuid] = node

        fact_records, _, _ = await self._graphiti.driver.execute_query(
            """
            /* event_analysis_fact_endpoint_candidates */
            UNWIND $labels AS candidate_label
            CALL (candidate_label) {
                MATCH (episode:Episodic {uuid: $episode_uuid, group_id: $group_id})
                MATCH (source:Entity)-[fact:RELATES_TO]->(target:Entity)
                WHERE episode.uuid IN coalesce(fact.episodes, [])
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
        for record in fact_records:
            node = await EntityNode.get_by_uuid(self._graphiti.driver, str(record["uuid"]))
            nodes_by_uuid[node.uuid] = node

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
        for query in classification.retrieval_queries:
            result = await self._graphiti.search_(
                query,
                config=config,
                group_ids=[GRAPHITI_GROUP_ID],
                search_filter=SearchFilters(node_labels=labels),
            )
            for node in result.nodes:
                nodes_by_uuid[node.uuid] = node

        if nodes_by_uuid:
            expanded, _, _ = await self._graphiti.driver.execute_query(
                """
                /* event_analysis_topology_anchor_candidates */
                UNWIND $labels AS candidate_label
                CALL (candidate_label) {
                    UNWIND $anchor_uuids AS anchor_uuid
                    MATCH (anchor:Entity {uuid: anchor_uuid, group_id: $group_id})
                          -[relation:RELATES_TO]-(neighbor:Entity)
                    WHERE relation.name IN $relation_names
                      AND candidate_label IN labels(neighbor)
                    RETURN DISTINCT neighbor.uuid AS uuid
                    ORDER BY uuid
                    LIMIT $per_type_limit
                }
                RETURN DISTINCT uuid
                """,
                anchor_uuids=sorted(nodes_by_uuid),
                group_id=GRAPHITI_GROUP_ID,
                relation_names=TOPOLOGY_RELATION_NAMES,
                labels=labels,
                per_type_limit=MAX_ANCHORS_PER_TYPE,
                routing_="r",
            )
            for record in expanded:
                node = await EntityNode.get_by_uuid(self._graphiti.driver, str(record["uuid"]))
                nodes_by_uuid[node.uuid] = node

        eligible_anchors = [
            candidate
            for node in nodes_by_uuid.values()
            if (candidate := self._anchor_candidate(node, allowed)) is not None
        ]
        anchors: list[AnchorCandidate] = []
        counts: dict[AnalysisAnchorType, int] = {}
        for candidate in eligible_anchors:
            count = counts.get(candidate.entity_type, 0)
            if count >= MAX_ANCHORS_PER_TYPE:
                continue
            counts[candidate.entity_type] = count + 1
            anchors.append(candidate)
        variables = await self._variables(classification)
        return CandidateSet(anchors=anchors, variables=variables)

    async def _variables(self, classification: EventClassification) -> list[VariableCandidate]:
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
        for query in classification.retrieval_queries:
            result = await self._graphiti.search_(
                query,
                config=config,
                group_ids=[GRAPHITI_GROUP_ID],
                search_filter=SearchFilters(node_labels=["Variable"]),
            )
            for node in result.nodes:
                if candidate := self._variable_candidate(node):
                    semantic[candidate.uuid] = candidate

        hints = set(classification.variable_group_hints)
        if hints:
            semantic = {uuid: candidate for uuid, candidate in semantic.items() if candidate.variable_group in hints}

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
        entity_type = next((item for item in allowed if item.value in node.labels), None)
        if entity_type is None:
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
