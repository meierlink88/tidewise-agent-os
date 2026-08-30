"""Workflow Function executors for the Evidence extraction capability."""

import asyncio
import calendar
import hashlib
import json
import re
import shutil
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput
from pydantic import ValidationError

from capabilities.evidence.internal.artifacts import (
    load_published_evidence_artifact,
    persist_evidence_identity_bindings,
)
from capabilities.evidence.internal.client import get_evidence_categories, post_publication
from capabilities.evidence.internal.models import (
    EvidenceAnalysisRequest,
    EvidenceCategoryCatalog,
    EvidenceCategoryDefinition,
    EvidenceExtractionDraft,
    EvidenceExtractionIdle,
    EvidenceIdentityBindings,
    EvidenceMetric,
    EvidencePublicationItem,
    EvidencePublicationResult,
    EvidenceSemantic,
    EvidenceSetPublicationResponse,
    EvidenceTime,
    PreparedEvidencePublication,
    PreparedRawDocument,
    RawEvidencePublication,
    RawEvidencePublicationResponse,
)
from capabilities.evidence.internal.storage import (
    advance_checkpoint,
    evidence_artifact_root,
    read_checkpoint,
    read_next_raw_document,
    write_json,
)

_CATEGORY_CATALOG_DEPENDENCY = "evidence_category_catalog"
_PREPARED_RAW_DOCUMENT_DEPENDENCY = "prepared_raw_document"
_EVIDENCE_RUN_STATE = "evidence_extraction"
_BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _normalize_json_literals(value: str) -> str:
    """Normalize non-standard uppercase JSON literals without touching quoted text."""
    replacements = {"NULL": "null", "TRUE": "true", "FALSE": "false"}
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(value):
        character = value[index]
        if in_string:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            output.append(character)
            index += 1
            continue
        matched = False
        for candidate, replacement in replacements.items():
            if value.startswith(candidate, index):
                output.append(replacement)
                index += len(candidate)
                matched = True
                break
        if not matched:
            output.append(character)
            index += 1
    return "".join(output)


def _model_from_content(model: type[Any], content: Any) -> Any:
    if isinstance(content, model):
        return content
    if isinstance(content, str):
        try:
            return model.model_validate_json(content)
        except ValidationError:
            normalized = _normalize_json_literals(content)
            if normalized == content:
                raise
            return model.model_validate_json(normalized)
    return model.model_validate(content)


def _previous_content(step_input: StepInput) -> Any:
    """Return the direct predecessor output without coupling to a Step display name."""
    content = step_input.previous_step_content
    if content is None:
        content = step_input.get_last_step_content()
    if content is None:
        raise ValueError("required previous Workflow step output is missing")
    return content


def _evidence_run_state(run_context: RunContext) -> dict[str, Any]:
    """Return state shared by every Step copy in one Evidence Workflow run."""
    if run_context.dependencies is not None:
        return run_context.dependencies

    session_state = run_context.session_state
    if session_state is None:
        session_state = {}
        run_context.session_state = session_state
    state = session_state.get(_EVIDENCE_RUN_STATE)
    if not isinstance(state, dict) or state.get("run_id") != run_context.run_id:
        state = {"run_id": run_context.run_id}
        session_state[_EVIDENCE_RUN_STATE] = state
    return state


async def prepare_evidence(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Select one pending Raw document and build the identity-free Agent request."""
    del step_input
    prepared, checkpoint = read_next_raw_document(read_checkpoint())
    if prepared is None:
        return StepOutput(content=EvidenceExtractionIdle(checkpoint=checkpoint), stop=True)
    run_state = _evidence_run_state(run_context)
    snapshot = run_state.get(_CATEGORY_CATALOG_DEPENDENCY)
    if snapshot is None:
        result = await asyncio.to_thread(get_evidence_categories)
        try:
            catalog = EvidenceCategoryCatalog.model_validate(result)
        except ValidationError as exc:
            raise ValueError("Data Service Evidence Category Catalog is invalid") from exc
        run_state[_CATEGORY_CATALOG_DEPENDENCY] = catalog.model_dump(mode="json")
    else:
        try:
            catalog = EvidenceCategoryCatalog.model_validate(snapshot)
        except ValidationError as exc:
            raise ValueError("run-scoped Evidence Category Catalog is invalid") from exc
    run_state[_PREPARED_RAW_DOCUMENT_DEPENDENCY] = prepared.model_dump(mode="json")
    request = EvidenceAnalysisRequest(
        document=prepared,
        categories=[
            EvidenceCategoryDefinition.model_validate(item.model_dump(exclude={"id"})) for item in catalog.categories
        ],
    )
    return StepOutput(content=request)


def evidence_extraction_complete(iteration_outputs: list[StepOutput]) -> bool:
    """Stop the Agno Loop when the preparation step reports no pending document."""
    for output in reversed(iteration_outputs):
        if output.content is None:
            continue
        try:
            _model_from_content(EvidenceExtractionIdle, output.content)
        except (ValidationError, ValueError, TypeError):
            continue
        return True
    return False


def _source_reference_id(identity: str) -> str:
    return "SRC_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:28]


def _publication_artifact_id(publication_key: str) -> str:
    return hashlib.sha256(publication_key.encode("utf-8")).hexdigest()


def _category_catalog_sha256(catalog: EvidenceCategoryCatalog) -> str:
    payload = json.dumps(
        catalog.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _collapse_space(value: str) -> str:
    return " ".join(value.split())


def _canonical_strings(values: list[str], *, required: bool) -> list[str]:
    unique: dict[str, str] = {}
    for value in values:
        normalized = _collapse_space(value.strip())
        if not normalized:
            raise ValueError("Evidence semantic collections cannot contain blank values")
        unique.setdefault(normalized.casefold(), normalized)
    result = sorted(unique.values(), key=lambda item: (item.casefold(), item))
    if required and not result:
        raise ValueError("Evidence semantic collection cannot be empty")
    return result


def _utc_day_bounds(value: date) -> tuple[datetime, datetime]:
    start = datetime.combine(value, time.min, tzinfo=_BUSINESS_TIMEZONE).astimezone(UTC)
    end = datetime.combine(value, time.max, tzinfo=_BUSINESS_TIMEZONE).astimezone(UTC)
    return start, end


def _normalize_evidence_time(raw: str | None, fallback_precision: str) -> EvidenceTime:
    """Normalize only exact source expressions; unresolved relative text remains unguessed."""
    if raw is None:
        return EvidenceTime(raw=None, start_at=None, end_at=None, precision="UNKNOWN")
    value = _collapse_space(raw.strip())
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None and parsed.tzinfo is not None:
        instant = parsed.astimezone(UTC)
        return EvidenceTime(raw=value, start_at=instant, end_at=instant, precision="INSTANT")

    day_match = re.fullmatch(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?", value)
    if day_match:
        start, end = _utc_day_bounds(date(*(int(part) for part in day_match.groups())))
        return EvidenceTime(raw=value, start_at=start, end_at=end, precision="DAY")

    range_match = re.fullmatch(
        r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?\s*(?:至|到|—|–|~)\s*"
        r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?",
        value,
    )
    if range_match:
        parts = [int(part) for part in range_match.groups()]
        start, _ = _utc_day_bounds(date(*parts[:3]))
        _, end = _utc_day_bounds(date(*parts[3:]))
        if start > end:
            raise ValueError("Evidence source time range is reversed")
        return EvidenceTime(raw=value, start_at=start, end_at=end, precision="RANGE")

    month_match = re.fullmatch(r"(\d{4})(?:-|年)(\d{1,2})月?", value)
    if month_match:
        year, month = (int(part) for part in month_match.groups())
        start, _ = _utc_day_bounds(date(year, month, 1))
        _, end = _utc_day_bounds(date(year, month, calendar.monthrange(year, month)[1]))
        return EvidenceTime(raw=value, start_at=start, end_at=end, precision="MONTH")

    quarter_match = re.fullmatch(r"(\d{4})(?:年)?(?:Q([1-4])|第([一二三四])季度)", value, re.IGNORECASE)
    if quarter_match:
        year = int(quarter_match.group(1))
        quarter = (
            int(quarter_match.group(2)) if quarter_match.group(2) else "一二三四".index(quarter_match.group(3)) + 1
        )
        first_month = (quarter - 1) * 3 + 1
        last_month = first_month + 2
        start, _ = _utc_day_bounds(date(year, first_month, 1))
        _, end = _utc_day_bounds(date(year, last_month, calendar.monthrange(year, last_month)[1]))
        return EvidenceTime(raw=value, start_at=start, end_at=end, precision="QUARTER")

    year_match = re.fullmatch(r"(\d{4})年?", value)
    if year_match:
        year = int(year_match.group(1))
        start, _ = _utc_day_bounds(date(year, 1, 1))
        _, end = _utc_day_bounds(date(year, 12, 31))
        return EvidenceTime(raw=value, start_at=start, end_at=end, precision="YEAR")

    precision = fallback_precision if fallback_precision in {"RANGE", "MONTH", "QUARTER", "YEAR"} else "UNKNOWN"
    return EvidenceTime(raw=value, start_at=None, end_at=None, precision=precision)


def _canonical_metrics(metrics: list[EvidenceMetric]) -> list[EvidenceMetric]:
    normalized: list[EvidenceMetric] = []
    identities: set[tuple[str, str]] = set()
    for metric in metrics:
        item = EvidenceMetric.model_validate(metric.model_dump())
        identity = (item.name.casefold(), (item.period or "").casefold())
        if identity in identities:
            raise ValueError("Evidence metrics must have unique name and period")
        identities.add(identity)
        normalized.append(item)
    return sorted(normalized, key=lambda item: (item.name.casefold(), (item.period or "").casefold()))


def _canonical_keywords(values: list[str]) -> list[str]:
    """Keep the first five valid publication keywords from one tolerant LLM draft."""
    normalized: list[str] = []
    for value in values:
        item = _collapse_space(value.strip())
        if not item or len(item) > 6 or item in normalized:
            continue
        normalized.append(item)
        if len(normalized) == 5:
            break
    if not normalized:
        raise ValueError("Evidence requires at least one valid keyword after curation")
    return normalized


def _canonicalize_evidence_drafts(draft: EvidenceExtractionDraft) -> list[EvidencePublicationItem]:
    items: list[EvidencePublicationItem] = []
    exact_payloads: set[str] = set()
    business_identities: dict[str, str] = {}
    for source in draft.evidences:
        semantic = EvidenceSemantic(
            actors=_canonical_strings(source.semantic.actors, required=True),
            action=_collapse_space(source.semantic.action),
            objects=_canonical_strings(source.semantic.objects, required=True),
            stage=source.semantic.stage,
            modality=source.semantic.modality,
            time=_normalize_evidence_time(source.semantic.time.raw, source.semantic.time.precision),
            jurisdictions=_canonical_strings(source.semantic.jurisdictions, required=False),
            reason=_collapse_space(source.semantic.reason) if source.semantic.reason is not None else None,
            method=_collapse_space(source.semantic.method) if source.semantic.method is not None else None,
            metrics=_canonical_metrics(source.semantic.metrics),
            attribution=source.semantic.attribution,
        )
        item = EvidencePublicationItem(
            summary=_collapse_space(source.summary),
            keywords=_canonical_keywords(source.keywords),
            semantic=semantic,
        )
        payload = json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if payload in exact_payloads:
            continue
        identity = json.dumps(
            {
                "actors": semantic.actors,
                "action": semantic.action,
                "objects": semantic.objects,
                "stage": semantic.stage,
                "modality": semantic.modality,
                "time": semantic.time.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        previous = business_identities.get(identity)
        if previous is not None and previous != payload:
            raise ValueError("Evidence business identity collision contains divergent content")
        business_identities[identity] = payload
        exact_payloads.add(payload)
        items.append(item)
    if not items:
        raise ValueError("Evidence extraction produced no canonical business proposition")
    return items


def _freeze_prepared_publication(
    path: Path,
    candidate: PreparedEvidencePublication,
) -> PreparedEvidencePublication:
    """Persist the first payload and reuse it across unknown-outcome retries."""
    if path.exists():
        try:
            frozen = PreparedEvidencePublication.model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError as exc:
            raise ValueError("pending Evidence publication payload is invalid") from exc
        if (
            frozen.prepared_raw != candidate.prepared_raw
            or frozen.raw_evidence.publication_key != candidate.raw_evidence.publication_key
        ):
            raise ValueError("pending Evidence publication identity conflict")
        return frozen
    write_json(path, candidate.model_dump(mode="json"))
    return candidate


def curate_evidence(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Validate and canonicalize the direct Agent output before any publication side effect."""
    draft = _model_from_content(EvidenceExtractionDraft, _previous_content(step_input))
    run_state = _evidence_run_state(run_context)
    prepared_snapshot = run_state.get(_PREPARED_RAW_DOCUMENT_DEPENDENCY)
    if prepared_snapshot is None:
        raise ValueError("run-scoped prepared Raw document is missing")
    try:
        prepared = PreparedRawDocument.model_validate(prepared_snapshot)
    except ValidationError as exc:
        raise ValueError("run-scoped prepared Raw document is invalid") from exc
    snapshot = run_state.get(_CATEGORY_CATALOG_DEPENDENCY)
    if snapshot is None:
        raise ValueError("run-scoped Evidence Category Catalog is missing")
    try:
        catalog = EvidenceCategoryCatalog.model_validate(snapshot)
    except ValidationError as exc:
        raise ValueError("run-scoped Evidence Category Catalog is invalid") from exc
    categories_by_code = {item.code: item for item in catalog.categories}
    category = categories_by_code.get(draft.raw_evidence.category_code)
    if category is None:
        raise ValueError(f"unknown Evidence Category code: {draft.raw_evidence.category_code}")
    quoted_name = draft.raw_evidence.quoted_source_name if not draft.raw_evidence.is_original else None
    is_original = draft.raw_evidence.is_original or quoted_name is None
    quoted_id = _source_reference_id(quoted_name) if quoted_name else None
    raw = RawEvidencePublication(
        publication_key=prepared.publication_key,
        source_id=prepared.source_id,
        source_name=prepared.source_name,
        source_level=prepared.source_level,
        source_url=prepared.source_url,
        is_original=is_original,
        quoted_source_id=quoted_id,
        quoted_source_name=quoted_name,
        title=prepared.title,
        raw_text=prepared.document_url_path,
        published_at=prepared.published_at,
        collected_at=prepared.collected_at,
        category_ids=[category.id],
    )
    evidences = _canonicalize_evidence_drafts(draft)
    publication = PreparedEvidencePublication(
        prepared_raw=prepared,
        category_catalog_sha256=_category_catalog_sha256(catalog),
        selected_category_code=category.code,
        raw_evidence=raw,
        evidences=evidences,
    )
    return StepOutput(content=publication)


def _read_final_manifest(path: Path, publication: PreparedEvidencePublication) -> EvidencePublicationResult | None:
    if not path.exists():
        return None
    try:
        artifact = load_published_evidence_artifact(path)
    except ValueError as exc:
        raise ValueError("published Evidence Artifact is invalid") from exc
    manifest = artifact.manifest
    frozen = artifact.prepared
    if (
        frozen.prepared_raw != publication.prepared_raw
        or frozen.raw_evidence.publication_key != publication.raw_evidence.publication_key
    ):
        raise ValueError("published Evidence Artifact source identity conflict")
    schema = manifest.get("schema")
    if schema != "evidence_extraction_manifest.v5":
        raise ValueError("published Evidence Artifact identity conflict")
    if (
        manifest.get("artifacts") != {"prepared": "prepared.json", "bindings": "bindings.json"}
        or artifact.bindings is None
    ):
        raise ValueError("published Evidence Artifact identity conflict")
    _enqueue_for_event(path, artifact.identities.ids)
    checkpoint = advance_checkpoint(frozen.prepared_raw)
    return EvidencePublicationResult(
        raw_evidence_id=artifact.identities.raw_evidence_id,
        evidence_ids=artifact.identities.ids,
        evidence_count=len(frozen.evidences),
        artifact_manifest_path=str(path),
        checkpoint=checkpoint,
    )


def _enqueue_for_event(manifest_path: Path, evidence_ids: list[str]) -> None:
    """Import lazily so Evidence and Event capabilities retain a one-way runtime seam."""

    from capabilities.event.functions import enqueue_evidence_artifact

    enqueue_evidence_artifact(str(manifest_path), evidence_ids)


async def publish_evidence(step_input: StepInput) -> StepOutput:
    """Publish Raw Evidence then the complete Evidence set and advance the file checkpoint."""
    publication = _model_from_content(
        PreparedEvidencePublication,
        _previous_content(step_input),
    )
    publication_key = publication.raw_evidence.publication_key
    artifact_id = _publication_artifact_id(publication_key)
    root = evidence_artifact_root()
    final_root = root / "documents" / artifact_id
    final_manifest = final_root / "manifest.json"
    existing = _read_final_manifest(final_manifest, publication)
    if existing is not None:
        return StepOutput(content=existing)

    pending = root / ".pending" / artifact_id
    publication = _freeze_prepared_publication(pending / "prepared.json", publication)
    raw_response_payload = await asyncio.to_thread(
        post_publication,
        "raw-evidence-publications",
        {"raw_evidence": publication.raw_evidence.model_dump(mode="json")},
    )
    try:
        raw_response = RawEvidencePublicationResponse.model_validate(raw_response_payload)
    except ValidationError as exc:
        raise ValueError("Raw Evidence publication response is invalid") from exc
    raw_id = raw_response.id
    evidence_response_payload = await asyncio.to_thread(
        post_publication,
        "evidence-publications",
        {
            "raw_evidence_id": raw_id,
            "evidences": [item.model_dump(mode="json") for item in publication.evidences],
        },
    )
    try:
        evidence_response = EvidenceSetPublicationResponse.model_validate(evidence_response_payload)
    except ValidationError as exc:
        raise ValueError("Evidence publication response is invalid") from exc
    if evidence_response.raw_evidence_id != raw_id:
        raise ValueError("Raw Evidence identity mismatch between publication responses")
    if len(evidence_response.ids) != len(publication.evidences):
        raise ValueError("Evidence identity count mismatch in publication response")
    if len(evidence_response.items) != len(publication.evidences):
        raise ValueError("Evidence identity mapping count mismatch in publication response")

    for name in ("prepared.json",):
        source = pending / name
        target = final_root / name
        if target.exists():
            if target.read_bytes() != source.read_bytes():
                raise ValueError(f"immutable Evidence Artifact conflict: {name}")
        else:
            write_json(target, json.loads(source.read_text(encoding="utf-8")))
    bindings = EvidenceIdentityBindings(
        publication_key=publication_key,
        raw_evidence_id=raw_id,
        document_sha256=publication.prepared_raw.document_sha256,
        evidence_count=len(publication.evidences),
        items=evidence_response.items,
    )
    persist_evidence_identity_bindings(final_root, bindings)
    artifacts = {"prepared": "prepared.json", "bindings": "bindings.json"}
    manifest = {
        "schema": "evidence_extraction_manifest.v5",
        "publication_key": publication_key,
        "raw_evidence_id": raw_id,
        "collection_id": publication.prepared_raw.collection_id,
        "document_path": publication.prepared_raw.document_path,
        "document_sha256": publication.prepared_raw.document_sha256,
        "evidence_count": len(publication.evidences),
        "evidence_ids": evidence_response.ids,
        "artifacts": artifacts,
    }
    write_json(final_manifest, manifest)
    _enqueue_for_event(final_manifest, evidence_response.ids)
    shutil.rmtree(pending, ignore_errors=True)
    checkpoint = advance_checkpoint(publication.prepared_raw)
    result = EvidencePublicationResult(
        raw_evidence_id=raw_id,
        evidence_ids=evidence_response.ids,
        evidence_count=len(publication.evidences),
        artifact_manifest_path=str(final_manifest),
        checkpoint=checkpoint,
    )
    return StepOutput(content=result)
