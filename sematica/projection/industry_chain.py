"""IndustryChain facts and canonical mappings projected from Tidewise Data."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime
from typing import Literal

import httpx
from graphiti_core import Graphiti
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from sematica.ontology import (
    EDGE_TYPE_MAP,
    IndustryChain,
    IndustryChainMappedToConcept,
    IndustryChainMappedToIndustry,
)
from sematica.ontology.enums import RecordStatus, ReviewStatus
from sematica.projection.authoritative_writer import GROUP_ID, edge_uuid, node_uuid, write_projection
from sematica.projection.runtime import ProjectionError, RuntimeConfig

INDUSTRY_CHAINS_PATH = "/api/data/v1/entities/industry-chains"
RESEARCH_GRAPH_PATH = "/api/data/v1/research-graph:search"
PAGE_SIZE = 100
SEARCH_BATCH_SIZE = 20
SEARCH_CONCURRENCY = 4
MAPPING_NAMES = {
    "mapped_to_industry": "IndustryChainMappedToIndustry",
    "mapped_to_concept": "IndustryChainMappedToConcept",
}
TARGET_TYPES = {
    "mapped_to_industry": "industry",
    "mapped_to_concept": "concept",
}
TARGET_LABELS = {"industry": "Industry", "concept": "Concept"}


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == UTC.utcoffset(value)


class DataIndustryChainDTO(BaseModel):
    """Frozen consumer contract for one Data-owned IndustryChain fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str = Field(min_length=1)
    aliases: list[str]
    scope: str = Field(min_length=1)
    target_output: str = Field(min_length=1)
    end_use: str = Field(min_length=1)
    geography: str = Field(min_length=1)
    primary_country_id: str | None
    as_of_date: date
    review_status: ReviewStatus
    review_note: str | None
    technology_route_qualifier: str | None
    observable_variables: list[str] = Field(min_length=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("aliases", "observable_variables")
    @classmethod
    def strings_must_be_nonblank_and_unique(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("list values must not be blank")
        if len(values) != len(set(values)):
            raise ValueError("list values must be unique")
        return values

    @model_validator(mode="after")
    def timestamps_must_be_consistent(self) -> DataIndustryChainDTO:
        if not _is_utc(self.created_at) or not _is_utc(self.updated_at):
            raise ValueError("IndustryChain timestamps must be explicit UTC")
        if self.updated_at < self.created_at:
            raise ValueError("IndustryChain updated_at precedes created_at")
        return self


class IndustryChainList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[DataIndustryChainDTO]
    next_cursor: str | None


class IndustryChainListEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=128)
    result: IndustryChainList


class DataResearchEntityDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_id: str
    entity_type: str
    name: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    aliases: list[str]
    status: RecordStatus


class DataMappingDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_relation_id: str
    from_entity_id: str
    to_entity_id: str
    relation_type: Literal["mapped_to_industry", "mapped_to_concept"]
    status: RecordStatus


class DataRelationDefinitionDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relation_type: str
    direction: Literal["directed"]


class ResearchGraphResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["research-graph-search.v1"]
    analysis_as_of: datetime
    query_fingerprint: str
    graph_fingerprint: str
    actual_depth: int
    entities: list[DataResearchEntityDTO]
    relation_definitions: list[DataRelationDefinitionDTO]
    entity_relations: list[DataMappingDTO]
    industry_chains: list[dict[str, object]]
    industry_chain_memberships: list[dict[str, object]]
    industry_chain_graph_edges: list[dict[str, object]]


class ResearchGraphEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=128)
    result: ResearchGraphResult


class IndustryChainFacts(BaseModel):
    model_config = ConfigDict(frozen=True)

    industry_chains: tuple[DataIndustryChainDTO, ...]
    entities: tuple[DataResearchEntityDTO, ...]
    mappings: tuple[DataMappingDTO, ...]


class IndustryChainPlan(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    industry_chain_count: int
    industry_mapping_count: int
    concept_mapping_count: int
    nodes: tuple[EntityNode, ...]
    edges: tuple[EntityEdge, ...]
    target_types: dict[str, str]

    def summary(self) -> dict[str, object]:
        return {
            "group_id": GROUP_ID,
            "industry_chains": self.industry_chain_count,
            "mapped_to_industry": self.industry_mapping_count,
            "mapped_to_concept": self.concept_mapping_count,
        }


async def _request_with_retry(
    operation: Callable[[], Awaitable[httpx.Response]],
) -> httpx.Response:
    for attempt in range(2):
        try:
            response = await operation()
        except httpx.TransportError:
            if attempt == 0:
                await asyncio.sleep(0.05)
                continue
            raise
        if attempt == 0 and (response.status_code == 429 or response.status_code >= 500):
            await asyncio.sleep(0.05)
            continue
        return response
    raise AssertionError("unreachable retry state")


async def _load_chains(client: httpx.AsyncClient, base_url: str) -> list[DataIndustryChainDTO]:
    items: list[DataIndustryChainDTO] = []
    cursor: str | None = None
    observed_cursors: set[str] = set()
    while True:
        params: dict[str, object] = {"page_size": PAGE_SIZE}
        if cursor is not None:
            params["cursor"] = cursor
        response = await _request_with_retry(lambda: client.get(f"{base_url}{INDUSTRY_CHAINS_PATH}", params=params))
        response.raise_for_status()
        envelope = IndustryChainListEnvelope.model_validate(response.json())
        items.extend(envelope.result.items)
        cursor = envelope.result.next_cursor
        if cursor is None:
            return items
        if cursor in observed_cursors:
            raise ProjectionError("IndustryChain API repeated an opaque cursor")
        observed_cursors.add(cursor)


async def _load_mapping_batch(
    client: httpx.AsyncClient,
    base_url: str,
    chain_ids: Sequence[str],
    analysis_as_of: datetime,
) -> ResearchGraphResult:
    body = {
        "analysis_as_of": analysis_as_of.isoformat().replace("+00:00", "Z"),
        "seed_entity_ids": list(chain_ids),
        "relation_filters": [
            {"relation_type": "mapped_to_industry", "direction": "outgoing"},
            {"relation_type": "mapped_to_concept", "direction": "outgoing"},
        ],
        "max_depth": 1,
        "node_budget": 500,
        "edge_budget": 1000,
    }
    response = await _request_with_retry(lambda: client.post(f"{base_url}{RESEARCH_GRAPH_PATH}", json=body))
    response.raise_for_status()
    return ResearchGraphEnvelope.model_validate(response.json()).result


async def load_facts(config: RuntimeConfig) -> IndustryChainFacts:
    """Read one complete IndustryChain and mapping snapshot through Data APIs."""

    base_url = str(config.tidewise_data_base_url).rstrip("/")
    headers = {"Authorization": f"Bearer {config.tidewise_data_service_token.get_secret_value()}"}
    try:
        async with httpx.AsyncClient(timeout=17, headers=headers) as client:
            chains = await _load_chains(client, base_url)
            analysis_as_of = datetime.now(UTC)
            batches = [chains[start : start + SEARCH_BATCH_SIZE] for start in range(0, len(chains), SEARCH_BATCH_SIZE)]
            results: list[ResearchGraphResult] = []
            for start in range(0, len(batches), SEARCH_CONCURRENCY):
                wave = batches[start : start + SEARCH_CONCURRENCY]
                results.extend(
                    await asyncio.gather(
                        *[
                            _load_mapping_batch(
                                client,
                                base_url,
                                [item.id for item in batch],
                                analysis_as_of,
                            )
                            for batch in wave
                        ]
                    )
                )
    except ValidationError:
        raise ProjectionError("IndustryChain Data API response violates its frozen DTO") from None
    except (httpx.HTTPError, ValueError) as exc:
        detail = exc.__class__.__name__
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f"HTTP {exc.response.status_code}"
        raise ProjectionError(f"IndustryChain Data API request failed ({detail})") from None

    entities_by_id: dict[str, DataResearchEntityDTO] = {}
    mappings_by_id: dict[str, DataMappingDTO] = {}
    for result in results:
        for entity in result.entities:
            previous = entities_by_id.get(entity.entity_id)
            if previous is not None and previous != entity:
                raise ProjectionError(f"conflicting Research Graph entity: {entity.entity_id}")
            entities_by_id[entity.entity_id] = entity
        for mapping in result.entity_relations:
            previous = mappings_by_id.get(mapping.entity_relation_id)
            if previous is not None and previous != mapping:
                raise ProjectionError(f"conflicting IndustryChain mapping: {mapping.entity_relation_id}")
            mappings_by_id[mapping.entity_relation_id] = mapping
    return IndustryChainFacts(
        industry_chains=tuple(chains),
        entities=tuple(entities_by_id.values()),
        mappings=tuple(mappings_by_id.values()),
    )


def build_plan(facts: IndustryChainFacts) -> IndustryChainPlan:
    """Validate the complete snapshot before constructing any Graphiti write."""

    if EDGE_TYPE_MAP.get(("IndustryChain", "Industry")) != ["IndustryChainMappedToIndustry"]:
        raise ProjectionError("ontology does not permit IndustryChainMappedToIndustry")
    if EDGE_TYPE_MAP.get(("IndustryChain", "Concept")) != ["IndustryChainMappedToConcept"]:
        raise ProjectionError("ontology does not permit IndustryChainMappedToConcept")

    chains_by_id: dict[str, DataIndustryChainDTO] = {}
    for chain in facts.industry_chains:
        if chain.id in chains_by_id:
            raise ProjectionError(f"duplicate IndustryChain ID: {chain.id}")
        chains_by_id[chain.id] = chain

    entities_by_id = {entity.entity_id: entity for entity in facts.entities}
    if len(entities_by_id) != len(facts.entities):
        raise ProjectionError("duplicate Research Graph entity ID")

    nodes: list[EntityNode] = []
    for chain in facts.industry_chains:
        try:
            attributes = IndustryChain(
                data_object_id=chain.id,
                aliases=chain.aliases,
                scope=chain.scope,
                target_output=chain.target_output,
                end_use=chain.end_use,
                geography=chain.geography,
                primary_country_id=chain.primary_country_id,
                as_of_date=chain.as_of_date,
                review_status=chain.review_status,
                review_note=chain.review_note,
                technology_route_qualifier=chain.technology_route_qualifier,
                observable_variables=chain.observable_variables,
                updated_at=chain.updated_at,
            ).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            raise ProjectionError(f"IndustryChain {chain.id} violates ontology: {exc}") from None
        nodes.append(
            EntityNode(
                uuid=node_uuid(chain.id),
                name=chain.name,
                group_id=GROUP_ID,
                labels=["IndustryChain"],
                created_at=chain.created_at,
                summary=(f"产业链：{chain.name}。目标产出：{chain.target_output}。主要用途：{chain.end_use}。"),
                attributes=attributes,
            )
        )

    endpoint_keys: set[tuple[str, str, str]] = set()
    target_types: dict[str, str] = {}
    edges: list[EntityEdge] = []
    mapping_counts: Counter[str] = Counter()
    planned_at = datetime.now(UTC)
    for mapping in facts.mappings:
        if mapping.status != RecordStatus.ACTIVE:
            raise ProjectionError(f"inactive IndustryChain mapping: {mapping.entity_relation_id}")
        chain = chains_by_id.get(mapping.from_entity_id)
        if chain is None:
            raise ProjectionError(f"mapping source is not a listed IndustryChain: {mapping.entity_relation_id}")
        target = entities_by_id.get(mapping.to_entity_id)
        if target is None:
            raise ProjectionError(f"mapping target is missing: {mapping.entity_relation_id}")
        expected_target_type = TARGET_TYPES[mapping.relation_type]
        if target.entity_type != expected_target_type:
            raise ProjectionError(f"mapping target type is invalid: {mapping.entity_relation_id}")
        if target.status != RecordStatus.ACTIVE:
            raise ProjectionError(f"mapping target is inactive: {mapping.to_entity_id}")
        endpoint_key = (mapping.relation_type, mapping.from_entity_id, mapping.to_entity_id)
        if endpoint_key in endpoint_keys:
            raise ProjectionError(f"duplicate IndustryChain mapping endpoints: {endpoint_key}")
        endpoint_keys.add(endpoint_key)
        previous_type = target_types.get(mapping.to_entity_id)
        if previous_type is not None and previous_type != expected_target_type:
            raise ProjectionError(f"mapping target has conflicting types: {mapping.to_entity_id}")
        target_types[mapping.to_entity_id] = expected_target_type

        relation_name = MAPPING_NAMES[mapping.relation_type]
        try:
            link_type = (
                IndustryChainMappedToIndustry
                if mapping.relation_type == "mapped_to_industry"
                else IndustryChainMappedToConcept
            )
            attributes = link_type(
                data_object_id=mapping.entity_relation_id,
                status=mapping.status,
            ).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            raise ProjectionError(
                f"IndustryChain mapping {mapping.entity_relation_id} violates ontology: {exc}"
            ) from None
        target_kind = "行业" if expected_target_type == "industry" else "概念"
        edges.append(
            EntityEdge(
                uuid=edge_uuid(relation_name, mapping.from_entity_id, mapping.to_entity_id),
                group_id=GROUP_ID,
                source_node_uuid=node_uuid(mapping.from_entity_id),
                target_node_uuid=node_uuid(mapping.to_entity_id),
                created_at=planned_at,
                name=relation_name,
                fact=f"{chain.name}映射到{target_kind}{target.canonical_name}",
                attributes=attributes,
            )
        )
        mapping_counts[mapping.relation_type] += 1

    industry_mapped_chains = {
        mapping.from_entity_id for mapping in facts.mappings if mapping.relation_type == "mapped_to_industry"
    }
    missing_industry = set(chains_by_id).difference(industry_mapped_chains)
    if missing_industry:
        raise ProjectionError(f"IndustryChain without mapped Industry: {sorted(missing_industry)[0]}")
    return IndustryChainPlan(
        industry_chain_count=len(chains_by_id),
        industry_mapping_count=mapping_counts["mapped_to_industry"],
        concept_mapping_count=mapping_counts["mapped_to_concept"],
        nodes=tuple(nodes),
        edges=tuple(edges),
        target_types=target_types,
    )


async def _validate_canonical_targets(graphiti: Graphiti, plan: IndustryChainPlan) -> None:
    result = await graphiti.driver.execute_query(
        """
        MATCH (n:Entity {group_id: $group_id})
        WHERE n.data_object_id IN $target_ids
        RETURN n.data_object_id AS data_object_id, n.uuid AS uuid, labels(n) AS labels
        ORDER BY data_object_id
        """,
        group_id=GROUP_ID,
        target_ids=list(plan.target_types),
    )
    records = [record.data() for record in result.records]
    by_id: dict[str, list[dict[str, object]]] = {}
    for record in records:
        by_id.setdefault(str(record["data_object_id"]), []).append(record)
    problems: list[str] = []
    for target_id, target_type in plan.target_types.items():
        matches = by_id.get(target_id, [])
        expected_labels = {"Entity", TARGET_LABELS[target_type]}
        if len(matches) != 1:
            problems.append(f"{target_id} resolves to {len(matches)} canonical nodes")
        elif set(matches[0]["labels"]) != expected_labels:
            problems.append(f"{target_id} does not have labels {sorted(expected_labels)}")
        elif matches[0]["uuid"] != node_uuid(target_id):
            problems.append(f"{target_id} does not use its deterministic Graphiti UUID")
    if problems:
        raise ProjectionError(f"IndustryChain target preflight failed: {problems[0]}")


async def execute_plan(
    graphiti: Graphiti,
    plan: IndustryChainPlan,
    *,
    replace: bool,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int, dict[str, int]]:
    await _validate_canonical_targets(graphiti, plan)
    return await write_projection(
        graphiti,
        nodes=plan.nodes,
        edges=plan.edges,
        owned_node_labels=frozenset({"IndustryChain"}),
        owned_edge_names=frozenset(MAPPING_NAMES.values()),
        replace=replace,
        progress=progress,
    )


async def inspect_graph_state(graphiti: Graphiti) -> dict[str, object]:
    node_result = await graphiti.driver.execute_query(
        """
        MATCH (n:IndustryChain {group_id: $group_id})
        RETURN n.uuid AS uuid, n.data_object_id AS data_object_id, labels(n) AS labels,
               size(n.name_embedding) AS embedding_dimension
        ORDER BY data_object_id
        """,
        group_id=GROUP_ID,
    )
    edge_result = await graphiti.driver.execute_query(
        """
        MATCH (source:IndustryChain {group_id: $group_id})-[r:RELATES_TO]->(target:Entity)
        WHERE r.name IN $relation_names
        RETURN r.uuid AS uuid, r.name AS name, r.data_object_id AS data_object_id,
               source.data_object_id AS source_id, target.data_object_id AS target_id,
               labels(target) AS target_labels, size(r.fact_embedding) AS embedding_dimension,
               r.created_at IS NOT NULL AS has_created_at
        ORDER BY name, source_id, target_id
        """,
        group_id=GROUP_ID,
        relation_names=list(MAPPING_NAMES.values()),
    )
    return {
        "nodes": [record.data() for record in node_result.records],
        "edges": [record.data() for record in edge_result.records],
    }


async def inspect_plan_graph_state(
    graphiti: Graphiti,
    plan: IndustryChainPlan,
) -> dict[str, object]:
    state = await inspect_graph_state(graphiti)
    target_result = await graphiti.driver.execute_query(
        """
        MATCH (n:Entity {group_id: $group_id})
        WHERE n.data_object_id IN $target_ids
        RETURN n.data_object_id AS data_object_id, n.uuid AS uuid, labels(n) AS labels
        ORDER BY data_object_id
        """,
        group_id=GROUP_ID,
        target_ids=list(plan.target_types),
    )
    state["targets"] = [record.data() for record in target_result.records]
    return state


def verify_state(plan: IndustryChainPlan, state: dict[str, object]) -> dict[str, object]:
    nodes = state["nodes"]
    edges = state["edges"]
    targets = state["targets"]
    assert isinstance(nodes, list) and isinstance(edges, list) and isinstance(targets, list)

    expected_nodes = {node.attributes["data_object_id"] for node in plan.nodes}
    actual_nodes = {record["data_object_id"] for record in nodes}
    node_id_by_uuid = {node.uuid: node.attributes["data_object_id"] for node in plan.nodes}
    target_id_by_uuid = {node_uuid(target_id): target_id for target_id in plan.target_types}
    expected_edges = {
        (
            edge.name,
            node_id_by_uuid[edge.source_node_uuid],
            target_id_by_uuid[edge.target_node_uuid],
            edge.attributes["data_object_id"],
        )
        for edge in plan.edges
    }
    actual_edges = {
        (
            record["name"],
            record["source_id"],
            record["target_id"],
            record["data_object_id"],
        )
        for record in edges
    }
    target_records = {record["data_object_id"]: record for record in targets}

    problems: list[str] = []
    if actual_nodes != expected_nodes or len(nodes) != len(expected_nodes):
        problems.append("IndustryChain ID set differs from Data API")
    if any(set(record["labels"]) != {"Entity", "IndustryChain"} for record in nodes):
        problems.append("IndustryChain labels are not exclusive")
    if any(record["embedding_dimension"] != 1024 for record in nodes):
        problems.append("IndustryChain embedding is missing or has wrong dimension")
    if actual_edges != expected_edges or len(edges) != len(expected_edges):
        problems.append("IndustryChain mapping set differs from Data API")
    if any(record["embedding_dimension"] != 1024 for record in edges):
        problems.append("IndustryChain mapping embedding is missing or has wrong dimension")
    if any(not record["has_created_at"] for record in edges):
        problems.append("IndustryChain mapping created_at is missing")
    if any(
        set(record["target_labels"])
        != {
            "Entity",
            "Industry" if record["name"] == "IndustryChainMappedToIndustry" else "Concept",
        }
        for record in edges
    ):
        problems.append("IndustryChain mapping has a wrongly typed graph target")
    for target_id, target_type in plan.target_types.items():
        record = target_records.get(target_id)
        expected_labels = {"Entity", TARGET_LABELS[target_type]}
        if record is None or set(record["labels"]) != expected_labels:
            problems.append("IndustryChain mapping target projection was not preserved")
            break
    if problems:
        raise ProjectionError("; ".join(problems))

    return {
        **plan.summary(),
        "node_total": len(nodes),
        "relation_total": len(edges),
        "canonical_targets_preserved": True,
        "verified": True,
    }
