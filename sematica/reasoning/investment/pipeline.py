"""Fixed investment-reasoning DAG with bounded multi-hop semantic stages."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from sematica.reasoning.investment.contracts import (
    AcceptedTransmission,
    AnalysisDraft,
    Confidence,
    Horizon,
    IndustryChainSnapshot,
    InvestmentAnalysisContext,
    InvestmentAnalysisResult,
    InvestmentAssessment,
    NodeTrendView,
    ReviewResult,
    TransmissionBatch,
    TransmissionProposal,
    Trend,
)


class InvestmentReasoner(Protocol):
    name: str

    async def propagate(
        self,
        context: InvestmentAnalysisContext,
        accepted: list[AcceptedTransmission],
        *,
        round_number: int,
    ) -> TransmissionBatch: ...

    async def aggregate(
        self,
        context: InvestmentAnalysisContext,
        transmissions: list[AcceptedTransmission],
    ) -> AnalysisDraft: ...

    async def review(
        self,
        context: InvestmentAnalysisContext,
        transmissions: list[AcceptedTransmission],
        draft: AnalysisDraft,
    ) -> ReviewResult: ...


class InvestmentReasoningPipeline:
    """Execute one frozen context through the same fixed DAG for every reasoner."""

    def __init__(self, reasoner: InvestmentReasoner) -> None:
        self._reasoner = reasoner

    async def run(self, context: InvestmentAnalysisContext) -> InvestmentAnalysisResult:
        fingerprint = self.context_fingerprint(context)
        accepted: list[AcceptedTransmission] = []
        rounds_executed = 0
        for round_number in range(1, context.request.max_hops + 1):
            batch = await self._reasoner.propagate(
                context,
                accepted,
                round_number=round_number,
            )
            rounds_executed += 1
            new_items = self._validate_round(
                context,
                accepted,
                batch,
                round_number=round_number,
            )
            accepted.extend(new_items)
            if not any(item.confidence != Confidence.LOW for item in new_items):
                break

        draft = await self._reasoner.aggregate(context, accepted)
        complete_draft = self._ensure_node_coverage(context, draft)
        grounded_draft = self._enforce_horizon_evidence(
            context,
            accepted,
            complete_draft,
        )
        review = await self._reasoner.review(context, accepted, grounded_draft)
        return InvestmentAnalysisResult(
            executor=self._reasoner.name,
            status="SUCCEEDED" if review.accepted else "NEEDS_REVIEW",
            context_fingerprint=fingerprint,
            transmissions=accepted,
            draft=grounded_draft,
            review=review,
            stage_metrics={
                "events": len(context.events),
                "facts": len(context.facts),
                "chains": len(context.chains),
                "node_memberships": sum(len(item.nodes) for item in context.chains),
                "topology_edges": sum(len(item.edges) for item in context.chains),
                "transmission_rounds": rounds_executed,
                "accepted_transmissions": len(accepted),
            },
            execution_issues=list(getattr(self._reasoner, "execution_issues", [])),
        )

    @staticmethod
    def context_fingerprint(context: InvestmentAnalysisContext) -> str:
        payload = context.model_dump(mode="json")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _validate_round(
        cls,
        context: InvestmentAnalysisContext,
        accepted: list[AcceptedTransmission],
        batch: TransmissionBatch,
        *,
        round_number: int,
    ) -> list[AcceptedTransmission]:
        chains = {item.business_id: item for item in context.chains}
        fact_ids = {item.uuid for item in context.facts}
        transmission_ids = {item.transmission_id for item in accepted}
        seen = {
            (
                item.chain_id,
                item.target_node_id,
                item.target_variable,
                item.horizon,
                item.direction,
            )
            for item in accepted
        }
        validated: list[AcceptedTransmission] = []
        for proposal in batch.proposals:
            chain = chains.get(proposal.chain_id)
            if chain is None:
                continue
            edge = next(
                (item for item in chain.edges if item.business_id == proposal.topology_edge_id),
                None,
            )
            if edge is None:
                continue
            if {proposal.source_node_id, proposal.target_node_id} != {
                edge.source_node_id,
                edge.target_node_id,
            }:
                continue
            if round_number == 1:
                if not proposal.source_fact_ids or not set(proposal.source_fact_ids).issubset(fact_ids):
                    continue
            else:
                if not proposal.parent_transmission_ids or not set(proposal.parent_transmission_ids).issubset(
                    transmission_ids
                ):
                    continue
            key = (
                proposal.chain_id,
                proposal.target_node_id,
                proposal.target_variable,
                proposal.horizon,
                proposal.direction,
            )
            if key in seen:
                continue
            seen.add(key)
            transmission_id = cls._transmission_id(proposal, round_number)
            validated.append(
                AcceptedTransmission(
                    **proposal.model_dump(),
                    transmission_id=transmission_id,
                    hop=round_number,
                )
            )
            transmission_ids.add(transmission_id)
        return validated

    @staticmethod
    def _transmission_id(proposal: TransmissionProposal, hop: int) -> str:
        payload = proposal.model_dump(mode="json")
        payload["hop"] = hop
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[
            :20
        ]
        return f"TX-{digest}"

    @staticmethod
    def _ensure_node_coverage(
        context: InvestmentAnalysisContext,
        draft: AnalysisDraft,
    ) -> AnalysisDraft:
        drafts = {item.chain_id: item for item in draft.chains}
        chains = []
        for chain in context.chains:
            item = drafts.get(chain.business_id)
            if item is None:
                item = InvestmentReasoningPipeline._insufficient_chain(chain)
            by_node = {node.node_id: node for node in item.nodes}
            completed = [
                by_node.get(node.business_id)
                or NodeTrendView(
                    chain_id=chain.business_id,
                    node_id=node.business_id,
                    node_name=node.name,
                    short=Trend.INSUFFICIENT_EVIDENCE,
                    medium=Trend.INSUFFICIENT_EVIDENCE,
                    long=Trend.INSUFFICIENT_EVIDENCE,
                    confidence=Confidence.LOW,
                    investment_assessment=InvestmentAssessment.INSUFFICIENT_EVIDENCE,
                    rationale="推理执行器没有为该真实产业链节点返回可验证结论。",
                    risks=["MISSING_NODE_ANALYSIS"],
                )
                for node in chain.nodes
            ]
            chains.append(item.model_copy(update={"nodes": completed}))
        return draft.model_copy(update={"chains": chains})

    @staticmethod
    def _insufficient_chain(chain: IndustryChainSnapshot):
        from sematica.reasoning.investment.contracts import ChainTrendView

        return ChainTrendView(
            chain_id=chain.business_id,
            chain_name=chain.name,
            short=Trend.INSUFFICIENT_EVIDENCE,
            medium=Trend.INSUFFICIENT_EVIDENCE,
            long=Trend.INSUFFICIENT_EVIDENCE,
            confidence=Confidence.LOW,
            summary="推理执行器没有返回该产业链结论。",
            nodes=[
                NodeTrendView(
                    chain_id=chain.business_id,
                    node_id=node.business_id,
                    node_name=node.name,
                    short=Trend.INSUFFICIENT_EVIDENCE,
                    medium=Trend.INSUFFICIENT_EVIDENCE,
                    long=Trend.INSUFFICIENT_EVIDENCE,
                    confidence=Confidence.LOW,
                    investment_assessment=InvestmentAssessment.INSUFFICIENT_EVIDENCE,
                    rationale="推理执行器没有为该真实产业链节点返回可验证结论。",
                    risks=["MISSING_NODE_ANALYSIS"],
                )
                for node in chain.nodes
            ],
        )

    @classmethod
    def _enforce_horizon_evidence(
        cls,
        context: InvestmentAnalysisContext,
        transmissions: list[AcceptedTransmission],
        draft: AnalysisDraft,
    ) -> AnalysisDraft:
        """Remove trend claims whose node and horizon have no scoped evidence."""

        chains = []
        for chain_view in draft.chains:
            nodes = []
            for node in chain_view.nodes:
                node_facts = [
                    fact
                    for fact in context.facts
                    if fact.source_business_id == node.node_id or fact.target_business_id == node.node_id
                ]
                node_transmissions = [
                    item
                    for item in transmissions
                    if item.chain_id == chain_view.chain_id
                    and (item.source_node_id == node.node_id or item.target_node_id == node.node_id)
                ]
                supported_horizons = {
                    horizon
                    for fact in node_facts
                    for horizon in (
                        fact.horizons if fact.horizons else ([Horizon.SHORT] if fact.kind == "ORDINARY" else [])
                    )
                } | {item.horizon for item in node_transmissions}
                updates = {}
                risks = list(node.risks)
                for horizon, field in (
                    (Horizon.SHORT, "short"),
                    (Horizon.MEDIUM, "medium"),
                    (Horizon.LONG, "long"),
                ):
                    if horizon not in supported_horizons:
                        updates[field] = Trend.INSUFFICIENT_EVIDENCE
                        if getattr(node, field) != Trend.INSUFFICIENT_EVIDENCE:
                            risks.append(f"UNSUPPORTED_{horizon.value}_NORMALIZED")
                valid_fact_ids = {item.uuid for item in node_facts}
                valid_transmission_ids = {item.transmission_id for item in node_transmissions}
                updates["supporting_fact_ids"] = [item for item in node.supporting_fact_ids if item in valid_fact_ids]
                updates["supporting_transmission_ids"] = [
                    item for item in node.supporting_transmission_ids if item in valid_transmission_ids
                ]
                updates["risks"] = list(dict.fromkeys(risks))[:10]
                normalized = node.model_copy(update=updates)
                if all(
                    getattr(normalized, field) == Trend.INSUFFICIENT_EVIDENCE for field in ("short", "medium", "long")
                ):
                    normalized = normalized.model_copy(
                        update={
                            "confidence": Confidence.LOW,
                            "investment_assessment": (InvestmentAssessment.INSUFFICIENT_EVIDENCE),
                        }
                    )
                nodes.append(normalized)
            chains.append(
                chain_view.model_copy(
                    update={
                        "short": cls._reduce_trend([item.short for item in nodes]),
                        "medium": cls._reduce_trend([item.medium for item in nodes]),
                        "long": cls._reduce_trend([item.long for item in nodes]),
                        "nodes": nodes,
                    }
                )
            )
        return draft.model_copy(update={"chains": chains})

    @staticmethod
    def _reduce_trend(values: list[Trend]) -> Trend:
        material = {item for item in values if item != Trend.INSUFFICIENT_EVIDENCE}
        if Trend.DIVERGENT in material or {
            Trend.WARMING,
            Trend.COOLING,
        }.issubset(material):
            return Trend.DIVERGENT
        if Trend.WARMING in material:
            return Trend.WARMING
        if Trend.COOLING in material:
            return Trend.COOLING
        if Trend.NO_MATERIAL_CHANGE in material:
            return Trend.NO_MATERIAL_CHANGE
        return Trend.INSUFFICIENT_EVIDENCE
