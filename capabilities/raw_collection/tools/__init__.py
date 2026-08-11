"""Channel tools exposed to the Collector Agent."""

from typing import Any

from capabilities.raw_collection.tools.bocha import search_bocha_news
from capabilities.raw_collection.tools.eastmoney import search_eastmoney_stock_news
from capabilities.raw_collection.tools.parallel import search_parallel_news
from capabilities.raw_collection.tools.professional import (
    search_cls_telegraph,
    search_eastmoney_fast_news,
    search_stcn_quick_news,
)
from capabilities.raw_collection.tools.tavily import search_tavily_news
from capabilities.raw_collection.tools.time_window import resolve_collection_time_window

COLLECTION_TOOLS: list[Any] = [
    resolve_collection_time_window,
    search_parallel_news,
    search_tavily_news,
    search_bocha_news,
    search_cls_telegraph,
    search_eastmoney_fast_news,
    search_eastmoney_stock_news,
    search_stcn_quick_news,
]

__all__ = [
    "COLLECTION_TOOLS",
    "resolve_collection_time_window",
    "search_bocha_news",
    "search_cls_telegraph",
    "search_eastmoney_fast_news",
    "search_eastmoney_stock_news",
    "search_parallel_news",
    "search_stcn_quick_news",
    "search_tavily_news",
]
