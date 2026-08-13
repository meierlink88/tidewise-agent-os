"""Web Search Tool façade."""

from agno.run import RunContext

from capabilities.raw_collection.acquisition import execute_fetch
from capabilities.raw_collection.channels import ChannelType


async def web_fetch(query: str, run_context: RunContext, lookback_hours: int = 48) -> str:
    """Search with the single enabled Web Search channel configured in PostgreSQL."""
    return await execute_fetch("web_fetch", ChannelType.WEB_SEARCH, query, run_context, lookback_hours)
