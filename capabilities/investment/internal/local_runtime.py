"""Compose AgentOS Agents with Graphiti's native retrieval surface."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from agno.agent import Agent
from pydantic import BaseModel

from capabilities.investment.internal.context import InvestmentContextBuilder
from capabilities.investment.internal.engine import InvestmentReasoningEngine
from capabilities.investment.internal.models import (
    AcceptedTransmission,
    AnalysisDraft,
    ChainTrendView,
    Confidence,
    InvestmentAnalysisContext,
    InvestmentAnalysisRequest,
    NodeAnalysisBatch,
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
        call_agent = copy.copy(agent)
        call_agent.db = None
        async with self._slots:
            output = await call_agent.arun(prompt, stream=False, output_schema=model)
        return _payload(output, model)

    async def propagate(
        self,
        context: InvestmentAnalysisContext,
        accepted: list[AcceptedTransmission],
        *,
        round_number: int,
    ) -> TransmissionBatch:
        calls = []
        for chain in context.chains:
            chain_accepted = [item for item in accepted if item.chain_id == chain.business_id]
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
                "decision_at": context.request.decision_at.isoformat(),
                "events": [item.model_dump(mode="json") for item in context.events],
                "chain": chain.model_dump(mode="json"),
                "facts": [item.model_dump(mode="json") for item in relevant_facts],
                "accepted_transmissions": [item.model_dump(mode="json") for item in chain_accepted],
            }
            prompt = (
                f"执行第 {round_number} 轮 Signal 传导。只返回 TransmissionBatch。"
                "第1轮的 source_fact_ids 只能引用 kind=SIGNAL 且当前有效、作用于 source_node_id 的 Fact。"
                "后续轮只能从已接受 Transmission 的 target_node_id 继续，并填写 parent_transmission_ids。"
                "证据不足时返回空 proposals。\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            calls.append(self._run(self._reasoner, prompt, TransmissionBatch))
        batches = await asyncio.gather(*calls) if calls else []
        return TransmissionBatch(
            proposals=[proposal for batch in batches for proposal in batch.proposals],
            stopped_reason=";".join(batch.stopped_reason for batch in batches if batch.stopped_reason) or None,
        )

    async def synthesize(
        self,
        context: InvestmentAnalysisContext,
        transmissions: list[AcceptedTransmission],
    ) -> AnalysisDraft:
        if not context.chains:
            return AnalysisDraft(
                one_sentence_conclusion="当前 Event 可用于相关性召回，但没有命中可分析的标准产业链。",
                limitations=["NO_INDUSTRY_CHAIN_CONTEXT"],
            )
        if not any(chain.signal_root_fact_ids for chain in context.chains):
            insufficient_chains = [InvestmentReasoningEngine.insufficient_chain(chain) for chain in context.chains]
            return AnalysisDraft(
                one_sentence_conclusion="当前时间窗内事件可召回相关产业链，但没有有效 Signal Fact 支持方向性投研结论。",
                chains=insufficient_chains,
                limitations=["NO_ELIGIBLE_SIGNAL_ROOT"],
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
            chain_transmissions = [item for item in transmissions if item.chain_id == chain.business_id]
            payload = {
                "question": context.request.question,
                "chain": chain.model_dump(mode="json"),
                "facts": [item.model_dump(mode="json") for item in facts],
                "accepted_transmissions": [item.model_dump(mode="json") for item in chain_transmissions],
            }
            prompt = (
                "输出 NodeAnalysisBatch，必须覆盖该产业链所有真实节点。"
                "仅当节点有直接有效 Signal Fact，或有可追溯 Signal 根的已接受 Transmission 时，"
                "才能给出 WARMING/COOLING/DIVERGENT/NO_MATERIAL_CHANGE。否则必须 INSUFFICIENT_EVIDENCE。"
                "不得发明 ID，不得把普通 Fact 当作方向证据。\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            calls.append((chain, self._run(self._reasoner, prompt, NodeAnalysisBatch)))
        batches = await asyncio.gather(*(item[1] for item in calls))
        chains: list[ChainTrendView] = []
        for (chain, _), raw in zip(calls, batches, strict=True):
            batch = NodeAnalysisBatch.model_validate(raw)
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
        conclusion = "；".join(f"{item.chain_name}：{item.summary}" for item in chains)[:2000]
        return AnalysisDraft(
            one_sentence_conclusion=conclusion,
            chains=chains,
            limitations=list(dict.fromkeys(context.validation_issues))[:20],
        )

    async def review(
        self,
        context: InvestmentAnalysisContext,
        transmissions: list[AcceptedTransmission],
        draft: AnalysisDraft,
    ) -> ReviewResult:
        payload = {
            "eligible_signal_fact_ids": sorted(context.eligible_signal_fact_ids),
            "active_signal_facts": [
                item.model_dump(mode="json") for item in context.facts if item.uuid in context.eligible_signal_fact_ids
            ],
            "canonical_chains": [item.model_dump(mode="json") for item in context.chains],
            "accepted_transmissions": [item.model_dump(mode="json") for item in transmissions],
            "draft": draft.model_dump(mode="json"),
        }
        prompt = (
            "审核该投研草案。如果任何方向结论没有可追溯 Signal 根，或使用了不存在的节点/拓扑边，accepted=false。\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        return ReviewResult.model_validate(await self._run(self._reviewer, prompt, ReviewResult))

    async def repair(
        self,
        context: InvestmentAnalysisContext,
        transmissions: list[AcceptedTransmission],
        draft: AnalysisDraft,
        review: ReviewResult,
    ) -> AnalysisDraft:
        payload = {
            "canonical_chains": [item.model_dump(mode="json") for item in context.chains],
            "eligible_signal_fact_ids": sorted(context.eligible_signal_fact_ids),
            "active_signal_facts": [
                item.model_dump(mode="json") for item in context.facts if item.uuid in context.eligible_signal_fact_ids
            ],
            "accepted_transmissions": [item.model_dump(mode="json") for item in transmissions],
            "rejected_draft": draft.model_dump(mode="json"),
            "review": review.model_dump(mode="json"),
        }
        prompt = (
            "这是唯一一次修正机会。按审核问题修正 AnalysisDraft；删除所有无法引用有效 Signal Fact "
            "或已接受 Transmission 的方向结论，不得新增 ID 或事实。\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        return await self._run(self._reasoner, prompt, AnalysisDraft)

    async def close(self) -> None:
        await self._graphiti.close()


def create_local_investment_workflow_runtime(
    model: Any,
    reasoner: Agent,
    reviewer: Agent,
) -> LocalInvestmentWorkflowRuntime:
    """Compose one app-owned Graphiti client with the published Agent components."""

    return LocalInvestmentWorkflowRuntime(create_agentos_graphiti(model), reasoner, reviewer)
