"""Prepare up to 20 Raw articles, then process one per native Workflow iteration."""

import json
from collections import Counter
from datetime import UTC, datetime

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput

from capabilities.collection_v2 import RawDocumentV2, archived_article_keys, read_archived_article
from capabilities.event_v2.internal.models import (
    DocumentEventDraft,
    DuplicateDecision,
    EventRecallCandidate,
    StagedDocumentEvent,
)
from capabilities.event_v2.internal.runtime import document_event_runtime
from capabilities.event_v2.internal.storage import decision_lock, path, read, write


def _batch(ctx: RunContext) -> dict:
    batch = read(path("runs", ctx.run_id, "batch"))
    if batch is None:
        raise ValueError("Document Event batch is missing")
    return batch


async def prepare_document_events(step_input: StepInput, run_context: RunContext) -> StepOutput:
    batch_file = path("runs", run_context.run_id, "batch")
    batch = read(batch_file)
    if batch is None:
        raw_input = step_input.input
        if isinstance(raw_input, str) and raw_input.strip().startswith("{"):
            raw_input = json.loads(raw_input)
        retry_failed = isinstance(raw_input, dict) and raw_input.get("retry_failed") is True
        selected = []
        for key in archived_article_keys():
            result = read(path("articles", key, "result"))
            if result is None or (retry_failed and result["status"] == "failed"):
                selected.append(key)
            if len(selected) == 20:
                break
        sources, preparation_errors = {}, {}
        for key in selected:
            try:
                sources[key] = read_archived_article(key).model_dump(mode="json")
            except (ValueError, FileNotFoundError) as exc:
                preparation_errors[key] = type(exc).__name__
        batch = {
            "sources": sources,
            "preparation_errors": preparation_errors,
            "run_id": run_context.run_id,
            "article_keys": selected,
            "cursor": 0,
            "items": [],
            "retry_failed": retry_failed,
            "agent_versions": document_event_runtime().versions(),
        }
        if selected:
            await document_event_runtime().ready()
        write(batch_file, batch)
    return StepOutput(
        content={
            "selected": len(batch["article_keys"]),
            "batch_size": 20,
            "outcome": "prepared" if batch["article_keys"] else "no_change",
            "article_keys": batch["article_keys"],
        },
        stop=not batch["article_keys"],
    )


async def _process(key: str, batch: dict) -> dict:
    runtime = document_event_runtime()
    result_file = path("articles", key, "result")
    previous = read(result_file)
    if previous is not None and not (batch["retry_failed"] and previous["status"] == "failed"):
        return {"article_key": key, "status": "already_processed", "previous_status": previous["status"]}
    stage = "read_raw"
    try:
        if key in batch["preparation_errors"]:
            raise ValueError("Raw archive could not be prepared")
        raw = RawDocumentV2.model_validate(batch["sources"][key])
        event_file = path("articles", key, "event")
        event = read(event_file)
        if event is None:
            stage = "extract"
            draft = await runtime.extract(
                {
                    "article_key": key,
                    "title": raw.candidate.title,
                    "content": raw.candidate.content,
                    "published_at": raw.candidate.published_at.isoformat() if raw.candidate.published_at else None,
                    "source_url": raw.canonical_url,
                },
                batch["agent_versions"],
                f"{batch['run_id']}:{key}",
            )
            draft = DocumentEventDraft.model_validate(draft)
            event = StagedDocumentEvent(
                candidate_id=f"document-event:{key}",
                article_key=key,
                raw_path=raw.url_path,
                collected_at=datetime.now(UTC),
                published_at=raw.candidate.published_at,
                event=draft,
            ).model_dump(mode="json")
            write(event_file, event)
        stage = "embedding"
        vector_file = path("articles", key, "vector")
        vector = read(vector_file)
        if vector is None:
            vector = await runtime.embed(event["event"]["title"], event["event"]["summary"])
            write(vector_file, vector)
        stage = "recall"
        decision_file = path("articles", key, "decision")
        frozen = read(decision_file)
        if frozen is None:
            candidates = [
                EventRecallCandidate.model_validate(c).model_dump(mode="json")
                for c in await runtime.recall(vector, key)
            ]
            write(path("articles", key, "recall"), {"candidates": candidates})
            stage = "identity"
            decision = (
                await runtime.decide(event["event"], candidates, batch["agent_versions"], f"{batch['run_id']}:{key}")
                if candidates
                else DuplicateDecision(duplicate=False, reason="No vector candidates")
            )
            decision = DuplicateDecision.model_validate(decision)
            allowed = {c["candidate_id"] for c in candidates}
            if (decision.duplicate and decision.matched_id not in allowed) or (
                not decision.duplicate and decision.matched_id is not None
            ):
                raise ValueError("Identity selected a candidate outside recall or contradictory identity")
            frozen = {"decision": decision.model_dump(mode="json"), "agent_versions": batch["agent_versions"]}
            write(decision_file, frozen)
        decision = DuplicateDecision.model_validate(frozen["decision"])
        stage = "stage_candidate"
        if not decision.duplicate:
            await runtime.stage(event, vector)
        result = {
            "article_key": key,
            "status": "duplicate" if decision.duplicate else "accepted",
            "candidate_id": None if decision.duplicate else event["candidate_id"],
            "matched_id": decision.matched_id,
            "reason": decision.reason,
            "completed_at": datetime.now(UTC).isoformat(),
        }
    except TimeoutError as exc:
        result = {
            "article_key": key,
            "status": "failed",
            "stage": stage,
            "error_code": type(exc).__name__,
            "completed_at": datetime.now(UTC).isoformat(),
        }
    except OSError:
        # A disk failure cannot safely record exclusion or release article progress.
        raise
    except Exception as exc:
        result = {
            "article_key": key,
            "status": "failed",
            "stage": stage,
            "error_code": type(exc).__name__,
            "completed_at": datetime.now(UTC).isoformat(),
        }
    write(result_file, result)
    return result


async def extract_next_document_event(step_input: StepInput, run_context: RunContext) -> StepOutput:
    del step_input
    # Held throughout recall/Agent judgment/staging: concurrent batches cannot both
    # accept the same story before seeing the other's committed candidate vector.
    async with decision_lock():
        batch = _batch(run_context)
        if batch["cursor"] >= len(batch["article_keys"]):
            return StepOutput(content={"done": True})
        if "current_item" not in batch:
            key = batch["article_keys"][batch["cursor"]]
            result = await _process(key, batch)
            batch["current_item"] = {**result, "steps": {"extract": {"status": result["status"]}}}
            write(path("runs", run_context.run_id, "batch"), batch)
        return StepOutput(content={**batch["current_item"], "done": False})


async def _pending_step(run_context: RunContext, name: str, previous: str, *, finish: bool = False) -> StepOutput:
    async with decision_lock():
        batch = _batch(run_context)
        item = batch.get("current_item")
        if item is None:
            raise ValueError("No current article for the requested Workflow step")
        steps = item["steps"]
        if previous not in steps:
            raise ValueError("Workflow steps must execute in order")
        if item["status"] != "accepted":
            result = {"status": "skipped", "reason": item["status"]}
        elif previous != "extract" and steps[previous]["status"] != "completed":
            result = {"status": "blocked", "reason": "previous_step_not_implemented"}
        else:
            result = {"status": "not_implemented", "reason": "skill_not_implemented"}
        steps[name] = result
        if finish:
            item["pipeline_completed"] = False
            write(path("articles", item["article_key"], "pipeline"), item)
            batch["items"].append(item)
            batch["cursor"] += 1
            del batch["current_item"]
        write(path("runs", run_context.run_id, "batch"), batch)
        return StepOutput(
            content={
                "article_key": item["article_key"],
                "step": name,
                **result,
                "done": finish and batch["cursor"] == len(batch["article_keys"]),
            }
        )


async def associate_document_story(step_input: StepInput, run_context: RunContext) -> StepOutput:
    del step_input
    return await _pending_step(run_context, "story_association", "extract")


async def discover_document_signals(step_input: StepInput, run_context: RunContext) -> StepOutput:
    del step_input
    return await _pending_step(run_context, "signal_discovery", "story_association")


async def publish_document_event(step_input: StepInput, run_context: RunContext) -> StepOutput:
    del step_input
    return await _pending_step(run_context, "data_publication", "signal_discovery", finish=True)


def document_events_complete(iteration_outputs: list[StepOutput]) -> bool:
    return any(isinstance(o.content, dict) and o.content.get("done") is True for o in iteration_outputs)


def summarize_document_events(step_input: StepInput, run_context: RunContext) -> StepOutput:
    del step_input
    batch = _batch(run_context)
    if batch["cursor"] != len(batch["article_keys"]):
        raise ValueError("Document Event loop stopped before completing selected articles")
    counts = Counter(i["status"] for i in batch["items"])
    result = {
        "extraction_outcome": "partial"
        if counts["failed"] and counts["failed"] < len(batch["items"])
        else "failed"
        if counts["failed"]
        else "completed",
        "selected": len(batch["article_keys"]),
        "accepted": counts["accepted"],
        "duplicates": counts["duplicate"],
        "failed": counts["failed"],
        "already_processed": counts["already_processed"],
        "items": batch["items"],
        "published": False,
        "pipeline_completed": False,
        "pending_steps": ["story_association", "signal_discovery", "data_publication"],
    }
    result["outcome"] = "incomplete" if counts["accepted"] else result["extraction_outcome"]
    write(path("runs", run_context.run_id, "result"), result)
    return StepOutput(content=result)
