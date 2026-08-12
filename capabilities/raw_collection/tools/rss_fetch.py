"""Agent-facing RSS/Atom Tool façade."""

from agno.run import RunContext

from capabilities.raw_collection.channels import ChannelType
from capabilities.raw_collection.tools.fetch_common import execute_fetch_tool


async def rss_fetch(query: str, run_context: RunContext, lookback_hours: int = 48) -> str:
    """Collect concurrently from every enabled RSS/Atom channel."""
    return await execute_fetch_tool("rss_fetch", ChannelType.RSS, query, run_context, lookback_hours)
