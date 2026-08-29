"""Five deterministic Workflow Functions for layered investment reasoning."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput

from capabilities.investment.internal.engine import InvestmentReasoningEngine
from capabilities.investment.internal.models import (
    Confidence,
    GeopoliticalAnalysisState,
    IndustryAnalysisState,
    InvestmentAnalysisRequest,
    InvestmentAnalysisResult,
    InvestmentConclusionArtifact,
    InvestmentReasoningInput,
    LayerAnalysisResult,
    MacroAnalysisState,
    PreparedInvestmentContext,
    ReviewResult,
    Trend,
)
from capabilities.investment.internal.runtime import investment_workflow_runtime
from capabilities.investment.internal.storage import conclusion_artifact_path, write_conclusion_artifact


def _content(step_input: StepInput, name: str, model: type[Any]) -> Any:
    output = step_input.get_step_output(name)
    if output is None or output.content is None:
        raise ValueError(f"required Workflow step output is missing: {name}")
    if isinstance(output.content, model):
        return output.content
    if isinstance(output.content, str):
        return model.model_validate_json(output.content)
    return model.model_validate(output.content)


def _reasoning_input(value: Any) -> InvestmentReasoningInput:
    """Accept the new structured Schedule payload and legacy natural-language message."""

    return InvestmentReasoningInput.model_validate(value)


async def prepare_investment_context(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Freeze decision time and retrieve only the shared Event/Signal base context."""

    del run_context
    workflow_input = _reasoning_input(step_input.input)
    request = InvestmentAnalysisRequest(
        **workflow_input.model_dump(),
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


async def analyze_geopolitical_impact(step_input: StepInput) -> StepOutput:
    """Analyze the frozen Events against the predefined geopolitical blueprint."""

    prepared = _content(step_input, "prepare-investment-context", PreparedInvestmentContext)
    result = await investment_workflow_runtime().analyze_geopolitical(prepared)
    return StepOutput(content=GeopoliticalAnalysisState(prepared=prepared, geopolitical=result))


async def analyze_macro_impact(step_input: StepInput) -> StepOutput:
    """Analyze macro anchors using Events, Signals, and accepted geopolitical claims."""

    state = _content(step_input, "analyze-geopolitical-impact", GeopoliticalAnalysisState)
    result = await investment_workflow_runtime().analyze_macro(state.prepared, state.geopolitical)
    return StepOutput(
        content=MacroAnalysisState(
            prepared=state.prepared,
            geopolitical=state.geopolitical,
            macro=result,
        )
    )


async def analyze_industry_impact(step_input: StepInput) -> StepOutput:
    """Resolve industry candidates, load topology, propagate, and synthesize node trends."""

    state = _content(step_input, "analyze-macro-impact", MacroAnalysisState)
    result = await investment_workflow_runtime().analyze_industry(
        state.prepared,
        state.geopolitical,
        state.macro,
    )
    if isinstance(result, LayerAnalysisResult):
        raise TypeError("investment runtime returned a layer result instead of IndustryAnalysisState")
    return StepOutput(content=IndustryAnalysisState.model_validate(result))


def _deterministic_issues(state: IndustryAnalysisState) -> list[str]:
    context = state.industry_context
    eligible = context.eligible_signal_fact_ids
    event_ids = {item.event_id for item in context.events}
    issues: list[str] = []
    all_claims = [*state.geopolitical.claims, *state.macro.claims, *state.industry.claims]
    for claim in all_claims:
        if not claim.root_signal_fact_ids or not set(claim.root_signal_fact_ids) <= eligible:
            issues.append(f"CLAIM_WITHOUT_ACTIVE_SIGNAL_ROOT:{claim.claim_id}")
        if not claim.root_event_ids or not set(claim.root_event_ids) <= event_ids:
            issues.append(f"CLAIM_WITHOUT_SCOPED_EVENT_ROOT:{claim.claim_id}")
    for transmission in state.transmissions:
        if not transmission.root_signal_fact_ids or not set(transmission.root_signal_fact_ids) <= eligible:
            issues.append(f"TRANSMISSION_WITHOUT_SIGNAL_ROOT:{transmission.transmission_id}")
    issues.extend(
        InvestmentReasoningEngine.directional_lineage_issues(
            context,
            state.transmissions,
            state.draft,
            state.industry.claims,
        )
    )
    return list(dict.fromkeys(issues))


def _requires_semantic_review(state: IndustryAnalysisState) -> bool:
    return bool(
        state.geopolitical.claims
        or state.macro.claims
        or state.industry.claims
        or state.transmissions
        or any(
            trend != Trend.INSUFFICIENT_EVIDENCE
            for chain in state.draft.chains
            for node in chain.nodes
            for trend in (node.short, node.medium, node.long)
        )
    )


def _safe_layer_result(result: LayerAnalysisResult, reason: str) -> LayerAnalysisResult:
    return result.model_copy(
        update={
            "claims": [],
            "supporting_facts": [],
            "summary": f"{result.layer.value} 层结论未通过最终门禁，已安全降级为证据不足。",
            "limitations": list(dict.fromkeys([*result.limitations, reason]))[:20],
        }
    )


async def review_and_finalize(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Apply deterministic lineage gates, then let the Reviewer check supported conclusions."""

    state = _content(step_input, "analyze-industry-impact", IndustryAnalysisState)
    draft = state.draft
    geopolitical = state.geopolitical
    macro = state.macro
    industry = state.industry
    transmissions = list(state.transmissions)
    hard_issues = _deterministic_issues(state)
    execution_issues = list(state.execution_issues)
    if hard_issues:
        draft = InvestmentReasoningEngine.safe_fallback_draft(
            state.industry_context,
            "DETERMINISTIC_GATE_SAFE_FALLBACK",
        )
        geopolitical = _safe_layer_result(geopolitical, "DETERMINISTIC_GATE_SAFE_FALLBACK")
        macro = _safe_layer_result(macro, "DETERMINISTIC_GATE_SAFE_FALLBACK")
        industry = _safe_layer_result(industry, "DETERMINISTIC_GATE_SAFE_FALLBACK")
        transmissions = []
        review = ReviewResult(
            accepted=True,
            confidence=Confidence.LOW,
            issue_codes=hard_issues[:30],
            review_summary="确定性门禁拒绝了无完整谱系的方向结论，结果已降级为安全弃权。",
        )
        execution_issues.extend(hard_issues)
    elif not _requires_semantic_review(state):
        review = ReviewResult(
            accepted=True,
            confidence=Confidence.HIGH,
            issue_codes=[],
            review_summary="所有产业链节点均未作无证据的方向断言；本次是成功的证据不足弃权。",
        )
    else:
        review = await investment_workflow_runtime().review(state)
        if not review.accepted:
            execution_issues.extend(review.issue_codes)
            draft = InvestmentReasoningEngine.safe_fallback_draft(
                state.industry_context,
                "REVIEW_REJECTED_SAFE_FALLBACK",
            )
            review = ReviewResult(
                accepted=True,
                confidence=Confidence.LOW,
                issue_codes=list(dict.fromkeys(execution_issues))[:30],
                review_summary="审核未通过，已删除方向断言并降级为安全弃权。",
            )
            geopolitical = _safe_layer_result(geopolitical, "REVIEW_REJECTED_SAFE_FALLBACK")
            macro = _safe_layer_result(macro, "REVIEW_REJECTED_SAFE_FALLBACK")
            industry = _safe_layer_result(industry, "REVIEW_REJECTED_SAFE_FALLBACK")
            transmissions = []

    layer_results = [geopolitical, macro, industry]
    result = InvestmentAnalysisResult(
        executor="agentos-investment-reasoning-workflow",
        status="SUCCEEDED",
        context_fingerprint=state.prepared.context_fingerprint,
        geopolitical=geopolitical,
        macro=macro,
        industry=industry,
        transmissions=transmissions,
        draft=draft,
        review=review,
        reasoning_tree=InvestmentReasoningEngine.build_reasoning_tree(
            state.industry_context,
            layer_results,
            transmissions,
            draft,
        ),
        stage_metrics={
            "events": len(state.industry_context.events),
            "facts": len(state.industry_context.facts),
            "eligible_signals": len(state.industry_context.eligible_signal_fact_ids),
            "geopolitical_claims": len(geopolitical.claims),
            "macro_claims": len(macro.claims),
            "industry_claims": len(industry.claims),
            "chains": len(state.industry_context.chains),
            "nodes": sum(len(item.nodes) for item in state.industry_context.chains),
            "topology_edges": sum(len(item.edges) for item in state.industry_context.chains),
            "transmission_rounds": state.rounds_executed,
            "accepted_transmissions": len(transmissions),
        },
        execution_issues=list(dict.fromkeys(execution_issues))[:100],
    )
    workflow_run_id = str(run_context.run_id or "").strip()
    path = conclusion_artifact_path(workflow_run_id)
    request = state.prepared.context.request
    has_supported_conclusion = bool(
        geopolitical.claims
        or macro.claims
        or industry.claims
        or transmissions
        or any(
            trend != Trend.INSUFFICIENT_EVIDENCE
            for chain in draft.chains
            for node in chain.nodes
            for trend in (node.short, node.medium, node.long)
        )
    )
    artifact = InvestmentConclusionArtifact(
        **result.model_dump(),
        workflow_run_id=workflow_run_id,
        artifact_path=str(path),
        decision_at=request.decision_at,
        question=request.question,
        event_window_hours=request.event_window_hours,
        conclusion_status="SUPPORTED" if has_supported_conclusion else "INSUFFICIENT_EVIDENCE",
    )
    await asyncio.to_thread(write_conclusion_artifact, artifact)
    return StepOutput(content=artifact, success=True)
