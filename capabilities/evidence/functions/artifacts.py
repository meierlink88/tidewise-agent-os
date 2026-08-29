"""Stable readers for published local Evidence Artifacts."""

from pathlib import Path

from capabilities.evidence.internal.artifacts import load_published_evidence_artifact
from capabilities.evidence.internal.models import (
    ResolvedEvidence,
)


def read_resolved_evidences(manifest_path: str | Path) -> list[ResolvedEvidence]:
    """Resolve one published Evidence Artifact without exposing its file-join contract."""
    artifact = load_published_evidence_artifact(manifest_path)
    if artifact.bindings is None:
        if len(artifact.identities.ids) != 1 or len(artifact.prepared.evidences) != 1:
            raise ValueError("published Evidence Artifact has no formal identity bindings")
        return [
            ResolvedEvidence(
                id=artifact.identities.ids[0],
                raw_evidence_id=artifact.identities.raw_evidence_id,
                summary=artifact.prepared.evidences[0].summary,
                semantic=artifact.prepared.evidences[0].semantic,
            )
        ]
    return [
        ResolvedEvidence(
            id=binding.id,
            raw_evidence_id=artifact.bindings.raw_evidence_id,
            summary=artifact.prepared.evidences[binding.input_index].summary,
            semantic=artifact.prepared.evidences[binding.input_index].semantic,
        )
        for binding in artifact.bindings.items
    ]
