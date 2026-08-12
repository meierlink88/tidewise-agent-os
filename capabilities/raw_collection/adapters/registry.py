"""Code-owned mapping from database adapter keys to provider implementations."""

from capabilities.raw_collection.adapters import ChannelAdapter
from capabilities.raw_collection.adapters.api import (
    ClsAdapter,
    EastmoneyFastAdapter,
    EastmoneyStockAdapter,
    StcnAdapter,
)
from capabilities.raw_collection.adapters.rss import GenericRssAdapter
from capabilities.raw_collection.adapters.web_search import BochaAdapter, ParallelAdapter, TavilyAdapter

ADAPTERS: dict[str, ChannelAdapter] = {
    "bocha": BochaAdapter(),
    "tavily": TavilyAdapter(),
    "parallel": ParallelAdapter(),
    "cls": ClsAdapter(),
    "eastmoney_fast": EastmoneyFastAdapter(),
    "eastmoney_stock": EastmoneyStockAdapter(),
    "stcn": StcnAdapter(),
    "generic_rss": GenericRssAdapter(),
}
