"""Deterministic safety gates for Agent-produced investment reasoning."""

from __future__ import annotations

import hashlib
import json

from capabilities.investment.internal.models import (
    AcceptedTransmission,
    AnalysisDraft,
    ChainTrendView,
    Confidence,
    Horizon,
    IndustryChainSnapshot,
    InvestmentAnalysisContext,
    InvestmentAssessment,
    NodeTrendView,
    TransmissionBatch,
    TransmissionProposal,
    Trend,
)


class InvestmentReasoningEngine:
    """Validate lineage and normalize unsupported LLM conclusions."""

    @staticmethod
    def context_fingerprint(context: InvestmentAnalysisContext) -> str:
        canonical = json.dumps(
            context.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def validate_round(
        cls,
        context: InvestmentAnalysisContext,
        accepted: list[AcceptedTransmission],
        batch: TransmissionBatch,
        *,
        round_number: int,
    ) -> list[AcceptedTransmission]:
        chains = {item.business_id: item for item in context.chains}
        eligible_signals = {
            fact.uuid: fact for fact in context.facts if fact.is_active_signal(context.request.decision_at)
        }
        accepted_by_id = {item.transmission_id: item for item in accepted}
        seen = {
            (item.chain_id, item.target_node_id, item.target_variable, item.horizon, item.direction)
            for item in accepted
        }
        validated: list[AcceptedTransmission] = []
        for proposal in batch.proposals:
            chain = chains.get(proposal.chain_id)
            if chain is None:
                continue
            edge = next((item for item in chain.edges if item.business_id == proposal.topology_edge_id), None)
            if edge is None:
                continue
            expected_endpoints = (
                (edge.source_node_id, edge.target_node_id)
                if proposal.flow == "ALONG_EDGE"
                else (edge.target_node_id, edge.source_node_id)
            )
            if (proposal.source_node_id, proposal.target_node_id) != expected_endpoints:
                continue

            root_ids: list[str]
            confidence_cap: Confidence
            if round_number == 1:
                cited = [
                    eligible_signals[item]
                    for item in proposal.source_fact_ids
                    if item in eligible_signals and proposal.horizon in eligible_signals[item].horizons
                ]
                if not cited:
                    continue
                # A first-hop Signal must be attached to the actual source node. An
                # ordinary Fact may still appear in the prompt, but can never pass this gate.
                if not any(
                    signal.target_business_id == proposal.source_node_id
                    or signal.source_business_id == proposal.source_node_id
                    for signal in cited
                ):
                    continue
                root_ids = list(dict.fromkeys(item.uuid for item in cited))
                confidence_cap = cls._degrade_confidence(cls._minimum_confidence([item.confidence for item in cited]))
            else:
                parents = [
                    accepted_by_id[item]
                    for item in proposal.parent_transmission_ids
                    if item in accepted_by_id
                    and accepted_by_id[item].chain_id == proposal.chain_id
                    and accepted_by_id[item].hop == round_number - 1
                    and accepted_by_id[item].target_node_id == proposal.source_node_id
                    and accepted_by_id[item].horizon == proposal.horizon
                ]
                if not parents:
                    continue
                root_ids = list(dict.fromkeys(root for parent in parents for root in parent.root_signal_fact_ids))
                if not root_ids or not set(root_ids) <= set(eligible_signals):
                    continue
                confidence_cap = cls._degrade_confidence(cls._minimum_confidence([item.confidence for item in parents]))

            if proposal.assumptions:
                confidence_cap = cls._degrade_confidence(confidence_cap)
            accepted_confidence = cls._minimum_confidence([proposal.confidence, confidence_cap])

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
            item = AcceptedTransmission(
                **proposal.model_dump(exclude={"confidence"}),
                confidence=accepted_confidence,
                transmission_id=transmission_id,
                hop=round_number,
                root_signal_fact_ids=root_ids,
            )
            validated.append(item)
            accepted_by_id[transmission_id] = item
        return validated

    @staticmethod
    def _minimum_confidence(values: list[Confidence | None]) -> Confidence:
        rank = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
        present = [item for item in values if item is not None]
        return min(present, key=rank.__getitem__) if present else Confidence.LOW

    @staticmethod
    def _degrade_confidence(value: Confidence) -> Confidence:
        return {
            Confidence.HIGH: Confidence.MEDIUM,
            Confidence.MEDIUM: Confidence.LOW,
            Confidence.LOW: Confidence.LOW,
        }[value]

    @staticmethod
    def _transmission_id(proposal: TransmissionProposal, hop: int) -> str:
        payload = proposal.model_dump(mode="json")
        payload["hop"] = hop
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[
            :20
        ]
        return f"TX-{digest}"

    @classmethod
    def normalize_draft(
        cls,
        context: InvestmentAnalysisContext,
        transmissions: list[AcceptedTransmission],
        draft: AnalysisDraft,
    ) -> AnalysisDraft:
        """Cover every canonical node and remove conclusions without Signal lineage."""

        proposed_chains = {item.chain_id: item for item in draft.chains}
        normalized_chains: list[ChainTrendView] = []
        for chain in context.chains:
            proposed = proposed_chains.get(chain.business_id) or cls.insufficient_chain(chain)
            by_node = {item.node_id: item for item in proposed.nodes}
            nodes: list[NodeTrendView] = []
            for canonical in chain.nodes:
                node = by_node.get(canonical.business_id) or cls.insufficient_node(
                    chain, canonical.business_id, canonical.name
                )
                nodes.append(
                    cls._normalize_node(context, transmissions, chain.business_id, canonical.business_id, node)
                )
            normalized_chains.append(
                proposed.model_copy(
                    update={
                        "chain_id": chain.business_id,
                        "chain_name": chain.name,
                        "short": cls._reduce_trend([item.short for item in nodes]),
                        "medium": cls._reduce_trend([item.medium for item in nodes]),
                        "long": cls._reduce_trend([item.long for item in nodes]),
                        "nodes": nodes,
                    }
                )
            )
        if not normalized_chains:
            return AnalysisDraft(
                one_sentence_conclusion="当前时间窗内没有可由有效 Signal Fact 驱动的产业链投资方向结论。",
                chains=[],
                limitations=list(dict.fromkeys(draft.limitations + ["NO_ELIGIBLE_SIGNAL_ROOT"]))[:20],
            )
        return draft.model_copy(update={"chains": normalized_chains})

    @classmethod
    def _normalize_node(
        cls,
        context: InvestmentAnalysisContext,
        transmissions: list[AcceptedTransmission],
        chain_id: str,
        node_id: str,
        node: NodeTrendView,
    ) -> NodeTrendView:
        direct_signals = [
            fact
            for fact in context.facts
            if fact.is_active_signal(context.request.decision_at)
            and (fact.source_business_id == node_id or fact.target_business_id == node_id)
        ]
        incoming = [item for item in transmissions if item.chain_id == chain_id and item.target_node_id == node_id]
        cited_signal_ids = set(node.supporting_fact_ids) & {fact.uuid for fact in direct_signals}
        cited_transmission_ids = set(node.supporting_transmission_ids) & {item.transmission_id for item in incoming}
        cited_signals = [item for item in direct_signals if item.uuid in cited_signal_ids]
        cited_incoming = [item for item in incoming if item.transmission_id in cited_transmission_ids]
        supported_horizons = {horizon for fact in cited_signals for horizon in fact.horizons} | {
            item.horizon for item in cited_incoming
        }
        updates: dict[str, object] = {
            "chain_id": chain_id,
            "node_id": node_id,
            "supporting_fact_ids": list(
                dict.fromkeys(item for item in node.supporting_fact_ids if item in cited_signal_ids)
            ),
            "supporting_transmission_ids": list(
                dict.fromkeys(item for item in node.supporting_transmission_ids if item in cited_transmission_ids)
            ),
        }
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
        updates["risks"] = list(dict.fromkeys(risks))[:10]
        normalized = node.model_copy(update=updates)
        if all(getattr(normalized, field) == Trend.INSUFFICIENT_EVIDENCE for field in ("short", "medium", "long")):
            normalized = normalized.model_copy(
                update={
                    "confidence": Confidence.LOW,
                    "investment_assessment": InvestmentAssessment.INSUFFICIENT_EVIDENCE,
                }
            )
        return normalized

    @classmethod
    def directional_lineage_issues(
        cls,
        context: InvestmentAnalysisContext,
        transmissions: list[AcceptedTransmission],
        draft: AnalysisDraft,
    ) -> list[str]:
        """Return hard-gate failures for every directional node/horizon claim."""

        active_signals = {
            item.uuid: item for item in context.facts if item.is_active_signal(context.request.decision_at)
        }
        transmissions_by_id = {item.transmission_id: item for item in transmissions}
        issues: list[str] = []
        for chain in draft.chains:
            for node in chain.nodes:
                for horizon, field in (
                    (Horizon.SHORT, "short"),
                    (Horizon.MEDIUM, "medium"),
                    (Horizon.LONG, "long"),
                ):
                    if getattr(node, field) == Trend.INSUFFICIENT_EVIDENCE:
                        continue
                    direct = any(
                        fact_id in active_signals
                        and horizon in active_signals[fact_id].horizons
                        and (
                            active_signals[fact_id].source_business_id == node.node_id
                            or active_signals[fact_id].target_business_id == node.node_id
                        )
                        for fact_id in node.supporting_fact_ids
                    )
                    propagated = any(
                        transmission_id in transmissions_by_id
                        and transmissions_by_id[transmission_id].chain_id == chain.chain_id
                        and transmissions_by_id[transmission_id].target_node_id == node.node_id
                        and transmissions_by_id[transmission_id].horizon == horizon
                        and set(transmissions_by_id[transmission_id].root_signal_fact_ids) <= set(active_signals)
                        for transmission_id in node.supporting_transmission_ids
                    )
                    if not direct and not propagated:
                        issues.append(
                            f"DIRECTIONAL_CLAIM_WITHOUT_SIGNAL_LINEAGE:{chain.chain_id}:{node.node_id}:{horizon.value}"
                        )
        return issues

    @staticmethod
    def insufficient_node(chain: IndustryChainSnapshot, node_id: str, node_name: str) -> NodeTrendView:
        return NodeTrendView(
            chain_id=chain.business_id,
            node_id=node_id,
            node_name=node_name,
            short=Trend.INSUFFICIENT_EVIDENCE,
            medium=Trend.INSUFFICIENT_EVIDENCE,
            long=Trend.INSUFFICIENT_EVIDENCE,
            confidence=Confidence.LOW,
            investment_assessment=InvestmentAssessment.INSUFFICIENT_EVIDENCE,
            rationale="没有可追溯至有效 Signal Fact 的直接信号或产业链传导。",
            risks=["NO_SIGNAL_LINEAGE"],
        )

    @classmethod
    def insufficient_chain(cls, chain: IndustryChainSnapshot) -> ChainTrendView:
        return ChainTrendView(
            chain_id=chain.business_id,
            chain_name=chain.name,
            short=Trend.INSUFFICIENT_EVIDENCE,
            medium=Trend.INSUFFICIENT_EVIDENCE,
            long=Trend.INSUFFICIENT_EVIDENCE,
            confidence=Confidence.LOW,
            summary="事件与普通 Fact 只能证明相关性，当前没有有效 Signal 根支持方向判断。",
            nodes=[cls.insufficient_node(chain, item.business_id, item.name) for item in chain.nodes],
        )

    @classmethod
    def safe_fallback_draft(cls, context: InvestmentAnalysisContext, reason: str) -> AnalysisDraft:
        """Produce a non-directional result when a generated draft cannot pass review."""

        return AnalysisDraft(
            one_sentence_conclusion="当前证据或推导谱系未通过门禁，本次不形成方向性投研结论。",
            chains=[cls.insufficient_chain(chain) for chain in context.chains],
            limitations=list(dict.fromkeys(context.validation_issues + [reason]))[:20],
        )

    @staticmethod
    def _reduce_trend(values: list[Trend]) -> Trend:
        material = {item for item in values if item != Trend.INSUFFICIENT_EVIDENCE}
        if Trend.DIVERGENT in material or {Trend.WARMING, Trend.COOLING}.issubset(material):
            return Trend.DIVERGENT
        if Trend.WARMING in material:
            return Trend.WARMING
        if Trend.COOLING in material:
            return Trend.COOLING
        if Trend.NO_MATERIAL_CHANGE in material:
            return Trend.NO_MATERIAL_CHANGE
        return Trend.INSUFFICIENT_EVIDENCE
