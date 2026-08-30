"""Deterministic gates around Company model target selection."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from datetime import datetime

from capabilities.company.internal.models import (
    CandidateChoice,
    CandidateSetAudit,
    CompanyInferenceDecision,
    CompanySubject,
    Confidence,
    DecisionStatus,
    ModelSelectionResponse,
    ProjectionRunManifest,
    ResolvedTarget,
    StageDecision,
    TargetCatalog,
    _canonical_hash,
)


def _industry_candidates(catalog: TargetCatalog, industry_ids: set[str]) -> list[CandidateChoice]:
    industries_by_id = {item.industry_id: item for item in catalog.industries}

    def hierarchy(industry_id: str) -> list[str]:
        names: list[str] = []
        current = industries_by_id[industry_id]
        while current.parent_id is not None:
            current = industries_by_id[current.parent_id]
            names.append(current.name)
        return list(reversed(names))

    return [
        CandidateChoice(
            key=f"I{index}",
            target_id=item.industry_id,
            name=item.name,
            definition=item.definition,
            context=hierarchy(item.industry_id),
        )
        for index, item in enumerate(
            sorted(
                (item for item in catalog.industries if item.industry_id in industry_ids),
                key=lambda item: item.industry_id,
            ),
            1,
        )
    ]


def industry_candidates(catalog: TargetCatalog) -> list[CandidateChoice]:
    """Return every existing canonical Industry behind a compact stable key."""

    return _industry_candidates(catalog, {item.industry_id for item in catalog.industries})


def industry_root_candidates(catalog: TargetCatalog) -> list[CandidateChoice]:
    """Return the existing root Industries used to keep the first model prompt bounded."""

    return _industry_candidates(
        catalog,
        {item.industry_id for item in catalog.industries if item.parent_id is None},
    )


def industry_candidates_for_roots(
    catalog: TargetCatalog,
    selected_root_ids: Sequence[str],
) -> list[CandidateChoice]:
    """Return only existing Industries in the selected root subtrees."""

    roots = {item.industry_id for item in catalog.industries if item.parent_id is None}
    unknown = set(selected_root_ids).difference(roots)
    if unknown:
        raise ValueError(f"selected scope is not a root Industry: {sorted(unknown)[0]}")
    if not selected_root_ids:
        return []
    return _industry_candidates(catalog, set(_descendant_distances(catalog, selected_root_ids)))


def _descendant_distances(catalog: TargetCatalog, selected_industry_ids: Sequence[str]) -> dict[str, int]:
    known = {item.industry_id for item in catalog.industries}
    unknown = set(selected_industry_ids).difference(known)
    if unknown:
        raise ValueError(f"unknown selected Industry: {sorted(unknown)[0]}")
    children: dict[str, list[str]] = defaultdict(list)
    for industry in catalog.industries:
        if industry.parent_id is not None:
            children[industry.parent_id].append(industry.industry_id)
    distances: dict[str, int] = {}
    queue = deque((industry_id, 0) for industry_id in selected_industry_ids)
    while queue:
        industry_id, distance = queue.popleft()
        previous = distances.get(industry_id)
        if previous is not None and previous <= distance:
            continue
        distances[industry_id] = distance
        queue.extend((child_id, distance + 1) for child_id in children.get(industry_id, []))
    return distances


def build_chain_node_candidates(
    catalog: TargetCatalog,
    selected_industry_ids: Sequence[str],
    *,
    max_candidates: int,
) -> list[CandidateChoice]:
    """Traverse only catalog topology and return a deterministic bounded candidate list."""

    if max_candidates < 1:
        raise ValueError("max_candidates must be positive")
    distances = _descendant_distances(catalog, selected_industry_ids)
    chain_sources: dict[str, set[str]] = defaultdict(set)
    chain_distance: dict[str, int] = {}
    for mapping in catalog.industry_chain_mappings:
        distance = distances.get(mapping.industry_id)
        if distance is None:
            continue
        chain_sources[mapping.industry_chain_id].add(mapping.industry_id)
        chain_distance[mapping.industry_chain_id] = min(
            chain_distance.get(mapping.industry_chain_id, distance),
            distance,
        )
    node_chains: dict[str, set[str]] = defaultdict(set)
    node_sources: dict[str, set[str]] = defaultdict(set)
    node_distance: dict[str, int] = {}
    for membership in catalog.chain_memberships:
        if membership.industry_chain_id not in chain_sources:
            continue
        node_chains[membership.chain_node_id].add(membership.industry_chain_id)
        node_sources[membership.chain_node_id].update(chain_sources[membership.industry_chain_id])
        distance = chain_distance[membership.industry_chain_id]
        node_distance[membership.chain_node_id] = min(node_distance.get(membership.chain_node_id, distance), distance)
    nodes_by_id = {item.chain_node_id: item for item in catalog.chain_nodes}
    industries_by_id = {item.industry_id: item for item in catalog.industries}
    chains_by_id = {item.industry_chain_id: item for item in catalog.industry_chains}
    ordered_ids = sorted(
        node_chains,
        key=lambda node_id: (node_distance[node_id], node_id),
    )[:max_candidates]
    return [
        CandidateChoice(
            key=f"N{index}",
            target_id=node_id,
            name=nodes_by_id[node_id].name,
            definition=nodes_by_id[node_id].definition,
            context=[
                *[f"行业：{industries_by_id[item].name}" for item in sorted(node_sources[node_id])],
                *[f"产业链：{chains_by_id[item].name}" for item in sorted(node_chains[node_id])],
            ],
            source_industry_ids=sorted(node_sources[node_id]),
            industry_chain_ids=sorted(node_chains[node_id]),
        )
        for index, node_id in enumerate(ordered_ids, 1)
    ]


def no_candidate_decision(reason: str = "No canonical target is reachable from the frozen topology") -> StageDecision:
    return StageDecision(
        status=DecisionStatus.NO_CANDIDATE,
        accepted_targets=[],
        rejected_targets=[],
        reason=reason,
    )


def validate_model_response(
    subjects: Sequence[CompanySubject],
    candidates_by_input: Mapping[int, Sequence[CandidateChoice]],
    response: ModelSelectionResponse,
    *,
    max_selections: int,
) -> dict[int, StageDecision]:
    """Resolve supplied keys and reject incomplete, cross-input, or invented output."""

    if max_selections < 1:
        raise ValueError("max_selections must be positive")
    expected_indexes = [item.input_index for item in subjects]
    actual_indexes = [item.input_index for item in response.items]
    if len(expected_indexes) != len(set(expected_indexes)):
        raise ValueError("subjects contain duplicate input indexes")
    if sorted(actual_indexes) != sorted(expected_indexes) or len(actual_indexes) != len(set(actual_indexes)):
        raise ValueError("model items must exactly cover input indexes once")
    if set(candidates_by_input) != set(expected_indexes):
        raise ValueError("candidate sets must exactly cover input indexes")
    result: dict[int, StageDecision] = {}
    for item in response.items:
        if len(item.selections) > max_selections:
            raise ValueError(f"input {item.input_index} exceeds the selection limit")
        candidates = list(candidates_by_input[item.input_index])
        candidates_by_key = {candidate.key: candidate for candidate in candidates}
        if len(candidates_by_key) != len(candidates):
            raise ValueError(f"input {item.input_index} has duplicate candidate keys")
        accepted: list[ResolvedTarget] = []
        rejected: list[ResolvedTarget] = []
        for selection in item.selections:
            candidate = candidates_by_key.get(selection.candidate_key)
            if candidate is None:
                raise ValueError(f"input {item.input_index} references unknown candidate key {selection.candidate_key}")
            resolved = ResolvedTarget(
                target_id=candidate.target_id,
                confidence=selection.confidence,
                rationale=selection.rationale,
                supporting_company_fields=selection.supporting_company_fields,
                source_industry_ids=candidate.source_industry_ids,
                industry_chain_ids=candidate.industry_chain_ids,
            )
            if selection.confidence in {Confidence.MEDIUM, Confidence.HIGH}:
                accepted.append(resolved)
            else:
                rejected.append(resolved)
        if accepted:
            status = DecisionStatus.MAPPED
            reason = None
        elif rejected:
            status = DecisionStatus.LOW_CONFIDENCE
            reason = "All selected targets were LOW confidence"
        else:
            status = DecisionStatus.NO_MATCH
            reason = item.no_match_reason
        result[item.input_index] = StageDecision(
            status=status,
            accepted_targets=accepted,
            rejected_targets=rejected,
            reason=reason,
        )
    return result


def finalize_company_decision(
    *,
    company: CompanySubject,
    industry_result: StageDecision | dict[str, object],
    chain_node_result: StageDecision | dict[str, object],
    root_industry_candidate_ids: Sequence[str],
    selected_root_industry_ids: Sequence[str],
    industry_candidate_ids: Sequence[str],
    chain_node_candidate_ids: Sequence[str],
    manifest: ProjectionRunManifest,
    decided_at: datetime,
    manifest_fingerprint: str | None = None,
) -> CompanyInferenceDecision:
    """Combine both gated stages into one immutable per-Company terminal decision."""

    industry = StageDecision.model_validate(industry_result)
    chain_node = StageDecision.model_validate(chain_node_result)
    if (
        company.input_index >= len(manifest.company_ids)
        or manifest.company_ids[company.input_index] != company.company_id
    ):
        raise ValueError("Company is outside the frozen projection manifest")
    if chain_node.status == DecisionStatus.MAPPED and industry.status != DecisionStatus.MAPPED:
        raise ValueError("ChainNode mapping requires an accepted Industry mapping")
    statuses = {industry.status, chain_node.status}
    if DecisionStatus.MAPPED in statuses:
        status = DecisionStatus.MAPPED
    elif DecisionStatus.LOW_CONFIDENCE in statuses:
        status = DecisionStatus.LOW_CONFIDENCE
    elif DecisionStatus.NO_MATCH in statuses:
        status = DecisionStatus.NO_MATCH
    else:
        status = DecisionStatus.NO_CANDIDATE
    candidates = CandidateSetAudit(
        root_industry_candidate_ids=list(root_industry_candidate_ids),
        selected_root_industry_ids=list(selected_root_industry_ids),
        industry_candidate_ids=list(industry_candidate_ids),
        chain_node_candidate_ids=list(chain_node_candidate_ids),
    )
    frozen_manifest_fingerprint = manifest_fingerprint or manifest.fingerprint()
    identity_payload = {
        "company_id": company.company_id,
        "source_company_fingerprint": company.fingerprint(),
        "manifest_fingerprint": frozen_manifest_fingerprint,
        "industry": industry.model_dump(mode="json"),
        "chain_node": chain_node.model_dump(mode="json"),
        "candidates": candidates.model_dump(mode="json"),
    }
    return CompanyInferenceDecision(
        decision_id=_canonical_hash(identity_payload),
        company_id=company.company_id,
        input_index=company.input_index,
        status=status,
        industry=industry,
        chain_node=chain_node,
        candidates=candidates,
        source_company_fingerprint=company.fingerprint(),
        snapshot_id=manifest.snapshot_id,
        target_catalog_fingerprint=manifest.target_catalog_fingerprint,
        ontology_version=manifest.ontology_version,
        policy_version=manifest.policy_version,
        model_id=manifest.model_id,
        prompt_contract_version=manifest.prompt_contract_version,
        decided_at=decided_at,
    )


def validate_decision_candidate_scope(
    decision: CompanyInferenceDecision,
    catalog: TargetCatalog,
    manifest: ProjectionRunManifest,
    *,
    catalog_fingerprint: str | None = None,
) -> None:
    """Reconstruct every offered target set from the frozen canonical topology."""

    current_catalog_fingerprint = catalog_fingerprint or catalog.fingerprint()
    if (
        current_catalog_fingerprint != manifest.target_catalog_fingerprint
        or decision.target_catalog_fingerprint != current_catalog_fingerprint
    ):
        raise ValueError("decision target catalog differs from the current canonical catalog")
    expected_roots = [item.target_id for item in industry_root_candidates(catalog)]
    if decision.candidates.root_industry_candidate_ids != expected_roots:
        raise ValueError("frozen root Industry candidates differ from the canonical catalog")
    selected_roots = decision.candidates.selected_root_industry_ids
    expected_industries = (
        [item.target_id for item in industry_candidates_for_roots(catalog, selected_roots)] if selected_roots else []
    )
    if decision.candidates.industry_candidate_ids != expected_industries:
        raise ValueError("frozen Industry candidates differ from the selected canonical root scope")
    if decision.industry.accepted_targets and not selected_roots:
        raise ValueError("accepted Industry decision has no frozen root scope")
    selected_industries = [item.target_id for item in decision.industry.accepted_targets]
    expected_chain_nodes = [
        item.target_id
        for item in build_chain_node_candidates(
            catalog,
            selected_industries,
            max_candidates=manifest.max_chain_candidates,
        )
    ]
    if decision.candidates.chain_node_candidate_ids != expected_chain_nodes:
        raise ValueError("frozen ChainNode candidates differ from the canonical topology")


__all__ = [
    "build_chain_node_candidates",
    "finalize_company_decision",
    "industry_candidates",
    "industry_candidates_for_roots",
    "industry_root_candidates",
    "no_candidate_decision",
    "validate_model_response",
    "validate_decision_candidate_scope",
]
