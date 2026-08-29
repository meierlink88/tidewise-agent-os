"""Deterministic comparison of two executions over one frozen context."""

from __future__ import annotations

from sematica.reasoning.investment.contracts import (
    ComparisonDifference,
    ComparisonReport,
    Horizon,
    InvestmentAnalysisResult,
    Trend,
)

MATERIAL_PAIRS = {
    frozenset((Trend.WARMING, Trend.COOLING)),
}


def compare_results(
    left: InvestmentAnalysisResult,
    right: InvestmentAnalysisResult,
) -> ComparisonReport:
    same_context = left.context_fingerprint == right.context_fingerprint
    left_nodes = {(chain.chain_id, node.node_id): node for chain in left.draft.chains for node in chain.nodes}
    right_nodes = {(chain.chain_id, node.node_id): node for chain in right.draft.chains for node in chain.nodes}
    differences: list[ComparisonDifference] = []
    exact = 0
    compatible = 0
    material = 0
    total = 0
    for key in sorted(set(left_nodes).intersection(right_nodes)):
        for horizon, field in (
            (Horizon.SHORT, "short"),
            (Horizon.MEDIUM, "medium"),
            (Horizon.LONG, "long"),
        ):
            total += 1
            left_value = getattr(left_nodes[key], field)
            right_value = getattr(right_nodes[key], field)
            if left_value == right_value:
                exact += 1
                continue
            is_material = frozenset((left_value, right_value)) in MATERIAL_PAIRS
            if is_material:
                material += 1
            else:
                compatible += 1
            differences.append(
                ComparisonDifference(
                    chain_id=key[0],
                    node_id=key[1],
                    horizon=horizon,
                    left=left_value,
                    right=right_value,
                    severity="MATERIAL" if is_material else "COMPATIBLE",
                )
            )
    similarity = (exact + compatible * 0.5) / total if total else 0.0
    return ComparisonReport(
        same_context=same_context,
        total_node_horizons=total,
        exact_matches=exact,
        compatible_matches=compatible,
        material_contradictions=material,
        weighted_similarity=round(similarity, 4),
        basically_consistent=same_context and similarity >= 0.75 and material == 0,
        differences=differences,
    )
