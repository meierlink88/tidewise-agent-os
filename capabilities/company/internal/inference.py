"""Resumable orchestration for bounded Company target selection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol

from capabilities.company.internal.engine import (
    build_chain_node_candidates,
    finalize_company_decision,
    industry_candidates_for_roots,
    industry_root_candidates,
    no_candidate_decision,
    validate_decision_candidate_scope,
    validate_model_response,
)
from capabilities.company.internal.models import (
    CandidateChoice,
    CompanyInferenceDecision,
    CompanySubject,
    DecisionStatus,
    ModelSelectionResponse,
    ProjectionRunManifest,
    StageDecision,
    TargetCatalog,
    _canonical_hash,
)
from capabilities.company.internal.storage import DecisionJournal

CHAIN_PROMPT_CANDIDATE_BUDGET = 300


class CompanyTargetSelector(Protocol):
    """Model boundary: return supplied candidate keys, never graph identifiers."""

    async def select_industry_roots(
        self,
        subjects: Sequence[CompanySubject],
        candidates: Sequence[CandidateChoice],
    ) -> ModelSelectionResponse: ...

    async def select_industries(
        self,
        subjects: Sequence[CompanySubject],
        candidates_by_input: dict[int, Sequence[CandidateChoice]],
    ) -> ModelSelectionResponse: ...

    async def select_chain_nodes(
        self,
        subjects: Sequence[CompanySubject],
        candidates_by_input: dict[int, Sequence[CandidateChoice]],
    ) -> ModelSelectionResponse: ...


def partition_candidate_subjects(
    subjects: Sequence[CompanySubject],
    candidates_by_input: Mapping[int, Sequence[CandidateChoice]],
    *,
    candidate_budget: int = CHAIN_PROMPT_CANDIDATE_BUDGET,
) -> list[list[CompanySubject]]:
    """Keep per-call candidate payloads bounded without separating a Company from its options."""

    if candidate_budget < 1:
        raise ValueError("candidate_budget must be positive")
    batches: list[list[CompanySubject]] = []
    current: list[CompanySubject] = []
    current_count = 0
    for subject in subjects:
        candidate_count = len(candidates_by_input[subject.input_index])
        if candidate_count < 1:
            raise ValueError("partitioned Company has no candidates")
        if current and current_count + candidate_count > candidate_budget:
            batches.append(current)
            current = []
            current_count = 0
        current.append(subject)
        current_count += candidate_count
    if current:
        batches.append(current)
    return batches


def company_snapshot_fingerprint(subjects: Sequence[CompanySubject]) -> str:
    """Fingerprint the ordered model-visible Company snapshot."""

    return _canonical_hash(
        [
            {
                "company_id": subject.company_id,
                "fingerprint": subject.fingerprint(),
            }
            for subject in subjects
        ]
    )


async def infer_companies(
    subjects: Sequence[CompanySubject],
    catalog: TargetCatalog,
    manifest: ProjectionRunManifest,
    journal: DecisionJournal,
    selector: CompanyTargetSelector,
    *,
    decided_at: datetime | None = None,
    industry_batch_size: int = 20,
    max_chain_candidates: int = 80,
    max_new_decisions: int | None = None,
) -> list[CompanyInferenceDecision]:
    """Infer and freeze one terminal decision per Company, reusing completed checkpoints."""

    if not subjects:
        raise ValueError("Company inference requires a non-empty frozen snapshot")
    if industry_batch_size < 1 or industry_batch_size > 50:
        raise ValueError("industry_batch_size must be between 1 and 50")
    if max_chain_candidates < 1 or max_chain_candidates > 200:
        raise ValueError("max_chain_candidates must be between 1 and 200")
    if max_chain_candidates != manifest.max_chain_candidates:
        raise ValueError("max_chain_candidates differs from the frozen projection manifest")
    if max_new_decisions is not None and max_new_decisions < 1:
        raise ValueError("max_new_decisions must be positive")
    company_ids = [subject.company_id for subject in subjects]
    input_indexes = [subject.input_index for subject in subjects]
    if len(company_ids) != len(set(company_ids)):
        raise ValueError("Company snapshot contains duplicate IDs")
    if len(input_indexes) != len(set(input_indexes)):
        raise ValueError("Company snapshot contains duplicate input indexes")
    if manifest.company_ids != company_ids:
        raise ValueError("Company IDs differ from the frozen projection manifest")
    if manifest.company_snapshot_fingerprint != company_snapshot_fingerprint(subjects):
        raise ValueError("Company snapshot fingerprint differs from the projection manifest")
    catalog_fingerprint = catalog.fingerprint()
    if manifest.target_catalog_fingerprint != catalog_fingerprint:
        raise ValueError("target catalog fingerprint differs from the projection manifest")
    manifest_fingerprint = manifest.fingerprint()
    journal.open_or_create(manifest)
    decision_time = decided_at or datetime.now(UTC)

    pending: list[CompanySubject] = []
    for subject in subjects:
        path = journal.decision_path(subject.company_id)
        if not path.is_file():
            pending.append(subject)
            continue
        frozen = journal.load(subject.company_id)
        if (
            frozen.company_id != subject.company_id
            or frozen.input_index != subject.input_index
            or frozen.source_company_fingerprint != subject.fingerprint()
        ):
            raise ValueError(f"frozen decision Company fingerprint mismatch: {subject.company_id}")
        validate_decision_candidate_scope(
            frozen,
            catalog,
            manifest,
            catalog_fingerprint=catalog_fingerprint,
        )
    if max_new_decisions is not None:
        pending = pending[:max_new_decisions]

    root_candidates = industry_root_candidates(catalog)
    if not root_candidates:
        raise ValueError("target catalog has no canonical root Industry candidates")
    for start in range(0, len(pending), industry_batch_size):
        batch = pending[start : start + industry_batch_size]
        root_response = await selector.select_industry_roots(batch, root_candidates)
        root_results = validate_model_response(
            batch,
            {subject.input_index: root_candidates for subject in batch},
            root_response,
            max_selections=3,
        )

        industry_results: dict[int, StageDecision] = {}
        industry_subjects: list[CompanySubject] = []
        industry_candidates_by_input: dict[int, Sequence[CandidateChoice]] = {}
        selected_roots_by_input: dict[int, list[str]] = {}
        for subject in batch:
            root_result = root_results[subject.input_index]
            if root_result.status != DecisionStatus.MAPPED:
                industry_results[subject.input_index] = root_result
                selected_roots_by_input[subject.input_index] = []
                continue
            selected_roots_by_input[subject.input_index] = [item.target_id for item in root_result.accepted_targets]
            candidates = industry_candidates_for_roots(
                catalog,
                selected_roots_by_input[subject.input_index],
            )
            if not candidates:
                industry_results[subject.input_index] = no_candidate_decision(
                    "No canonical Industry is reachable from the selected root scope"
                )
                continue
            industry_subjects.append(subject)
            industry_candidates_by_input[subject.input_index] = candidates

        if industry_subjects:
            industry_response = await selector.select_industries(
                industry_subjects,
                industry_candidates_by_input,
            )
            industry_results.update(
                validate_model_response(
                    industry_subjects,
                    industry_candidates_by_input,
                    industry_response,
                    max_selections=3,
                )
            )

        chain_candidates_by_input: dict[int, Sequence[CandidateChoice]] = {}
        chain_results: dict[int, StageDecision] = {}
        chain_subjects: list[CompanySubject] = []
        for subject in batch:
            industry_result = industry_results[subject.input_index]
            if industry_result.status != DecisionStatus.MAPPED:
                chain_results[subject.input_index] = no_candidate_decision(
                    "No ChainNode candidates are evaluated without an accepted Industry target"
                )
                continue
            selected_industry_ids = [item.target_id for item in industry_result.accepted_targets]
            chain_candidates = build_chain_node_candidates(
                catalog,
                selected_industry_ids,
                max_candidates=max_chain_candidates,
            )
            if not chain_candidates:
                chain_results[subject.input_index] = no_candidate_decision()
                continue
            chain_subjects.append(subject)
            chain_candidates_by_input[subject.input_index] = chain_candidates

        for chain_batch in partition_candidate_subjects(chain_subjects, chain_candidates_by_input):
            batch_candidates = {item.input_index: chain_candidates_by_input[item.input_index] for item in chain_batch}
            chain_response = await selector.select_chain_nodes(chain_batch, batch_candidates)
            chain_results.update(
                validate_model_response(
                    chain_batch,
                    batch_candidates,
                    chain_response,
                    max_selections=8,
                )
            )

        for subject in batch:
            decision = finalize_company_decision(
                company=subject,
                industry_result=industry_results[subject.input_index],
                chain_node_result=chain_results[subject.input_index],
                root_industry_candidate_ids=[item.target_id for item in root_candidates],
                selected_root_industry_ids=selected_roots_by_input[subject.input_index],
                industry_candidate_ids=[
                    item.target_id for item in industry_candidates_by_input.get(subject.input_index, [])
                ],
                chain_node_candidate_ids=[
                    item.target_id for item in chain_candidates_by_input.get(subject.input_index, [])
                ],
                manifest=manifest,
                decided_at=decision_time,
                manifest_fingerprint=manifest_fingerprint,
            )
            journal.freeze(decision)
    if max_new_decisions is not None:
        return journal.completed()
    return journal.assert_complete()


__all__ = [
    "CompanyTargetSelector",
    "company_snapshot_fingerprint",
    "infer_companies",
    "partition_candidate_subjects",
]
