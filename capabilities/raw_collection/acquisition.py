"""Shared deterministic acquisition core used by Workflow Functions and Tool façades."""

import asyncio
import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import cast

from agno.run import RunContext

from capabilities.raw_collection.adapters import ChannelAdapter, FetchRequest
from capabilities.raw_collection.buffer import write_tool_batch
from capabilities.raw_collection.channels import AdapterKey, ChannelCatalog, ChannelType, CollectionChannel
from capabilities.raw_collection.dispatchers import dispatch_channels
from capabilities.raw_collection.models import (
    Candidate,
    ChannelFetchReceipt,
    FetchReceipt,
    ToolBatchReceipt,
)


def _dependencies(run_context: RunContext) -> dict[str, object]:
    return cast(dict[str, object], run_context.dependencies or {})


def _request(query: str, lookback_hours: int, run_context: RunContext) -> FetchRequest:
    normalized = query.strip()
    if not normalized or len(normalized) > 512:
        raise ValueError("query must contain 1..512 characters")
    maximum = int(os.getenv("COLLECTOR_MAX_QUERY_WINDOW_HOURS", "8760"))
    if lookback_hours < 1 or lookback_hours > maximum:
        raise ValueError("lookback_hours is outside the allowed range")
    cutoff_value = _dependencies(run_context).get("collector_cutoff")
    if not isinstance(cutoff_value, str):
        raise ValueError("collector cutoff is missing")
    cutoff = datetime.fromisoformat(cutoff_value.replace("Z", "+00:00"))
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("collector cutoff must include a timezone")
    before = cutoff.astimezone(UTC)
    return FetchRequest(
        query=normalized,
        published_after=before - timedelta(hours=lookback_hours),
        published_before=before,
    )


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
    from capabilities.raw_collection.adapters.registry import ADAPTERS

    return ADAPTERS


def _persist_candidates(
    connector: str,
    request: FetchRequest,
    run_context: RunContext,
    candidates: list[Candidate],
) -> str:
    dependencies = run_context.dependencies or {}
    component_id = dependencies.get("collector_agent_component_id")
    version = dependencies.get("collector_agent_config_version")
    instructions_sha256 = dependencies.get("collector_instructions_sha256")
    if not isinstance(component_id, str) or not component_id:
        raise ValueError("collector Agent component identity is missing")
    if not isinstance(version, int) or version < 1:
        raise ValueError("collector Agent config version is missing")
    if not isinstance(instructions_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", instructions_sha256):
        raise ValueError("collector Agent instructions hash is missing")

    batch = write_tool_batch(
        collection_id=run_context.run_id,
        connector=connector,
        query=request.query,
        requested_after=request.published_after,
        requested_before=request.published_before,
        agent_component_id=component_id,
        agent_config_version=version,
        instructions_sha256=instructions_sha256,
        candidates=candidates,
    )
    in_window = sum(
        request.published_after <= (item.published_at or item.collected_at) <= request.published_before
        for item in batch.candidates
    )
    return ToolBatchReceipt(
        batch_id=batch.batch_id,
        connector=batch.connector,
        query=batch.query,
        requested_after=batch.requested_after,
        requested_before=batch.requested_before,
        result_count=len(batch.candidates),
        in_window_result_count=in_window,
        candidate_ids=[item.candidate_id for item in batch.candidates],
    ).model_dump_json(exclude_none=True)


async def _persist_candidates_async(
    connector: str,
    request: FetchRequest,
    run_context: RunContext,
    candidates: list[Candidate],
) -> str:
    return await asyncio.to_thread(_persist_candidates, connector, request, run_context, candidates)


async def execute_fetch(
    tool: str,
    channel_type: ChannelType,
    query: str,
    run_context: RunContext,
    lookback_hours: int,
) -> str:
    """Execute one channel class against the frozen run configuration."""
    try:
        request = _request(query, lookback_hours, run_context)
        channels = await asyncio.to_thread(_catalog(run_context).list_enabled, channel_type)
        if channel_type == ChannelType.WEB_SEARCH and len(channels) > 1:
            return json.dumps({"tool": tool, "error": "invalid_channel_catalog"})
        if not channels:
            return FetchReceipt(
                tool=tool,
                outcome="no_channels",
                query=request.query,
                requested_after=request.published_after,
                requested_before=request.published_before,
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
                        in_window_result_count=0,
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
                    in_window_result_count=persisted.in_window_result_count,
                )
            )
        successes = sum(item.outcome == "succeeded" for item in receipts)
        outcome = "succeeded" if successes == len(receipts) else "failed" if successes == 0 else "partial"
        return FetchReceipt(
            tool=tool,
            outcome=outcome,
            query=request.query,
            requested_after=request.published_after,
            requested_before=request.published_before,
            channels=receipts,
        ).model_dump_json()
    except (TypeError, ValueError):
        return json.dumps({"tool": tool, "error": "invalid_request"})
