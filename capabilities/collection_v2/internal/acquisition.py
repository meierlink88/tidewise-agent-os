"""Run the existing provider protocols without model planning, filtering or old queues."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from capabilities.collection import (
    SOURCE_ADAPTERS,
    ChannelFetchReceipt,
    ChannelType,
    CollectionChannel,
    CollectionRequest,
    FetchReceipt,
    FetchRequest,
    ToolBatch,
    dispatch_channels,
    load_active_source_snapshot,
)
from capabilities.collection_v2.internal.models import RawAcquisitionV2
from capabilities.collection_v2.internal.storage import run_root, write_text


async def acquire(collection_id: str, request: CollectionRequest) -> RawAcquisitionV2:
    query = request.objective.strip()
    if not query or len(query) > 512:
        raise ValueError("query must contain 1..512 characters")
    path = run_root(collection_id) / "acquisition.json"
    if path.exists():
        saved = RawAcquisitionV2.model_validate_json(path.read_text())
        if saved.collection_id != collection_id or saved.query != query:
            raise ValueError("Acquisition run identity conflict")
        return saved
    # Credentials remain in memory only. Persist candidate results, never Source configuration.
    snapshot = tuple(channel.model_copy(deep=True) for channel in await asyncio.to_thread(load_active_source_snapshot))
    if sum(item.enabled and item.channel_type == ChannelType.WEB_SEARCH for item in snapshot) > 1:
        raise ValueError("Only one enabled Web Search source is allowed")

    async def group(channel_type: ChannelType) -> tuple[FetchReceipt, list[ToolBatch]]:
        channels: list[CollectionChannel] = [
            item for item in snapshot if item.enabled and item.channel_type == channel_type
        ]
        results = await dispatch_channels(channels, SOURCE_ADAPTERS, FetchRequest(query=query))
        receipts: list[ChannelFetchReceipt] = []
        batches: list[ToolBatch] = []
        for result in results:
            if result.error_code:
                receipts.append(
                    ChannelFetchReceipt(
                        channel_code=result.channel.code, outcome="failed", result_count=0, error_code=result.error_code
                    )
                )
                continue
            batch = ToolBatch(
                batch_id=str(uuid4()),
                collection_id=collection_id,
                connector=result.channel.code,
                query=query,
                collected_at=min((item.collected_at for item in result.candidates), default=datetime.now(UTC)),
                candidates=result.candidates,
            )
            # A complete successful response is retained even if another channel later fails.
            await asyncio.to_thread(
                write_text,
                run_root(collection_id) / "batches" / f"{batch.batch_id}.json",
                batch.model_dump_json(indent=2),
            )
            batches.append(batch)
            receipts.append(
                ChannelFetchReceipt(
                    channel_code=result.channel.code,
                    outcome="succeeded",
                    result_count=len(batch.candidates),
                    batch_id=batch.batch_id,
                )
            )
        successes = sum(item.outcome == "succeeded" for item in receipts)
        outcome = (
            "no_channels"
            if not receipts
            else ("succeeded" if successes == len(receipts) else "failed" if successes == 0 else "partial")
        )
        return FetchReceipt(channel_group=channel_type.value, outcome=outcome, query=query, channels=receipts), batches

    groups = await asyncio.gather(*(group(channel_type) for channel_type in ChannelType))
    batches = [batch for _, items in groups for batch in items]
    candidate_ids = [candidate.candidate_id for batch in batches for candidate in batch.candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("Candidate IDs must be unique within a collection")
    acquired = RawAcquisitionV2(
        collection_id=collection_id, query=query, receipts=[receipt for receipt, _ in groups], batches=batches
    )
    await asyncio.to_thread(write_text, path, acquired.model_dump_json(indent=2))
    return acquired
