"""Shared validation and persistence for published Evidence Artifacts."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from capabilities.evidence.internal.models import (
    EvidenceIdentityBindings,
    EvidenceSetPublicationResponse,
    PreparedEvidencePublication,
)
from capabilities.evidence.internal.storage import evidence_artifact_root, write_json


@dataclass(frozen=True)
class PublishedEvidenceArtifact:
    """One validated publication Artifact with an optional formal identity mapping."""

    manifest_path: Path
    manifest: dict[str, Any]
    prepared: PreparedEvidencePublication
    identities: EvidenceSetPublicationResponse
    bindings: EvidenceIdentityBindings | None


def persist_evidence_identity_bindings(directory: Path, bindings: EvidenceIdentityBindings) -> Path:
    """Append one immutable identity-binding Artifact, accepting an exact replay."""
    path = directory / "bindings.json"
    if path.exists():
        try:
            existing = EvidenceIdentityBindings.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            raise ValueError("published Evidence Artifact bindings are invalid") from exc
        if existing != bindings:
            raise ValueError("published Evidence Artifact bindings are immutable")
        return path
    write_json(path, bindings.model_dump(mode="json"))
    return path


def load_published_evidence_artifact(manifest_path: str | Path) -> PublishedEvidenceArtifact:
    """Load and validate the shared identity invariants of one publication Artifact."""
    root = evidence_artifact_root()
    path = Path(manifest_path).resolve()
    if root not in path.parents:
        raise ValueError("Evidence manifest path escapes its Artifact root")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("published Evidence manifest must be an object")
        prepared = PreparedEvidencePublication.model_validate_json(
            (path.parent / "prepared.json").read_text(encoding="utf-8")
        )
        identities = EvidenceSetPublicationResponse(
            raw_evidence_id=manifest.get("raw_evidence_id"),
            ids=manifest.get("evidence_ids"),
        )
        bindings_path = path.parent / "bindings.json"
        bindings = (
            EvidenceIdentityBindings.model_validate_json(bindings_path.read_text(encoding="utf-8"))
            if bindings_path.exists()
            else None
        )
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise ValueError("published Evidence Artifact is invalid") from exc
    if (
        manifest.get("publication_key") != prepared.raw_evidence.publication_key
        or manifest.get("document_sha256") != prepared.prepared_raw.document_sha256
        or manifest.get("evidence_count") != len(prepared.evidences)
        or len(identities.ids) != len(prepared.evidences)
    ):
        raise ValueError("published Evidence Artifact identity conflict")
    if bindings is not None and (
        bindings.publication_key != prepared.raw_evidence.publication_key
        or bindings.raw_evidence_id != identities.raw_evidence_id
        or bindings.document_sha256 != prepared.prepared_raw.document_sha256
        or bindings.evidence_count != len(prepared.evidences)
        or {item.id for item in bindings.items} != set(identities.ids)
    ):
        raise ValueError("published Evidence Artifact identity binding conflict")
    return PublishedEvidenceArtifact(
        manifest_path=path,
        manifest=manifest,
        prepared=prepared,
        identities=identities,
        bindings=bindings,
    )
