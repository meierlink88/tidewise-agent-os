"""Workflow Function executors for the Evidence extraction capability."""

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput
from pydantic import ValidationError

from capabilities.evidence.internal.client import get_evidence_categories, post_publication
from capabilities.evidence.internal.models import (
    EvidenceAnalysisRequest,
    EvidenceCategoryCatalog,
    EvidenceCategoryDefinition,
    EvidenceExtractionDraft,
    EvidenceExtractionIdle,
    EvidencePublicationItem,
    EvidencePublicationResult,
    EvidenceSetPublicationResponse,
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


def _model_from_content(model: type[Any], content: Any) -> Any:
    if isinstance(content, model):
        return content
    if isinstance(content, str):
        return model.model_validate_json(content)
    return model.model_validate(content)


def _step_content(step_input: StepInput, name: str) -> Any:
    output = step_input.get_step_output(name)
    if output is None or output.content is None:
        raise ValueError(f"required Workflow step output is missing: {name}")
    return output.content


def prepare_raw_document(step_input: StepInput) -> StepOutput:
    """Select and verify one unprocessed Raw document from the incremental index."""
    del step_input
    prepared, checkpoint = read_next_raw_document(read_checkpoint())
    if prepared is None:
        return StepOutput(content=EvidenceExtractionIdle(checkpoint=checkpoint), stop=True)
    return StepOutput(content=prepared)


async def prepare_evidence_analysis(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Freeze one formal Category Catalog per run and expose identity-free semantics to the Agent."""
    prepared = _model_from_content(PreparedRawDocument, _step_content(step_input, "prepare-raw-document"))
    dependencies = dict(run_context.dependencies or {})
    snapshot = dependencies.get(_CATEGORY_CATALOG_DEPENDENCY)
    if snapshot is None:
        result = await asyncio.to_thread(get_evidence_categories)
        try:
            catalog = EvidenceCategoryCatalog.model_validate(result)
        except ValidationError as exc:
            raise ValueError("Data Service Evidence Category Catalog is invalid") from exc
        dependencies[_CATEGORY_CATALOG_DEPENDENCY] = catalog.model_copy(deep=True)
        run_context.dependencies = dependencies
    else:
        try:
            catalog = EvidenceCategoryCatalog.model_validate(snapshot)
        except ValidationError as exc:
            raise ValueError("run-scoped Evidence Category Catalog is invalid") from exc
    request = EvidenceAnalysisRequest(
        document=prepared,
        categories=[
            EvidenceCategoryDefinition.model_validate(item.model_dump(exclude={"id"})) for item in catalog.categories
        ],
    )
    return StepOutput(content=request)


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


def validate_evidence_analysis(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Validate Agent semantics, resolve one formal Category ID and add publication metadata."""
    prepared = _model_from_content(PreparedRawDocument, _step_content(step_input, "prepare-raw-document"))
    draft = _model_from_content(EvidenceExtractionDraft, _step_content(step_input, "analyze-raw-evidence"))
    dependencies = run_context.dependencies or {}
    snapshot = dependencies.get(_CATEGORY_CATALOG_DEPENDENCY)
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
    quoted_name = draft.raw_evidence.quoted_source_name
    quoted_id = _source_reference_id(quoted_name) if quoted_name else None
    raw = RawEvidencePublication(
        publication_key=prepared.publication_key,
        source_id=prepared.source_id,
        source_name=prepared.source_name,
        source_level=prepared.source_level,
        source_url=prepared.source_url,
        is_original=draft.raw_evidence.is_original,
        quoted_source_id=quoted_id,
        quoted_source_name=quoted_name,
        title=prepared.title,
        raw_text=prepared.document_url_path,
        published_at=prepared.published_at,
        collected_at=prepared.collected_at,
        keywords=draft.raw_evidence.keywords,
        category_ids=[category.id],
    )
    evidences = [EvidencePublicationItem.model_validate(item.model_dump()) for item in draft.evidences]
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
    manifest = json.loads(path.read_text(encoding="utf-8"))
    prepared_path = path.parent / "prepared.json"
    try:
        frozen = PreparedEvidencePublication.model_validate_json(prepared_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError("published Evidence Artifact prepared payload is invalid") from exc
    if (
        frozen.prepared_raw != publication.prepared_raw
        or frozen.raw_evidence.publication_key != publication.raw_evidence.publication_key
    ):
        raise ValueError("published Evidence Artifact source identity conflict")
    evidence_ids = manifest.get("evidence_ids")
    if (
        manifest.get("schema") != "evidence_extraction_manifest.v4"
        or manifest.get("publication_key") != frozen.raw_evidence.publication_key
        or manifest.get("document_sha256") != frozen.prepared_raw.document_sha256
        or manifest.get("evidence_count") != len(frozen.evidences)
        or not isinstance(evidence_ids, list)
        or len(evidence_ids) != len(frozen.evidences)
    ):
        raise ValueError("published Evidence Artifact identity conflict")
    try:
        response = EvidenceSetPublicationResponse(
            raw_evidence_id=manifest.get("raw_evidence_id"),
            ids=evidence_ids,
        )
    except ValidationError as exc:
        raise ValueError("published Evidence Artifact contains invalid formal identities") from exc
    if len(set(response.ids)) != len(response.ids):
        raise ValueError("published Evidence Artifact contains invalid formal identities")
    checkpoint = advance_checkpoint(frozen.prepared_raw)
    return EvidencePublicationResult(
        raw_evidence_id=response.raw_evidence_id,
        evidence_ids=response.ids,
        evidence_count=len(frozen.evidences),
        artifact_manifest_path=str(path),
        checkpoint=checkpoint,
    )


async def publish_evidences(step_input: StepInput) -> StepOutput:
    """Publish Raw Evidence then the complete Evidence set and advance the file checkpoint."""
    publication = _model_from_content(
        PreparedEvidencePublication,
        _step_content(step_input, "validate-evidence-analysis"),
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
    if len(set(evidence_response.ids)) != len(evidence_response.ids):
        raise ValueError("Evidence publication response contains duplicate identities")

    for name in ("prepared.json",):
        source = pending / name
        target = final_root / name
        if target.exists():
            if target.read_bytes() != source.read_bytes():
                raise ValueError(f"immutable Evidence Artifact conflict: {name}")
        else:
            write_json(target, json.loads(source.read_text(encoding="utf-8")))
    manifest = {
        "schema": "evidence_extraction_manifest.v4",
        "publication_key": publication_key,
        "raw_evidence_id": raw_id,
        "collection_id": publication.prepared_raw.collection_id,
        "document_path": publication.prepared_raw.document_path,
        "document_sha256": publication.prepared_raw.document_sha256,
        "evidence_count": len(publication.evidences),
        "evidence_ids": evidence_response.ids,
        "artifacts": {"prepared": "prepared.json"},
    }
    write_json(final_manifest, manifest)
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
