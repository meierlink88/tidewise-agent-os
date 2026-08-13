"""Stable collection Tool façades exposed through the AgentOS Registry."""

from typing import Any

from capabilities.collection.tools.api_fetch import api_fetch
from capabilities.collection.tools.rss_fetch import rss_fetch
from capabilities.collection.tools.web_fetch import web_fetch

COLLECTION_TOOLS: list[Any] = [
    web_fetch,
    api_fetch,
    rss_fetch,
]

__all__ = [
    "COLLECTION_TOOLS",
    "api_fetch",
    "rss_fetch",
    "web_fetch",
]
