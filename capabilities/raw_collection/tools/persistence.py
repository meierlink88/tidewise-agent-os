"""Collection Buffer persistence shared by the three Tool façades."""

import asyncio
import re

from agno.run import RunContext

from capabilities.raw_collection.adapters import FetchRequest
from capabilities.raw_collection.buffer import write_tool_batch
from capabilities.raw_collection.models import Candidate, ToolBatchReceipt


def persist_candidates(
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


async def persist_candidates_async(
    connector: str,
    request: FetchRequest,
    run_context: RunContext,
    candidates: list[Candidate],
) -> str:
    return await asyncio.to_thread(persist_candidates, connector, request, run_context, candidates)
