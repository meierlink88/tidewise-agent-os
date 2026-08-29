"""Shared deterministic acquisition core used only by Workflow Functions."""

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Literal, cast

from agno.run import RunContext

from capabilities.collection.internal.adapters import ChannelAdapter, FetchRequest
from capabilities.collection.internal.buffer import write_tool_batch
from capabilities.collection.internal.channels import AdapterKey, ChannelCatalog, ChannelType, CollectionChannel
from capabilities.collection.internal.dispatchers import dispatch_channels
from capabilities.collection.internal.models import (
    Candidate,
    ChannelFetchReceipt,
    FetchReceipt,
    ToolBatchReceipt,
)


def _dependencies(run_context: RunContext) -> dict[str, object]:
    return cast(dict[str, object], run_context.dependencies or {})


def _request(query: str) -> FetchRequest:
    normalized = query.strip()
    if not normalized or len(normalized) > 512:
        raise ValueError("query must contain 1..512 characters")
    return FetchRequest(query=normalized)


def _catalog(run_context: RunContext) -> ChannelCatalog:
    injected = _dependencies(run_context).get("collection_channel_catalog")
    if injected is not None:
        return cast(ChannelCatalog, injected)
    snapshot = _dependencies(run_context).get("collection_channel_snapshot")
    if not isinstance(snapshot, Sequence) or isinstance(snapshot, (str, bytes)):
        raise ValueError("collection channel snapshot is missing")
    channels = tuple(CollectionChannel.model_validate(item) for item in snapshot)

    class SnapshotCatalog:
        def list_enabled(self, channel_type: ChannelType) -> list[CollectionChannel]:
            return [item for item in channels if item.enabled and item.channel_type == channel_type]

    return SnapshotCatalog()


def _adapters(run_context: RunContext) -> Mapping[AdapterKey, ChannelAdapter]:
    injected = _dependencies(run_context).get("collection_adapter_registry")
    if injected is not None:
        return cast(Mapping[AdapterKey, ChannelAdapter], injected)
    from capabilities.collection.internal.adapters.registry import ADAPTERS

    return ADAPTERS


def _persist_candidates(
    connector: str,
    request: FetchRequest,
    run_context: RunContext,
    candidates: list[Candidate],
) -> str:
    batch = write_tool_batch(
        collection_id=run_context.run_id,
        connector=connector,
        query=request.query,
        candidates=candidates,
    )
    return ToolBatchReceipt(
        batch_id=batch.batch_id,
        connector=batch.connector,
        query=batch.query,
        result_count=len(batch.candidates),
        candidate_ids=[item.candidate_id for item in batch.candidates],
    ).model_dump_json(exclude_none=True)


async def _persist_candidates_async(
    connector: str,
    request: FetchRequest,
    run_context: RunContext,
    candidates: list[Candidate],
) -> str:
    return await asyncio.to_thread(_persist_candidates, connector, request, run_context, candidates)


async def execute_channel_group(
    channel_group: Literal["web_search", "api", "rss"],
    channel_type: ChannelType,
    query: str,
    run_context: RunContext,
) -> str:
    """Execute one channel class against the frozen run configuration."""
    try:
        request = _request(query)
        channels = await asyncio.to_thread(_catalog(run_context).list_enabled, channel_type)
        if channel_type == ChannelType.WEB_SEARCH and len(channels) > 1:
            return json.dumps({"channel_group": channel_group, "error": "invalid_channel_catalog"})
        if not channels:
            return FetchReceipt(
                channel_group=channel_group,
                outcome="no_channels",
                query=request.query,
                channels=[],
            ).model_dump_json()

        results = await dispatch_channels(channels, _adapters(run_context), request)
        receipts: list[ChannelFetchReceipt] = []
        for result in results:
            if result.error_code is not None:
                receipts.append(
                    ChannelFetchReceipt(
                        channel_code=result.channel.code,
                        outcome="failed",
                        result_count=0,
                        error_code=result.error_code,
                    )
                )
                continue
            persisted = ToolBatchReceipt.model_validate_json(
                await _persist_candidates_async(result.channel.code, request, run_context, result.candidates)
            )
            receipts.append(
                ChannelFetchReceipt(
                    channel_code=result.channel.code,
                    outcome="succeeded",
                    batch_id=persisted.batch_id,
                    result_count=persisted.result_count,
                )
            )
        successes = sum(item.outcome == "succeeded" for item in receipts)
        outcome = "succeeded" if successes == len(receipts) else "failed" if successes == 0 else "partial"
        return FetchReceipt(
            channel_group=channel_group,
            outcome=outcome,
            query=request.query,
            channels=receipts,
        ).model_dump_json()
    except (TypeError, ValueError):
        return json.dumps({"channel_group": channel_group, "error": "invalid_request"})
