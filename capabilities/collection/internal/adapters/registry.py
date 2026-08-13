"""Code-owned mapping from database adapter keys to provider implementations."""

from capabilities.collection.internal.adapters import ChannelAdapter
from capabilities.collection.internal.adapters.api import (
    ClsAdapter,
    EastmoneyFastAdapter,
    EastmoneyStockAdapter,
    StcnAdapter,
)
from capabilities.collection.internal.adapters.rss import GenericRssAdapter
from capabilities.collection.internal.adapters.web_search import BochaAdapter, ParallelAdapter, TavilyAdapter
from capabilities.collection.internal.channels import AdapterKey

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
