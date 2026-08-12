"""Typed contracts for collection channel configuration."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from capabilities.raw_collection.models import SourceLevel


class OwnershipType(StrEnum):
    FIXED = "fixed"
    DYNAMIC = "dynamic"


class ChannelType(StrEnum):
    WEB_SEARCH = "web_search"
    API = "api"
    RSS = "rss"


class CollectionChannel(BaseModel):
    """One executable channel instance loaded from PostgreSQL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    ownership_type: OwnershipType
    channel_type: ChannelType
    adapter_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    enabled: bool
    endpoint: HttpUrl
    app_key: str | None = None
    config: dict[str, Any]
    priority: int = Field(default=1, ge=1)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    max_results: int = Field(default=10, ge=1, le=100)
    default_source_level: SourceLevel
    created_at: datetime
    updated_at: datetime


class ChannelCatalog(Protocol):
    """Read interface consumed by collection Tool façades."""

    def list_enabled(self, channel_type: ChannelType) -> list[CollectionChannel]: ...
