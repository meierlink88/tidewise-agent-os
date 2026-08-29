"""Five deterministic Workflow Functions for investment reasoning."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput

from capabilities.investment.internal.engine import InvestmentReasoningEngine
from capabilities.investment.internal.models import (
    AcceptedTransmission,
    Confidence,
    InvestmentAnalysisPlan,
    InvestmentAnalysisRequest,
    InvestmentAnalysisResult,
    InvestmentDraftState,
    InvestmentTransmissionState,
    PreparedInvestmentContext,
    ReviewResult,
    Trend,
)
from capabilities.investment.internal.runtime import investment_workflow_runtime


def _content(step_input: StepInput, name: str, model: type[Any]) -> Any:
    output = step_input.get_step_output(name)
    if output is None or output.content is None:
        raise ValueError(f"required Workflow step output is missing: {name}")
    if isinstance(output.content, model):
        return output.content
    if isinstance(output.content, str):
        return model.model_validate_json(output.content)
    return model.model_validate(output.content)


async def prepare_investment_context(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Freeze decision time and retrieve the bounded Graphiti context."""

    del run_context
    plan = _content(step_input, "plan-investment-analysis", InvestmentAnalysisPlan)
    request = InvestmentAnalysisRequest(
        **plan.model_dump(),
        decision_at=datetime.now(UTC),
        forward_horizon_days=1095,
        min_anchor_matches=1,
        max_chains=10,
        max_hops=3,
    )
    context = await investment_workflow_runtime().prepare(request)
    return StepOutput(
        content=PreparedInvestmentContext(
            context=context,
            context_fingerprint=InvestmentReasoningEngine.context_fingerprint(context),
        )
    )


async def reason_signal_transmissions(step_input: StepInput) -> StepOutput:
    """Execute up to three LLM rounds while code enforces Signal-root lineage."""

    prepared = _content(step_input, "prepare-investment-context", PreparedInvestmentContext)
    accepted: list[AcceptedTransmission] = []
    rounds = 0
    if any(chain.signal_root_node_ids for chain in prepared.context.chains):
        for round_number in range(1, prepared.context.request.max_hops + 1):
            batch = await investment_workflow_runtime().propagate(prepared.context, accepted, round_number=round_number)
            rounds += 1
            new_items = InvestmentReasoningEngine.validate_round(
                prepared.context, accepted, batch, round_number=round_number
            )
            accepted.extend(new_items)
            if not any(item.confidence != Confidence.LOW for item in new_items):
                break
    return StepOutput(
        content=InvestmentTransmissionState(
            context=prepared.context,
            context_fingerprint=prepared.context_fingerprint,
            transmissions=accepted,
            rounds_executed=rounds,
        )
    )


async def synthesize_investment_conclusion(step_input: StepInput) -> StepOutput:
    """Ask the Reasoner for node conclusions, then normalize every unsupported horizon."""

    state = _content(step_input, "reason-signal-transmissions", InvestmentTransmissionState)
    draft = await investment_workflow_runtime().synthesize(state.context, state.transmissions)
    draft = InvestmentReasoningEngine.normalize_draft(state.context, state.transmissions, draft)
    return StepOutput(content=InvestmentDraftState(**state.model_dump(), draft=draft))


def _deterministic_issues(state: InvestmentDraftState) -> list[str]:
    issues: list[str] = []
    eligible = state.context.eligible_signal_fact_ids
    for transmission in state.transmissions:
        if not transmission.root_signal_fact_ids or not set(transmission.root_signal_fact_ids) <= eligible:
            issues.append("TRANSMISSION_WITHOUT_SIGNAL_ROOT")
    issues.extend(
        InvestmentReasoningEngine.directional_lineage_issues(
            state.context,
            state.transmissions,
            state.draft,
        )
    )
    return list(dict.fromkeys(issues))


def _has_directional_claims(state: InvestmentDraftState) -> bool:
    return any(
        trend != Trend.INSUFFICIENT_EVIDENCE
        for chain in state.draft.chains
        for node in chain.nodes
        for trend in (node.short, node.medium, node.long)
    )


async def review_and_finalize(step_input: StepInput) -> StepOutput:
    """Apply hard gates before the Reviewer and return the persisted Workflow result."""

    state = _content(step_input, "synthesize-investment-conclusion", InvestmentDraftState)
    draft = state.draft
    hard_issues = _deterministic_issues(state)
    execution_issues = list(state.execution_issues)
    if hard_issues:
        draft = InvestmentReasoningEngine.safe_fallback_draft(
            state.context,
            "DETERMINISTIC_GATE_SAFE_FALLBACK",
        )
        review = ReviewResult(
            accepted=True,
            confidence=Confidence.LOW,
            issue_codes=hard_issues[:30],
            review_summary="确定性门禁拒绝方向结论，已降级为安全弃权。",
        )
        execution_issues.extend(hard_issues)
    elif not _has_directional_claims(state):
        review = ReviewResult(
            accepted=True,
            confidence=Confidence.HIGH,
            issue_codes=[],
            review_summary=("确定性门禁已确认所有节点均未作出方向性断言；当前结果是一次成功的证据不足弃权。"),
        )
    else:
        review = await investment_workflow_runtime().review(state.context, state.transmissions, draft)
        if not review.accepted:
            execution_issues.extend(review.issue_codes)
            repaired = await investment_workflow_runtime().repair(
                state.context,
                state.transmissions,
                draft,
                review,
            )
            draft = InvestmentReasoningEngine.normalize_draft(state.context, state.transmissions, repaired)
            repaired_state = state.model_copy(update={"draft": draft})
            repair_issues = _deterministic_issues(repaired_state)
            if repair_issues:
                review = ReviewResult(
                    accepted=False,
                    confidence=Confidence.LOW,
                    issue_codes=repair_issues,
                    review_summary="修正后仍未通过确定性谱系门禁。",
                )
            else:
                review = await investment_workflow_runtime().review(state.context, state.transmissions, draft)
            if not review.accepted:
                execution_issues.extend(review.issue_codes)
                draft = InvestmentReasoningEngine.safe_fallback_draft(
                    state.context,
                    "REVIEW_REJECTED_SAFE_FALLBACK",
                )
                review = ReviewResult(
                    accepted=True,
                    confidence=Confidence.LOW,
                    issue_codes=list(dict.fromkeys(execution_issues))[:30],
                    review_summary="一次有界修正仍未通过审核，已降级为无方向性断言的安全弃权。",
                )
    result = InvestmentAnalysisResult(
        executor="agentos-investment-reasoning-workflow",
        status="SUCCEEDED" if review.accepted else "NEEDS_REVIEW",
        context_fingerprint=state.context_fingerprint,
        transmissions=state.transmissions,
        draft=draft,
        review=review,
        stage_metrics={
            "events": len(state.context.events),
            "facts": len(state.context.facts),
            "eligible_signals": len(state.context.eligible_signal_fact_ids),
            "chain_signal_roots": len(
                {fact_id for chain in state.context.chains for fact_id in chain.signal_root_fact_ids}
            ),
            "chains": len(state.context.chains),
            "nodes": sum(len(item.nodes) for item in state.context.chains),
            "topology_edges": sum(len(item.edges) for item in state.context.chains),
            "transmission_rounds": state.rounds_executed,
            "accepted_transmissions": len(state.transmissions),
        },
        execution_issues=list(dict.fromkeys(execution_issues))[:100],
    )
    return StepOutput(content=result, success=review.accepted)
