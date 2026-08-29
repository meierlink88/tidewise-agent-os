"""Single active durable Event Analysis worker loop."""

from __future__ import annotations

import asyncio

from sematica.analysis.event.module import EventAnalysisModule


async def run_worker(
    module: EventAnalysisModule,
    *,
    stop_event: asyncio.Event,
    poll_interval_seconds: float = 1.0,
    batch_size: int = 5,
) -> None:
    while not stop_event.is_set():
        if await module.process_pending(limit=batch_size):
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
        except TimeoutError:
            pass
