"""Tidewise 图实体与关系类型共用的 Graphiti 校验规则。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

NonBlankText = Annotated[str, StringConstraints(pattern=r"\S")]


ShortName = Annotated[str, StringConstraints(min_length=1, max_length=5, pattern=r"\S")]


class TidewiseEntity(BaseModel):
    """Graphiti 自定义实体属性的基础校验规则。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TidewiseEntityLink(BaseModel):
    """Graphiti 自定义实体关系属性的基础校验规则。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
