"""宏观经济政策动作实体。"""

from datetime import datetime

from pydantic import BaseModel, Field

from sematica.ontology.entities.base import NonBlankText, TidewiseEntity
from sematica.ontology.enums import MacroEconomicCategory, MacroEconomicStatus


class MacroEconomic(TidewiseEntity):
    """可复用的宏观经济政策动作，如加息、降息、财政刺激或产业补贴。

    该实体表达政策动作的稳定语义，不将国家名称编入实体身份。哪个国家可实施该动作，
    由 CountryImplementsMacroEconomic 关系单独表达。
    """

    data_object_id: str | None = Field(
        default=None,
        pattern=r"^MEC[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        description="Tidewise Data 中权威的 MacroEconomic ID；禁止推测或编造。",
    )
    policy_key: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{1,63}$",
        description="政策动作在版本化目录中稳定、全大写的业务键。",
    )
    name_en: NonBlankText | None = Field(
        default=None,
        max_length=100,
        description="政策动作的标准英文名称。",
    )
    category: MacroEconomicCategory | None = Field(
        default=None,
        description="政策动作所属的受控宏观经济分类，如货币政策线或财政政策线。",
    )
    description: NonBlankText | None = Field(
        default=None,
        description="政策动作的标准定义、适用边界和典型实施方式。",
    )
    status: MacroEconomicStatus | None = Field(
        default=None,
        description="政策动作蓝图的受控生命周期状态。",
    )
    updated_at: datetime | None = Field(
        default=None,
        description=("Tidewise Data 中该政策动作最后变更的权威时间；禁止推测。"),
    )


ENTITY_TYPES = {"MacroEconomic": MacroEconomic}
EDGE_TYPES: dict[str, type[BaseModel]] = {}
EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {}
