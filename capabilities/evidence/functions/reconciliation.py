"""Explicit reconciliation of historical Evidence identity bindings."""

from pathlib import Path

from pydantic import ValidationError

from capabilities.evidence.functions.artifacts import read_resolved_evidences
from capabilities.evidence.internal.artifacts import (
    load_published_evidence_artifact,
    persist_evidence_identity_bindings,
)
from capabilities.evidence.internal.client import post_publication
from capabilities.evidence.internal.models import (
    EvidenceBindingReconciliationIssue,
    EvidenceBindingReconciliationResult,
    EvidenceIdentityBindings,
    EvidencePublicationResponseItem,
    EvidenceSetPublicationResponse,
    PreparedEvidencePublication,
)
from capabilities.evidence.internal.storage import evidence_artifact_root


def _ineligible(path: Path, reason: str) -> EvidenceBindingReconciliationIssue:
    return EvidenceBindingReconciliationIssue(artifact_manifest_path=str(path), reason=reason)


def _legacy_artifact(path: Path) -> tuple[PreparedEvidencePublication, EvidenceSetPublicationResponse]:
    try:
        artifact = load_published_evidence_artifact(path)
    except ValueError as exc:
        raise ValueError("historical Evidence Artifact is invalid") from exc
    if artifact.manifest.get("schema") != "evidence_extraction_manifest.v4" or artifact.bindings is not None:
        raise ValueError("historical Evidence Artifact identity conflict")
    return artifact.prepared, artifact.identities


def _remote_bindings(
    prepared: PreparedEvidencePublication,
    identities: EvidenceSetPublicationResponse,
) -> EvidenceIdentityBindings:
    payload = post_publication(
        "evidence-publications",
        {
            "raw_evidence_id": identities.raw_evidence_id,
            "evidences": [item.model_dump(mode="json") for item in prepared.evidences],
        },
    )
    try:
        response = EvidenceSetPublicationResponse.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("Data Service returned an invalid Evidence identity mapping") from exc
    if (
        response.items is None
        or response.raw_evidence_id != identities.raw_evidence_id
        or set(response.ids) != set(identities.ids)
        or len(response.items) != len(prepared.evidences)
    ):
        raise ValueError("Data Service Evidence identity mapping conflicts with the historical Artifact")
    return EvidenceIdentityBindings(
        publication_key=prepared.raw_evidence.publication_key,
        raw_evidence_id=identities.raw_evidence_id,
        document_sha256=prepared.prepared_raw.document_sha256,
        evidence_count=len(prepared.evidences),
        items=response.items,
    )


def reconcile_evidence_bindings() -> EvidenceBindingReconciliationResult:
    """Append authoritative bindings to eligible historical v4 Artifacts."""
    result = EvidenceBindingReconciliationResult()
    manifests = sorted((evidence_artifact_root() / "documents").glob("*/manifest.json"))
    for path in manifests:
        if (path.parent / "bindings.json").exists():
            try:
                read_resolved_evidences(path)
            except ValueError as exc:
                result.ineligible.append(_ineligible(path, str(exc)))
            else:
                result.already_bound += 1
            continue
        try:
            prepared, identities = _legacy_artifact(path)
            if len(prepared.evidences) == 1:
                bindings = EvidenceIdentityBindings(
                    publication_key=prepared.raw_evidence.publication_key,
                    raw_evidence_id=identities.raw_evidence_id,
                    document_sha256=prepared.prepared_raw.document_sha256,
                    evidence_count=1,
                    items=[EvidencePublicationResponseItem(input_index=0, id=identities.ids[0])],
                )
                persist_evidence_identity_bindings(path.parent, bindings)
                result.locally_bound += 1
            else:
                persist_evidence_identity_bindings(path.parent, _remote_bindings(prepared, identities))
                result.remotely_bound += 1
        except ValueError as exc:
            result.ineligible.append(_ineligible(path, str(exc)))
    return result
