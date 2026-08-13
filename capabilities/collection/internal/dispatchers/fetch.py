"""Bounded concurrent execution for configured collection channels."""

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass

from capabilities.collection.internal.adapters import ChannelAdapter, FetchRequest
from capabilities.collection.internal.channels.models import AdapterKey, CollectionChannel
from capabilities.collection.internal.models import Candidate


@dataclass(frozen=True)
class ChannelFetchResult:
    channel: CollectionChannel
    candidates: list[Candidate]
    error_code: str | None = None


async def dispatch_channels(
    channels: list[CollectionChannel],
    adapters: Mapping[AdapterKey, ChannelAdapter],
    request: FetchRequest,
) -> list[ChannelFetchResult]:
    """Execute every channel in stable order while isolating provider failures."""
    concurrency = int(os.getenv("COLLECTOR_CHANNEL_CONCURRENCY", "8"))
    if concurrency < 1 or concurrency > 64:
        raise ValueError("COLLECTOR_CHANNEL_CONCURRENCY must be between 1 and 64")
    semaphore = asyncio.Semaphore(concurrency)

    async def execute(channel: CollectionChannel) -> ChannelFetchResult:
        adapter = adapters.get(channel.adapter_key)
        if adapter is None:
            return ChannelFetchResult(channel=channel, candidates=[], error_code="not_configured")
        try:
            async with semaphore:
                candidates = await asyncio.wait_for(
                    adapter.fetch(channel, request),
                    timeout=channel.timeout_seconds,
                )
            if any(item.connector != channel.code for item in candidates):
                raise ValueError("Adapter returned a Candidate for another channel")
            return ChannelFetchResult(channel=channel, candidates=candidates)
        except TimeoutError:
            return ChannelFetchResult(channel=channel, candidates=[], error_code="timeout")
        except Exception:
            return ChannelFetchResult(channel=channel, candidates=[], error_code="request_failed")

    ordered = sorted(channels, key=lambda item: (item.priority, item.code))
    return list(await asyncio.gather(*(execute(channel) for channel in ordered)))
