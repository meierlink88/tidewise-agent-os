"""Agent-facing structured API Tool façade."""

from agno.run import RunContext

from capabilities.raw_collection.channels import ChannelType
from capabilities.raw_collection.tools.fetch_common import execute_fetch_tool


async def api_fetch(query: str, run_context: RunContext, lookback_hours: int = 48) -> str:
    """Collect concurrently from every enabled structured API channel."""
    return await execute_fetch_tool("api_fetch", ChannelType.API, query, run_context, lookback_hours)
