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
from capabilities.raw_collection.channels import AdapterKey

ADAPTERS: dict[AdapterKey, ChannelAdapter] = {
    AdapterKey.BOCHA: BochaAdapter(),
    AdapterKey.TAVILY: TavilyAdapter(),
    AdapterKey.PARALLEL: ParallelAdapter(),
    AdapterKey.CLS: ClsAdapter(),
    AdapterKey.EASTMONEY_FAST: EastmoneyFastAdapter(),
    AdapterKey.EASTMONEY_STOCK: EastmoneyStockAdapter(),
    AdapterKey.STCN: StcnAdapter(),
    AdapterKey.GENERIC_RSS: GenericRssAdapter(),
}
