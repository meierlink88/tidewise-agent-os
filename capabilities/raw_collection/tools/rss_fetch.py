"""RSS/Atom Tool façade."""

from agno.run import RunContext

from capabilities.raw_collection.acquisition import execute_fetch
from capabilities.raw_collection.channels import ChannelType


async def rss_fetch(query: str, run_context: RunContext, lookback_hours: int = 48) -> str:
    """Collect concurrently from every enabled RSS/Atom channel."""
    return await execute_fetch("rss_fetch", ChannelType.RSS, query, run_context, lookback_hours)
