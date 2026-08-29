"""源自 Tidewise Data 合同的 GeopoliticRivalry 实体。"""

from datetime import datetime

from pydantic import BaseModel, Field

from sematica.ontology.entities.base import NonBlankText, TidewiseEntity
from sematica.ontology.enums import GeopoliticRivalryStatus, GeopoliticRivalryType


class GeopoliticRivalry(TidewiseEntity):
    """稳定的地缘政治竞争或军事冲突议题蓝图，用于将动态 Event 归入持续议题。

    它不是一次具体 Event。参与方与影响区域字段是 Tidewise Data 中经审阅的文本，不能单凭这些文本
    证明或创建 Country、Region、Organization 等权威图关系。
    """

    data_object_id: str | None = Field(
        default=None,
        pattern=r"^GPR[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        description=("Tidewise Data 中权威的 GeopoliticRivalry ID；禁止推测或编造。"),
    )
    name_en: NonBlankText | None = Field(
        default=None,
        max_length=100,
        description="地缘政治议题蓝图的标准英文名称。",
    )
    rivalry_type: GeopoliticRivalryType | None = Field(
        default=None,
        description="地缘政治竞争或军事战争蓝图的受控类型。",
    )
    description: NonBlankText | None = Field(
        default=None,
        description="地缘政治议题蓝图范围边界的标准自然语言定义。",
    )
    core_actors: NonBlankText | None = Field(
        default=None,
        description=("经审阅的核心参与方文本；该字段不创建权威实体关系。"),
    )
    peripheral_actors: str | None = Field(
        default=None,
        min_length=1,
        description="可选的、经审阅的外围参与方文本。",
    )
    influenced_regions: list[str] | None = Field(
        default=None,
        description=(
            "可选的、经审阅的影响区域文本；null 与空列表保留不同的 Data 事实语义，两者都不能证明 Region 关系。"
        ),
    )
    status: GeopoliticRivalryStatus | None = Field(
        default=None,
        description="地缘政治议题蓝图的受控生命周期状态。",
    )
    updated_at: datetime | None = Field(
        default=None,
        description=("Tidewise Data 中该议题蓝图最后变更的权威时间；禁止推测。"),
    )


ENTITY_TYPES = {"GeopoliticRivalry": GeopoliticRivalry}
EDGE_TYPES: dict[str, type[BaseModel]] = {}
EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {}
