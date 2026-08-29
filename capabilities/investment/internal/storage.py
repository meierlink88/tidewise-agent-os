"""Crash-safe local storage for finalized Investment Conclusion Artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from capabilities.investment.internal.models import InvestmentConclusionArtifact


def investment_artifact_root() -> Path:
    """Return the configured persistent investment Artifact root."""

    return Path(os.getenv("INVESTMENT_ARTIFACT_ROOT", "data/investment")).resolve()


def conclusion_artifact_path(workflow_run_id: str) -> Path:
    """Resolve the immutable conclusion path for one Workflow run identity."""

    if not workflow_run_id or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in workflow_run_id
    ):
        raise ValueError("workflow_run_id is not safe for an Artifact path")
    path = (investment_artifact_root() / "conclusions" / f"{workflow_run_id}.json").resolve()
    root = investment_artifact_root()
    if root != path and root not in path.parents:
        raise ValueError("Investment Conclusion Artifact path escapes its root")
    return path


def _atomic_create_json(path: Path, value: object) -> bool:
    """Create without replacement; return False when another writer won."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.link(temporary, path)
    except FileExistsError:
        return False
    finally:
        temporary.unlink(missing_ok=True)
    return True


def write_conclusion_artifact(artifact: InvestmentConclusionArtifact) -> Path:
    """Atomically create one immutable result, accepting an identical retry."""

    path = conclusion_artifact_path(artifact.workflow_run_id)
    if Path(artifact.artifact_path).resolve() != path:
        raise ValueError("Investment Conclusion Artifact path does not match its Workflow run identity")
    if _atomic_create_json(path, artifact.model_dump(mode="json")):
        return path
    existing = InvestmentConclusionArtifact.model_validate_json(path.read_text(encoding="utf-8"))
    if existing != artifact:
        raise ValueError("Investment Conclusion Artifact identity conflict")
    return path


def read_conclusion_artifact(workflow_run_id: str) -> InvestmentConclusionArtifact:
    """Read and validate a previously finalized product result."""

    return InvestmentConclusionArtifact.model_validate_json(
        conclusion_artifact_path(workflow_run_id).read_text(encoding="utf-8")
    )
