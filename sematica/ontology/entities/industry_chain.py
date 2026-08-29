"""IndustryChain 实体及其出向 Graphiti 关系。"""

from datetime import date, datetime

from pydantic import Field

from sematica.ontology.entities.base import TidewiseEntity, TidewiseEntityLink
from sematica.ontology.enums import RecordStatus, ReviewStatus


class IndustryChain(TidewiseEntity):
    """围绕目标产出与终端用途组织的有向投研产业链子图。"""

    data_object_id: str | None = Field(
        default=None,
        pattern=r"^ICH[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        description="Tidewise Data 中权威的 IndustryChain ID；禁止推测或编造。",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="用于将文本提及解析到同一 IndustryChain 的稳定别名。",
    )
    scope: str | None = Field(
        default=None,
        min_length=1,
        description="IndustryChain 覆盖的业务与产品范围。",
    )
    target_output: str | None = Field(
        default=None,
        min_length=1,
        description="IndustryChain 所围绕的最终目标产出。",
    )
    end_use: str | None = Field(
        default=None,
        min_length=1,
        description="IndustryChain 目标产出的主要终端用途。",
    )
    geography: str | None = Field(
        default=None,
        min_length=1,
        description="IndustryChain 权威的自由文本地理范围。",
    )
    primary_country_id: str | None = Field(
        default=None,
        pattern=r"^COU[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        description=(
            "作为 IndustryChain 属性保留的可选 Tidewise Data 权威 Country ID；"
            "该字段不创建 IndustryChain 到 Country 的图关系。"
        ),
    )
    as_of_date: date | None = Field(
        default=None,
        description="IndustryChain 拓扑所对应的业务有效日期。",
    )
    review_status: ReviewStatus | None = Field(
        default=None,
        description="IndustryChain 事实是候选状态还是已审核状态。",
    )
    review_note: str | None = Field(
        default=None,
        min_length=1,
        description="IndustryChain 的可选权威审核备注。",
    )
    technology_route_qualifier: str | None = Field(
        default=None,
        min_length=1,
        description="用于界定 IndustryChain 边界的可选技术路线限定语。",
    )
    observable_variables: list[str] = Field(
        default_factory=list,
        description="用于观测 IndustryChain 状态的稳定 Variable 业务键。",
    )
    updated_at: datetime | None = Field(
        default=None,
        description="Tidewise Data 中 IndustryChain 事实最后变更的权威时间；禁止推测。",
    )


class IndustryChainMapping(TidewiseEntityLink):
    """Data 权威拥有的 IndustryChain 映射关系共用身份与生命周期字段。"""

    data_object_id: str | None = Field(
        default=None,
        pattern=r"^ERL[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        description="Tidewise Data 中权威的 EntityRelation ID；禁止推测或编造。",
    )
    status: RecordStatus | None = Field(
        default=None,
        description="权威映射关系是否处于活跃或非活跃状态。",
    )


class IndustryChainMappedToIndustry(IndustryChainMapping):
    """一条 IndustryChain 映射到 Tidewise Data 中的一个 Industry 分类事实。"""


class IndustryChainMappedToConcept(IndustryChainMapping):
    """一条 IndustryChain 映射到 Tidewise Data 中的一个跨行业 Concept 事实。"""


ENTITY_TYPES = {"IndustryChain": IndustryChain}
EDGE_TYPES = {
    "IndustryChainMappedToIndustry": IndustryChainMappedToIndustry,
    "IndustryChainMappedToConcept": IndustryChainMappedToConcept,
}
EDGE_TYPE_MAP = {
    ("IndustryChain", "Industry"): ["IndustryChainMappedToIndustry"],
    ("IndustryChain", "Concept"): ["IndustryChainMappedToConcept"],
}
