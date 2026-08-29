"""源自 Tidewise Data Concept schema 的概念实体。"""

from datetime import datetime

from pydantic import BaseModel, Field

from sematica.ontology.entities.base import TidewiseEntity
from sematica.ontology.enums import ConceptType, ReviewStatus


class Concept(TidewiseEntity):
    """由权威主数据维护的股票市场概念板块或投资主题。

    Concept 用于归集共享投资叙事、技术方向或商业逻辑的证券及产业对象。只有已存在于
    Tidewise Data Concept 主数据、或能明确解析到该主数据的名称才属于本类型。一般政策措施、
    监管动作、事件标签、产品、普通技术名词、行业、产业链、产业链节点、公司和证券均不属于 Concept。
    例如“出口管制”是政策动作，不是股票市场概念板块。
    """

    data_object_id: str | None = Field(
        default=None,
        pattern=r"^CON[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        description="Tidewise Data 中权威的 Concept ID；禁止推测或编造。",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="用于将文本提及解析到同一权威股票市场概念板块的稳定别名。",
    )
    concept_type: ConceptType | None = Field(
        default=None,
        description=(
            "股票市场概念板块的受控主题类型；POLICY 仅表示已纳入 Concept 主数据的"
            "政策主题板块，不表示加息、出口管制等具体政策动作。"
        ),
    )
    definition: str | None = Field(
        default=None,
        min_length=1,
        description="股票市场概念板块的权威业务定义、覆盖范围和排除边界。",
    )
    review_status: ReviewStatus | None = Field(
        default=None,
        description="Concept 事实是候选状态还是已审核状态。",
    )
    updated_at: datetime | None = Field(
        default=None,
        description="Tidewise Data 中 Concept 事实最后变更的权威时间；禁止推测。",
    )


ENTITY_TYPES = {"Concept": Concept}
EDGE_TYPES: dict[str, type[BaseModel]] = {}
EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {}
