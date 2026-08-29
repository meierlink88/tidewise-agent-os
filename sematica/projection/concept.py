"""Concept facts projected independently from Tidewise Data into Graphiti."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from graphiti_core import Graphiti
from graphiti_core.nodes import EntityNode
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from sematica.ontology import Concept
from sematica.ontology.enums import ConceptType, ReviewStatus
from sematica.projection.authoritative_writer import GROUP_ID, node_uuid, write_projection
from sematica.projection.runtime import ProjectionError, RuntimeConfig

CONCEPTS_PATH = "/api/data/v1/entities/concepts"
PAGE_SIZE = 100


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == UTC.utcoffset(value)


class DataConceptDTO(BaseModel):
    """Frozen consumer contract for one Data-owned Concept fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str = Field(min_length=1)
    aliases: list[str]
    concept_type: ConceptType
    definition: str = Field(min_length=1)
    review_status: ReviewStatus
    created_at: datetime
    updated_at: datetime

    @field_validator("aliases")
    @classmethod
    def aliases_must_be_nonblank_and_unique(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("Concept aliases must not be blank")
        if len(values) != len(set(values)):
            raise ValueError("Concept aliases must be unique")
        return values

    @model_validator(mode="after")
    def timestamps_must_be_consistent(self) -> DataConceptDTO:
        if not _is_utc(self.created_at) or not _is_utc(self.updated_at):
            raise ValueError("Concept timestamps must be explicit UTC")
        if self.updated_at < self.created_at:
            raise ValueError("Concept updated_at precedes created_at")
        return self


class ConceptList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[DataConceptDTO]
    next_cursor: str | None


class ConceptListEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=128)
    result: ConceptList


class ConceptFacts(BaseModel):
    model_config = ConfigDict(frozen=True)

    concepts: tuple[DataConceptDTO, ...]


class ConceptPlan(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    concept_count: int
    nodes: tuple[EntityNode, ...]

    def summary(self) -> dict[str, object]:
        return {"group_id": GROUP_ID, "concepts": self.concept_count}


async def _get_page(
    client: httpx.AsyncClient,
    *,
    url: str,
    cursor: str | None,
) -> httpx.Response:
    params = {"page_size": PAGE_SIZE}
    if cursor is not None:
        params["cursor"] = cursor
    for attempt in range(2):
        try:
            response = await client.get(url, params=params)
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


async def load_facts(config: RuntimeConfig) -> ConceptFacts:
    """Read the complete paginated Concept snapshot through the Data API."""

    base_url = str(config.tidewise_data_base_url).rstrip("/")
    headers = {"Authorization": f"Bearer {config.tidewise_data_service_token.get_secret_value()}"}
    items: list[DataConceptDTO] = []
    cursor: str | None = None
    observed_cursors: set[str] = set()
    try:
        async with httpx.AsyncClient(timeout=2.3, headers=headers) as client:
            while True:
                response = await _get_page(
                    client,
                    url=f"{base_url}{CONCEPTS_PATH}",
                    cursor=cursor,
                )
                response.raise_for_status()
                envelope = ConceptListEnvelope.model_validate(response.json())
                items.extend(envelope.result.items)
                cursor = envelope.result.next_cursor
                if cursor is None:
                    break
                if cursor in observed_cursors:
                    raise ProjectionError("Concept API repeated an opaque cursor")
                observed_cursors.add(cursor)
    except ValidationError:
        raise ProjectionError("Concept API response violates its frozen DTO") from None
    except (httpx.HTTPError, ValueError) as exc:
        detail = exc.__class__.__name__
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f"HTTP {exc.response.status_code}"
        raise ProjectionError(f"Concept API request failed ({detail})") from None
    return ConceptFacts(concepts=tuple(items))


def build_plan(facts: ConceptFacts) -> ConceptPlan:
    """Validate the complete Concept snapshot before constructing any graph write."""

    concept_ids: set[str] = set()
    nodes: list[EntityNode] = []
    for item in facts.concepts:
        if item.id in concept_ids:
            raise ProjectionError(f"duplicate Concept ID: {item.id}")
        concept_ids.add(item.id)
        try:
            attributes = Concept(
                data_object_id=item.id,
                aliases=item.aliases,
                concept_type=item.concept_type,
                definition=item.definition,
                review_status=item.review_status,
                updated_at=item.updated_at,
            ).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            raise ProjectionError(f"Concept {item.id} violates ontology: {exc}") from None
        nodes.append(
            EntityNode(
                uuid=node_uuid(item.id),
                name=item.name,
                group_id=GROUP_ID,
                labels=["Concept"],
                created_at=item.created_at,
                summary=f"投研概念：{item.name}。{item.definition}",
                attributes=attributes,
            )
        )
    return ConceptPlan(concept_count=len(concept_ids), nodes=tuple(nodes))


async def execute_plan(
    graphiti: Graphiti,
    plan: ConceptPlan,
    *,
    replace: bool,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int, dict[str, int]]:
    return await write_projection(
        graphiti,
        nodes=plan.nodes,
        edges=(),
        owned_node_labels=frozenset({"Concept"}),
        owned_edge_names=frozenset(),
        replace=replace,
        progress=progress,
    )


async def inspect_graph_state(graphiti: Graphiti) -> dict[str, object]:
    node_result = await graphiti.driver.execute_query(
        """
        MATCH (n:Concept {group_id: $group_id})
        RETURN n.uuid AS uuid, n.data_object_id AS data_object_id, labels(n) AS labels,
               size(n.name_embedding) AS embedding_dimension
        ORDER BY data_object_id
        """,
        group_id=GROUP_ID,
    )
    base_result = await graphiti.driver.execute_query(
        """
        MATCH (n:Entity {group_id: $group_id})
        RETURN count(CASE WHEN n:Country THEN 1 END) AS countries,
               count(CASE WHEN n:Region THEN 1 END) AS regions
        """,
        group_id=GROUP_ID,
    )
    cross_result = await graphiti.driver.execute_query(
        """
        MATCH (:Concept {group_id: $group_id})-[r]-(:Industry)
        RETURN count(r) AS concept_industry_relations
        """,
        group_id=GROUP_ID,
    )
    return {
        "nodes": [record.data() for record in node_result.records],
        "base": base_result.records[0].data(),
        "cross": cross_result.records[0].data(),
    }


def verify_state(plan: ConceptPlan, state: dict[str, object]) -> dict[str, object]:
    nodes = state["nodes"]
    base = state["base"]
    cross = state["cross"]
    assert isinstance(nodes, list) and isinstance(base, dict) and isinstance(cross, dict)

    expected_ids = {node.attributes["data_object_id"] for node in plan.nodes}
    actual_ids = {node["data_object_id"] for node in nodes}
    problems: list[str] = []
    if actual_ids != expected_ids:
        problems.append("Concept ID set differs from Data API")
    if len(nodes) != len(actual_ids):
        problems.append("duplicate Concept node")
    if any(set(node["labels"]) != {"Entity", "Concept"} for node in nodes):
        problems.append("Concept labels are not exclusive")
    if any(node["embedding_dimension"] != 1024 for node in nodes):
        problems.append("Concept embedding is missing or has wrong dimension")
    if base != {"countries": 201, "regions": 22}:
        problems.append("Country/Region base projection was not preserved")
    if cross != {"concept_industry_relations": 0}:
        problems.append("unexpected Concept-to-Industry relation exists")
    if problems:
        raise ProjectionError("; ".join(problems))

    return {
        **plan.summary(),
        "node_total": len(nodes),
        "concept_industry_relations": 0,
        "country_region_base_preserved": True,
        "verified": True,
    }
