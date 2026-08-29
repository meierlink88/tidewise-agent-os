"""构建并校验图谱专用的宏观经济政策动作目录。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from graphiti_core import Graphiti
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from sematica.ontology import CountryImplementsMacroEconomic, MacroEconomic
from sematica.ontology.entities.base import NonBlankText
from sematica.ontology.enums import MacroEconomicCategory, MacroEconomicStatus
from sematica.projection.authoritative_writer import GROUP_ID, write_projection
from sematica.projection.runtime import ProjectionError

CATALOG_PATH = Path(__file__).with_name("catalog.v1.json")
DEMO_CATALOG_SOURCE = "tidewise-reason/macroeconomic-policy-demo"
RELATION_NAME = "IMPLEMENTS"
APPROVED_COUNTRY_CODES = frozenset({"CN", "US", "JP", "KR", "GB"})
EXPECTED_CATEGORY_COUNTS = {
    MacroEconomicCategory.MONETARY: 8,
    MacroEconomicCategory.FISCAL: 8,
    MacroEconomicCategory.INDUSTRIAL_POLICY: 8,
    MacroEconomicCategory.GROWTH_CYCLE: 6,
    MacroEconomicCategory.INFLATION_PRICES: 8,
    MacroEconomicCategory.EMPLOYMENT_LABOR: 8,
    MacroEconomicCategory.FINANCIAL_STABILITY: 8,
    MacroEconomicCategory.EXTERNAL_SECTOR: 8,
    MacroEconomicCategory.DEBT_LEVERAGE: 8,
    MacroEconomicCategory.REAL_ESTATE: 8,
}
CATEGORY_NAMES = {
    MacroEconomicCategory.MONETARY: "货币政策线",
    MacroEconomicCategory.FISCAL: "财政政策线",
    MacroEconomicCategory.INDUSTRIAL_POLICY: "产业政策线",
    MacroEconomicCategory.GROWTH_CYCLE: "增长/周期线",
    MacroEconomicCategory.INFLATION_PRICES: "通胀/价格线",
    MacroEconomicCategory.EMPLOYMENT_LABOR: "就业/劳动力线",
    MacroEconomicCategory.FINANCIAL_STABILITY: "金融稳定线",
    MacroEconomicCategory.EXTERNAL_SECTOR: "对外/国际收支线",
    MacroEconomicCategory.DEBT_LEVERAGE: "债务/杠杆线",
    MacroEconomicCategory.REAL_ESTATE: "房地产/土地线",
}


def demo_node_uuid(policy_key: str) -> str:
    """为非 Data 权威节点生成稳定图谱身份。"""

    return str(uuid5(NAMESPACE_URL, f"urn:tidewise:demo-macroeconomic-policy:{policy_key}"))


def demo_edge_uuid(country_code: str, policy_key: str) -> str:
    """为国家与政策动作之间的适用性关系生成稳定身份。"""

    return str(
        uuid5(
            NAMESPACE_URL,
            f"urn:tidewise:demo-country-implements-policy:{country_code}:{policy_key}",
        )
    )


class DemoMacroEconomicPolicy(BaseModel):
    """一项经过审阅、不冒充 Data 权威数据的政策动作。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_key: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    name: NonBlankText = Field(max_length=100)
    name_en: NonBlankText = Field(max_length=100)
    category: MacroEconomicCategory
    description: NonBlankText = Field(max_length=1000)
    country_codes: tuple[str, ...] = Field(min_length=1, max_length=5)

    @field_validator("country_codes")
    @classmethod
    def country_codes_must_be_approved_and_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("country_codes 不能重复")
        unknown = set(values) - APPROVED_COUNTRY_CODES
        if unknown:
            raise ValueError("包含未批准的国家代码")
        return values


class DemoMacroEconomicCatalog(BaseModel):
    """可版本化、可审阅的图谱演示政策目录。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: str = Field(pattern=r"^demo-macroeconomic-policy/v[1-9][0-9]*$")
    published_at: datetime
    items: tuple[DemoMacroEconomicPolicy, ...] = Field(min_length=78, max_length=78)

    @model_validator(mode="after")
    def catalog_must_match_the_reviewed_shape(self) -> DemoMacroEconomicCatalog:
        if self.published_at.tzinfo is None or self.published_at.utcoffset() != UTC.utcoffset(self.published_at):
            raise ValueError("published_at 必须是明确的 UTC 时间")
        for field_name in ("policy_key", "name", "name_en"):
            values = [getattr(item, field_name) for item in self.items]
            if len(values) != len(set(values)):
                raise ValueError(f"宏观经济目录存在重复 {field_name}")
        counts = Counter(item.category for item in self.items)
        if counts != Counter(EXPECTED_CATEGORY_COUNTS):
            raise ValueError("宏观经济目录的十类数量与审阅结果不一致")
        used_country_codes = {code for item in self.items for code in item.country_codes}
        if used_country_codes != APPROVED_COUNTRY_CODES:
            raise ValueError("宏观经济目录的国家范围与审阅结果不一致")
        return self


class CountryReference(BaseModel):
    """图中已有的权威 Country 节点引用。"""

    model_config = ConfigDict(frozen=True)

    uuid: str
    data_object_id: str
    code: str
    name: str


class MacroEconomicPlan(BaseModel):
    """宏观经济节点与关系写入前的完整预检结果。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    catalog_version: str
    published_at: datetime
    items: tuple[DemoMacroEconomicPolicy, ...]
    nodes: tuple[EntityNode, ...]

    @property
    def relation_count(self) -> int:
        return sum(len(item.country_codes) for item in self.items)

    def summary(self) -> dict[str, object]:
        return {
            "group_id": GROUP_ID,
            "catalog_version": self.catalog_version,
            "macroeconomic_policies": len(self.nodes),
            "country_implements": self.relation_count,
            "countries": len(APPROVED_COUNTRY_CODES),
            "data_authoritative": False,
        }


class ResolvedMacroEconomicPlan(BaseModel):
    """已将国家代码安全解析为现有图节点的写入计划。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    base: MacroEconomicPlan
    countries: tuple[CountryReference, ...]
    edges: tuple[EntityEdge, ...]


def load_catalog(path: Path = CATALOG_PATH) -> DemoMacroEconomicCatalog:
    """读取并校验版本化的宏观经济政策目录。"""

    try:
        return DemoMacroEconomicCatalog.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise ProjectionError(f"无效的宏观经济政策目录：{exc}") from None


def build_plan(catalog: DemoMacroEconomicCatalog) -> MacroEconomicPlan:
    """在访问图之前，用公开 ontology 校验全部政策节点。"""

    nodes: list[EntityNode] = []
    for item in catalog.items:
        try:
            attributes = MacroEconomic(
                policy_key=item.policy_key,
                name_en=item.name_en,
                category=item.category,
                description=item.description,
                status=MacroEconomicStatus.ACTIVE,
            ).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            raise ProjectionError(f"宏观经济政策 {item.policy_key} 违反 ontology：{exc}") from None
        nodes.append(
            EntityNode(
                uuid=demo_node_uuid(item.policy_key),
                name=item.name,
                group_id=GROUP_ID,
                labels=["MacroEconomic"],
                created_at=catalog.published_at,
                summary=(f"宏观经济政策动作：{item.name}。分类：{CATEGORY_NAMES[item.category]}。{item.description}"),
                attributes={
                    **attributes,
                    "demo_catalog_source": DEMO_CATALOG_SOURCE,
                    "demo_catalog_version": catalog.catalog_version,
                },
            )
        )
    return MacroEconomicPlan(
        catalog_version=catalog.catalog_version,
        published_at=catalog.published_at,
        items=catalog.items,
        nodes=tuple(nodes),
    )


async def resolve_country_nodes(graphiti: Graphiti, plan: MacroEconomicPlan) -> ResolvedMacroEconomicPlan:
    """只将批准的国家代码解析到图中已有的权威 Country 节点。"""

    result = await graphiti.driver.execute_query(
        """
        MATCH (country:Country:Entity {group_id: $group_id})
        WHERE country.code IN $country_codes AND country.data_object_id IS NOT NULL
        RETURN country.uuid AS uuid, country.data_object_id AS data_object_id,
               country.code AS code, country.name AS name
        ORDER BY country.code
        """,
        group_id=GROUP_ID,
        country_codes=sorted(APPROVED_COUNTRY_CODES),
    )
    try:
        countries = tuple(CountryReference.model_validate(record.data()) for record in result.records)
    except ValidationError as exc:
        raise ProjectionError(f"图中 Country 节点违反引用合同：{exc}") from None
    countries_by_code = {country.code: country for country in countries}
    if len(countries_by_code) != len(countries):
        raise ProjectionError("图中存在重复的 Country code")
    missing = APPROVED_COUNTRY_CODES - countries_by_code.keys()
    if missing:
        raise ProjectionError("图中缺少已批准国家：" + ", ".join(sorted(missing)))

    CountryImplementsMacroEconomic()
    edges: list[EntityEdge] = []
    for item in plan.items:
        target_uuid = demo_node_uuid(item.policy_key)
        for country_code in item.country_codes:
            country = countries_by_code[country_code]
            edges.append(
                EntityEdge(
                    uuid=demo_edge_uuid(country_code, item.policy_key),
                    group_id=GROUP_ID,
                    source_node_uuid=country.uuid,
                    target_node_uuid=target_uuid,
                    created_at=plan.published_at,
                    name=RELATION_NAME,
                    fact=f"{country.name}在其政策体系中可实施{item.name}",
                    attributes={
                        "relation_schema": "CountryImplementsMacroEconomic",
                        "demo_catalog_source": DEMO_CATALOG_SOURCE,
                        "demo_catalog_version": plan.catalog_version,
                    },
                )
            )
    return ResolvedMacroEconomicPlan(base=plan, countries=countries, edges=tuple(edges))


async def execute_plan(
    graphiti: Graphiti,
    plan: ResolvedMacroEconomicPlan,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int, dict[str, int]]:
    """幂等写入审阅过的节点和关系，不删除其他图数据。"""

    return await write_projection(
        graphiti,
        nodes=plan.base.nodes,
        edges=plan.edges,
        owned_node_labels=frozenset({"MacroEconomic"}),
        owned_edge_names=frozenset({RELATION_NAME}),
        replace=False,
        progress=progress,
    )


async def inspect_graph_state(graphiti: Graphiti, plan: ResolvedMacroEconomicPlan) -> dict[str, object]:
    """读取本目录拥有的节点和关系以便校验。"""

    node_result = await graphiti.driver.execute_query(
        """
        MATCH (policy:MacroEconomic:Entity {group_id: $group_id})
        WHERE policy.demo_catalog_source = $catalog_source
        RETURN policy.uuid AS uuid, policy.name AS name, policy.summary AS summary,
               policy.policy_key AS policy_key, policy.name_en AS name_en,
               policy.category AS category, policy.description AS description,
               policy.status AS status, policy.data_object_id AS data_object_id,
               policy.demo_catalog_source AS demo_catalog_source,
               policy.demo_catalog_version AS demo_catalog_version,
               labels(policy) AS labels, size(policy.name_embedding) AS embedding_dimension
        ORDER BY policy.policy_key
        """,
        group_id=GROUP_ID,
        catalog_source=DEMO_CATALOG_SOURCE,
    )
    edge_result = await graphiti.driver.execute_query(
        """
        MATCH (country:Country:Entity)-[relation:RELATES_TO]->
              (policy:MacroEconomic:Entity {group_id: $group_id})
        WHERE relation.name = $relation_name
          AND relation.demo_catalog_source = $catalog_source
        RETURN relation.uuid AS uuid, country.code AS country_code,
               policy.policy_key AS policy_key, relation.name AS name,
               relation.fact AS fact, size(relation.fact_embedding) AS embedding_dimension,
               relation.relation_schema AS relation_schema,
               relation.demo_catalog_version AS demo_catalog_version
        ORDER BY country_code, policy_key
        """,
        group_id=GROUP_ID,
        relation_name=RELATION_NAME,
        catalog_source=DEMO_CATALOG_SOURCE,
    )
    return {
        "nodes": [record.data() for record in node_result.records],
        "edges": [record.data() for record in edge_result.records],
    }


def verify_state(plan: ResolvedMacroEconomicPlan, state: dict[str, object]) -> dict[str, object]:
    """要求图中结果与版本化目录完全一致。"""

    nodes = state.get("nodes")
    edges = state.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ProjectionError("无效的宏观经济图谱检查结果")

    expected_nodes = {node.uuid: node for node in plan.base.nodes}
    actual_nodes = {node["uuid"]: node for node in nodes}
    expected_edges = {edge.uuid: edge for edge in plan.edges}
    actual_edges = {edge["uuid"]: edge for edge in edges}
    problems: list[str] = []
    if set(actual_nodes) != set(expected_nodes) or len(nodes) != len(actual_nodes):
        problems.append("政策节点身份集与目录不一致")
    if set(actual_edges) != set(expected_edges) or len(edges) != len(actual_edges):
        problems.append("国家政策关系集与目录不一致")
    if any(set(node["labels"]) != {"Entity", "MacroEconomic"} for node in nodes):
        problems.append("政策节点 label 不唯一")
    if any(node["data_object_id"] is not None for node in nodes):
        problems.append("演示政策节点冒充了 Data 对象")
    if any(node["embedding_dimension"] != 1024 for node in nodes):
        problems.append("政策节点向量缺失或维度错误")
    if any(edge["embedding_dimension"] != 1024 for edge in edges):
        problems.append("国家政策关系向量缺失或维度错误")
    for uuid, expected in expected_nodes.items():
        actual = actual_nodes.get(uuid)
        if actual is None:
            continue
        expected_properties = {
            "name": expected.name,
            "summary": expected.summary,
            **expected.attributes,
        }
        if any(actual.get(key) != value for key, value in expected_properties.items()):
            problems.append(f"政策节点属性与目录不一致：{uuid}")
    for uuid, expected in expected_edges.items():
        actual = actual_edges.get(uuid)
        if actual is None:
            continue
        if (
            actual.get("name") != expected.name
            or actual.get("fact") != expected.fact
            or actual.get("relation_schema") != "CountryImplementsMacroEconomic"
            or actual.get("demo_catalog_version") != plan.base.catalog_version
        ):
            problems.append(f"国家政策关系属性与目录不一致：{uuid}")
    if problems:
        raise ProjectionError("; ".join(problems))

    return {
        **plan.base.summary(),
        "node_total": len(nodes),
        "relationship_total": len(edges),
        "verified": True,
    }
