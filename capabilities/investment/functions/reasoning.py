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
        max_hops=5,
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
    for receipt in context.retrieval_receipts:
        missing = set(receipt.required_actions) - set(receipt.completed_actions)
        for action in sorted(missing):
            issues.append(f"REQUIRED_RETRIEVAL_NOT_EXECUTED:{receipt.stage}:{action}")
    required_stages = {"PREPARE", "GEOPOLITICAL", "MACRO_ECONOMIC", "INDUSTRY"}
    completed_stages = {receipt.stage for receipt in context.retrieval_receipts}
    for stage in sorted(required_stages - completed_stages):
        issues.append(f"RETRIEVAL_RECEIPT_MISSING:{stage}")
    all_assessments = [
        *state.geopolitical.assessments,
        *state.macro.assessments,
        *state.industry.assessments,
    ]
    receipt_signal_ids_by_layer = {
        layer: {
            fact_id
            for receipt in context.retrieval_receipts
            if receipt.layer == layer
            for fact_id in receipt.direct_signal_fact_ids
        }
        for layer in ImpactLayer
    }
    for assessment in all_assessments:
        if not assessment.direct_signal_fact_ids or not set(assessment.direct_signal_fact_ids) <= eligible:
            issues.append(f"ASSESSMENT_REFERENCE_OUTSIDE_CONTEXT:{assessment.assessment_id}")
        if not set(assessment.direct_signal_fact_ids) <= receipt_signal_ids_by_layer[assessment.layer]:
            issues.append(f"ASSESSMENT_REFERENCE_OUTSIDE_LAYER_RETRIEVAL:{assessment.assessment_id}")
        if not assessment.root_event_ids or not set(assessment.root_event_ids) <= event_ids:
            issues.append(f"ASSESSMENT_EVENT_REFERENCE_OUTSIDE_CONTEXT:{assessment.assessment_id}")
    assessment_ids = {item.assessment_id for item in all_assessments}
    ordinary_fact_ids = {item.uuid for item in context.facts if item.kind == "ORDINARY"}
    cross_layer_items = [
        *state.macro_transmission.accepted,
        *state.macro_transmission.candidates,
        *state.industry_transmission.accepted,
        *state.industry_transmission.candidates,
    ]
    for cross_item in cross_layer_items:
        if {cross_item.source_assessment_id, cross_item.target_assessment_id} - assessment_ids:
            issues.append(f"CROSS_LAYER_REFERENCE_OUTSIDE_CONTEXT:{cross_item.source_assessment_id}")
        if not set(cross_item.mechanism_fact_ids) <= ordinary_fact_ids:
            issues.append(f"CROSS_LAYER_FACT_REFERENCE_OUTSIDE_CONTEXT:{cross_item.source_assessment_id}")
    for node_transmission in state.transmissions:
        if not node_transmission.root_signal_fact_ids or not set(node_transmission.root_signal_fact_ids) <= eligible:
            issues.append(f"TRANSMISSION_WITHOUT_SIGNAL_ROOT:{node_transmission.transmission_id}")
    issues.extend(
        InvestmentReasoningEngine.directional_lineage_issues(
            context,
            state.transmissions,
            state.draft,
            state.industry.assessments,
        )
    )
    return list(dict.fromkeys(issues))


def _requires_semantic_review(state: IndustryAnalysisState) -> bool:
    return bool(
        state.geopolitical.assessments
        or state.macro.assessments
        or state.industry.assessments
        or state.transmissions
        or any(
            trend != Trend.INSUFFICIENT_EVIDENCE
            for chain in state.draft.chains
            for node in chain.nodes
            for trend in (node.short, node.medium, node.long)
        )
    )


async def review_and_finalize(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Verify required actions and output references, then record semantic review notes."""

    state = _content(step_input, IndustryAnalysisState)
    hard_issues = _deterministic_issues(state)
    execution_issues = list(state.execution_issues)
    if hard_issues:
        review = ReviewResult(
            accepted=False,
            confidence=Confidence.LOW,
            issue_codes=hard_issues[:30],
            review_summary="Workflow 必需检索动作或输出引用不完整，不发布最终报告。",
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
        runtime = investment_workflow_runtime()
        semantic_review = await runtime.review(state)
        execution_issues.extend(semantic_review.issue_codes)
        if "REVIEW_OUTPUT_INVALID" in semantic_review.issue_codes:
            review = semantic_review.model_copy(
                update={
                    "accepted": False,
                    "review_summary": "Reviewer 输出未通过结构合同，不发布最终报告。",
                }
            )
        elif not semantic_review.accepted:
            repair = getattr(runtime, "repair", None)
            if repair is None:
                review = semantic_review
            else:
                state = await repair(state, semantic_review)
                repair_hard_issues = _deterministic_issues(state)
                if repair_hard_issues:
                    execution_issues.extend(repair_hard_issues)
                    review = ReviewResult(
                        accepted=False,
                        confidence=Confidence.LOW,
                        issue_codes=repair_hard_issues[:30],
                        review_summary="一次语义返工后仍未通过确定性谱系校验。",
                    )
                else:
                    second_review = await runtime.review(state)
                    execution_issues.extend(second_review.issue_codes)
                    review = second_review.model_copy(
                        update={"review_summary": ("已执行一次语义返工。" + second_review.review_summary)[:2000]}
                    )
        else:
            review = semantic_review.model_copy(
                update={
                    "review_summary": (
                        "Workflow 检索与输出合同完整。"
                        + (f"语义审核备注：{semantic_review.review_summary}" if semantic_review.issue_codes else "")
                    )[:2000],
                }
            )

    draft = state.draft
    geopolitical = state.geopolitical
    macro = state.macro
    industry = state.industry
    transmissions = list(state.transmissions)
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
            "geopolitical_assessments": len(geopolitical.assessments),
            "macro_assessments": len(macro.assessments),
            "industry_assessments": len(industry.assessments),
            "chains": len(state.industry_context.chains),
            "nodes": sum(len(item.nodes) for item in state.industry_context.chains),
            "topology_edges": sum(len(item.edges) for item in state.industry_context.chains),
            "transmission_rounds": state.rounds_executed,
            "transmission_inclusion_threshold": state.transmission_metrics.inclusion_threshold,
            "transmission_continuation_threshold": state.transmission_metrics.continuation_threshold,
            "accepted_transmissions": len(transmissions),
            "transmission_candidates_enumerated": state.transmission_metrics.candidates_enumerated,
            "transmission_candidates_evaluated": state.transmission_metrics.candidates_evaluated,
            "transmission_rejected_below_inclusion": state.transmission_metrics.rejected_below_inclusion,
            "transmission_stopped_by_confidence": state.transmission_metrics.stopped_by_confidence,
            "transmission_stopped_by_no_neighbor": state.transmission_metrics.stopped_by_no_unvisited_neighbor,
            "transmission_semantic_review_issues": state.transmission_metrics.semantic_review_issues,
            "transmission_semantic_repaired": state.transmission_metrics.semantic_repaired,
            "transmission_semantic_dropped": state.transmission_metrics.semantic_dropped,
        },
        execution_issues=list(dict.fromkeys(execution_issues))[:100],
    )
    workflow_run_id = str(run_context.run_id or "").strip()
    path = conclusion_artifact_path(workflow_run_id)
    request = state.prepared.context.request
    has_supported_conclusion = bool(
        geopolitical.assessments
        or macro.assessments
        or industry.assessments
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
    if not analysis.review.accepted:
        return StepOutput(
            content=InvestmentReportWorkflowOutput(
                source_report_id=f"agentos-investment-{analysis.workflow_run_id}",
                report_artifact_path="",
                audit_artifact_path=analysis.artifact_path,
                generation_status="SKIPPED",
                reason="Workflow 必需检索动作或输出引用未通过审核。",
            ),
            success=True,
        )
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
