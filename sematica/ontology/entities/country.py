"""Country 实体及其出向 Graphiti 关系。"""

from datetime import date

from pydantic import Field

from sematica.ontology.entities.base import TidewiseEntity, TidewiseEntityLink
from sematica.ontology.enums import MembershipType


class Country(TidewiseEntity):
    """具有 ISO 3166-1 标准身份的主权国家；不是跨国区域、国内行政区或国际组织。"""

    data_object_id: str | None = Field(
        default=None,
        pattern=r"^COU[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        description="Tidewise Data 中权威的 Country ID；禁止推测或编造。",
    )
    code: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{2}$",
        description="明确已知时使用 ISO 3166-1 alpha-2 大写国家代码。",
    )
    name_en: str | None = Field(
        default=None,
        min_length=1,
        description="来自权威国家事实的官方英文简称。",
    )
    strategic_positioning: str | None = Field(
        default=None,
        min_length=1,
        description="该国家战略定位的权威描述。",
    )
    key_resources: str | None = Field(
        default=None,
        min_length=1,
        description="该国家关键战略资源的权威描述。",
    )


class CountryInRegion(TidewiseEntityLink):
    """一个国家属于一个稳定的跨国分析区域。"""


class CountryMemberOfOrganization(TidewiseEntityLink):
    """一个国家在可选的起止日期内参与一个国际组织。"""

    membership_type: MembershipType | None = Field(
        default=None,
        description="国家参与国际组织的受控会员类型。",
    )
    effective_date: date | None = Field(
        default=None,
        description="已知时，会员资格开始生效的首个日历日。",
    )
    expiry_date: date | None = Field(
        default=None,
        description="已知时，会员资格最后有效的日历日。",
    )


class CountryImplementsMacroEconomic(TidewiseEntityLink):
    """一个国家在其政策体系中可实施某类宏观经济政策动作。

    该关系表达政策工具的制度适用性，不表示该国家此刻正在执行该政策。
    """


ENTITY_TYPES = {"Country": Country}
EDGE_TYPES = {
    "CountryInRegion": CountryInRegion,
    "CountryMemberOfOrganization": CountryMemberOfOrganization,
    "CountryImplementsMacroEconomic": CountryImplementsMacroEconomic,
}
EDGE_TYPE_MAP = {
    ("Country", "Region"): ["CountryInRegion"],
    ("Country", "Organization"): ["CountryMemberOfOrganization"],
    ("Country", "MacroEconomic"): ["CountryImplementsMacroEconomic"],
}
