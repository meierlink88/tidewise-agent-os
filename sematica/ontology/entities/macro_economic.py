"""Data-owned macroeconomic storyline and its primary domain profile."""

import json
from datetime import datetime

from pydantic import BaseModel, Field, TypeAdapter, field_validator

from sematica.ontology.entities.base import NonBlankText, TidewiseEntity

ID_SUFFIX = r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"


class MacroEconomicTactic(TidewiseEntity):
    name: NonBlankText
    description: NonBlankText


class MacroEconomic(TidewiseEntity):
    """单一核心命题的宏观经济故事线；目录属性来自 Data，禁止从新闻编造。
    Event 归类使用名称、领域与参考手段；命题描述对中国的影响，候选资产只用于匹配后研究。
    """

    data_object_id: str | None = Field(default=None, pattern="^MEC" + ID_SUFFIX + "$")
    core_proposition: NonBlankText | None = None
    domain_code: NonBlankText | None = None
    domain_name: NonBlankText | None = None
    domain_description: NonBlankText | None = None
    tactics: str | None = Field(
        default=None, description="完整手段 JSON 数组文本；每项仅有 name、description。Neo4j 不支持对象数组属性。"
    )
    candidate_assets: list[str] | None = Field(
        default=None, description="匹配后的有序候选资产，不用于 Event 归类或表达投资结论。"
    )
    updated_at: datetime | None = None

    @field_validator("tactics")
    @classmethod
    def valid_tactics(cls, value):
        if value is not None:
            items = TypeAdapter(list[MacroEconomicTactic]).validate_python(json.loads(value))
            if not items or len({t.name for t in items}) != len(items):
                raise ValueError("tactics must be nonempty and unique by name")
        return value

    @field_validator("candidate_assets")
    @classmethod
    def valid_assets(cls, value):
        if value is not None and (
            not value or len(set(value)) != len(value) or any(not x or x != x.strip() or len(x) > 100 for x in value)
        ):
            raise ValueError("invalid candidate assets")
        return value


ENTITY_TYPES = {"MacroEconomic": MacroEconomic}
EDGE_TYPES: dict[str, type[BaseModel]] = {}
EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {}
