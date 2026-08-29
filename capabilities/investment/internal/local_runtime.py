"""Compose AgentOS Agents with Graphiti's layered retrieval surface."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agno.agent import Agent
from pydantic import BaseModel

from capabilities.investment.internal.context import InvestmentContextBuilder
from capabilities.investment.internal.engine import InvestmentReasoningEngine
from capabilities.investment.internal.models import (
    AcceptedImpactClaim,
    AcceptedTransmission,
    AnalysisDraft,
    ChainTrendView,
    Confidence,
    ImpactLayer,
    IndustryAnalysisState,
    InvestmentAnalysisContext,
    InvestmentAnalysisRequest,
    LayerAnalysisContext,
    LayerAnalysisResult,
    LayerImpactBatch,
    NodeAnalysisBatch,
    PreparedInvestmentContext,
    ReviewResult,
    TransmissionBatch,
)
from sematica.graphiti.investment import GraphitiInvestmentReader
from sematica.graphiti.runtime import create_agentos_graphiti


def _payload[ModelT: BaseModel](value: Any, model: type[ModelT]) -> ModelT:
    content = getattr(value, "content", value)
    if isinstance(content, model):
        return content
    if isinstance(content, str):
        return model.model_validate_json(content)
    return model.model_validate(content)


class LocalInvestmentWorkflowRuntime:
    """Run bounded model calls while deterministic Functions own state transitions."""

    def __init__(self, graphiti, reasoner: Agent, reviewer: Agent) -> None:
        self._graphiti = graphiti
        self._provider = InvestmentContextBuilder(GraphitiInvestmentReader(graphiti))
        self._reasoner = reasoner
        self._reviewer = reviewer
        self._slots = asyncio.Semaphore(3)

    async def prepare(self, request: InvestmentAnalysisRequest) -> InvestmentAnalysisContext:
        return await self._provider.build(request)

    async def _run[ModelT: BaseModel](self, agent: Agent, prompt: str, model: type[ModelT]) -> ModelT:
        call_agent = agent.deep_copy(update={"db": None})
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
    ) -> LayerAnalysisResult:
        result, _ = await self._analyze_layer(
            prepared,
            ImpactLayer.MACRO_ECONOMIC,
            geopolitical.claims,
        )
        return result

    async def _analyze_layer(
        self,
        prepared: PreparedInvestmentContext,
        layer: ImpactLayer,
        parent_claims: list[AcceptedImpactClaim],
    ) -> tuple[LayerAnalysisResult, LayerAnalysisContext]:
        context = await self._provider.build_layer_context(prepared.context, layer, parent_claims)
        audit_facts_by_id = {fact.uuid: fact for fact in context.facts}
        if not context.anchors:
            return (
                LayerAnalysisResult(
                    layer=layer,
                    claims=[],
                    supporting_facts=[],
                    summary=f"{layer.value} 层未召回标准锚点，因此不形成方向结论。",
                    limitations=["NO_STANDARD_ANCHOR_CANDIDATE"],
                ),
                context,
            )
        first = await self._reason_layer(context)
        accepted = InvestmentReasoningEngine.validate_layer_batch(
            context,
            parent_claims,
            first,
            layer=layer,
        )
        limitations = list(first.limitations)
        summary = first.summary
        rounds = 1
        second_had_proposals = False
        if first.supplemental_queries:
            second_context = await self._provider.build_layer_context(
                prepared.context,
                layer,
                parent_claims,
                supplemental_queries=first.supplemental_queries,
                retrieval_round=2,
            )
            second = await self._reason_layer(second_context)
            audit_facts_by_id.update({fact.uuid: fact for fact in second_context.facts})
            second_had_proposals = bool(second.proposals)
            second_accepted = InvestmentReasoningEngine.validate_layer_batch(
                second_context,
                parent_claims,
                second,
                layer=layer,
            )
            by_id = {item.claim_id: item for item in [*accepted, *second_accepted]}
            accepted = list(by_id.values())
            limitations.extend(second.limitations)
            summary = second.summary
            context = second_context
            rounds = 2
        if not accepted:
            had_proposals = bool(first.proposals) or second_had_proposals
            limitations = ["NO_ACCEPTED_SIGNAL_LINEAGE"]
            if had_proposals:
                limitations.append("PROPOSALS_REJECTED_BY_LINEAGE_GATE")
            summary = (
                f"{layer.value} 层没有通过确定性根谱门禁的方向结论；"
                "相关 Event、普通 Fact 或模型候选不能替代有效 Signal 与跨层机制证据。"
            )
        supporting_fact_ids = {
            fact_id
            for claim in accepted
            for fact_id in [
                *claim.source_fact_ids,
                *claim.mechanism_fact_ids,
                *claim.root_signal_fact_ids,
            ]
        }
        supporting_facts = [fact for fact_id, fact in audit_facts_by_id.items() if fact_id in supporting_fact_ids]
        return (
            LayerAnalysisResult(
                layer=layer,
                claims=accepted,
                supporting_facts=supporting_facts,
                summary=summary,
                limitations=list(dict.fromkeys(limitations))[:20],
                retrieval_rounds=rounds,
            ),
            context,
        )

    async def _reason_layer(self, context: LayerAnalysisContext) -> LayerImpactBatch:
        layer_rules = {
            ImpactLayer.GEOPOLITICAL: (
                "只分析预置 GeopoliticRivalry 锚点。方向结论必须引用直接有效 Signal；"
                "不得因为 Event 文字相关就强行归入不存在的地缘蓝图。"
            ),
            ImpactLayer.MACRO_ECONOMIC: (
                "只分析预置 MacroEconomic 锚点。可引用直接宏观 Signal；或引用已接受的地缘父结论，"
                "但后者必须同时引用把父层影响连接到宏观锚点的普通 mechanism Fact。"
            ),
            ImpactLayer.INDUSTRY: (
                "只分析预置 IndustryChain/ChainNode 锚点。可引用直接产业 Signal；或引用已接受的上层结论，"
                "但后者必须同时引用连接到产业锚点的普通 mechanism Fact。"
            ),
        }[context.layer]
        payload = context.model_dump(mode="json")
        prompt = (
            "你在固定的分层投研 Workflow 中执行单层判断，只返回 LayerImpactBatch。"
            "不得发明任何 Anchor、Fact、Event、Variable 或 Claim ID。"
            "source_fact_ids 只能引用给定的直接 Signal Fact；mechanism_fact_ids 只能引用给定普通 Fact；"
            "parent_claim_ids 只能引用给定父层结论。证据不足时 proposals 必须为空。"
            "直接Signal提案的 variable_id、direction、horizons 必须逐字采用所引 Signal 的对应字段，"
            "不得把 MEDIUM 改成 SHORT 或自行改写方向。"
            "如果仅缺少少量明确事实，可给出最多4条 supplemental_queries；第二轮后不得继续请求。"
            f"本层规则：{layer_rules}\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        return await self._run(self._reasoner, prompt, LayerImpactBatch)

    async def analyze_industry(
        self,
        prepared: PreparedInvestmentContext,
        geopolitical: LayerAnalysisResult,
        macro: LayerAnalysisResult,
    ) -> IndustryAnalysisState:
        parents = [*geopolitical.claims, *macro.claims]
        industry, layer_context = await self._analyze_layer(prepared, ImpactLayer.INDUSTRY, parents)
        industry_context = await self._provider.expand_industry_context(
            prepared.context,
            layer_context,
            industry.claims,
        )
        audit_facts = [
            *geopolitical.supporting_facts,
            *macro.supporting_facts,
            *industry.supporting_facts,
        ]
        facts_by_id = {item.uuid: item for item in [*audit_facts, *industry_context.facts]}
        industry_context = industry_context.model_copy(update={"facts": list(facts_by_id.values())[:2000]})
        accepted: list[AcceptedTransmission] = []
        rounds = 0
        if any(chain.signal_root_node_ids for chain in industry_context.chains):
            for round_number in range(1, industry_context.request.max_hops + 1):
                batch = await self._propagate(
                    industry_context,
                    industry.claims,
                    accepted,
                    round_number=round_number,
                )
                rounds += 1
                new_items = InvestmentReasoningEngine.validate_round(
                    industry_context,
                    accepted,
                    batch,
                    round_number=round_number,
                    root_claims=industry.claims,
                )
                accepted.extend(new_items)
                if not any(item.confidence != Confidence.LOW for item in new_items):
                    break
        draft = await self._synthesize(industry_context, industry.claims, accepted)
        draft = InvestmentReasoningEngine.normalize_draft(
            industry_context,
            accepted,
            draft,
            industry.claims,
        )
        return IndustryAnalysisState(
            prepared=prepared,
            geopolitical=geopolitical,
            macro=macro,
            industry=industry,
            industry_context=industry_context,
            transmissions=accepted,
            rounds_executed=rounds,
            draft=draft,
            execution_issues=[],
        )

    async def _propagate(
        self,
        context: InvestmentAnalysisContext,
        industry_claims: list[AcceptedImpactClaim],
        accepted: list[AcceptedTransmission],
        *,
        round_number: int,
    ) -> TransmissionBatch:
        calls = []
        for chain in context.chains:
            chain_accepted = [item for item in accepted if item.chain_id == chain.business_id]
            chain_claims = [
                item
                for item in industry_claims
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
                "chain": chain.model_dump(mode="json"),
                "facts": [item.model_dump(mode="json") for item in relevant_facts],
                "industry_claims": [item.model_dump(mode="json") for item in chain_claims],
                "accepted_transmissions": [item.model_dump(mode="json") for item in chain_accepted],
            }
            prompt = (
                f"执行第 {round_number} 轮产业链拓扑传导，只返回 TransmissionBatch。"
                "第1轮必须由作用于 source_node_id 的直接有效 Signal（source_fact_ids），"
                "或已接受 Industry ChainNode Claim（source_claim_ids）启动。"
                "后续轮只能从上一轮已接受 Transmission 的 target_node_id 继续。"
                "不得发明节点、边或ID；证据不足时返回空 proposals。\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            calls.append(self._run(self._reasoner, prompt, TransmissionBatch))
        batches = await asyncio.gather(*calls) if calls else []
        return TransmissionBatch(
            proposals=[proposal for batch in batches for proposal in batch.proposals],
            stopped_reason=";".join(batch.stopped_reason for batch in batches if batch.stopped_reason) or None,
        )

    async def _synthesize(
        self,
        context: InvestmentAnalysisContext,
        industry_claims: list[AcceptedImpactClaim],
        transmissions: list[AcceptedTransmission],
    ) -> AnalysisDraft:
        if not context.chains:
            return AnalysisDraft(
                one_sentence_conclusion="当前事件与上层结论没有召回可验证的标准产业链。",
                limitations=["NO_INDUSTRY_CHAIN_CONTEXT"],
            )
        if not industry_claims and not any(chain.signal_root_fact_ids for chain in context.chains):
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
            claims = [
                item for item in industry_claims if item.anchor_id == chain.business_id or item.anchor_id in node_ids
            ]
            chain_transmissions = [item for item in transmissions if item.chain_id == chain.business_id]
            payload = {
                "question": context.request.question,
                "chain": chain.model_dump(mode="json"),
                "facts": [item.model_dump(mode="json") for item in facts],
                "accepted_industry_claims": [item.model_dump(mode="json") for item in claims],
                "accepted_transmissions": [item.model_dump(mode="json") for item in chain_transmissions],
            }
            prompt = (
                "输出 NodeAnalysisBatch，覆盖产业链所有真实节点。只有直接有效 Signal、作用于该节点的"
                "已接受 Industry Claim，或有可追溯 Signal 根的 Transmission 才能形成方向结论；"
                "分别填写 supporting_fact_ids、supporting_claim_ids、supporting_transmission_ids。"
                "其余节点必须 INSUFFICIENT_EVIDENCE，不得发明ID。\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
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

    async def review(self, state: IndustryAnalysisState) -> ReviewResult:
        payload = {
            "active_signal_facts": [
                item.model_dump(mode="json")
                for item in state.industry_context.facts
                if item.uuid in state.industry_context.eligible_signal_fact_ids
            ],
            "geopolitical": state.geopolitical.model_dump(mode="json"),
            "macro": state.macro.model_dump(mode="json"),
            "industry": state.industry.model_dump(mode="json"),
            "accepted_transmissions": [item.model_dump(mode="json") for item in state.transmissions],
            "draft": state.draft.model_dump(mode="json"),
        }
        prompt = (
            "审核该分层投研结果。不得补充新结论；任何方向结论没有 Event→Signal→层结论/传导谱系，"
            "或引用不存在的锚点、节点、Fact、Claim、拓扑边时 accepted=false。\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        return await self._run(self._reviewer, prompt, ReviewResult)

    async def close(self) -> None:
        await self._graphiti.close()


def create_local_investment_workflow_runtime(
    model: Any,
    reasoner: Agent,
    reviewer: Agent,
) -> LocalInvestmentWorkflowRuntime:
    """Compose one app-owned Graphiti client with the published Agent components."""

    return LocalInvestmentWorkflowRuntime(create_agentos_graphiti(model), reasoner, reviewer)
