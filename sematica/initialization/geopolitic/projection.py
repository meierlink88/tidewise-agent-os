"""Build and verify a demo-only GeopoliticRivalry Graphiti catalog."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from graphiti_core import Graphiti
from graphiti_core.nodes import EntityNode
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from sematica.ontology import GeopoliticRivalry
from sematica.ontology.entities.base import NonBlankText
from sematica.ontology.enums import GeopoliticRivalryStatus, GeopoliticRivalryType
from sematica.projection.authoritative_writer import GROUP_ID, write_projection
from sematica.projection.runtime import ProjectionError

CATALOG_PATH = Path(__file__).with_name("catalog.v1.json")
DEMO_CATALOG_SOURCE = "tidewise-reason/geopolitic-demo"
APPROVED_DEMO_IDENTITIES = frozenset(
    {
        ("china_us_strategic_technology_competition", "中美战略与科技竞争"),
        ("taiwan_strait_security_cross_strait_relations", "台海安全与两岸关系"),
        ("south_china_sea_maritime_security_disputes", "南海海洋权益与安全争端"),
        (
            "russia_ukraine_war_western_security_confrontation",
            "俄乌战争及俄西方安全对抗",
        ),
        ("iran_us_israel_gulf_security_confrontation", "伊朗—美以及海湾安全对抗"),
        ("israel_palestine_gaza_war", "巴以冲突与加沙战争"),
        ("red_sea_yemen_maritime_security_conflict", "红海—也门航运安全冲突"),
        (
            "korean_peninsula_nuclear_security_confrontation",
            "朝鲜半岛核与安全对抗",
        ),
        ("eastern_drc_m23_rwanda_conflict", "刚果（金）东部—M23—卢旺达冲突"),
    }
)


def demo_node_uuid(catalog_key: str) -> str:
    """Derive a stable graph identity without impersonating a Data object ID."""

    return str(
        uuid5(
            NAMESPACE_URL,
            f"urn:tidewise:demo-geopolitic-rivalry:{catalog_key}",
        )
    )


class DemoGeopoliticRivalry(BaseModel):
    """One reviewed, non-Data geopolitical narrative blueprint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,79}$")
    name: NonBlankText = Field(max_length=100, description="Reviewed Chinese display name.")
    name_en: NonBlankText = Field(
        max_length=100,
        description="Reviewed English display name.",
    )
    rivalry_type: GeopoliticRivalryType
    description: NonBlankText = Field(max_length=4000)
    core_actors: NonBlankText = Field(max_length=1000)
    peripheral_actors: NonBlankText | None = Field(default=None, max_length=1000)
    influenced_regions: list[NonBlankText] = Field(min_length=1, max_length=12)
    status: GeopoliticRivalryStatus

    @field_validator("influenced_regions")
    @classmethod
    def regions_must_be_unique(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("influenced_regions must be unique")
        return values


class DemoGeopoliticCatalog(BaseModel):
    """Versioned and reviewable input for the graph-only demo initializer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: str = Field(pattern=r"^demo-geopolitic-rivalry/v[1-9][0-9]*$")
    published_at: datetime
    items: tuple[DemoGeopoliticRivalry, ...] = Field(min_length=1, max_length=9)

    @model_validator(mode="after")
    def catalog_must_be_unambiguous(self) -> DemoGeopoliticCatalog:
        if self.published_at.tzinfo is None or self.published_at.utcoffset() != UTC.utcoffset(self.published_at):
            raise ValueError("published_at must be explicit UTC")
        for field_name in ("catalog_key", "name", "name_en"):
            values = [getattr(item, field_name) for item in self.items]
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate geopolitical catalog {field_name}")
        if any(item.status != GeopoliticRivalryStatus.ACTIVE for item in self.items):
            raise ValueError("the initial demo catalog may contain only ACTIVE blueprints")
        return self


class GeopoliticDemoPlan(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    catalog_version: str
    nodes: tuple[EntityNode, ...]

    def summary(self) -> dict[str, object]:
        return {
            "group_id": GROUP_ID,
            "catalog_version": self.catalog_version,
            "geopolitic_rivalries": len(self.nodes),
            "data_authoritative": False,
        }


def load_catalog(path: Path = CATALOG_PATH) -> DemoGeopoliticCatalog:
    """Load the approved demo catalog without accessing Tidewise Data."""

    try:
        catalog = DemoGeopoliticCatalog.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise ProjectionError(f"invalid geopolitical demo catalog: {exc}") from None
    actual_identities = frozenset((item.catalog_key, item.name) for item in catalog.items)
    if actual_identities != APPROVED_DEMO_IDENTITIES:
        raise ProjectionError("geopolitical demo catalog differs from the approved nine identities")
    return catalog


def build_plan(catalog: DemoGeopoliticCatalog) -> GeopoliticDemoPlan:
    """Validate every catalog record against the public ontology before graph access."""

    nodes: list[EntityNode] = []
    for item in catalog.items:
        try:
            ontology_attributes = GeopoliticRivalry(
                name_en=item.name_en,
                rivalry_type=item.rivalry_type,
                description=item.description,
                core_actors=item.core_actors,
                peripheral_actors=item.peripheral_actors,
                influenced_regions=item.influenced_regions,
                status=item.status,
            ).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            raise ProjectionError(f"geopolitical demo item {item.catalog_key} violates ontology: {exc}") from None
        if "data_object_id" in ontology_attributes:
            raise ProjectionError("demo geopolitical node must not claim a Data object ID")
        attributes = {
            **ontology_attributes,
            "demo_catalog_source": DEMO_CATALOG_SOURCE,
            "demo_catalog_key": item.catalog_key,
            "demo_catalog_version": catalog.catalog_version,
        }
        nodes.append(
            EntityNode(
                uuid=demo_node_uuid(item.catalog_key),
                name=item.name,
                group_id=GROUP_ID,
                labels=["GeopoliticRivalry"],
                created_at=catalog.published_at,
                summary=f"地缘政治叙事蓝图：{item.name}。{item.description}",
                attributes=attributes,
            )
        )
    return GeopoliticDemoPlan(catalog_version=catalog.catalog_version, nodes=tuple(nodes))


async def execute_plan(
    graphiti: Graphiti,
    plan: GeopoliticDemoPlan,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int, dict[str, int]]:
    """Idempotently upsert only the planned demo nodes; never delete graph data."""

    return await write_projection(
        graphiti,
        nodes=plan.nodes,
        edges=(),
        owned_node_labels=frozenset({"GeopoliticRivalry"}),
        owned_edge_names=frozenset(),
        replace=False,
        progress=progress,
    )


async def inspect_graph_state(
    graphiti: Graphiti,
    plan: GeopoliticDemoPlan,
) -> dict[str, object]:
    result = await graphiti.driver.execute_query(
        """
        MATCH (n:GeopoliticRivalry {group_id: $group_id})
        WHERE n.demo_catalog_source = $demo_catalog_source
        OPTIONAL MATCH (n)-[r]-()
        RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary,
               n.name_en AS name_en, n.rivalry_type AS rivalry_type,
               n.description AS description, n.core_actors AS core_actors,
               n.peripheral_actors AS peripheral_actors,
               n.influenced_regions AS influenced_regions, n.status AS status,
               n.demo_catalog_source AS demo_catalog_source,
               n.demo_catalog_key AS demo_catalog_key,
               n.demo_catalog_version AS demo_catalog_version,
               n.data_object_id AS data_object_id,
               n.created_at.epochMillis AS created_at_epoch_ms,
               labels(n) AS labels, size(n.name_embedding) AS embedding_dimension,
               count(r) AS relationship_count
        ORDER BY n.name
        """,
        group_id=GROUP_ID,
        demo_catalog_source=DEMO_CATALOG_SOURCE,
    )
    return {"nodes": [record.data() for record in result.records]}


def verify_state(plan: GeopoliticDemoPlan, state: dict[str, object]) -> dict[str, object]:
    nodes = state.get("nodes")
    if not isinstance(nodes, list):
        raise ProjectionError("invalid geopolitical graph inspection result")

    expected = {node.uuid: node for node in plan.nodes}
    actual = {node["uuid"]: node for node in nodes}
    problems: list[str] = []
    if set(actual) != set(expected):
        problems.append("geopolitical demo identity set differs from the catalog")
    if len(nodes) != len(actual):
        problems.append("duplicate geopolitical demo node")
    if any(set(node["labels"]) != {"Entity", "GeopoliticRivalry"} for node in nodes):
        problems.append("geopolitical demo labels are not exclusive")
    if any(node["data_object_id"] is not None for node in nodes):
        problems.append("geopolitical demo node claims a Data object ID")
    if any(node["embedding_dimension"] != 1024 for node in nodes):
        problems.append("geopolitical demo embedding is missing or has wrong dimension")
    if any(node["relationship_count"] != 0 for node in nodes):
        problems.append("geopolitical demo initializer must not create relationships")
    for uuid, expected_node in expected.items():
        actual_node = actual.get(uuid)
        if actual_node is None:
            continue
        expected_properties = {
            "name": expected_node.name,
            "summary": expected_node.summary,
            **expected_node.attributes,
        }
        if any(actual_node.get(key) != value for key, value in expected_properties.items()):
            problems.append(f"geopolitical demo properties differ from catalog: {uuid}")
        expected_created_at_epoch_ms = int(expected_node.created_at.timestamp() * 1000)
        if actual_node.get("created_at_epoch_ms") != expected_created_at_epoch_ms:
            problems.append(f"geopolitical demo creation time differs from catalog: {uuid}")
    if problems:
        raise ProjectionError("; ".join(problems))

    return {
        **plan.summary(),
        "node_total": len(nodes),
        "relationship_total": sum(int(node["relationship_count"]) for node in nodes),
        "verified": True,
    }
