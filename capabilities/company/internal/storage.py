"""Atomic, immutable checkpoints for Company inference decisions."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import IO

from capabilities.company.internal.models import (
    CompanyInferenceDecision,
    DecisionStatus,
    ProjectionRunManifest,
    _canonical_hash,
)


@dataclass(frozen=True)
class _RunContext:
    manifest: ProjectionRunManifest
    manifest_fingerprint: str
    company_ids: frozenset[str]
    company_indexes: dict[str, int]


def _atomic_create_json(path: Path, payload: dict[str, object]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(rendered)
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


class DecisionJournal:
    """One run directory with a frozen manifest and one immutable file per Company."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._context: _RunContext | None = None

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def decision_path(self, company_id: str) -> Path:
        if not company_id.startswith("COM") or any(character not in "0123456789abcdef-COM" for character in company_id):
            raise ValueError("Company ID is unsafe for a checkpoint path")
        path = (self.root / "decisions" / f"{company_id}.json").resolve()
        if self.root not in path.parents:
            raise ValueError("decision path escapes its journal root")
        return path

    def open_or_create(self, manifest: ProjectionRunManifest) -> Path:
        payload = manifest.model_dump(mode="json")
        if _atomic_create_json(self.manifest_path, payload):
            self._remember_manifest(manifest)
            return self.manifest_path
        existing = self._run_context().manifest
        if existing != manifest:
            raise ValueError("projection run manifest mismatch")
        return self.manifest_path

    def manifest(self) -> ProjectionRunManifest:
        return self._run_context().manifest.model_copy(deep=True)

    def _remember_manifest(self, manifest: ProjectionRunManifest) -> _RunContext:
        cached = ProjectionRunManifest.model_validate(manifest.model_dump(mode="json"))
        self._context = _RunContext(
            manifest=cached,
            manifest_fingerprint=cached.fingerprint(),
            company_ids=frozenset(cached.company_ids),
            company_indexes={company_id: index for index, company_id in enumerate(cached.company_ids)},
        )
        return self._context

    def _run_context(self) -> _RunContext:
        if self._context is None:
            manifest = ProjectionRunManifest.model_validate_json(self.manifest_path.read_text(encoding="utf-8"))
            return self._remember_manifest(manifest)
        return self._context

    def _validate_decision(
        self,
        decision: CompanyInferenceDecision,
        *,
        expected_company_id: str | None = None,
    ) -> None:
        context = self._run_context()
        manifest = context.manifest
        if decision.company_id not in context.company_ids:
            raise ValueError("decision Company is outside the run manifest")
        if expected_company_id is not None and decision.company_id != expected_company_id:
            raise ValueError("frozen Company decision does not match its checkpoint path")
        if decision.input_index != context.company_indexes[decision.company_id]:
            raise ValueError("decision input_index differs from the run manifest order")
        statuses = {decision.industry.status, decision.chain_node.status}
        if DecisionStatus.MAPPED in statuses:
            terminal_status = DecisionStatus.MAPPED
        elif DecisionStatus.LOW_CONFIDENCE in statuses:
            terminal_status = DecisionStatus.LOW_CONFIDENCE
        elif DecisionStatus.NO_MATCH in statuses:
            terminal_status = DecisionStatus.NO_MATCH
        else:
            terminal_status = DecisionStatus.NO_CANDIDATE
        if decision.status != terminal_status:
            raise ValueError("decision status disagrees with its stage decision content")
        identity = (
            decision.snapshot_id,
            decision.target_catalog_fingerprint,
            decision.ontology_version,
            decision.policy_version,
            decision.model_id,
            decision.prompt_contract_version,
        )
        expected = (
            manifest.snapshot_id,
            manifest.target_catalog_fingerprint,
            manifest.ontology_version,
            manifest.policy_version,
            manifest.model_id,
            manifest.prompt_contract_version,
        )
        if identity != expected:
            raise ValueError("decision provenance does not match the run manifest")
        identity_payload = {
            "company_id": decision.company_id,
            "source_company_fingerprint": decision.source_company_fingerprint,
            "manifest_fingerprint": context.manifest_fingerprint,
            "industry": decision.industry.model_dump(mode="json"),
            "chain_node": decision.chain_node.model_dump(mode="json"),
            "candidates": decision.candidates.model_dump(mode="json"),
        }
        if decision.decision_id != _canonical_hash(identity_payload):
            raise ValueError("decision_id does not match the frozen Company decision content")

    def freeze(self, decision: CompanyInferenceDecision) -> tuple[Path, bool]:
        self._validate_decision(decision)
        path = self.decision_path(decision.company_id)
        created = _atomic_create_json(path, decision.model_dump(mode="json"))
        if created:
            return path, True
        existing = self.load(decision.company_id)
        if existing != decision:
            raise ValueError("frozen Company decision identity conflict")
        return path, False

    def load(self, company_id: str) -> CompanyInferenceDecision:
        decision = CompanyInferenceDecision.model_validate_json(
            self.decision_path(company_id).read_text(encoding="utf-8")
        )
        self._validate_decision(decision, expected_company_id=company_id)
        return decision

    def assert_complete(self) -> list[CompanyInferenceDecision]:
        manifest = self._run_context().manifest
        decisions = [self.load(company_id) for company_id in manifest.company_ids]
        if [item.company_id for item in decisions] != manifest.company_ids:
            raise ValueError("Company decision journal is incomplete or duplicated")
        return decisions

    def completed(self) -> list[CompanyInferenceDecision]:
        """Return frozen decisions in manifest order without implying completeness."""

        manifest = self._run_context().manifest
        return [
            self.load(company_id) for company_id in manifest.company_ids if self.decision_path(company_id).is_file()
        ]

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Prevent concurrent infer/write/sweep operations for one run directory."""

        self.root.mkdir(parents=True, exist_ok=True)
        handle: IO[str]
        with (self.root / ".lock").open("a", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Company projection run is already active") from None
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = ["DecisionJournal"]
