"""Country and Region facts projected from the owning Data API into Graphiti."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import httpx
from graphiti_core import Graphiti
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sematica.ontology import EDGE_TYPE_MAP, Country, CountryInRegion, Region
from sematica.projection.authoritative_writer import GROUP_ID, edge_uuid, node_uuid, write_projection
from sematica.projection.runtime import ProjectionError, RuntimeConfig

COUNTRIES_PATH = "/api/data/v1/entities/countries"


class DataRegionDTO(BaseModel):
    """Country API's embedded canonical Region fact."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    code: str
    name: str = Field(min_length=1)
    name_en: str | None = None
    region_type: str


class DataCountryDTO(BaseModel):
    """Canonical Country fact returned by Tidewise Data."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    code: str
    name: str = Field(min_length=1)
    name_en: str | None = None
    strategic_positioning: str | None = None
    key_resources: str | None = None
    regions: list[DataRegionDTO] = Field(min_length=1)


class CountryListResult(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    items: list[DataCountryDTO]


class CountryListEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    request_id: str = Field(min_length=1)
    result: CountryListResult


class PlannedTriplet(BaseModel):
    """One fully validated, deterministic Graphiti write."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    source: EntityNode
    edge: EntityEdge
    target: EntityNode


class ProjectionPlan(BaseModel):
    """The complete preflight result; no graph write occurs while this is built."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    country_count: int
    region_count: int
    relation_count: int
    triplets: tuple[PlannedTriplet, ...]

    def summary(self) -> dict[str, object]:
        return {
            "group_id": GROUP_ID,
            "countries": self.country_count,
            "regions": self.region_count,
            "country_in_region": self.relation_count,
        }


async def load_countries(config: RuntimeConfig) -> list[DataCountryDTO]:
    """Read Country facts through the authenticated owner-service contract."""

    headers = {"Authorization": f"Bearer {config.tidewise_data_service_token.get_secret_value()}"}
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            response = await client.get(f"{str(config.tidewise_data_base_url).rstrip('/')}{COUNTRIES_PATH}")
            response.raise_for_status()
            envelope = CountryListEnvelope.model_validate(response.json())
    except ValidationError:
        raise ProjectionError("Country API response violates the projection DTO contract") from None
    except (httpx.HTTPError, ValueError) as exc:
        detail = exc.__class__.__name__
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f"HTTP {exc.response.status_code}"
        raise ProjectionError(f"Country API request failed ({detail})") from None
    return envelope.result.items


def build_plan(countries: Sequence[DataCountryDTO]) -> ProjectionPlan:
    """Validate the whole source batch and ontology before producing any Graphiti object."""

    if EDGE_TYPE_MAP.get(("Country", "Region")) != ["CountryInRegion"]:
        raise ProjectionError("ontology does not permit CountryInRegion for Country -> Region")

    created_at = datetime.now(UTC)
    country_ids: set[str] = set()
    country_codes: set[str] = set()
    region_by_id: dict[str, DataRegionDTO] = {}
    region_id_by_code: dict[str, str] = {}
    triplets: list[PlannedTriplet] = []

    for item in countries:
        if item.id in country_ids:
            raise ProjectionError(f"duplicate Country ID: {item.id}")
        if item.code in country_codes:
            raise ProjectionError(f"duplicate Country code: {item.code}")
        country_ids.add(item.id)
        country_codes.add(item.code)

        try:
            country_attributes = Country(
                data_object_id=item.id,
                code=item.code,
                name_en=item.name_en,
                strategic_positioning=item.strategic_positioning,
                key_resources=item.key_resources,
            ).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            raise ProjectionError(f"Country {item.id} violates ontology: {exc}") from None

        for region_item in item.regions:
            previous_region = region_by_id.get(region_item.id)
            if previous_region is not None and previous_region != region_item:
                raise ProjectionError(f"conflicting Region facts for ID: {region_item.id}")
            previous_id = region_id_by_code.get(region_item.code)
            if previous_id is not None and previous_id != region_item.id:
                raise ProjectionError(f"Region code {region_item.code} maps to multiple IDs")
            region_by_id[region_item.id] = region_item
            region_id_by_code[region_item.code] = region_item.id

            try:
                region_attributes = Region(
                    data_object_id=region_item.id,
                    code=region_item.code,
                    name_en=region_item.name_en,
                    region_type=region_item.region_type,
                ).model_dump(mode="json", exclude_none=True)
                edge_attributes = CountryInRegion().model_dump(mode="json", exclude_none=True)
            except ValidationError as exc:
                raise ProjectionError(
                    f"CountryInRegion {item.id} -> {region_item.id} violates ontology: {exc}"
                ) from None

            source_uuid = node_uuid(item.id)
            target_uuid = node_uuid(region_item.id)
            source = EntityNode(
                uuid=source_uuid,
                name=item.name,
                group_id=GROUP_ID,
                labels=["Country"],
                created_at=created_at,
                summary=f"国家：{item.name}（{item.code}）",
                attributes=country_attributes,
            )
            target = EntityNode(
                uuid=target_uuid,
                name=region_item.name,
                group_id=GROUP_ID,
                labels=["Region"],
                created_at=created_at,
                summary=f"地理区域：{region_item.name}（{region_item.code}）",
                attributes=region_attributes,
            )
            edge = EntityEdge(
                uuid=edge_uuid("CountryInRegion", item.id, region_item.id),
                group_id=GROUP_ID,
                source_node_uuid=source_uuid,
                target_node_uuid=target_uuid,
                created_at=created_at,
                name="CountryInRegion",
                fact=f"{item.name}属于{region_item.name}",
                attributes=edge_attributes,
            )
            triplets.append(PlannedTriplet(source=source, edge=edge, target=target))

    return ProjectionPlan(
        country_count=len(country_ids),
        region_count=len(region_by_id),
        relation_count=len(triplets),
        triplets=tuple(triplets),
    )


async def execute_plan(
    graphiti: Graphiti,
    plan: ProjectionPlan,
    *,
    limit: int | None = None,
    replace: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int, dict[str, int]]:
    """Bulk-write canonical IDs without invoking Graphiti's LLM entity resolution."""

    selected = plan.triplets if limit is None else plan.triplets[:limit]
    nodes: list[EntityNode] = []
    edges: list[EntityEdge] = []
    for triplet in selected:
        nodes.extend((triplet.source, triplet.target))
        edges.append(triplet.edge)
    return await write_projection(
        graphiti,
        nodes=nodes,
        edges=edges,
        owned_node_labels=frozenset({"Country", "Region"}),
        owned_edge_names=frozenset({"CountryInRegion"}),
        replace=replace,
        progress=progress,
    )


async def inspect_graph_state(graphiti: Graphiti) -> dict[str, object]:
    """Read exact authoritative IDs and CountryInRegion endpoints from the projection group."""

    node_result = await graphiti.driver.execute_query(
        """
        MATCH (n:Entity {group_id: $group_id})
        WHERE n:Country OR n:Region
        RETURN n.uuid AS uuid, n.data_object_id AS data_object_id, labels(n) AS labels,
               CASE WHEN n:Country THEN 'Country'
                    WHEN n:Region THEN 'Region'
                    ELSE 'Unexpected' END AS kind
        ORDER BY kind, data_object_id
        """,
        group_id=GROUP_ID,
    )
    edge_result = await graphiti.driver.execute_query(
        """
        MATCH (source:Country {group_id: $group_id})-[r:RELATES_TO]->(target:Region)
        WHERE r.name = 'CountryInRegion'
        RETURN r.uuid AS uuid, r.name AS name,
               source.data_object_id AS source_id,
               target.data_object_id AS target_id,
               source:Country AS source_is_country,
               target:Region AS target_is_region
        ORDER BY source_id, target_id
        """,
        group_id=GROUP_ID,
    )
    nodes = [record.data() for record in node_result.records]
    edges = [record.data() for record in edge_result.records]
    return {"nodes": nodes, "edges": edges}


def verify_state(plan: ProjectionPlan, state: dict[str, object]) -> dict[str, object]:
    """Require the graph projection to equal the current authoritative Data API plan."""

    nodes = state["nodes"]
    edges = state["edges"]
    assert isinstance(nodes, list) and isinstance(edges, list)

    actual_countries = {item["data_object_id"] for item in nodes if item["kind"] == "Country"}
    actual_regions = {item["data_object_id"] for item in nodes if item["kind"] == "Region"}
    unexpected_nodes = [item for item in nodes if item["kind"] == "Unexpected"]
    invalid_labels = [
        item for item in nodes if set(item["labels"]) not in ({"Entity", "Country"}, {"Entity", "Region"})
    ]
    actual_relations = {
        (item["source_id"], item["target_id"])
        for item in edges
        if item["name"] == "CountryInRegion" and item["source_is_country"] and item["target_is_region"]
    }
    unexpected_edges = [
        item
        for item in edges
        if item["name"] != "CountryInRegion" or not item["source_is_country"] or not item["target_is_region"]
    ]

    expected_countries = {triplet.source.attributes["data_object_id"] for triplet in plan.triplets}
    expected_regions = {triplet.target.attributes["data_object_id"] for triplet in plan.triplets}
    expected_relations = {
        (
            triplet.source.attributes["data_object_id"],
            triplet.target.attributes["data_object_id"],
        )
        for triplet in plan.triplets
    }

    problems: list[str] = []
    if actual_countries != expected_countries:
        problems.append("Country ID set differs from Data API")
    if actual_regions != expected_regions:
        problems.append("Region ID set differs from Data API")
    if actual_relations != expected_relations:
        problems.append("CountryInRegion endpoint set differs from Data API")
    if len(nodes) != len(actual_countries) + len(actual_regions):
        problems.append("duplicate data_object_id or duplicate typed node")
    if len(edges) != len(actual_relations):
        problems.append("duplicate or unexpected relation")
    if unexpected_nodes:
        problems.append("unexpected Entity type in projection group")
    if invalid_labels:
        problems.append("Country and Region labels are not mutually exclusive")
    if unexpected_edges:
        problems.append("unexpected relation in projection group")
    if problems:
        raise ProjectionError("; ".join(problems))

    return {
        **plan.summary(),
        "node_total": len(nodes),
        "relation_total": len(edges),
        "verified": True,
    }
