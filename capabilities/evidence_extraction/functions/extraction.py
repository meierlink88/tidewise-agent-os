"""Workflow Function executors for the Evidence extraction capability."""

import asyncio
import hashlib
import json
import shutil
import unicodedata
from pathlib import Path
from typing import Any

from agno.workflow import StepInput, StepOutput

from capabilities.evidence_extraction.client import post_publication
from capabilities.evidence_extraction.models import (
    EvidenceExtractionDraft,
    EvidenceExtractionIdle,
    EvidencePublicationItem,
    EvidencePublicationResult,
    PreparedEvidencePublication,
    PreparedRawDocument,
    RawEvidencePublication,
)
from capabilities.evidence_extraction.storage import (
    advance_checkpoint,
    evidence_artifact_root,
    read_checkpoint,
    read_next_raw_document,
    write_json,
)

_FINGERPRINT_VERSION = "evidence-expression.v1"


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


def _fingerprint_key(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).lower()
    normalized = "".join(
        character for character in normalized if not unicodedata.category(character).startswith(("P", "Z"))
    )
    if not normalized:
        raise ValueError("Evidence expression fingerprint has no normalized content")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, identity: str) -> str:
    return prefix + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:28]


def validate_evidence_draft(step_input: StepInput) -> StepOutput:
    """Validate Agent semantics and add all deterministic publication identities."""
    prepared = _model_from_content(PreparedRawDocument, _step_content(step_input, "prepare-raw-document"))
    draft = _model_from_content(EvidenceExtractionDraft, _step_content(step_input, "extract-evidences"))
    quoted_name = draft.raw_evidence.quoted_source_name
    quoted_id = _stable_id("SRC_", quoted_name) if quoted_name else None
    raw = RawEvidencePublication(
        raw_evidence_id=prepared.raw_evidence_id,
        source_id=prepared.source_id,
        source_name=prepared.source_name,
        source_level=prepared.source_level,
        source_url=prepared.source_url,
        is_original=draft.raw_evidence.is_original,
        quoted_source_id=quoted_id,
        quoted_source_name=quoted_name,
        title=prepared.title,
        raw_text=prepared.raw_text,
        published_at=prepared.published_at,
        collected_at=prepared.collected_at,
        keywords=draft.raw_evidence.keywords,
    )
    evidences: list[EvidencePublicationItem] = []
    for split_order, item in enumerate(draft.evidences):
        values = item.model_dump()
        fingerprint = item.expression_fingerprint.strip()
        values.update(
            {
                "evidence_id": _stable_id("EVD_", f"{prepared.raw_evidence_id}:{split_order}"),
                "split_order": split_order,
                "expression_fingerprint": fingerprint,
                "expression_key": _fingerprint_key(fingerprint),
                "fingerprint_version": _FINGERPRINT_VERSION,
            }
        )
        evidences.append(EvidencePublicationItem.model_validate(values))
    publication = PreparedEvidencePublication(prepared_raw=prepared, raw_evidence=raw, evidences=evidences)
    return StepOutput(content=publication)


def _read_final_manifest(path: Path, publication: PreparedEvidencePublication) -> EvidencePublicationResult | None:
    if not path.exists():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (
        manifest.get("raw_evidence_id") != publication.raw_evidence.raw_evidence_id
        or manifest.get("document_sha256") != publication.prepared_raw.document_sha256
        or manifest.get("evidence_count") != len(publication.evidences)
    ):
        raise ValueError("published Evidence Artifact identity conflict")
    checkpoint = advance_checkpoint(publication.prepared_raw)
    return EvidencePublicationResult(
        raw_evidence_id=publication.raw_evidence.raw_evidence_id,
        evidence_count=len(publication.evidences),
        artifact_manifest_path=str(path),
        checkpoint=checkpoint,
    )


async def publish_evidences(step_input: StepInput) -> StepOutput:
    """Publish Raw Evidence then the complete Evidence set and advance the file checkpoint."""
    publication = _model_from_content(
        PreparedEvidencePublication,
        _step_content(step_input, "validate-evidence-draft"),
    )
    raw_id = publication.raw_evidence.raw_evidence_id
    root = evidence_artifact_root()
    final_root = root / "documents" / raw_id
    final_manifest = final_root / "manifest.json"
    existing = _read_final_manifest(final_manifest, publication)
    if existing is not None:
        return StepOutput(content=existing)

    pending = root / ".pending" / raw_id
    write_json(pending / "prepared.json", publication.model_dump(mode="json"))
    await asyncio.to_thread(
        post_publication,
        "raw-evidence-publications",
        {"raw_evidence": publication.raw_evidence.model_dump(mode="json")},
    )
    await asyncio.to_thread(
        post_publication,
        "evidence-publications",
        {
            "raw_evidence_id": raw_id,
            "evidences": [item.model_dump(mode="json") for item in publication.evidences],
        },
    )

    for name in ("prepared.json",):
        source = pending / name
        target = final_root / name
        if target.exists():
            if target.read_bytes() != source.read_bytes():
                raise ValueError(f"immutable Evidence Artifact conflict: {name}")
        else:
            write_json(target, json.loads(source.read_text(encoding="utf-8")))
    manifest = {
        "schema": "evidence_extraction_manifest.v1",
        "raw_evidence_id": raw_id,
        "collection_id": publication.prepared_raw.collection_id,
        "document_path": publication.prepared_raw.document_path,
        "document_sha256": publication.prepared_raw.document_sha256,
        "evidence_count": len(publication.evidences),
        "artifacts": {"prepared": "prepared.json"},
    }
    write_json(final_manifest, manifest)
    shutil.rmtree(pending, ignore_errors=True)
    checkpoint = advance_checkpoint(publication.prepared_raw)
    result = EvidencePublicationResult(
        raw_evidence_id=raw_id,
        evidence_count=len(publication.evidences),
        artifact_manifest_path=str(final_manifest),
        checkpoint=checkpoint,
    )
    return StepOutput(content=result)
