"""Compose AgentOS Agents with Graphiti's layered retrieval surface."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agno.agent import Agent
from pydantic import BaseModel, ValidationError

from capabilities.investment.internal.context import InvestmentContextBuilder
from capabilities.investment.internal.engine import InvestmentReasoningEngine
from capabilities.investment.internal.models import (
    AcceptedTransmission,
    AnalysisDraft,
    CandidateCrossLayerMechanism,
    ChainTrendView,
    Confidence,
    CrossLayerAnalysisResult,
    CrossLayerTransmissionBatch,
    CrossLayerTransmissionProposal,
    ImpactLayer,
    IndustryAnalysisState,
    InvestmentAnalysisContext,
    InvestmentAnalysisRequest,
    LayerAnalysisContext,
    LayerAnalysisResult,
    LayerAssessment,
    LayerAssessmentBatch,
    LayerAssessmentProposal,
    NodeAnalysisBatch,
    NodeTrendView,
    PreparedInvestmentContext,
    ReportNarrativeBatch,
    ReportNarrativeRewrite,
    ReviewResult,
    TransmissionBatch,
    TransmissionCandidate,
    TransmissionExecutionMetrics,
    TransmissionProposal,
    TransmissionSemanticIssue,
    TransmissionSemanticReview,
)
from capabilities.investment.internal.report_contract import InvestmentReportArtifact
from capabilities.investment.internal.report_publication import (
    ReportPublicationReceipt,
    ReportPublicationRequest,
    ReportPublisher,
)
from capabilities.investment.internal.reporting import apply_report_narratives, extract_report_narratives
from sematica.graphiti.investment import GraphitiInvestmentReader
from sematica.graphiti.runtime import create_agentos_graphiti

LAYER_REASONING_BATCH_SIZE = 100
TRANSMISSION_REVIEW_BATCH_SIZE = 50
REPORT_NARRATIVE_BATCH_SIZE = 40


def _raw_payload(value: Any) -> Any:
    content = getattr(value, "content", value)
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            stripped = "\n".join(lines[1:-1]).strip()
        return json.loads(stripped)
    return content


def _valid_items[ModelT: BaseModel](values: Any, model: type[ModelT]) -> tuple[list[ModelT], int]:
    accepted: list[ModelT] = []
    rejected = 0
    for value in values if isinstance(values, list) else []:
        try:
            accepted.append(model.model_validate(value))
        except ValidationError:
            rejected += 1
    return accepted, rejected


def _trend_value(value: Any) -> Any:
    if isinstance(value, dict) and len(value) == 1:
        return next(iter(value))
    return value


def _string_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()][:limit]


def _bounded_text(value: Any, *, limit: int, fallback: str | None = None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return fallback
    return value.strip()[:limit]


def _tolerant_payload[ModelT: BaseModel](value: Any, model: type[ModelT]) -> ModelT:
    """Keep valid semantic items and degrade malformed model output instead of aborting a run."""

    try:
        raw = _raw_payload(value)
    except (json.JSONDecodeError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if model is NodeAnalysisBatch:
        normalized = []
        for item in raw.get("nodes", []) if isinstance(raw.get("nodes"), list) else []:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    **item,
                    "short": _trend_value(item.get("short")),
                    "medium": _trend_value(item.get("medium")),
                    "long": _trend_value(item.get("long")),
                }
            )
        nodes, _ = _valid_items(normalized, NodeTrendView)
        return model.model_validate({"nodes": [item.model_dump(mode="json") for item in nodes]})
    if model is LayerAssessmentBatch:
        layer_proposals, layer_rejected = _valid_items(raw.get("proposals"), LayerAssessmentProposal)
        layer_limitations = _string_list(raw.get("limitations"), limit=19)
        if layer_rejected:
            layer_limitations.append("LLM_OUTPUT_ITEM_REJECTED")
        layer_summary = raw.get("summary")
        return model.model_validate(
            {
                "proposals": [item.model_dump(mode="json") for item in layer_proposals],
                "supplemental_queries": _string_list(raw.get("supplemental_queries"), limit=4),
                "summary": layer_summary
                if isinstance(layer_summary, str) and layer_summary.strip()
                else "模型输出不完整，仅保留通过合同校验的候选。",
                "limitations": layer_limitations,
            }
        )
    if model is TransmissionBatch:
        transmission_proposals, _ = _valid_items(raw.get("proposals"), TransmissionProposal)
        stopped_reason = raw.get("stopped_reason")
        normalized_stopped_reason = _bounded_text(
            stopped_reason,
            limit=500,
            fallback="LLM_OUTPUT_ITEM_REJECTED",
        )
        return model.model_validate(
            {
                "proposals": [item.model_dump(mode="json") for item in transmission_proposals],
                "stopped_reason": normalized_stopped_reason,
            }
        )
    if model is CrossLayerTransmissionBatch:
        cross_layer_proposals, cross_layer_rejected = _valid_items(raw.get("proposals"), CrossLayerTransmissionProposal)
        cross_layer_limitations = _string_list(raw.get("limitations"), limit=19)
        if cross_layer_rejected:
            cross_layer_limitations.append("LLM_OUTPUT_ITEM_REJECTED")
        return model.model_validate(
            {
                "proposals": [item.model_dump(mode="json") for item in cross_layer_proposals],
                "limitations": cross_layer_limitations,
            }
        )
    if model is TransmissionSemanticReview:
        issues, _ = _valid_items(raw.get("issues"), TransmissionSemanticIssue)
        return model.model_validate({"issues": [item.model_dump(mode="json") for item in issues]})
    if model is ReviewResult:
        return model.model_validate(
            {
                "accepted": False,
                "confidence": Confidence.LOW,
                "issue_codes": ["REVIEW_OUTPUT_INVALID"],
                "review_summary": "审核模型输出不合规，工作流已进入安全降级。",
            }
        )
    if model is ReportNarrativeBatch:
        rewrites, _ = _valid_items(raw.get("rewrites"), ReportNarrativeRewrite)
        return model.model_validate({"rewrites": [item.model_dump(mode="json") for item in rewrites]})
    raise ValueError(f"unsupported tolerant investment output model: {model.__name__}")


def _payload[ModelT: BaseModel](value: Any, model: type[ModelT]) -> ModelT:
    content = getattr(value, "content", value)
    if isinstance(content, model):
        return content
    try:
        return model.model_validate(_raw_payload(content))
    except (ValidationError, json.JSONDecodeError, TypeError):
        return _tolerant_payload(content, model)


class LocalInvestmentWorkflowRuntime:
    """Run bounded model calls while deterministic Functions own state transitions."""

    def __init__(
        self,
        graphiti,
        reasoner: Agent,
        reviewer: Agent,
        report_writer: Agent | None = None,
        report_publisher: ReportPublisher | None = None,
    ) -> None:
        self._graphiti = graphiti
        self._provider = InvestmentContextBuilder(GraphitiInvestmentReader(graphiti))
        self._reasoner = reasoner
        self._report_writer = report_writer or reasoner
        self._reviewer = reviewer
        self._report_publisher = report_publisher
        self._slots = asyncio.Semaphore(3)

    async def prepare(self, request: InvestmentAnalysisRequest) -> InvestmentAnalysisContext:
        return await self._provider.build(request)

    async def _run[ModelT: BaseModel](self, agent: Agent, prompt: str, model: type[ModelT]) -> ModelT:
        call_agent = agent.deep_copy(update={"db": None, "parse_response": False})
        async with self._slots:
            output = await call_agent.arun(prompt, stream=False, output_schema=model)
        return _payload(output, model)

    async def analyze_geopolitical(self, prepared: PreparedInvestmentContext) -> LayerAnalysisResult:
        result, _ = await self._analyze_layer(prepared, ImpactLayer.GEOPOLITICAL, [])
        return result

    async def analyze_macro(
        self,
        prepared: PreparedInvestmentContext,
        geopolitical: LayerAnalysisResult,
    ) -> tuple[LayerAnalysisResult, CrossLayerAnalysisResult]:
        result, context = await self._analyze_layer(
            prepared,
            ImpactLayer.MACRO_ECONOMIC,
            geopolitical.assessments,
        )
        transmission = await self._analyze_cross_layer(
            context,
            geopolitical.assessments,
            result.assessments,
        )
        return result, transmission

    async def _analyze_layer(
        self,
        prepared: PreparedInvestmentContext,
        layer: ImpactLayer,
        parent_assessments: list[LayerAssessment],
    ) -> tuple[LayerAnalysisResult, LayerAnalysisContext]:
        context = await self._provider.build_layer_context(prepared.context, layer, parent_assessments)
        audit_facts_by_id = {fact.uuid: fact for fact in context.facts}
        if not context.anchors:
            return (
                LayerAnalysisResult(
                    layer=layer,
                    assessments=[],
                    supporting_facts=[],
                    summary=f"{layer.value} 层未召回标准锚点，因此不形成方向结论。",
                    limitations=["NO_STANDARD_ANCHOR_CANDIDATE"],
                    retrieval_receipts=[context.retrieval_receipt],
                ),
                context,
            )
        first = await self._reason_layer(context)
        assessments = InvestmentReasoningEngine.build_layer_assessments(
            context,
            first,
            layer=layer,
        )
        limitations = list(first.limitations)
        summary = first.summary
        rounds = 1
        receipts = [context.retrieval_receipt]
        if first.supplemental_queries:
            second_context = await self._provider.build_layer_context(
                prepared.context,
                layer,
                parent_assessments,
                supplemental_queries=first.supplemental_queries,
                retrieval_round=2,
            )
            second = await self._reason_layer(second_context)
            audit_facts_by_id.update({fact.uuid: fact for fact in second_context.facts})
            second_assessments = InvestmentReasoningEngine.build_layer_assessments(
                second_context,
                second,
                layer=layer,
            )
            by_id = {item.assessment_id: item for item in [*assessments, *second_assessments]}
            assessments = list(by_id.values())
            limitations.extend(second.limitations)
            summary = second.summary
            context = second_context
            receipts.append(second_context.retrieval_receipt)
            rounds = 2
        if not assessments:
            limitations = ["NO_DIRECT_SIGNAL_ASSESSMENT"]
            summary = (
                f"{layer.value} 层未检索到可直接评估的 Signal 锚点；相关 Event 和普通 Fact 仅作为背景或待验证机制。"
            )
        elif summary.startswith("模型输出不完整"):
            result_labels = {
                "WARMING": "升温",
                "COOLING": "降温",
                "DIVERGENT": "分化",
                "NO_MATERIAL_CHANGE": "无显著变化",
                "INSUFFICIENT_EVIDENCE": "证据不足",
            }
            counts: dict[str, int] = {}
            for assessment in assessments:
                label = result_labels[assessment.result.value]
                counts[label] = counts.get(label, 0) + 1
            distribution = "、".join(f"{label}{count}个" for label, count in counts.items())
            summary = f"{layer.value} 层形成 {len(assessments)} 个直接 Signal 锚点评估：{distribution}。"
        return (
            LayerAnalysisResult(
                layer=layer,
                assessments=assessments,
                # Preserve the bounded graph instances used by this layer so
                # later cross-layer mechanism references remain auditable.
                supporting_facts=list(audit_facts_by_id.values())[:1200],
                summary=summary,
                limitations=list(dict.fromkeys(limitations))[:20],
                retrieval_receipts=receipts,
                retrieval_rounds=rounds,
            ),
            context,
        )

    async def _reason_layer(self, context: LayerAnalysisContext) -> LayerAssessmentBatch:
        if len(context.anchors) <= LAYER_REASONING_BATCH_SIZE:
            return await self._reason_layer_once(context)

        batches = []
        for offset in range(0, len(context.anchors), LAYER_REASONING_BATCH_SIZE):
            anchors = context.anchors[offset : offset + LAYER_REASONING_BATCH_SIZE]
            anchor_uuids = {item.uuid for item in anchors}
            anchor_ids = {item.business_id for item in anchors}
            facts = [
                fact
                for fact in context.facts
                if fact.source_uuid in anchor_uuids
                or fact.target_uuid in anchor_uuids
                or fact.source_business_id in anchor_ids
                or fact.target_business_id in anchor_ids
            ]
            fact_ids = {item.uuid for item in facts}
            direct_signal_fact_ids = [item for item in context.direct_signal_fact_ids if item in fact_ids]
            receipt = context.retrieval_receipt.model_copy(
                update={
                    "anchor_ids": [item.business_id for item in anchors],
                    "fact_ids": [item.uuid for item in facts],
                    "direct_signal_fact_ids": direct_signal_fact_ids,
                }
            )
            batches.append(
                context.model_copy(
                    update={
                        "anchors": anchors,
                        "facts": facts,
                        "direct_signal_fact_ids": direct_signal_fact_ids,
                        "retrieval_receipt": receipt,
                    }
                )
            )
        results = await asyncio.gather(*(self._reason_layer_once(batch) for batch in batches))
        proposals_by_anchor = {proposal.anchor_id: proposal for result in results for proposal in result.proposals}
        summaries = list(dict.fromkeys(result.summary for result in results if result.summary))
        return LayerAssessmentBatch(
            proposals=list(proposals_by_anchor.values()),
            supplemental_queries=list(
                dict.fromkeys(query for result in results for query in result.supplemental_queries)
            )[:4],
            summary="\n".join(summaries)[:1600] or "分批分析完成。",
            limitations=list(dict.fromkeys(item for result in results for item in result.limitations))[:20],
        )

    async def _reason_layer_once(self, context: LayerAnalysisContext) -> LayerAssessmentBatch:
        layer_rules = {
            ImpactLayer.GEOPOLITICAL: ("只解读已检索的 GeopoliticRivalry 锚点及其直接 Signal。"),
            ImpactLayer.MACRO_ECONOMIC: (
                "只解读已检索的 MacroEconomic 锚点及其直接 Signal；地缘结果仅作为后续跨层传导的上下文。"
            ),
            ImpactLayer.INDUSTRY: (
                "只解读已检索的 ChainNode 直接 Signal。IndustryChain 是节点集合视图，不拥有直接 Signal 或单独评估。"
            ),
        }[context.layer]
        context_payload = context.model_dump(mode="json")
        payload = {
            "ontology": context_payload.pop("ontology"),
            "retrieval_receipt": context_payload.pop("retrieval_receipt"),
            "instances": context_payload,
        }
        prompt = (
            "你在固定的分层投研 Workflow 中解读单层图谱数据，只返回 LayerAssessmentBatch。"
            "ontology 定义数据概念与关系，instances 是本次检索实例。"
            "不得发明 Anchor、Fact、Event 或 Variable ID。每个存在直接 Signal 的锚点应输出一条综合评估；"
            "Signal Fact 和 Event 引用由 Workflow 自动绑定，你不需要抄写这些 ID。"
            "Signal 方向是变量方向，需结合 Variable 定义综合为升温、降温、分化或无显著变化。"
            "如果仅缺少少量明确事实，可给出最多4条 supplemental_queries；第二轮后不得继续请求。"
            f"本层规则：{layer_rules}\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        return await self._run(self._reasoner, prompt, LayerAssessmentBatch)

    async def _analyze_cross_layer(
        self,
        context: LayerAnalysisContext,
        source_assessments: list[LayerAssessment],
        target_assessments: list[LayerAssessment],
    ) -> CrossLayerAnalysisResult:
        if not source_assessments or not target_assessments:
            return CrossLayerAnalysisResult(
                target_layer=context.layer,
                limitations=["NO_CLOSED_CROSS_LAYER_ASSESSMENT_PAIR"],
            )
        payload = {
            "target_layer": context.layer.value,
            "ontology": context.ontology.model_dump(mode="json"),
            "retrieval_receipt": context.retrieval_receipt.model_dump(mode="json"),
            "source_assessments": [item.model_dump(mode="json") for item in source_assessments],
            "target_assessments": [item.model_dump(mode="json") for item in target_assessments],
            "ordinary_facts": [item.model_dump(mode="json") for item in context.facts if item.kind == "ORDINARY"],
        }
        prompt = (
            "识别上层锚点评估如何影响本层锚点评估，只返回 CrossLayerTransmissionBatch。"
            "source_assessment_id 和 target_assessment_id 只能引用输入；"
            "mechanism_fact_ids 只能引用输入中的普通 Fact。"
            "同一 Event 在两层分别产生直接 Signal 只能说明同源影响，不能写成因果桥梁；"
            "仍可作为并行背景说明。普通 Fact 能解释机制时引用；"
            "无普通 Fact 但逻辑合理时保留为低置信度待验证传导，不得发明锚点。\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        target_batches = [
            target_assessments[offset : offset + LAYER_REASONING_BATCH_SIZE]
            for offset in range(0, len(target_assessments), LAYER_REASONING_BATCH_SIZE)
        ]

        async def analyze_batch(target_batch: list[LayerAssessment]) -> CrossLayerAnalysisResult:
            batch_payload = {
                **payload,
                "target_assessments": [item.model_dump(mode="json") for item in target_batch],
            }
            batch_prompt = (
                prompt.rsplit("\n", 1)[0]
                + "\n"
                + json.dumps(
                    batch_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            batch = await self._run(self._reasoner, batch_prompt, CrossLayerTransmissionBatch)
            return InvestmentReasoningEngine.validate_cross_layer_batch(
                context,
                source_assessments,
                target_batch,
                batch,
            )

        results = await asyncio.gather(*(analyze_batch(target_batch) for target_batch in target_batches))
        combined = CrossLayerAnalysisResult(
            target_layer=context.layer,
            accepted=[item for result in results for item in result.accepted],
            candidates=[item for result in results for item in result.candidates],
            limitations=list(dict.fromkeys(item for result in results for item in result.limitations))[:20],
        )
        return await self._review_and_repair_cross_layer(
            context,
            source_assessments,
            target_assessments,
            combined,
        )

    async def _review_transmission_semantics(
        self,
        *,
        transmission_kind: str,
        transmissions: list[AcceptedTransmission] | list[Any],
        context_payload: dict[str, Any],
    ) -> list[TransmissionSemanticIssue]:
        """Review accepted hypotheses locally; malformed review output never aborts the run."""

        if not transmissions:
            return []
        calls = []
        for offset in range(0, len(transmissions), TRANSMISSION_REVIEW_BATCH_SIZE):
            batch = transmissions[offset : offset + TRANSMISSION_REVIEW_BATCH_SIZE]
            payload = {
                "transmission_kind": transmission_kind,
                "review_rules": {
                    "scope": "只审核输入中的传导假设，不新增结论",
                    "semantic_chain": "源变量及方向 -> 明确经济机制 -> 目标变量及方向",
                    "topology_rule": "拓扑 flow 只表示遍历方向，不代表变量涨跌方向",
                    "same_source_rule": "同源 Signal 可以并列影响两层，但不能伪装成因果桥梁",
                    "causal_asymmetry_rule": (
                        "下游终端需求可以拉动上游组件需求；上游组件需求上升不能反向证明下游终端需求上升"
                    ),
                    "profit_rule": "上游价格或利润改善不等于下游利润改善，必须说明成本转嫁或供需机制",
                    "hop_rule": "多跳传导必须从上一跳的目标变量出发，不得跳过中间节点直接复用根信号结论",
                    "confidence_rule": "低置信度本身不是错误",
                    "output_rule": "只返回存在具体语义矛盾的 transmission_id",
                },
                "context": context_payload,
                "accepted_transmissions": [item.model_dump(mode="json") for item in batch],
            }
            prompt = (
                "你是传导路径的局部语义审核员，只返回 TransmissionSemanticReview。"
                "逐条检查源变量、机制、目标变量与方向能否组成连贯的经济命题。"
                "技术成熟度、商业化进度、市场需求等不同变量之间必须写出桥梁；"
                "重点排除组件需求反推终端需求、上游利润直接复制到下游，以及多跳路径跳过中间变量的情况；"
                "重复使用同一根证据只有在目标机制不同且逻辑成立时才允许。"
                "不要因为置信度低、待验证或缺少额外材料而报错；只列出实际矛盾。\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            calls.append(self._run(self._reviewer, prompt, TransmissionSemanticReview))
        reviews = await asyncio.gather(*calls)
        known_ids = {item.transmission_id for item in transmissions}
        by_id: dict[str, TransmissionSemanticIssue] = {}
        for review in reviews:
            for issue in review.issues:
                if issue.transmission_id in known_ids:
                    by_id.setdefault(issue.transmission_id, issue)
        return list(by_id.values())

    async def _review_and_repair_cross_layer(
        self,
        context: LayerAnalysisContext,
        source_assessments: list[LayerAssessment],
        target_assessments: list[LayerAssessment],
        result: CrossLayerAnalysisResult,
    ) -> CrossLayerAnalysisResult:
        if not result.accepted:
            return result
        context_payload = {
            "ontology": context.ontology.model_dump(mode="json"),
            "source_assessments": [item.model_dump(mode="json") for item in source_assessments],
            "target_assessments": [item.model_dump(mode="json") for item in target_assessments],
            "ordinary_facts": [item.model_dump(mode="json") for item in context.facts if item.kind == "ORDINARY"],
        }
        issues = await self._review_transmission_semantics(
            transmission_kind="CROSS_LAYER",
            transmissions=result.accepted,
            context_payload=context_payload,
        )
        if not issues:
            return result
        issue_by_id = {item.transmission_id: item for item in issues}
        clean = [item for item in result.accepted if item.transmission_id not in issue_by_id]
        flagged = [item for item in result.accepted if item.transmission_id in issue_by_id]
        allowed_pairs = {(item.source_assessment_id, item.target_assessment_id) for item in flagged}
        repair_payload = {
            **context_payload,
            "rejected_transmissions": [item.model_dump(mode="json") for item in flagged],
            "review_issues": [item.model_dump(mode="json") for item in issues],
        }
        repair_prompt = (
            "修复被局部审核指出矛盾的跨层传导，只返回 CrossLayerTransmissionBatch。"
            "source_assessment_id 和 target_assessment_id 必须保持原配对；不得新增配对或 ID。"
            "只改 logic、confidence、status 和必要的 mechanism_fact_ids。"
            "无法形成连贯经济机制的传导直接省略。\n"
            + json.dumps(repair_payload, ensure_ascii=False, separators=(",", ":"))
        )
        repair_batch = await self._run(self._reasoner, repair_prompt, CrossLayerTransmissionBatch)
        repair_batch = repair_batch.model_copy(
            update={
                "proposals": [
                    item
                    for item in repair_batch.proposals
                    if (item.source_assessment_id, item.target_assessment_id) in allowed_pairs
                ]
            }
        )
        repaired_result = InvestmentReasoningEngine.validate_cross_layer_batch(
            context,
            source_assessments,
            target_assessments,
            repair_batch,
        )
        second_issues = await self._review_transmission_semantics(
            transmission_kind="CROSS_LAYER_REPAIR",
            transmissions=repaired_result.accepted,
            context_payload=context_payload,
        )
        still_invalid = {item.transmission_id for item in second_issues}
        repaired = [item for item in repaired_result.accepted if item.transmission_id not in still_invalid]
        dropped = max(0, len(flagged) - len(repaired))
        dropped_candidates = [
            CandidateCrossLayerMechanism(
                **item.model_dump(exclude={"transmission_id", "source_layer", "target_layer", "relation_type"}),
                reason="局部语义审核后仍无法形成一致的跨层传导，已从下游推理输入剔除。",
            )
            for item in flagged
            if not any(
                repaired_item.source_assessment_id == item.source_assessment_id
                and repaired_item.target_assessment_id == item.target_assessment_id
                for repaired_item in repaired
            )
        ]
        limitations = [*result.limitations, f"LOCAL_SEMANTIC_ISSUES:{len(issues)}"]
        if repaired:
            limitations.append(f"LOCAL_SEMANTIC_REPAIRED:{len(repaired)}")
        if dropped:
            limitations.append(f"LOCAL_SEMANTIC_DROPPED:{dropped}")
        return result.model_copy(
            update={
                "accepted": [*clean, *repaired],
                "candidates": [*result.candidates, *repaired_result.candidates, *dropped_candidates],
                "limitations": list(dict.fromkeys(limitations))[:20],
            }
        )

    async def _review_and_repair_node_round(
        self,
        context: InvestmentAnalysisContext,
        root_assessments: list[LayerAssessment],
        accepted: list[AcceptedTransmission],
        candidates: list[TransmissionCandidate],
        items: list[AcceptedTransmission],
        *,
        round_number: int,
    ) -> tuple[list[AcceptedTransmission], int, int, int]:
        if not items:
            return [], 0, 0, 0
        candidate_by_id = {item.candidate_id: item for item in candidates}
        relevant_fact_ids = {
            fact_id for item in items for fact_id in [*item.source_fact_ids, *item.root_signal_fact_ids]
        }
        context_payload = {
            "round_number": round_number,
            "ontology": context.ontology.model_dump(mode="json"),
            "facts": [item.model_dump(mode="json") for item in context.facts if item.uuid in relevant_fact_ids],
            "root_assessments": [item.model_dump(mode="json") for item in root_assessments],
            "parent_transmissions": [item.model_dump(mode="json") for item in accepted],
            "transmission_candidates": [item.model_dump(mode="json") for item in candidates],
        }
        issues = await self._review_transmission_semantics(
            transmission_kind="INDUSTRY_TOPOLOGY",
            transmissions=items,
            context_payload=context_payload,
        )
        if not issues:
            return items, 0, 0, 0
        issue_by_id = {item.transmission_id: item for item in issues}
        clean = [item for item in items if item.transmission_id not in issue_by_id]
        flagged = [item for item in items if item.transmission_id in issue_by_id]
        flagged_candidate_ids = {
            item.candidate_id
            for item in flagged
            if item.candidate_id is not None and item.candidate_id in candidate_by_id
        }
        repair_payload = {
            **context_payload,
            "rejected_transmissions": [item.model_dump(mode="json") for item in flagged],
            "review_issues": [item.model_dump(mode="json") for item in issues],
        }
        repair_prompt = (
            "修复被局部审核指出矛盾的产业链节点传导，只返回 TransmissionBatch。"
            "candidate_id、节点、边、flow、周期和谱系必须保持冻结；"
            "只改 target_variable、direction、confidence、mechanism 和 assumptions。"
            "无法形成连贯经济机制的候选直接省略。\n"
            + json.dumps(repair_payload, ensure_ascii=False, separators=(",", ":"))
        )
        repair_batch = await self._run(self._reasoner, repair_prompt, TransmissionBatch)
        repair_batch = repair_batch.model_copy(
            update={
                "proposals": [item for item in repair_batch.proposals if item.candidate_id in flagged_candidate_ids]
            }
        )
        flagged_candidates = [candidate_by_id[item] for item in flagged_candidate_ids]
        repaired_items = InvestmentReasoningEngine.validate_round(
            context,
            [*accepted, *clean],
            repair_batch,
            round_number=round_number,
            root_assessments=root_assessments,
            candidates=flagged_candidates,
        )
        second_issues = await self._review_transmission_semantics(
            transmission_kind="INDUSTRY_TOPOLOGY_REPAIR",
            transmissions=repaired_items,
            context_payload=context_payload,
        )
        still_invalid = {item.transmission_id for item in second_issues}
        repaired = [item for item in repaired_items if item.transmission_id not in still_invalid]
        dropped = max(0, len(flagged) - len(repaired))
        return [*clean, *repaired], len(issues), len(repaired), dropped

    async def analyze_industry(
        self,
        prepared: PreparedInvestmentContext,
        geopolitical: LayerAnalysisResult,
        macro: LayerAnalysisResult,
        macro_transmission: CrossLayerAnalysisResult | None = None,
    ) -> IndustryAnalysisState:
        parents = [*geopolitical.assessments, *macro.assessments]
        industry, layer_context = await self._analyze_layer(prepared, ImpactLayer.INDUSTRY, parents)
        industry_transmission = await self._analyze_cross_layer(layer_context, parents, industry.assessments)
        industry_context = await self._provider.expand_industry_context(
            prepared.context,
            layer_context,
            industry.assessments,
        )
        audit_facts = [
            *geopolitical.supporting_facts,
            *macro.supporting_facts,
            *industry.supporting_facts,
        ]
        facts_by_id = {item.uuid: item for item in [*audit_facts, *industry_context.facts]}
        macro_cross_layer = macro_transmission or CrossLayerAnalysisResult(target_layer=ImpactLayer.MACRO_ECONOMIC)
        mechanism_fact_ids = list(
            dict.fromkeys(
                fact_id
                for transmission in [*macro_cross_layer.accepted, *industry_transmission.accepted]
                for fact_id in transmission.mechanism_fact_ids
            )
        )
        prioritized_facts = [facts_by_id[fact_id] for fact_id in mechanism_fact_ids if fact_id in facts_by_id]
        prioritized_ids = {item.uuid for item in prioritized_facts}
        prioritized_facts.extend(item for item in facts_by_id.values() if item.uuid not in prioritized_ids)
        receipts_by_identity = {
            (item.stage, item.retrieval_round, tuple(item.required_actions)): item
            for item in [
                *industry_context.retrieval_receipts,
                *geopolitical.retrieval_receipts,
                *macro.retrieval_receipts,
                *industry.retrieval_receipts,
            ]
        }
        industry_context = industry_context.model_copy(
            update={
                "facts": prioritized_facts[:2000],
                "retrieval_receipts": list(receipts_by_identity.values())[:10],
            }
        )
        accepted: list[AcceptedTransmission] = []
        rounds = 0
        candidates_enumerated = 0
        candidates_evaluated = 0
        rejected_below_inclusion = 0
        stopped_by_confidence = 0
        stopped_by_no_unvisited_neighbor = 0
        semantic_review_issues = 0
        semantic_repaired = 0
        semantic_dropped = 0
        if any(chain.signal_root_node_ids for chain in industry_context.chains):
            for round_number in range(1, industry_context.request.max_hops + 1):
                candidates = InvestmentReasoningEngine.enumerate_transmission_candidates(
                    industry_context,
                    accepted,
                    round_number=round_number,
                    root_assessments=industry.assessments,
                )
                if not candidates:
                    has_continuable_frontier = round_number == 1 or any(
                        item.hop == round_number - 1
                        and item.path_score >= InvestmentReasoningEngine.TRANSMISSION_CONTINUATION_THRESHOLD
                        for item in accepted
                    )
                    if has_continuable_frontier:
                        stopped_by_no_unvisited_neighbor += 1
                    break
                candidates_enumerated += len(candidates)
                batch = await self._propagate(
                    industry_context,
                    industry.assessments,
                    accepted,
                    candidates,
                    round_number=round_number,
                )
                rounds += 1
                # Every enumerated candidate was included in the Agent's fixed
                # evaluation set. Omitted proposals are explicit rejections.
                candidates_evaluated += len(candidates)
                new_items = InvestmentReasoningEngine.validate_round(
                    industry_context,
                    accepted,
                    batch,
                    round_number=round_number,
                    root_assessments=industry.assessments,
                    candidates=candidates,
                )
                new_items, round_issues, round_repaired, round_dropped = await self._review_and_repair_node_round(
                    industry_context,
                    industry.assessments,
                    accepted,
                    candidates,
                    new_items,
                    round_number=round_number,
                )
                semantic_review_issues += round_issues
                semantic_repaired += round_repaired
                semantic_dropped += round_dropped
                accepted.extend(new_items)
                rejected_below_inclusion += max(0, len(batch.proposals) - len(new_items))
                stopped_by_confidence += sum(
                    item.path_score < InvestmentReasoningEngine.TRANSMISSION_CONTINUATION_THRESHOLD
                    for item in new_items
                )
        draft = await self._synthesize(industry_context, industry.assessments, accepted)
        draft = InvestmentReasoningEngine.normalize_draft(
            industry_context,
            accepted,
            draft,
            industry.assessments,
        )
        return IndustryAnalysisState(
            prepared=prepared,
            geopolitical=geopolitical,
            macro=macro,
            industry=industry,
            macro_transmission=macro_transmission or CrossLayerAnalysisResult(target_layer=ImpactLayer.MACRO_ECONOMIC),
            industry_transmission=industry_transmission,
            industry_context=industry_context,
            transmissions=accepted,
            rounds_executed=rounds,
            transmission_metrics=TransmissionExecutionMetrics(
                candidates_enumerated=candidates_enumerated,
                candidates_evaluated=candidates_evaluated,
                accepted=len(accepted),
                rejected_below_inclusion=rejected_below_inclusion,
                stopped_by_confidence=stopped_by_confidence,
                stopped_by_no_unvisited_neighbor=stopped_by_no_unvisited_neighbor,
                semantic_review_issues=semantic_review_issues,
                semantic_repaired=semantic_repaired,
                semantic_dropped=semantic_dropped,
            ),
            draft=draft,
            execution_issues=([f"LOCAL_SEMANTIC_REPAIRED:{semantic_repaired}"] if semantic_repaired else [])
            + ([f"LOCAL_SEMANTIC_DROPPED:{semantic_dropped}"] if semantic_dropped else []),
        )

    async def _propagate(
        self,
        context: InvestmentAnalysisContext,
        industry_assessments: list[LayerAssessment],
        accepted: list[AcceptedTransmission],
        candidates: list[TransmissionCandidate],
        *,
        round_number: int,
    ) -> TransmissionBatch:
        calls = []
        for chain in context.chains:
            chain_candidates = [item for item in candidates if item.chain_id == chain.business_id]
            if not chain_candidates:
                continue
            chain_accepted = [item for item in accepted if item.chain_id == chain.business_id]
            chain_assessments = [
                item
                for item in industry_assessments
                if item.anchor_id == chain.business_id
                or any(node.business_id == item.anchor_id for node in chain.nodes)
            ]
            if round_number == 1 and not chain.signal_root_node_ids:
                continue
            if round_number > 1 and not chain_accepted:
                continue
            node_ids = {item.business_id for item in chain.nodes}
            relevant_facts = [
                fact
                for fact in context.facts
                if fact.source_business_id in node_ids | {chain.business_id}
                or fact.target_business_id in node_ids | {chain.business_id}
            ]
            payload = {
                "round_number": round_number,
                "question": context.request.question,
                "ontology": context.ontology.model_dump(mode="json"),
                "chain": chain.model_dump(mode="json"),
                "facts": [item.model_dump(mode="json") for item in relevant_facts],
                "industry_assessments": [item.model_dump(mode="json") for item in chain_assessments],
                "accepted_transmissions": [item.model_dump(mode="json") for item in chain_accepted],
                "transmission_candidates": [item.model_dump(mode="json") for item in chain_candidates],
            }
            prompt = (
                f"执行第 {round_number} 轮产业链拓扑传导，只返回 TransmissionBatch。"
                "必须逐条评估 transmission_candidates；只有机制成立的候选才返回 proposal，"
                "并原样复制 candidate_id 以及其中全部节点、边、方向、周期和谱系 ID。"
                "你只补充 target_variable、direction、confidence、mechanism 和 assumptions。"
                "不得自行选择候选之外的节点或边；不成立的候选直接省略。\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            calls.append(self._run(self._reasoner, prompt, TransmissionBatch))
        batches = await asyncio.gather(*calls) if calls else []
        return TransmissionBatch(
            proposals=[proposal for batch in batches for proposal in batch.proposals],
            stopped_reason=_bounded_text(
                ";".join(batch.stopped_reason for batch in batches if batch.stopped_reason),
                limit=500,
            ),
        )

    async def _synthesize(
        self,
        context: InvestmentAnalysisContext,
        industry_assessments: list[LayerAssessment],
        transmissions: list[AcceptedTransmission],
        repair_issues: list[str] | None = None,
    ) -> AnalysisDraft:
        if not context.chains:
            return AnalysisDraft(
                one_sentence_conclusion="当前事件与上层结论没有召回可验证的标准产业链。",
                limitations=["NO_INDUSTRY_CHAIN_CONTEXT"],
            )
        if not industry_assessments and not any(chain.signal_root_fact_ids for chain in context.chains):
            return AnalysisDraft(
                one_sentence_conclusion="当前事件可召回相关产业链，但没有有效 Signal 谱系支持方向性结论。",
                chains=[InvestmentReasoningEngine.insufficient_chain(chain) for chain in context.chains],
                limitations=["NO_ELIGIBLE_INDUSTRY_SIGNAL_ROOT"],
            )

        calls = []
        for chain in context.chains:
            node_ids = {item.business_id for item in chain.nodes}
            facts = [
                fact
                for fact in context.facts
                if fact.source_business_id in node_ids | {chain.business_id}
                or fact.target_business_id in node_ids | {chain.business_id}
            ]
            assessments = [
                item
                for item in industry_assessments
                if item.anchor_id == chain.business_id or item.anchor_id in node_ids
            ]
            chain_transmissions = [item for item in transmissions if item.chain_id == chain.business_id]
            payload = {
                "question": context.request.question,
                "ontology": context.ontology.model_dump(mode="json"),
                "chain": chain.model_dump(mode="json"),
                "facts": [item.model_dump(mode="json") for item in facts],
                "industry_assessments": [item.model_dump(mode="json") for item in assessments],
                "accepted_transmissions": [item.model_dump(mode="json") for item in chain_transmissions],
                "review_repair_issues": list(repair_issues or []),
            }
            prompt = (
                "输出 NodeAnalysisBatch，覆盖产业链所有真实节点。只有直接有效 Signal、作用于该节点的"
                "Industry Assessment，或有可追溯 Signal 根的 Transmission 才能形成方向结论；"
                "分别填写 supporting_fact_ids、supporting_assessment_ids、supporting_transmission_ids。"
                "其余节点必须 INSUFFICIENT_EVIDENCE，不得发明ID。\n"
                "rationale 只写面向投研读者的业务事实和传导逻辑，不得出现内部 ID、英文枚举、"
                "flow 或框架对象名。\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            calls.append((chain, self._run(self._reasoner, prompt, NodeAnalysisBatch)))
        batches = await asyncio.gather(*(item[1] for item in calls))
        chains: list[ChainTrendView] = []
        for (chain, _), batch in zip(calls, batches, strict=True):
            by_id = {item.node_id: item for item in batch.nodes}
            nodes = [
                by_id.get(node.business_id)
                or InvestmentReasoningEngine.insufficient_node(chain, node.business_id, node.name)
                for node in chain.nodes
            ]
            chains.append(
                ChainTrendView(
                    chain_id=chain.business_id,
                    chain_name=chain.name,
                    short=InvestmentReasoningEngine._reduce_trend([item.short for item in nodes]),
                    medium=InvestmentReasoningEngine._reduce_trend([item.medium for item in nodes]),
                    long=InvestmentReasoningEngine._reduce_trend([item.long for item in nodes]),
                    confidence=max(
                        (item.confidence for item in nodes),
                        key={Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}.__getitem__,
                        default=Confidence.LOW,
                    ),
                    summary="；".join(item.rationale for item in nodes)[:1600],
                    nodes=nodes,
                )
            )
        return AnalysisDraft(
            one_sentence_conclusion="；".join(f"{item.chain_name}：{item.summary}" for item in chains)[:2000],
            chains=chains,
            limitations=list(dict.fromkeys(context.validation_issues))[:20],
        )

    async def repair(self, state: IndustryAnalysisState, review: ReviewResult) -> IndustryAnalysisState:
        """Repair aggregate summaries once without reopening accepted graph identities."""

        async def repair_layer(result: LayerAnalysisResult) -> LayerAnalysisResult:
            if not result.assessments:
                return result
            payload = {
                "layer": result.layer.value,
                "current_summary": result.summary,
                "assessments": [item.model_dump(mode="json") for item in result.assessments],
                "review_issues": review.issue_codes,
            }
            prompt = (
                "修复一个分层分析摘要，只返回 LayerAssessmentBatch。proposals 和 supplemental_queries 必须为空；"
                "summary 必须完整覆盖输入 assessments，不能声称已存在的锚点或方向不存在，"
                "也不能新增锚点、Signal、Fact 或结论。limitations 只保留必要边界。\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            repaired = await self._run(self._reasoner, prompt, LayerAssessmentBatch)
            return result.model_copy(
                update={
                    "summary": repaired.summary,
                    "limitations": list(dict.fromkeys([*result.limitations, *repaired.limitations]))[:20],
                }
            )

        geopolitical, macro, industry = await asyncio.gather(
            repair_layer(state.geopolitical),
            repair_layer(state.macro),
            repair_layer(state.industry),
        )

        draft = await self._synthesize(
            state.industry_context,
            industry.assessments,
            state.transmissions,
            repair_issues=review.issue_codes,
        )
        normalized = InvestmentReasoningEngine.normalize_draft(
            state.industry_context,
            state.transmissions,
            draft,
            industry.assessments,
        )
        return state.model_copy(
            update={
                "geopolitical": geopolitical,
                "macro": macro,
                "industry": industry,
                "draft": normalized,
                "execution_issues": list(
                    dict.fromkeys([*state.execution_issues, *review.issue_codes, "SEMANTIC_REPAIR_EXECUTED"])
                ),
            }
        )

    async def review(self, state: IndustryAnalysisState) -> ReviewResult:
        def compact_layer(result: LayerAnalysisResult) -> dict[str, Any]:
            return {
                "layer": result.layer.value,
                "summary": result.summary,
                "limitations": result.limitations,
                "assessments": [
                    {
                        "assessment_id": item.assessment_id,
                        "anchor_id": item.anchor_id,
                        "anchor_name": item.anchor_name,
                        "result": item.result.value,
                        "confidence": item.confidence.value,
                        "summary": item.summary,
                        "reasoning": item.reasoning,
                    }
                    for item in result.assessments
                ],
            }

        payload = {
            "deterministic_audit": {
                "retrieval_receipts_complete": True,
                "references_scoped_to_context": True,
                "note": "ID、检索动作与谱系已由确定性门禁验证，Reviewer 不重复校验图谱事实。",
            },
            "geopolitical": compact_layer(state.geopolitical),
            "macro": compact_layer(state.macro),
            "industry": compact_layer(state.industry),
            "cross_layer_transmissions": [
                *[item.model_dump(mode="json") for item in state.macro_transmission.accepted],
                *[item.model_dump(mode="json") for item in state.industry_transmission.accepted],
            ],
            "draft": state.draft.model_dump(mode="json"),
        }
        prompt = (
            "审核该分层投研 Workflow 的整体执行完整性，不得补充新结论。"
            "检查必需检索动作是否完成、输出引用是否来自本次上下文、"
            "直接 Signal 与传导假设是否分开，并检查节点结论、链级聚合与最终摘要是否整体一致。"
            "跨层和节点 Transmission 已在各推理轮内部完成逐条审核、修复或剔除；"
            "最终审核不得仅因某条路径置信度低或仍待验证而否决整份报告。"
            "只有保留下来的输出仍造成实际的全局方向或聚合矛盾时，才 accepted=false 并返回 "
            "REASONING_INCONSISTENCY；孤立备注应 accepted=true 并记录 issue。\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        return await self._run(self._reviewer, prompt, ReviewResult)

    async def write_and_edit_report(self, report: InvestmentReportArtifact) -> InvestmentReportArtifact:
        """Let Agents write report prose while the immutable report contract owns all business fields."""

        async def rewrite(
            source: InvestmentReportArtifact,
            agent: Agent,
            instruction: str,
        ) -> InvestmentReportArtifact:
            fields = extract_report_narratives(source)
            if not fields:
                return source
            calls = []
            for offset in range(0, len(fields), REPORT_NARRATIVE_BATCH_SIZE):
                chunk = fields[offset : offset + REPORT_NARRATIVE_BATCH_SIZE]
                prompt = (
                    instruction + "只返回 ReportNarrativeBatch，rewrites 对输入中的每个 key 返回一次；"
                    "key 必须原样保留，text 只写最终中文文案。"
                    "locked_business_fields 是不可改变的方向、时间窗口、置信度、依据性质和 Evidence 引用。"
                    "不得新增事实、锚点、传导关系或投资结论。\n"
                    + json.dumps(
                        {"fields": [field.prompt_payload() for field in chunk]},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                calls.append(self._run(agent, prompt, ReportNarrativeBatch))
            batches = await asyncio.gather(*calls)
            rewritten = source
            for batch in batches:
                rewritten = apply_report_narratives(rewritten, fields, batch)
            return rewritten

        written = await rewrite(
            report,
            self._report_writer,
            "以专业金融分析师面向普通投研读者的方式重写报告字段。"
            "保留明确业务含义，解释事实、影响和传导，不使用系统实现或调试口吻。"
            "role 为传导逻辑或推导说明时，只用 1 至 2 句话表达原因、影响机制和结果，"
            "不重复事件背景、Evidence 清单、置信度或结论摘要。",
        )
        return await rewrite(
            written,
            self._reviewer,
            "直接润色报告中机械、生硬或技术化的表达。"
            "role 为传导逻辑或推导说明时，将仍然重复或冗长的文字压缩为 1 至 2 句最简单的因果说明。"
            "保持原结论、条件、不确定性及强弱边界，不解释审核过程，不输出修改说明。",
        )

    async def close(self) -> None:
        await self._graphiti.close()

    async def publish_report(self, request: ReportPublicationRequest) -> ReportPublicationReceipt:
        if self._report_publisher is None:
            raise RuntimeError("Investment Report publisher is not configured")
        return await self._report_publisher.publish(request)


def create_local_investment_workflow_runtime(
    model: Any,
    reasoner: Agent,
    report_writer: Agent,
    reviewer: Agent,
    report_publisher: ReportPublisher,
) -> LocalInvestmentWorkflowRuntime:
    """Compose one app-owned Graphiti client with the published Agent components."""

    return LocalInvestmentWorkflowRuntime(
        create_agentos_graphiti(model),
        reasoner,
        reviewer,
        report_writer,
        report_publisher,
    )
