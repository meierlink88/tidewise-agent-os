"""Typed contracts for collection channel configuration."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from capabilities.collection.internal.models import SourceLevel


class OwnershipType(StrEnum):
    FIXED = "fixed"
    DYNAMIC = "dynamic"


class ChannelType(StrEnum):
    WEB_SEARCH = "web_search"
    API = "api"
    RSS = "rss"


class AdapterKey(StrEnum):
    BOCHA = "bocha"
    TAVILY = "tavily"
    PARALLEL = "parallel"
    CLS = "cls"
    EASTMONEY_FAST = "eastmoney_fast"
    EASTMONEY_STOCK = "eastmoney_stock"
    STCN = "stcn"
    GENERIC_RSS = "generic_rss"


_ADAPTER_CHANNEL_TYPES = {
    AdapterKey.BOCHA: ChannelType.WEB_SEARCH,
    AdapterKey.TAVILY: ChannelType.WEB_SEARCH,
    AdapterKey.PARALLEL: ChannelType.WEB_SEARCH,
    AdapterKey.CLS: ChannelType.API,
    AdapterKey.EASTMONEY_FAST: ChannelType.API,
    AdapterKey.EASTMONEY_STOCK: ChannelType.API,
    AdapterKey.STCN: ChannelType.API,
    AdapterKey.GENERIC_RSS: ChannelType.RSS,
}


class CollectionChannel(BaseModel):
    """One executable channel instance frozen from a Data Service Source Snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    ownership_type: OwnershipType
    channel_type: ChannelType
    adapter_key: AdapterKey
    enabled: bool
    endpoint: HttpUrl
    app_key: str | None = None
    config: dict[str, Any]
    priority: int = Field(default=1, ge=1, le=5)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    max_results: int = Field(default=10, ge=1, le=100)
    default_source_level: SourceLevel
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_protocol(self) -> "CollectionChannel":
        if _ADAPTER_CHANNEL_TYPES[self.adapter_key] != self.channel_type:
            raise ValueError("adapter_key is incompatible with channel_type")
        if self.ownership_type == OwnershipType.DYNAMIC and (
            self.channel_type != ChannelType.RSS or self.adapter_key != AdapterKey.GENERIC_RSS
        ):
            raise ValueError("dynamic channels must use the generic RSS/Atom protocol")
        return self


class ChannelCatalog(Protocol):
    """Read interface consumed by deterministic collection Functions."""

    def list_enabled(self, channel_type: ChannelType) -> list[CollectionChannel]: ...
