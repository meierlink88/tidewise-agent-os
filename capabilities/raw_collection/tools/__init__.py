"""Stable Tool façades exposed to the Collector Agent."""

from typing import Any

from capabilities.raw_collection.tools.api_fetch import api_fetch
from capabilities.raw_collection.tools.rss_fetch import rss_fetch
from capabilities.raw_collection.tools.web_fetch import web_fetch

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
