"""Deterministic Workflow Functions for layered investment reasoning and Report generation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput

from capabilities.investment.internal.engine import InvestmentReasoningEngine
from capabilities.investment.internal.models import (
    Confidence,
    CrossLayerAnalysisResult,
    GeopoliticalAnalysisState,
    ImpactLayer,
    IndustryAnalysisState,
    InvestmentAnalysisRequest,
    InvestmentAnalysisResult,
    InvestmentConclusionArtifact,
    InvestmentReasoningInput,
    InvestmentReportWorkflowOutput,
    LayerAnalysisResult,
    MacroAnalysisState,
    PreparedInvestmentContext,
    ReviewedInvestmentState,
    ReviewResult,
    Trend,
)
from capabilities.investment.internal.reporting import InvestmentReportAssembler, ReportNotPublishable
from capabilities.investment.internal.runtime import investment_workflow_runtime
from capabilities.investment.internal.storage import (
    conclusion_artifact_path,
    write_conclusion_artifact,
    write_report_artifact,
)


def _content(step_input: StepInput, model: type[Any]) -> Any:
    """Read only the direct predecessor, independent of its Studio display name."""

    content = step_input.previous_step_content
    if content is None:
        content = step_input.get_last_step_content()
    if content is None:
        raise ValueError("required previous Workflow step output is missing")
    if isinstance(content, model):
        return content
    if isinstance(content, str):
        return model.model_validate_json(content)
    return model.model_validate(content)


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
        max_chains=100,
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

    prepared = _content(step_input, PreparedInvestmentContext)
    result = await investment_workflow_runtime().analyze_geopolitical(prepared)
    return StepOutput(content=GeopoliticalAnalysisState(prepared=prepared, geopolitical=result))


async def analyze_macro_impact(step_input: StepInput) -> StepOutput:
    """Analyze macro anchors using Events, Signals, and accepted geopolitical claims."""

    state = _content(step_input, GeopoliticalAnalysisState)
    response = await investment_workflow_runtime().analyze_macro(state.prepared, state.geopolitical)
    if isinstance(response, tuple):
        result, transmission = response
    else:
        result = response
        transmission = CrossLayerAnalysisResult(target_layer=ImpactLayer.MACRO_ECONOMIC)
    return StepOutput(
        content=MacroAnalysisState(
            prepared=state.prepared,
            geopolitical=state.geopolitical,
            macro=result,
            macro_transmission=transmission,
        )
    )


async def analyze_industry_impact(step_input: StepInput) -> StepOutput:
    """Resolve industry candidates, load topology, propagate, and synthesize node trends."""

    state = _content(step_input, MacroAnalysisState)
    result = await investment_workflow_runtime().analyze_industry(
        state.prepared,
        state.geopolitical,
        state.macro,
        state.macro_transmission,
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

    state = _content(step_input, IndustryAnalysisState)
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
            # Deterministic lineage gates already removed unsupported conclusions.
            # The semantic Reviewer remains an audit signal; it must not erase an
            # otherwise valid partial report or turn model uncertainty into a run failure.
            execution_issues.extend(review.issue_codes)

    layer_results = [geopolitical, macro, industry]
    result = InvestmentAnalysisResult(
        executor="agentos-investment-reasoning-workflow",
        status="SUCCEEDED" if review.accepted else "NEEDS_REVIEW",
        context_fingerprint=state.prepared.context_fingerprint,
        geopolitical=geopolitical,
        macro=macro,
        industry=industry,
        cross_layer_transmissions=[
            *state.macro_transmission.accepted,
            *state.industry_transmission.accepted,
        ],
        cross_layer_candidates=[
            *state.macro_transmission.candidates,
            *state.industry_transmission.candidates,
        ],
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
    return StepOutput(
        content=ReviewedInvestmentState(analysis=artifact, context=state.industry_context),
        success=True,
    )


async def generate_investment_report(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Assemble and persist the fixed Report Artifact without an external service dependency."""

    del run_context
    reviewed = _content(step_input, ReviewedInvestmentState)
    analysis = reviewed.analysis
    try:
        package = InvestmentReportAssembler().assemble(analysis, reviewed.context)
    except ReportNotPublishable as exc:
        return StepOutput(
            content=InvestmentReportWorkflowOutput(
                source_report_id=f"agentos-investment-{analysis.workflow_run_id}",
                report_artifact_path="",
                audit_artifact_path=analysis.artifact_path,
                generation_status="SKIPPED",
                reason=str(exc),
            ),
            success=True,
        )
    path = await asyncio.to_thread(write_report_artifact, analysis.workflow_run_id, package)
    return StepOutput(
        content=InvestmentReportWorkflowOutput(
            source_report_id=package.source_report_id,
            report_artifact_path=str(path),
            audit_artifact_path=analysis.artifact_path,
            generation_status="GENERATED",
        ),
        success=True,
    )


async def publish_investment_report(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Compatibility alias for previously stored Workflow versions; no external publication occurs."""

    return await generate_investment_report(step_input, run_context)
