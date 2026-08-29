"""ChainNode 实体及其出向 Graphiti 关系。"""

from datetime import datetime

from pydantic import Field, PositiveInt

from sematica.ontology.entities.base import TidewiseEntity, TidewiseEntityLink
from sematica.ontology.enums import ContextualStage, ReviewStatus


class ChainNode(TidewiseEntity):
    """可在多条产业链中复用的业务、产品或技术环节；其产业链位置属于成员关系。"""

    data_object_id: str | None = Field(
        default=None,
        pattern=r"^CND[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        description="Tidewise Data 中权威的 ChainNode ID；禁止推测或编造。",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="用于将文本提及解析到同一 ChainNode 的稳定别名。",
    )
    definition: str | None = Field(
        default=None,
        min_length=1,
        description="ChainNode 的权威定义与业务边界。",
    )
    review_status: ReviewStatus | None = Field(
        default=None,
        description="ChainNode 事实是候选状态还是已审核状态。",
    )
    updated_at: datetime | None = Field(
        default=None,
        description="Tidewise Data 中 ChainNode 事实最后变更的权威时间。",
    )


class ChainNodeBelongsToIndustryChain(TidewiseEntityLink):
    """一个 ChainNode 在指定环节阶段和顺序位置上属于一条 IndustryChain。"""

    position: PositiveInt | None = Field(
        default=None,
        description="仅在当前 IndustryChain 内有效的正整数展示或遍历顺序。",
    )
    contextual_stage: ContextualStage | None = Field(
        default=None,
        description="仅在当前 IndustryChain 内有效的上游、中游或下游阶段。",
    )


class _ChainScopedTopologyLink(TidewiseEntityLink):
    """产业链作用域内拓扑关系的私有共用身份合同；不注册为 Graphiti 关系类型。"""

    data_object_id: str | None = Field(
        default=None,
        pattern=r"^IGE[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        description="Tidewise Data 中权威的 IndustryChainGraphEdge ID。",
    )
    industry_chain_id: str | None = Field(
        default=None,
        pattern=r"^ICH[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        description="该 ChainNode 到 ChainNode 关系成立时所属的 IndustryChain 上下文。",
    )


class ChainNodeInputTo(_ChainScopedTopologyLink):
    """在指定产业链内，源 ChainNode 向目标 ChainNode 提供输入。"""


class ChainNodeIsComponentOf(_ChainScopedTopologyLink):
    """在指定产业链内，源 ChainNode 是目标 ChainNode 的组成部分。"""


class ChainNodeDependsOn(_ChainScopedTopologyLink):
    """在指定产业链内，源 ChainNode 在结构上依赖目标 ChainNode。"""


ENTITY_TYPES = {"ChainNode": ChainNode}
EDGE_TYPES = {
    "ChainNodeBelongsToIndustryChain": ChainNodeBelongsToIndustryChain,
    "ChainNodeInputTo": ChainNodeInputTo,
    "ChainNodeIsComponentOf": ChainNodeIsComponentOf,
    "ChainNodeDependsOn": ChainNodeDependsOn,
}
EDGE_TYPE_MAP = {
    ("ChainNode", "IndustryChain"): ["ChainNodeBelongsToIndustryChain"],
    ("ChainNode", "ChainNode"): [
        "ChainNodeInputTo",
        "ChainNodeIsComponentOf",
        "ChainNodeDependsOn",
    ],
}
