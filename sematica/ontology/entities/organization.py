"""Organization 实体及其出向 Graphiti 关系。"""

from datetime import date

from pydantic import Field

from sematica.ontology.entities.base import TidewiseEntity, TidewiseEntityLink
from sematica.ontology.enums import BindingPowerLevel, InfluenceRating


class Organization(TidewiseEntity):
    """经审阅的国际联盟、多边组织、协会或国际机制。

    例如联合国、北约、东盟和欧佩克。它不是公司、上市发行人、媒体企业、普通国内企业或
    一般政府部门。
    """

    data_object_id: str | None = Field(
        default=None,
        pattern=r"^ORG[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        description="Tidewise Data 中权威的 Organization ID；禁止推测或编造。",
    )
    code: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]*$",
        description="稳定的全大写 Organization 业务代码。",
    )
    name_en: str | None = Field(
        default=None,
        min_length=1,
        description="国际组织的官方英文名称。",
    )
    category_code: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]*$",
        description="Tidewise Data 提供的权威国际组织分类代码。",
    )
    function_code: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]*$",
        description="Tidewise Data 提供的权威国际组织核心职能代码。",
    )
    domain_tag_codes: list[str] = Field(
        default_factory=list,
        description="Tidewise Data 提供的权威国际组织领域标签代码。",
    )
    legal_entity_code: str | None = Field(
        default=None,
        min_length=1,
        description="当该国际组织是法律实体时，可选的 ISO 17442 LEI。",
    )
    binding_power_level: BindingPowerLevel | None = Field(
        default=None,
        description="国际组织约束力强度的权威等级。",
    )
    influence_rating: InfluenceRating | None = Field(
        default=None,
        description="国际组织的全球或领域影响力权威评级。",
    )
    strategic_positioning: str | None = Field(
        default=None,
        min_length=1,
        description="国际组织战略定位的权威描述。",
    )
    core_impact_scope: str | None = Field(
        default=None,
        min_length=1,
        description="国际组织核心影响范围的权威描述。",
    )
    founding_document: str | None = Field(
        default=None,
        min_length=1,
        description="设立该国际组织的条约或正式文件。",
    )
    established_date: date | None = Field(
        default=None,
        description="国际组织成立的日历日期。",
    )
    headquarters_city: str | None = Field(
        default=None,
        min_length=1,
        description="国际组织总部所在城市。",
    )
    headquarters_subdivision_id: str | None = Field(
        default=None,
        min_length=1,
        description="Data 合同中保留的国内行政区标识；当前不创建图关系。",
    )
    description: str | None = Field(
        default=None,
        min_length=1,
        description="国际组织的权威补充说明。",
    )


class OrganizationInRegion(TidewiseEntityLink):
    """一个区域性国际组织属于一个稳定的跨国分析区域。"""


ENTITY_TYPES = {"Organization": Organization}
EDGE_TYPES = {"OrganizationInRegion": OrganizationInRegion}
EDGE_TYPE_MAP = {
    ("Organization", "Region"): ["OrganizationInRegion"],
}
