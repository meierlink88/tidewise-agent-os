"""Industry hierarchy facts projected from Tidewise Data into Graphiti."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from graphiti_core import Graphiti
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from sematica.ontology import EDGE_TYPE_MAP, Industry, IndustryHasParent
from sematica.ontology.enums import ReviewStatus
from sematica.projection.authoritative_writer import GROUP_ID, edge_uuid, node_uuid, write_projection
from sematica.projection.runtime import ProjectionError, RuntimeConfig

INDUSTRIES_PATH = "/api/data/v1/entities/industries"
PAGE_SIZE = 100


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == UTC.utcoffset(value)


class DataIndustryDTO(BaseModel):
    """Frozen consumer contract for one Data-owned Industry fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str = Field(min_length=1)
    aliases: list[str]
    classification_system: str = Field(min_length=1)
    industry_code: str = Field(min_length=1)
    parent_industry_id: str | None
    hierarchy_path_codes: list[str] = Field(min_length=1)
    definition: str = Field(min_length=1)
    review_status: ReviewStatus
    created_at: datetime
    updated_at: datetime

    @field_validator("aliases", "hierarchy_path_codes")
    @classmethod
    def strings_must_be_nonblank_and_unique(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("list values must not be blank")
        if len(values) != len(set(values)):
            raise ValueError("list values must be unique")
        return values

    @model_validator(mode="after")
    def timestamps_and_path_must_be_consistent(self) -> DataIndustryDTO:
        if not _is_utc(self.created_at) or not _is_utc(self.updated_at):
            raise ValueError("Industry timestamps must be explicit UTC")
        if self.updated_at < self.created_at:
            raise ValueError("Industry updated_at precedes created_at")
        if self.hierarchy_path_codes[-1] != self.industry_code:
            raise ValueError("Industry hierarchy path must end with industry_code")
        return self


class IndustryList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[DataIndustryDTO]
    next_cursor: str | None


class IndustryListEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=128)
    result: IndustryList


class IndustryFacts(BaseModel):
    model_config = ConfigDict(frozen=True)

    industries: tuple[DataIndustryDTO, ...]


class IndustryPlan(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    industry_count: int
    parent_relation_count: int
    nodes: tuple[EntityNode, ...]
    edges: tuple[EntityEdge, ...]

    def summary(self) -> dict[str, object]:
        return {
            "group_id": GROUP_ID,
            "industries": self.industry_count,
            "industry_has_parent": self.parent_relation_count,
        }


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


async def load_facts(config: RuntimeConfig) -> IndustryFacts:
    """Read the complete paginated Industry snapshot through the Data API."""

    base_url = str(config.tidewise_data_base_url).rstrip("/")
    headers = {"Authorization": f"Bearer {config.tidewise_data_service_token.get_secret_value()}"}
    items: list[DataIndustryDTO] = []
    cursor: str | None = None
    observed_cursors: set[str] = set()
    try:
        async with httpx.AsyncClient(timeout=2.3, headers=headers) as client:
            while True:
                response = await _get_page(
                    client,
                    url=f"{base_url}{INDUSTRIES_PATH}",
                    cursor=cursor,
                )
                response.raise_for_status()
                envelope = IndustryListEnvelope.model_validate(response.json())
                items.extend(envelope.result.items)
                cursor = envelope.result.next_cursor
                if cursor is None:
                    break
                if cursor in observed_cursors:
                    raise ProjectionError("Industry API repeated an opaque cursor")
                observed_cursors.add(cursor)
    except ValidationError:
        raise ProjectionError("Industry API response violates its frozen DTO") from None
    except (httpx.HTTPError, ValueError) as exc:
        detail = exc.__class__.__name__
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f"HTTP {exc.response.status_code}"
        raise ProjectionError(f"Industry API request failed ({detail})") from None
    return IndustryFacts(industries=tuple(items))


def _validate_hierarchy(industries_by_id: dict[str, DataIndustryDTO]) -> None:
    for industry in industries_by_id.values():
        if industry.parent_industry_id is None:
            if len(industry.hierarchy_path_codes) != 1:
                raise ProjectionError(f"root Industry has a non-root hierarchy path: {industry.id}")
            continue
        parent = industries_by_id.get(industry.parent_industry_id)
        if parent is None:
            raise ProjectionError(f"Industry references a missing parent: {industry.id}")
        if parent.classification_system != industry.classification_system:
            raise ProjectionError(f"Industry parent crosses classification systems: {industry.id}")
        if industry.hierarchy_path_codes[:-1] != parent.hierarchy_path_codes:
            raise ProjectionError(f"Industry parent does not match hierarchy path: {industry.id}")


def build_plan(facts: IndustryFacts) -> IndustryPlan:
    """Validate the complete Industry snapshot before constructing any graph write."""

    if EDGE_TYPE_MAP.get(("Industry", "Industry")) != ["IndustryHasParent"]:
        raise ProjectionError("ontology does not permit IndustryHasParent")

    industries_by_id: dict[str, DataIndustryDTO] = {}
    natural_keys: set[tuple[str, str]] = set()
    for item in facts.industries:
        if item.id in industries_by_id:
            raise ProjectionError(f"duplicate Industry ID: {item.id}")
        natural_key = (item.classification_system, item.industry_code)
        if natural_key in natural_keys:
            raise ProjectionError(f"duplicate Industry classification key: {natural_key}")
        industries_by_id[item.id] = item
        natural_keys.add(natural_key)
    _validate_hierarchy(industries_by_id)

    nodes: list[EntityNode] = []
    edges: list[EntityEdge] = []
    for item in facts.industries:
        try:
            attributes = Industry(
                data_object_id=item.id,
                aliases=item.aliases,
                classification_system=item.classification_system,
                industry_code=item.industry_code,
                hierarchy_path_codes=item.hierarchy_path_codes,
                definition=item.definition,
                review_status=item.review_status,
                updated_at=item.updated_at,
            ).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            raise ProjectionError(f"Industry {item.id} violates ontology: {exc}") from None
        nodes.append(
            EntityNode(
                uuid=node_uuid(item.id),
                name=item.name,
                group_id=GROUP_ID,
                labels=["Industry"],
                created_at=item.created_at,
                summary=f"行业：{item.name}。{item.definition}",
                attributes=attributes,
            )
        )
        if item.parent_industry_id is None:
            continue
        parent = industries_by_id[item.parent_industry_id]
        try:
            edge_attributes = IndustryHasParent().model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            raise ProjectionError(f"IndustryHasParent {item.id} -> {parent.id} violates ontology: {exc}") from None
        edges.append(
            EntityEdge(
                uuid=edge_uuid("IndustryHasParent", item.id, parent.id),
                group_id=GROUP_ID,
                source_node_uuid=node_uuid(item.id),
                target_node_uuid=node_uuid(parent.id),
                created_at=item.created_at,
                name="IndustryHasParent",
                fact=f"{item.name}的直接父级行业是{parent.name}",
                attributes=edge_attributes,
            )
        )

    return IndustryPlan(
        industry_count=len(industries_by_id),
        parent_relation_count=len(edges),
        nodes=tuple(nodes),
        edges=tuple(edges),
    )


async def execute_plan(
    graphiti: Graphiti,
    plan: IndustryPlan,
    *,
    replace: bool,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int, dict[str, int]]:
    return await write_projection(
        graphiti,
        nodes=plan.nodes,
        edges=plan.edges,
        owned_node_labels=frozenset({"Industry"}),
        owned_edge_names=frozenset({"IndustryHasParent"}),
        replace=replace,
        progress=progress,
    )


async def inspect_graph_state(graphiti: Graphiti) -> dict[str, object]:
    node_result = await graphiti.driver.execute_query(
        """
        MATCH (n:Industry {group_id: $group_id})
        RETURN n.uuid AS uuid, n.data_object_id AS data_object_id, labels(n) AS labels,
               size(n.name_embedding) AS embedding_dimension
        ORDER BY data_object_id
        """,
        group_id=GROUP_ID,
    )
    edge_result = await graphiti.driver.execute_query(
        """
        MATCH (source:Industry {group_id: $group_id})-[r:RELATES_TO]->(target:Industry)
        WHERE r.name = 'IndustryHasParent'
        RETURN r.uuid AS uuid, source.data_object_id AS source_id,
               target.data_object_id AS target_id,
               size(r.fact_embedding) AS embedding_dimension
        ORDER BY source_id, target_id
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
    return {
        "nodes": [record.data() for record in node_result.records],
        "edges": [record.data() for record in edge_result.records],
        "base": base_result.records[0].data(),
    }


def verify_state(plan: IndustryPlan, state: dict[str, object]) -> dict[str, object]:
    nodes = state["nodes"]
    edges = state["edges"]
    base = state["base"]
    assert isinstance(nodes, list) and isinstance(edges, list) and isinstance(base, dict)

    expected_ids = {node.attributes["data_object_id"] for node in plan.nodes}
    expected_edges = {
        (
            next(node.attributes["data_object_id"] for node in plan.nodes if node.uuid == edge.source_node_uuid),
            next(node.attributes["data_object_id"] for node in plan.nodes if node.uuid == edge.target_node_uuid),
        )
        for edge in plan.edges
    }
    actual_ids = {node["data_object_id"] for node in nodes}
    actual_edges = {(edge["source_id"], edge["target_id"]) for edge in edges}

    problems: list[str] = []
    if actual_ids != expected_ids:
        problems.append("Industry ID set differs from Data API")
    if actual_edges != expected_edges:
        problems.append("IndustryHasParent endpoints differ from Data API")
    if len(nodes) != len(actual_ids):
        problems.append("duplicate Industry node")
    if len(edges) != len(actual_edges):
        problems.append("duplicate IndustryHasParent relation")
    if any(set(node["labels"]) != {"Entity", "Industry"} for node in nodes):
        problems.append("Industry labels are not exclusive")
    if any(node["embedding_dimension"] != 1024 for node in nodes):
        problems.append("Industry embedding is missing or has wrong dimension")
    if any(edge["embedding_dimension"] != 1024 for edge in edges):
        problems.append("IndustryHasParent embedding is missing or has wrong dimension")
    if any(edge["source_id"] == edge["target_id"] for edge in edges):
        problems.append("IndustryHasParent contains a self-loop")
    if base != {"countries": 201, "regions": 22}:
        problems.append("Country/Region base projection was not preserved")
    if problems:
        raise ProjectionError("; ".join(problems))

    return {
        **plan.summary(),
        "node_total": len(nodes),
        "relation_total": len(edges),
        "country_region_base_preserved": True,
        "verified": True,
    }
