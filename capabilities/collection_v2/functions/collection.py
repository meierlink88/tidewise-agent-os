"""Two model-free Workflow stages with an isolated per-run archive manifest."""

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput

from capabilities.collection import CollectionRequest, canonical_source_url, configured_raw_document_store
from capabilities.collection_v2.internal.acquisition import acquire
from capabilities.collection_v2.internal.models import RawAcquisitionV2, RawArchiveItemV2, RawCollectionResultV2
from capabilities.collection_v2.internal.storage import archive_candidate, file_lock, run_root, sha256, write_text


def _request(value: Any) -> CollectionRequest:
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    return CollectionRequest.model_validate(value)


async def collect_raw_v2(step_input: StepInput, run_context: RunContext) -> StepOutput:
    acquired = await acquire(run_context.run_id, _request(step_input.input))
    return StepOutput(
        content={
            "collection_id": acquired.collection_id,
            "candidates": sum(len(batch.candidates) for batch in acquired.batches),
            "channels": [receipt.model_dump(mode="json") for receipt in acquired.receipts],
        }
    )


def _publish(collection_id: str) -> RawCollectionResultV2:
    directory = run_root(collection_id)
    with file_lock(directory / ".publish.lock"):
        final_path = directory / "manifest.json"
        if final_path.exists():
            saved = json.loads(final_path.read_text())
            result = RawCollectionResultV2.model_validate(saved["result"])
            if result.collection_id != collection_id:
                raise ValueError("Archive manifest identity conflict")
            return result
        acquired = RawAcquisitionV2.model_validate_json((directory / "acquisition.json").read_text())
        if acquired.collection_id != collection_id:
            raise ValueError("Acquisition identity conflict")
        candidates = [candidate for batch in acquired.batches for candidate in batch.candidates]
        candidates.sort(key=lambda candidate: (str(candidate.url), candidate.connector, candidate.candidate_id))
        store = configured_raw_document_store() if candidates else None
        items: list[RawArchiveItemV2] = []
        handled: dict[tuple[str, str], RawArchiveItemV2] = {}
        if store is not None:
            for candidate in candidates:
                try:
                    identity = (canonical_source_url(str(candidate.url)), sha256(candidate.content.strip()))
                except ValueError:
                    items.append(archive_candidate(candidate, store))
                    continue
                previous = handled.get(identity)
                if previous is not None:
                    # One upload attempt per version per run, including failed versions.
                    status = "duplicate" if previous.status in {"archived", "duplicate"} else previous.status
                    items.append(previous.model_copy(update={"candidate_id": candidate.candidate_id, "status": status}))
                    continue
                item = archive_candidate(candidate, store)
                handled[identity] = item
                items.append(item)
        counts = Counter(item.status for item in items)
        failed_channels = sum(channel.outcome == "failed" for group in acquired.receipts for channel in group.channels)
        failed = counts["failed"] + counts["invalid"] + failed_channels
        usable = counts["archived"] + counts["duplicate"]
        outcome = (
            "partial"
            if failed and usable
            else "failed"
            if failed
            else ("completed" if counts["archived"] else "no_change")
        )
        result = RawCollectionResultV2(
            collection_id=collection_id,
            outcome=outcome,
            archived=counts["archived"],
            duplicates=counts["duplicate"],
            failed=counts["failed"],
            invalid=counts["invalid"],
            failed_channels=failed_channels,
            manifest_path=str(final_path),
            completed_at=datetime.now(UTC),
        )
        write_text(
            final_path,
            json.dumps(
                {
                    "schema_version": "raw_collection_v2_manifest.v1",
                    "result": result.model_dump(mode="json"),
                    "items": [item.model_dump(mode="json") for item in items],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        return result


async def publish_raw_v2(step_input: StepInput, run_context: RunContext) -> StepOutput:
    del step_input
    result = await asyncio.to_thread(_publish, run_context.run_id)
    return StepOutput(content=result)
