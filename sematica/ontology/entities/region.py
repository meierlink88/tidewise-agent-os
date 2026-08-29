"""源自 Tidewise Data Region schema 的区域实体。"""

from pydantic import BaseModel, Field

from sematica.ontology.entities.base import TidewiseEntity
from sematica.ontology.enums import RegionType


class Region(TidewiseEntity):
    """经审阅的全球或跨国分析区域，如东亚或中非。

    可以是地理、大洲、多边机制或投资口径定义的区域。它不是国家，也不是省、州、市、县等
    国内行政区；四川不是本模型中的 Region。
    """

    data_object_id: str | None = Field(
        default=None,
        pattern=r"^REG[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        description="Tidewise Data 中权威的 Region ID；禁止推测或编造。",
    )
    code: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]*$",
        description="稳定的全大写 Region 业务代码。",
    )
    name_en: str | None = Field(
        default=None,
        min_length=1,
        description="Region 的官方英文名称。",
    )
    region_type: RegionType | None = Field(
        default=None,
        description="Region 是大洲、地理、多边机制还是投资口径定义的受控类型。",
    )
    description: str | None = Field(
        default=None,
        min_length=1,
        description="Region 范围边界或业务含义的权威说明。",
    )


ENTITY_TYPES = {"Region": Region}
EDGE_TYPES: dict[str, type[BaseModel]] = {}
EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {}
