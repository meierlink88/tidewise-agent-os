"""构建并校验 Reason-owned 基本面 Variable 图谱目录。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from graphiti_core import Graphiti
from graphiti_core.nodes import EntityNode
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from sematica.ontology import Variable
from sematica.ontology.entities.base import NonBlankText
from sematica.ontology.entities.variable import VariableID, validate_variable_catalog
from sematica.ontology.enums import AnalysisAnchorType, VariableGroup, VariableRole
from sematica.projection.authoritative_writer import GROUP_ID, write_projection
from sematica.projection.runtime import ProjectionError

CATALOG_PATH = Path(__file__).with_name("catalog.v1.json")
VARIABLE_CATALOG_SOURCE = "tidewise-reason/fundamental-variable-catalog"
EXPECTED_VARIABLE_COUNT = 56
EXPECTED_GROUP_COUNTS = {
    VariableGroup.DEMAND: 4,
    VariableGroup.SUPPLY_CAPACITY: 7,
    VariableGroup.PRICE_PROFITABILITY: 6,
    VariableGroup.CAPITAL_CYCLE: 2,
    VariableGroup.TECHNOLOGY: 6,
    VariableGroup.COMPETITION_SECURITY: 8,
    VariableGroup.MACRO_POLICY: 9,
    VariableGroup.GEOPOLITICAL: 6,
    VariableGroup.COMPANY_FINANCIAL: 8,
}
GROUP_NAMES = {
    VariableGroup.DEMAND: "需求",
    VariableGroup.SUPPLY_CAPACITY: "供给与产能",
    VariableGroup.PRICE_PROFITABILITY: "价格与盈利",
    VariableGroup.CAPITAL_CYCLE: "资本周期",
    VariableGroup.TECHNOLOGY: "技术路线",
    VariableGroup.COMPETITION_SECURITY: "竞争与供应链安全",
    VariableGroup.MACRO_POLICY: "宏观与政策",
    VariableGroup.GEOPOLITICAL: "地缘政治",
    VariableGroup.COMPANY_FINANCIAL: "公司经营与财务",
}


def variable_node_uuid(variable_id: str) -> str:
    """为 Reason-owned Variable 生成稳定图谱身份。"""

    return str(uuid5(NAMESPACE_URL, f"urn:tidewise:reason-variable:{variable_id}"))


class FundamentalVariableItem(BaseModel):
    """一个经审阅的基本面 Variable 目录项。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variable_id: VariableID
    name: NonBlankText = Field(max_length=100)
    aliases: tuple[NonBlankText, ...] = Field(default_factory=tuple, max_length=12)
    variable_group: VariableGroup
    definition: NonBlankText = Field(max_length=2000)
    measurement_basis: NonBlankText = Field(max_length=2000)
    unit: NonBlankText | None = Field(default=None, max_length=100)
    allowed_anchor_types: tuple[AnalysisAnchorType, ...] = Field(min_length=1)
    mutually_exclusive_variable_ids: tuple[VariableID, ...] = Field(default_factory=tuple)
    derived_from_variable_ids: tuple[VariableID, ...] = Field(default_factory=tuple)

    @field_validator(
        "aliases",
        "allowed_anchor_types",
        "mutually_exclusive_variable_ids",
        "derived_from_variable_ids",
    )
    @classmethod
    def tuple_values_must_be_unique(cls, values: tuple[object, ...]) -> tuple[object, ...]:
        if len(values) != len(set(values)):
            raise ValueError("目录字段不能包含重复值")
        return values

    @model_validator(mode="after")
    def investment_group_is_not_a_fundamental_item(self) -> FundamentalVariableItem:
        if self.variable_group == VariableGroup.INVESTMENT_ASSESSMENT:
            raise ValueError("基本面目录不能包含投资判断变量")
        return self


class FundamentalVariableCatalog(BaseModel):
    """Reason-owned 基本面 Variable 版本化目录。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: str = Field(pattern=r"^variable-catalog/v[1-9][0-9]*$")
    published_at: datetime
    maintenance_owner: NonBlankText = Field(max_length=200)
    items: tuple[FundamentalVariableItem, ...] = Field(
        min_length=EXPECTED_VARIABLE_COUNT,
        max_length=EXPECTED_VARIABLE_COUNT,
    )

    @model_validator(mode="after")
    def catalog_must_match_the_reviewed_shape(self) -> FundamentalVariableCatalog:
        if self.published_at.tzinfo is None or self.published_at.utcoffset() != UTC.utcoffset(self.published_at):
            raise ValueError("published_at 必须是明确的 UTC 时间")
        for field_name in ("variable_id", "name"):
            values = [getattr(item, field_name) for item in self.items]
            if len(values) != len(set(values)):
                raise ValueError(f"Variable 目录存在重复 {field_name}")
        counts = Counter(item.variable_group for item in self.items)
        if counts != Counter(EXPECTED_GROUP_COUNTS):
            raise ValueError("Variable 目录分组数量与审阅结果不一致")

        primary_names = {item.name.strip().casefold(): item.variable_id for item in self.items}
        terms = dict(primary_names)
        for item in self.items:
            for alias in item.aliases:
                normalized = alias.strip().casefold()
                previous = terms.get(normalized)
                if previous is not None and previous != item.variable_id:
                    raise ValueError(f"Variable 名称或别名 {alias!r} 同时指向 {previous} 和 {item.variable_id}")
                if normalized == item.name.strip().casefold():
                    raise ValueError(f"Variable {item.variable_id} 的别名重复主名")
                terms[normalized] = item.variable_id
        return self


class VariableInitializationPlan(BaseModel):
    """Variable 写入前的完整预检结果。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    catalog_version: str
    nodes: tuple[EntityNode, ...]

    def summary(self) -> dict[str, object]:
        return {
            "group_id": GROUP_ID,
            "catalog_version": self.catalog_version,
            "fundamental_variables": len(self.nodes),
            "variable_groups": len(EXPECTED_GROUP_COUNTS),
            "relationships": 0,
            "authority": "Tidewise Reason Variable catalog",
        }


def load_catalog(path: Path = CATALOG_PATH) -> FundamentalVariableCatalog:
    """读取并校验打包的基本面 Variable 目录。"""

    try:
        return FundamentalVariableCatalog.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise ProjectionError(f"无效的 Variable 目录：{exc}") from None


def build_plan(catalog: FundamentalVariableCatalog) -> VariableInitializationPlan:
    """在访问图之前，用公开 ontology 校验全部 Variable。"""

    variables: list[Variable] = []
    for item in catalog.items:
        try:
            variables.append(
                Variable(
                    variable_id=item.variable_id,
                    variable_role=VariableRole.FUNDAMENTAL,
                    variable_group=item.variable_group,
                    aliases=list(item.aliases),
                    definition=item.definition,
                    measurement_basis=item.measurement_basis,
                    unit=item.unit,
                    allowed_anchor_types=list(item.allowed_anchor_types),
                    mutually_exclusive_variable_ids=list(item.mutually_exclusive_variable_ids),
                    derived_from_variable_ids=list(item.derived_from_variable_ids),
                    maintenance_owner=catalog.maintenance_owner,
                    catalog_version=catalog.catalog_version,
                )
            )
        except ValidationError as exc:
            raise ProjectionError(f"Variable {item.variable_id} 违反 ontology：{exc}") from None
    try:
        validated = validate_variable_catalog(variables)
    except ValueError as exc:
        raise ProjectionError(f"Variable 目录跨记录校验失败：{exc}") from None

    names = {item.variable_id: item.name for item in catalog.items}
    nodes = tuple(
        EntityNode(
            uuid=variable_node_uuid(variable.variable_id),
            name=names[variable.variable_id],
            group_id=GROUP_ID,
            labels=["Variable"],
            created_at=catalog.published_at,
            summary=(
                f"投研基本面变量：{names[variable.variable_id]}。"
                f"分组：{GROUP_NAMES[variable.variable_group]}。{variable.definition}"
            ),
            attributes={
                **variable.model_dump(mode="json"),
                "variable_catalog_source": VARIABLE_CATALOG_SOURCE,
            },
        )
        for variable in validated
    )
    return VariableInitializationPlan(catalog_version=catalog.catalog_version, nodes=nodes)


async def execute_plan(
    graphiti: Graphiti,
    plan: VariableInitializationPlan,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int, dict[str, int]]:
    """幂等写入 Variable 节点，不写关系且不删除图数据。"""

    return await write_projection(
        graphiti,
        nodes=plan.nodes,
        edges=(),
        owned_node_labels=frozenset({"Variable"}),
        owned_edge_names=frozenset(),
        replace=False,
        progress=progress,
    )


async def inspect_graph_state(graphiti: Graphiti, plan: VariableInitializationPlan) -> dict[str, object]:
    """读取目录拥有的 Variable 节点以便校验。"""

    result = await graphiti.driver.execute_query(
        """
        MATCH (variable:Variable:Entity {group_id: $group_id})
        WHERE variable.variable_catalog_source = $catalog_source
        OPTIONAL MATCH (variable)-[relation]-()
        RETURN variable.uuid AS uuid, variable.name AS name,
               variable.summary AS summary, variable.variable_id AS variable_id,
               variable.variable_role AS variable_role,
               variable.variable_group AS variable_group,
               variable.aliases AS aliases, variable.definition AS definition,
               variable.measurement_basis AS measurement_basis, variable.unit AS unit,
               variable.allowed_anchor_types AS allowed_anchor_types,
               variable.mutually_exclusive_variable_ids AS mutually_exclusive_variable_ids,
               variable.derived_from_variable_ids AS derived_from_variable_ids,
               variable.maintenance_owner AS maintenance_owner,
               variable.catalog_version AS catalog_version,
               variable.variable_catalog_source AS variable_catalog_source,
               labels(variable) AS labels,
               size(variable.name_embedding) AS embedding_dimension,
               count(relation) AS relationship_count,
               count(CASE WHEN relation.name = 'SIGNAL_ON' THEN relation END)
                   AS signal_relationship_count
        ORDER BY variable.variable_id
        """,
        group_id=GROUP_ID,
        catalog_source=VARIABLE_CATALOG_SOURCE,
    )
    return {"nodes": [record.data() for record in result.records]}


def verify_state(plan: VariableInitializationPlan, state: dict[str, object]) -> dict[str, object]:
    """要求图中 Variable 结果与版本化目录完全一致。"""

    nodes = state.get("nodes")
    if not isinstance(nodes, list):
        raise ProjectionError("无效的 Variable 图谱检查结果")
    expected = {node.uuid: node for node in plan.nodes}
    actual = {node["uuid"]: node for node in nodes}
    problems: list[str] = []
    if set(actual) != set(expected) or len(nodes) != len(actual):
        problems.append("Variable 节点身份集与目录不一致")
    if any(set(node["labels"]) != {"Entity", "Variable"} for node in nodes):
        problems.append("Variable 节点 label 不唯一")
    if any(node["embedding_dimension"] != 1024 for node in nodes):
        problems.append("Variable 节点向量缺失或维度错误")
    if any(node["relationship_count"] != node["signal_relationship_count"] for node in nodes):
        problems.append("基本面 Variable 只能具有分析阶段创建的 Signal Fact 关系")
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
            problems.append(f"Variable 节点属性与目录不一致：{uuid}")
    if problems:
        raise ProjectionError("; ".join(problems))

    return {
        **plan.summary(),
        "node_total": len(nodes),
        "signal_relationship_total": sum(int(node["signal_relationship_count"]) for node in nodes),
        "catalog_relationship_total": 0,
        "verified": True,
    }
