"""Structured API Tool façade."""

from agno.run import RunContext

from capabilities.collection.internal.acquisition import execute_fetch
from capabilities.collection.internal.channels import ChannelType


async def api_fetch(query: str, run_context: RunContext, lookback_hours: int = 48) -> str:
    """Collect concurrently from every enabled structured API channel."""
    return await execute_fetch("api_fetch", ChannelType.API, query, run_context, lookback_hours)
