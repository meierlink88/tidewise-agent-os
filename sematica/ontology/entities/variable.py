"""Tidewise Reason 拥有的受控 Variable 实体与目录校验规则。"""

from collections.abc import Iterable
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from sematica.ontology.entities.base import NonBlankText, TidewiseEntity
from sematica.ontology.enums import AnalysisAnchorType, VariableGroup, VariableRole

VariableID = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,63}$"),
]


class Variable(TidewiseEntity):
    """全局可复用的受控观测维度，Signal 用它表达某个投研锚点发生了什么变化。

    同一 Variable 可适用于多种锚点类型。变化方向、影响周期、影响强度和具体锚点属于
    Signal Fact，不应因不同锚点复制新的 Variable 实体。Variable 本身不是 Signal，也不表示看多或看空。
    """

    variable_id: VariableID = Field(
        description="Reason Variable 版本化目录中稳定、全小写的业务键。",
    )
    variable_role: VariableRole = Field(
        description=("Variable 在投研推理中的受控角色：基本面观测维度或投资判断维度。"),
    )
    variable_group: VariableGroup = Field(
        description=("Variable 的主分类，用于按因果通道缩小 AI 候选变量范围；不创建任何锚点关系。"),
    )
    aliases: list[NonBlankText] = Field(
        default_factory=list,
        description="经审阅且可解析到同一受控 Variable 的别名。",
    )
    definition: NonBlankText = Field(
        description="Variable 的标准含义与适用边界。",
    )
    measurement_basis: NonBlankText = Field(
        description=("用于观测变化的定量测量方式或经审阅的定性判定依据。"),
    )
    unit: NonBlankText | None = Field(
        default=None,
        description="可选的标准计量单位；定性判定时为 null。",
    )
    allowed_anchor_types: list[AnalysisAnchorType] = Field(
        min_length=1,
        description=("该 Variable 具有意义的投研锚点类型；这是适用性元数据，不直接创建 Variable 到锚点的图事实。"),
    )
    mutually_exclusive_variable_ids: list[VariableID] = Field(
        default_factory=list,
        description="不得与当前 Variable 同时描述同一原子观测的受控 Variable 键。",
    )
    derived_from_variable_ids: list[VariableID] = Field(
        default_factory=list,
        description="当前 Variable 可明确派生自哪些受控 Variable 键。",
    )
    maintenance_owner: NonBlankText = Field(
        description="负责审阅与维护 Variable 定义的领域负责人。",
    )
    catalog_version: str = Field(
        pattern=r"^variable-catalog/v[1-9][0-9]*$",
        description="定义当前 Variable 的版本化目录合同。",
    )

    @field_validator("aliases")
    @classmethod
    def canonicalize_aliases(cls, values: list[str]) -> list[str]:
        return [value.strip() for value in values]

    @model_validator(mode="after")
    def validate_local_rules(self) -> "Variable":
        investment_role = self.variable_role == VariableRole.INVESTMENT_ASSESSMENT
        investment_group = self.variable_group == VariableGroup.INVESTMENT_ASSESSMENT
        if investment_role != investment_group:
            raise ValueError("INVESTMENT_ASSESSMENT role and group must be declared together")
        if len(set(self.allowed_anchor_types)) != len(self.allowed_anchor_types):
            raise ValueError("allowed_anchor_types must not contain duplicates")

        normalized_aliases = [alias.strip().casefold() for alias in self.aliases]
        if len(normalized_aliases) != len(set(normalized_aliases)):
            raise ValueError("Variable aliases must be unique after normalization")
        if self.variable_id.casefold() in normalized_aliases:
            raise ValueError("Variable alias must not repeat its canonical identity")

        mutually_exclusive = set(self.mutually_exclusive_variable_ids)
        derived_from = set(self.derived_from_variable_ids)
        if len(mutually_exclusive) != len(self.mutually_exclusive_variable_ids):
            raise ValueError("mutually_exclusive_variable_ids must not contain duplicates")
        if len(derived_from) != len(self.derived_from_variable_ids):
            raise ValueError("derived_from_variable_ids must not contain duplicates")
        if self.variable_id in mutually_exclusive or self.variable_id in derived_from:
            raise ValueError("Variable must not reference itself")
        if mutually_exclusive & derived_from:
            raise ValueError("one Variable cannot be both mutually exclusive and a derivation source")
        return self


def validate_variable_catalog(variables: Iterable[Variable]) -> tuple[Variable, ...]:
    """Validate cross-record identities and references before catalog initialization."""

    records = tuple(variables)
    identities = [variable.variable_id for variable in records]
    duplicate_identities = sorted(identity for identity in set(identities) if identities.count(identity) > 1)
    if duplicate_identities:
        raise ValueError("duplicate Variable identity: " + ", ".join(duplicate_identities))

    known_identities = set(identities)
    referenced_identities = {
        reference
        for variable in records
        for reference in (
            *variable.mutually_exclusive_variable_ids,
            *variable.derived_from_variable_ids,
        )
    }
    unknown_references = sorted(referenced_identities - known_identities)
    if unknown_references:
        raise ValueError("unknown Variable reference: " + ", ".join(unknown_references))

    catalog_versions = {variable.catalog_version for variable in records}
    if len(catalog_versions) > 1:
        raise ValueError("Variable catalog must contain exactly one catalog_version")

    derivations = {variable.variable_id: set(variable.derived_from_variable_ids) for variable in records}
    visit_state: dict[str, int] = {}

    def visit(variable_id: str) -> None:
        state = visit_state.get(variable_id, 0)
        if state == 1:
            raise ValueError(f"cyclic Variable derivation: {variable_id}")
        if state == 2:
            return
        visit_state[variable_id] = 1
        for source_id in derivations[variable_id]:
            visit(source_id)
        visit_state[variable_id] = 2

    for identity in identities:
        visit(identity)

    resolved_terms: dict[str, str] = {}
    for variable in records:
        for term in (variable.variable_id, *variable.aliases):
            normalized = term.strip().casefold()
            owner = resolved_terms.get(normalized)
            if owner is not None and owner != variable.variable_id:
                raise ValueError(f"ambiguous Variable alias {term!r}: {owner}, {variable.variable_id}")
            resolved_terms[normalized] = variable.variable_id
    return records


ENTITY_TYPES = {"Variable": Variable}
EDGE_TYPES: dict[str, type[BaseModel]] = {}
EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {}
