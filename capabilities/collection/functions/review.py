"""Per-article Workflow functions; the one semantic call remains a visible Agent Step."""

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput
from pydantic import BaseModel

from capabilities.collection.functions.collection import collect_raw_evidence
from capabilities.collection.internal.article_queue import (
    claim_next,
    enqueue_candidate,
    enqueue_legacy_document,
    fail_claim,
    finish_claim,
    item_root,
    queue_counts,
    read_prepared,
    record_article_failure,
    reject_review,
    save_claimed,
    upload_article,
)
from capabilities.collection.internal.buffer import (
    artifact_root,
    collection_staging_root,
    read_title_curation_if_present,
    read_tool_batches,
    write_json,
)
from capabilities.evidence import (
    ArticleReviewDraft,
    ArticleReviewRequest,
    EvidenceAnalysisRequest,
    EvidenceReviewDraft,
    EvidenceReviewRequest,
    PreparedEvidencePublication,
    SkippedEvidencePublication,
)
from capabilities.evidence.functions import (
    curate_evidence,
    prepare_evidence_analysis,
    publish_evidence,
    recover_evidence_publication,
    reuse_published_evidence,
    transfer_legacy_raw_documents,
)


class ArticleFailureRecordingError(RuntimeError):
    """The workflow cannot safely continue without a durable error record."""


def _state(context: RunContext) -> dict[str, Any]:
    if context.session_state is None:
        context.session_state = {}
    state = context.session_state.setdefault("article_review", {})
    if state.get("run_id") != context.run_id:
        state.clear()
        state["run_id"] = context.run_id
    return state


def _content(step_input: StepInput) -> Any:
    value = step_input.previous_step_content
    return value if value is not None else step_input.get_last_step_content()


async def collect_articles(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Persist acquisition once; failed-channel details remain in original Tool receipts."""
    marker = collection_staging_root(run_context.run_id) / "article-acquisition.json"
    if not marker.exists():
        await collect_raw_evidence(step_input, run_context)
        write_json(marker, {"complete": True})
    new = duplicates = 0
    for batch in read_tool_batches(run_context.run_id):
        for candidate in batch.candidates:
            _, created = enqueue_candidate(candidate, run_context.run_id)
            new += int(created)
            duplicates += int(not created)
    return StepOutput(content={"enqueued": new, "known_article_versions": duplicates, **queue_counts()})


async def evidence_collect(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Collect sources and perform deterministic article-version deduplication."""
    return await collect_articles(step_input, run_context)


async def prepare_evidence_review(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Select only an unreviewed article; publication failures are not recovery work."""
    del step_input
    state = _state(run_context)
    state.pop("skip", None)
    state.pop("claim", None)
    claim = claim_next(run_context.run_id, fresh_only=True)
    if claim is None:
        return StepOutput(
            content={"idle": True, **queue_counts(), "run_failed_articles": state.get("failed_count", 0)}, stop=True
        )
    state["claim"] = claim
    try:
        prepared = read_prepared(claim["article_key"])
        existing = reuse_published_evidence(prepared)
        if existing is not None:
            payload = {**existing.model_dump(mode="json"), "reused_publication": True}
            finish_claim(claim, "completed", payload)
            state["skip"] = {"article_key": claim["article_key"], "status": "already_published", **payload}
            return StepOutput(content=state["skip"])
        analysis = await prepare_evidence_analysis(prepared, run_context)
        request = EvidenceAnalysisRequest.model_validate(analysis.content)
        # Keep machine identity exclusively in run_context; send only semantic source fields.
        document = request.document.model_dump(
            include={"title", "raw_text", "source_name", "source_url", "published_at", "collected_at"}
        )
        return StepOutput(content=EvidenceReviewRequest(document=document, categories=request.categories))
    except Exception as exc:
        return fail_current_article(run_context, "Prepare Evidence Review", exc)


def article_review_gate(run_context: RunContext) -> StepOutput | None:
    """Skip downstream work only when this iteration has a recorded terminal disposition."""
    skip = _state(run_context).get("skip")
    return StepOutput(content=skip) if skip is not None else None


def fail_current_article(run_context: RunContext, step: str, exc: Exception) -> StepOutput:
    """Record a handled article error. Never convert a failure to persist the record into success."""
    state = _state(run_context)
    claim = state["claim"]
    message = re.sub(r"(?i)(bearer\s+|sk-)[^\s\"'<>]+", "[REDACTED]", str(exc))[:2000]
    error = {
        "run_id": run_context.run_id,
        "step": step,
        "error_type": type(exc).__name__,
        "message": message,
        "at": datetime.now(UTC).isoformat(),
    }
    try:
        record_article_failure(claim, error)
    except Exception as recording_error:
        raise ArticleFailureRecordingError("Unable to persist article failure safely") from recording_error
    state["failed_count"] = state.get("failed_count", 0) + 1
    state["skip"] = {"article_key": claim["article_key"], "status": "failed", "error": error}
    return StepOutput(content=state["skip"])


async def evidence_publish(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Own all deterministic result handling; never call an Agent or schedule recovery."""
    skipped = article_review_gate(run_context)
    if skipped is not None:
        return skipped
    claim = _state(run_context)["claim"]
    stage = "Evidence Reviewer"
    try:
        content = _content(step_input)
        draft = (
            EvidenceReviewDraft.model_validate_json(content)
            if isinstance(content, str)
            else EvidenceReviewDraft.model_validate(content)
        )
        # Bind the semantic result to the code-owned claim, never to a generated identifier.
        bound = ArticleReviewDraft(article_key=claim["article_key"], **draft.model_dump())
        stage = "Evidence Publish"
        save_article_review(StepInput(previous_step_content=bound), run_context)
        validated = validate_article_review(StepInput(), run_context)
        if not article_has_evidence(StepInput(previous_step_content=validated.content)):
            return validated
        return await publish_reviewed_article(StepInput(), run_context)
    except Exception as exc:
        return fail_current_article(run_context, stage, exc)


async def prepare_next_article(step_input: StepInput, run_context: RunContext) -> StepOutput:
    del step_input
    state = _state(run_context)
    claim = claim_next(run_context.run_id)
    if claim is None:
        return StepOutput(content={"idle": True, **queue_counts()}, stop=True)
    state["claim"] = claim
    key = claim["article_key"]
    try:
        prepared = read_prepared(key)
        publication = recover_evidence_publication(prepared.publication_key)
        publication_path = item_root(key) / "publication.json"
        if publication is None and publication_path.exists():
            publication = PreparedEvidencePublication.model_validate_json(publication_path.read_text())
        if publication is not None:
            if publication.prepared_raw.publication_key != prepared.publication_key:
                raise ValueError("Recovered publication identity mismatch")
            save_claimed(claim, "publication.json", publication.model_dump(mode="json"))
            return StepOutput(content={"article_key": key, "review_required": False})
        analysis = await prepare_evidence_analysis(prepared, run_context)
        if (item_root(key) / "review.json").exists():
            return StepOutput(content={"article_key": key, "review_required": False})
        request = EvidenceAnalysisRequest.model_validate(analysis.content)
        return StepOutput(content=ArticleReviewRequest(article_key=key, **request.model_dump()))
    except Exception as exc:
        fail_claim(claim, type(exc).__name__)
        raise


def article_needs_review(step_input: StepInput) -> bool:
    return isinstance(_content(step_input), ArticleReviewRequest) or (
        isinstance(_content(step_input), dict) and "document" in _content(step_input)
    )


def save_article_review(step_input: StepInput, run_context: RunContext) -> StepOutput:
    claim = _state(run_context)["claim"]
    try:
        content = _content(step_input)
        review = (
            ArticleReviewDraft.model_validate_json(content)
            if isinstance(content, str)
            else ArticleReviewDraft.model_validate(content)
        )
        if review.article_key != claim["article_key"]:
            raise ValueError("Article review identity mismatch")
        save_claimed(claim, "review.json", review.model_dump(mode="json"))
        return StepOutput(content={"article_key": review.article_key, "review_saved": True})
    except Exception as exc:
        fail_claim(claim, type(exc).__name__)
        raise


def validate_article_review(step_input: StepInput, run_context: RunContext) -> StepOutput:
    del step_input
    claim = _state(run_context)["claim"]
    key = claim["article_key"]
    try:
        if (item_root(key) / "publication.json").exists():
            return StepOutput(content={"article_key": key, "publish": True})
        review = ArticleReviewDraft.model_validate_json((item_root(key) / "review.json").read_text())
        if not review.is_relevant:
            finish_claim(claim, "excluded", {"reason": "IRRELEVANT"})
            return StepOutput(content={"article_key": key, "publish": False, "reason": "IRRELEVANT"})
        output = curate_evidence(StepInput(previous_step_content=review.extraction), run_context)
        if isinstance(output.content, SkippedEvidencePublication):
            finish_claim(claim, "excluded", {"reason": "NO_VALID_EVIDENCE"})
            return StepOutput(content={"article_key": key, "publish": False, "reason": "NO_VALID_EVIDENCE"})
        publication = PreparedEvidencePublication.model_validate(output.content)
        save_claimed(claim, "publication.json", publication.model_dump(mode="json"))
        return StepOutput(content={"article_key": key, "publish": True, "evidence_count": len(publication.evidences)})
    except Exception as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(("UNKNOWN_CATEGORY", "NONCOMPLIANT_LLM_OUTPUT")):
            reject_review(claim)
        fail_claim(claim, type(exc).__name__)
        raise


def article_has_evidence(step_input: StepInput) -> bool:
    value = _content(step_input)
    return isinstance(value, dict) and value.get("publish") is True


async def publish_reviewed_article(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """One visible publish Step: archive, Raw, Evidence, Event enqueue, terminal state."""
    del step_input
    claim = _state(run_context)["claim"]
    try:
        publication = PreparedEvidencePublication.model_validate_json(
            (item_root(claim["article_key"]) / "publication.json").read_text()
        )
        await asyncio.to_thread(upload_article, claim, publication.prepared_raw)
        result = await publish_evidence(StepInput(previous_step_content=publication), advance_cursor=False)
        if not isinstance(result.content, BaseModel):
            raise ValueError("Invalid Evidence publication result")
        payload = result.content.model_dump(mode="json")
        finish_claim(claim, "completed", payload)
        return StepOutput(content={"article_key": claim["article_key"], **payload})
    except Exception as exc:
        fail_claim(claim, type(exc).__name__)
        raise


def article_processing_complete(outputs: list[StepOutput]) -> bool:
    return any(isinstance(output.content, dict) and output.content.get("idle") is True for output in outputs)


def import_legacy_articles() -> dict[str, int]:
    """Explicit cutover; legacy cursor advances only after durable per-article enqueue."""
    count = transfer_legacy_raw_documents(enqueue_legacy_document)
    staged = 0
    for run in sorted((artifact_root() / ".pending").iterdir()) if (artifact_root() / ".pending").exists() else []:
        if not run.is_dir():
            continue
        decisions = read_title_curation_if_present(run.name)
        rejected = {item.candidate_id for item in decisions.decisions if not item.is_relevant} if decisions else set()
        for batch in read_tool_batches(run.name):
            for candidate in batch.candidates:
                _, created = enqueue_candidate(
                    candidate,
                    run.name,
                    exclusion_reason="LEGACY_IRRELEVANT" if candidate.candidate_id in rejected else None,
                )
                staged += int(created)
    return {"transferred_legacy_documents": count, "transferred_staged_articles": staged, **queue_counts()}
