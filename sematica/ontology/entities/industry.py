"""Industry 实体及其出向 Graphiti 关系。"""

from datetime import datetime

from pydantic import Field

from sematica.ontology.entities.base import TidewiseEntity, TidewiseEntityLink
from sematica.ontology.enums import ReviewStatus


class Industry(TidewiseEntity):
    """受控行业分类体系中的行业类目；用于行业归类，不是产业链。"""

    data_object_id: str | None = Field(
        default=None,
        pattern=r"^IND[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        description="Tidewise Data 中权威的 Industry ID；禁止推测或编造。",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="用于将文本提及解析到同一 Industry 的稳定别名。",
    )
    classification_system: str | None = Field(
        default=None,
        min_length=1,
        description="定义该 Industry 代码与层级的受控行业分类体系。",
    )
    industry_code: str | None = Field(
        default=None,
        min_length=1,
        description="该 Industry 在所属分类体系内的稳定代码。",
    )
    hierarchy_path_codes: list[str] = Field(
        default_factory=list,
        description="从根行业到当前 Industry 的有序分类代码路径。",
    )
    definition: str | None = Field(
        default=None,
        min_length=1,
        description="Industry 业务范围边界的权威定义。",
    )
    review_status: ReviewStatus | None = Field(
        default=None,
        description="Industry 事实是候选状态还是已审核状态。",
    )
    updated_at: datetime | None = Field(
        default=None,
        description="Tidewise Data 中 Industry 事实最后变更的权威时间；禁止推测。",
    )


class IndustryHasParent(TidewiseEntityLink):
    """一个 Industry 指向同一行业分类体系中的直接上级类目。"""


ENTITY_TYPES = {"Industry": Industry}
EDGE_TYPES = {"IndustryHasParent": IndustryHasParent}
EDGE_TYPE_MAP = {
    ("Industry", "Industry"): ["IndustryHasParent"],
}
