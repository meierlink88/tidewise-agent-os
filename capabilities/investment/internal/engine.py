"""Deterministic safety gates for Agent-produced investment reasoning."""

from __future__ import annotations

import hashlib
import json

from capabilities.investment.internal.models import (
    TRANSMISSION_CONTINUATION_THRESHOLD as DEFAULT_TRANSMISSION_CONTINUATION_THRESHOLD,
)
from capabilities.investment.internal.models import (
    TRANSMISSION_INCLUSION_THRESHOLD as DEFAULT_TRANSMISSION_INCLUSION_THRESHOLD,
)
from capabilities.investment.internal.models import (
    AcceptedCrossLayerTransmission,
    AcceptedTransmission,
    AnalysisDraft,
    CandidateCrossLayerMechanism,
    ChainTrendView,
    Confidence,
    CrossLayerAnalysisResult,
    CrossLayerTransmissionBatch,
    Direction,
    FactSnapshot,
    Horizon,
    ImpactLayer,
    IndustryChainSnapshot,
    InvestmentAnalysisContext,
    InvestmentAssessment,
    LayerAnalysisContext,
    LayerAnalysisResult,
    LayerAssessment,
    LayerAssessmentBatch,
    NodeTrendView,
    ReasoningTraceNode,
    TransmissionBatch,
    TransmissionCandidate,
    TransmissionProposal,
    Trend,
)


class InvestmentReasoningEngine:
    """Validate Workflow outputs and normalize unsupported model conclusions."""

    TRANSMISSION_INCLUSION_THRESHOLD = DEFAULT_TRANSMISSION_INCLUSION_THRESHOLD
    TRANSMISSION_CONTINUATION_THRESHOLD = DEFAULT_TRANSMISSION_CONTINUATION_THRESHOLD

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
    def build_layer_assessments(
        cls,
        context: LayerAnalysisContext,
        batch: LayerAssessmentBatch,
        *,
        layer: ImpactLayer,
    ) -> list[LayerAssessment]:
        """Interpret every retrieved direct Signal anchor without re-validating graph semantics."""

        if context.layer != layer:
            raise ValueError(f"layer context mismatch: expected {layer.value}, got {context.layer.value}")
        anchors = {item.business_id: item for item in context.anchors}
        scoped_event_ids = {event.event_id for event in context.events}
        direct_signals = [item for item in context.facts if item.uuid in context.direct_signal_fact_ids]
        proposals = {
            item.anchor_id: item
            for item in batch.proposals
            if item.anchor_id in anchors and item.result != Trend.INSUFFICIENT_EVIDENCE
        }
        assessments: list[LayerAssessment] = []
        for anchor_id, anchor in anchors.items():
            attached = [
                signal
                for signal in direct_signals
                if anchor.uuid in {signal.source_uuid, signal.target_uuid}
                or anchor_id in {signal.source_business_id, signal.target_business_id}
            ]
            if not attached:
                continue
            signal_ids = list(dict.fromkeys(item.uuid for item in attached))
            root_event_ids = list(
                dict.fromkeys(
                    event_id
                    for signal in attached
                    for event_id in signal.source_event_ids
                    if event_id in scoped_event_ids
                )
            )
            if not root_event_ids:
                continue
            proposal = proposals.get(anchor_id)
            fallback_summary = (
                "；".join(item.fact for item in attached if item.fact)[:1200] or f"{anchor.name}存在直接 Signal。"
            )
            fallback_reasoning = "；".join(item.mechanism or item.fact for item in attached)[:1600]
            horizons = sorted({horizon for item in attached for horizon in item.horizons}, key=lambda item: item.value)
            result = proposal.result if proposal is not None else cls._trend_from_signals(attached)
            confidence = (
                proposal.confidence
                if proposal is not None
                else cls._minimum_confidence([item.confidence for item in attached])
            )
            assessment_payload = {
                "layer": layer.value,
                "anchor_id": anchor_id,
                "signal_fact_ids": signal_ids,
            }
            digest = hashlib.sha256(
                json.dumps(assessment_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:20]
            assessments.append(
                LayerAssessment(
                    anchor_id=anchor_id,
                    result=result,
                    confidence=confidence,
                    summary=proposal.summary if proposal is not None else fallback_summary,
                    reasoning=proposal.reasoning if proposal is not None else fallback_reasoning,
                    direct_signal_fact_ids=signal_ids,
                    assumptions=proposal.assumptions if proposal is not None else [],
                    risks=proposal.risks if proposal is not None else [],
                    assessment_id=f"ASSESS-{digest}",
                    layer=layer,
                    anchor_name=anchor.name,
                    anchor_type=anchor.entity_type,
                    horizons=horizons,
                    root_event_ids=root_event_ids,
                )
            )
        return assessments

    @staticmethod
    def _trend_from_signals(signals: list[FactSnapshot]) -> Trend:
        return InvestmentReasoningEngine._trend_from_directions(
            [item.direction for item in signals if item.direction is not None]
        )

    @staticmethod
    def _trend_from_directions(values: list[Direction]) -> Trend:
        directions = {item for item in values if item != Direction.UNKNOWN}
        if Direction.MIXED in directions or {Direction.UP, Direction.DOWN}.issubset(directions):
            return Trend.DIVERGENT
        if Direction.UP in directions:
            return Trend.WARMING
        if Direction.DOWN in directions:
            return Trend.COOLING
        if Direction.STABLE in directions:
            return Trend.NO_MATERIAL_CHANGE
        return Trend.INSUFFICIENT_EVIDENCE

    @staticmethod
    def _fact_is_valid_at(fact: FactSnapshot, decision_at) -> bool:
        return (fact.valid_at is None or fact.valid_at <= decision_at) and (
            fact.invalid_at is None or fact.invalid_at > decision_at
        )

    @staticmethod
    def _fact_touches_anchor(fact: FactSnapshot, anchor_uuid: str, anchor_id: str) -> bool:
        return anchor_uuid in {fact.source_uuid, fact.target_uuid} or anchor_id in {
            fact.source_business_id,
            fact.target_business_id,
        }

    @classmethod
    def validate_cross_layer_batch(
        cls,
        context: LayerAnalysisContext,
        source_assessments: list[LayerAssessment],
        target_assessments: list[LayerAssessment],
        batch: CrossLayerTransmissionBatch,
    ) -> CrossLayerAnalysisResult:
        """Separate causal bridges from same-source context and unresolved hypotheses."""

        sources = {item.assessment_id: item for item in source_assessments}
        targets = {item.assessment_id: item for item in target_assessments}
        facts = {item.uuid: item for item in context.facts}
        accepted: list[AcceptedCrossLayerTransmission] = []
        candidates: list[CandidateCrossLayerMechanism] = []
        seen: set[tuple[str, str]] = set()
        for proposal in batch.proposals:
            source = sources.get(proposal.source_assessment_id)
            target = targets.get(proposal.target_assessment_id)
            if source is None or target is None or target.layer != context.layer:
                continue
            key = (source.assessment_id, target.assessment_id)
            if key in seen:
                continue
            seen.add(key)
            mechanisms = [
                facts[item]
                for item in proposal.mechanism_fact_ids
                if item in facts
                and facts[item].kind == "ORDINARY"
                and cls._fact_is_valid_at(facts[item], context.decision_at)
            ]
            bridge = mechanisms[0] if mechanisms else None
            same_source = bool(set(source.root_event_ids).intersection(target.root_event_ids))
            confidence = cls._minimum_confidence([proposal.confidence, source.confidence, target.confidence])
            if bridge is not None:
                relation_type = "CROSS_LAYER"
                mechanism_ids = [bridge.uuid]
            elif same_source:
                relation_type = "SAME_SOURCE_SIGNAL"
                mechanism_ids = []
                confidence = cls._degrade_confidence(confidence)
            else:
                candidates.append(
                    CandidateCrossLayerMechanism(
                        **proposal.model_dump(exclude={"mechanism_fact_ids", "confidence"}),
                        mechanism_fact_ids=[item.uuid for item in mechanisms],
                        confidence=cls._degrade_confidence(confidence),
                        reason="缺少连接上下层真实锚点的普通 Fact，保留为待验证机制。",
                    )
                )
                continue
            payload = {
                "source_assessment_id": source.assessment_id,
                "target_assessment_id": target.assessment_id,
                "mechanism_fact_ids": mechanism_ids,
                "logic": proposal.logic,
                "relation_type": relation_type,
            }
            digest = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:20]
            accepted.append(
                AcceptedCrossLayerTransmission(
                    **proposal.model_dump(exclude={"mechanism_fact_ids", "confidence"}),
                    mechanism_fact_ids=mechanism_ids,
                    confidence=confidence,
                    transmission_id=f"XLT-{digest}",
                    source_layer=source.layer,
                    target_layer=target.layer,
                    relation_type=relation_type,
                )
            )
        return CrossLayerAnalysisResult(
            target_layer=context.layer,
            accepted=accepted,
            candidates=candidates,
            limitations=list(dict.fromkeys(batch.limitations))[:20],
        )

    @classmethod
    def validate_round(
        cls,
        context: InvestmentAnalysisContext,
        accepted: list[AcceptedTransmission],
        batch: TransmissionBatch,
        *,
        round_number: int,
        root_assessments: list[LayerAssessment] | None = None,
        candidates: list[TransmissionCandidate] | None = None,
    ) -> list[AcceptedTransmission]:
        chains = {item.business_id: item for item in context.chains}
        eligible_signals = {fact.uuid: fact for fact in context.facts if fact.uuid in context.eligible_signal_fact_ids}
        accepted_by_id = {item.transmission_id: item for item in accepted}
        assessments_by_id = {item.assessment_id: item for item in (root_assessments or [])}
        seen = {
            (item.chain_id, item.target_node_id, item.target_variable, item.horizon, item.direction)
            for item in accepted
        }
        validated: list[AcceptedTransmission] = []
        candidates_by_id = {item.candidate_id: item for item in candidates or []}
        for generated in batch.proposals:
            proposal = generated
            if candidates is not None:
                candidate = candidates_by_id.get(generated.candidate_id or "")
                if candidate is None:
                    continue
                proposal = generated.model_copy(
                    update={
                        "chain_id": candidate.chain_id,
                        "topology_edge_id": candidate.topology_edge_id,
                        "source_node_id": candidate.source_node_id,
                        "target_node_id": candidate.target_node_id,
                        "flow": candidate.flow,
                        "horizon": candidate.horizon,
                        "source_fact_ids": candidate.source_fact_ids,
                        "source_assessment_ids": candidate.source_assessment_ids,
                        "parent_transmission_ids": candidate.parent_transmission_ids,
                    }
                )
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
                cited = [eligible_signals[item] for item in proposal.source_fact_ids if item in eligible_signals]
                cited_assessments = [
                    assessments_by_id[item]
                    for item in proposal.source_assessment_ids
                    if item in assessments_by_id
                    and assessments_by_id[item].anchor_type == "ChainNode"
                    and assessments_by_id[item].anchor_id == proposal.source_node_id
                ]
                if not cited and not cited_assessments:
                    continue
                # A first-hop Signal must be attached to the actual source node. An
                # ordinary Fact may still appear in the prompt, but can never pass this gate.
                if cited and not any(
                    signal.target_business_id == proposal.source_node_id
                    or signal.source_business_id == proposal.source_node_id
                    for signal in cited
                ):
                    continue
                root_ids = list(
                    dict.fromkeys(
                        [item.uuid for item in cited]
                        + [root for assessment in cited_assessments for root in assessment.direct_signal_fact_ids]
                    )
                )
                confidence_cap = cls._degrade_confidence(
                    cls._minimum_confidence(
                        [item.confidence for item in cited] + [item.confidence for item in cited_assessments]
                    )
                )
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
            path_score = cls._path_score(
                proposal,
                accepted_by_id,
                cited_confidence=(
                    cls._minimum_confidence(
                        [item.confidence for item in cited] + [item.confidence for item in cited_assessments]
                    )
                    if round_number == 1
                    else None
                ),
            )
            if path_score < cls.TRANSMISSION_INCLUSION_THRESHOLD:
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
            item = AcceptedTransmission(
                **proposal.model_dump(exclude={"confidence"}),
                confidence=accepted_confidence,
                transmission_id=transmission_id,
                hop=round_number,
                root_signal_fact_ids=root_ids,
                path_score=path_score,
            )
            validated.append(item)
            accepted_by_id[transmission_id] = item
        return validated

    @classmethod
    def enumerate_transmission_candidates(
        cls,
        context: InvestmentAnalysisContext,
        accepted: list[AcceptedTransmission],
        *,
        round_number: int,
        root_assessments: list[LayerAssessment] | None = None,
    ) -> list[TransmissionCandidate]:
        """Enumerate every real, unvisited adjacent topology move for this round."""

        eligible = {item.uuid: item for item in context.facts if item.uuid in context.eligible_signal_fact_ids}
        assessments = list(root_assessments or [])
        accepted_by_id = {item.transmission_id: item for item in accepted}
        result: list[TransmissionCandidate] = []
        seen: set[tuple[str, str, str, Horizon, tuple[str, ...]]] = set()
        for chain in context.chains:
            if round_number == 1:
                frontiers: list[tuple[str, Horizon, list[str], list[str], list[str], set[str]]] = []
                for node_id in chain.signal_root_node_ids:
                    facts = [
                        item
                        for item in eligible.values()
                        if node_id in {item.source_business_id, item.target_business_id}
                    ]
                    node_assessments = [
                        item for item in assessments if item.anchor_type == "ChainNode" and item.anchor_id == node_id
                    ]
                    horizons = {horizon for item in facts for horizon in item.horizons} | {
                        horizon for item in node_assessments for horizon in item.horizons
                    }
                    for horizon in sorted(horizons, key=lambda item: item.value):
                        horizon_facts = [item for item in facts if horizon in item.horizons]
                        horizon_assessments = [item for item in node_assessments if horizon in item.horizons]
                        frontiers.append(
                            (
                                node_id,
                                horizon,
                                [item.uuid for item in horizon_facts],
                                [item.assessment_id for item in horizon_assessments],
                                [],
                                {node_id},
                            )
                        )
            else:
                frontiers = []
                for parent in accepted:
                    if (
                        parent.chain_id != chain.business_id
                        or parent.hop != round_number - 1
                        or parent.path_score < cls.TRANSMISSION_CONTINUATION_THRESHOLD
                    ):
                        continue
                    visited = cls._visited_nodes(parent, accepted_by_id)
                    frontiers.append(
                        (
                            parent.target_node_id,
                            parent.horizon,
                            [],
                            [],
                            [parent.transmission_id],
                            visited,
                        )
                    )
            for source_id, horizon, fact_ids, assessment_ids, parent_ids, visited in frontiers:
                for edge in chain.edges:
                    if edge.source_node_id == source_id:
                        target_id, flow = edge.target_node_id, "ALONG_EDGE"
                    elif edge.target_node_id == source_id:
                        target_id, flow = edge.source_node_id, "AGAINST_EDGE"
                    else:
                        continue
                    if target_id in visited:
                        continue
                    key = (chain.business_id, edge.business_id, source_id, horizon, tuple(parent_ids))
                    if key in seen:
                        continue
                    seen.add(key)
                    payload = {
                        "chain": chain.business_id,
                        "edge": edge.business_id,
                        "source": source_id,
                        "target": target_id,
                        "horizon": horizon.value,
                        "parents": parent_ids,
                    }
                    digest = hashlib.sha256(
                        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest()[:20]
                    result.append(
                        TransmissionCandidate(
                            candidate_id=f"TC-{digest}",
                            chain_id=chain.business_id,
                            topology_edge_id=edge.business_id,
                            source_node_id=source_id,
                            target_node_id=target_id,
                            flow=flow,
                            horizon=horizon,
                            source_fact_ids=fact_ids,
                            source_assessment_ids=assessment_ids,
                            parent_transmission_ids=parent_ids,
                        )
                    )
        return result

    @classmethod
    def _visited_nodes(
        cls,
        item: AcceptedTransmission,
        accepted_by_id: dict[str, AcceptedTransmission],
    ) -> set[str]:
        visited = {item.source_node_id, item.target_node_id}
        pending = list(item.parent_transmission_ids)
        while pending:
            parent = accepted_by_id.get(pending.pop())
            if parent is None:
                continue
            visited.update((parent.source_node_id, parent.target_node_id))
            pending.extend(parent.parent_transmission_ids)
        return visited

    @classmethod
    def _path_score(
        cls,
        proposal: TransmissionProposal,
        accepted_by_id: dict[str, AcceptedTransmission],
        *,
        cited_confidence: Confidence | None,
    ) -> float:
        confidence_score = {Confidence.LOW: 0.4, Confidence.MEDIUM: 0.7, Confidence.HIGH: 1.0}
        semantic = confidence_score[proposal.confidence]
        if cited_confidence is not None:
            upstream = confidence_score[cited_confidence]
        else:
            parents = [accepted_by_id[item] for item in proposal.parent_transmission_ids if item in accepted_by_id]
            upstream = min((item.path_score for item in parents), default=0.0)
        assumptions_penalty = min(len(proposal.assumptions) * 0.08, 0.24)
        return round(max(0.0, min(1.0, upstream * 0.60 + semantic * 0.40 - assumptions_penalty)), 4)

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
        industry_assessments: list[LayerAssessment] | None = None,
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
                    cls._normalize_node(
                        context,
                        transmissions,
                        chain.business_id,
                        canonical.business_id,
                        node,
                        industry_assessments or [],
                    )
                )
            normalized_chains.append(
                proposed.model_copy(
                    update={
                        "chain_id": chain.business_id,
                        "chain_name": chain.name,
                        "short": cls._reduce_trend([item.short for item in nodes]),
                        "medium": cls._reduce_trend([item.medium for item in nodes]),
                        "long": cls._reduce_trend([item.long for item in nodes]),
                        "confidence": cls._minimum_confidence(
                            [
                                item.confidence
                                for item in nodes
                                if any(
                                    value != Trend.INSUFFICIENT_EVIDENCE
                                    for value in (item.short, item.medium, item.long)
                                )
                            ]
                        ),
                        "summary": cls._chain_summary(chain.name, nodes),
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
        industry_assessments: list[LayerAssessment] | None = None,
    ) -> NodeTrendView:
        direct_signals = [
            fact
            for fact in context.facts
            if fact.uuid in context.eligible_signal_fact_ids
            and (fact.source_business_id == node_id or fact.target_business_id == node_id)
        ]
        incoming = [item for item in transmissions if item.chain_id == chain_id and item.target_node_id == node_id]
        direct_assessments = [
            item
            for item in (industry_assessments or [])
            if item.layer == ImpactLayer.INDUSTRY
            and item.anchor_type == "ChainNode"
            and item.anchor_id == node_id
            and set(item.direct_signal_fact_ids) <= context.eligible_signal_fact_ids
        ]
        # These records already passed the Graphiti retrieval contract. Bind them
        # deterministically instead of requiring the Agent to copy every ID.
        cited_signals = direct_signals
        cited_incoming = incoming
        cited_assessments = direct_assessments
        cited_signal_ids = {item.uuid for item in cited_signals}
        cited_transmission_ids = {item.transmission_id for item in cited_incoming}
        cited_assessment_ids = {item.assessment_id for item in cited_assessments}
        supported_horizons = (
            {horizon for fact in cited_signals for horizon in fact.horizons}
            | {item.horizon for item in cited_incoming}
            | {horizon for assessment in cited_assessments for horizon in assessment.horizons}
        )
        updates: dict[str, object] = {
            "chain_id": chain_id,
            "node_id": node_id,
            "supporting_fact_ids": sorted(cited_signal_ids),
            "supporting_assessment_ids": sorted(cited_assessment_ids),
            "supporting_transmission_ids": sorted(cited_transmission_ids),
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
            elif getattr(node, field) == Trend.INSUFFICIENT_EVIDENCE:
                directions = [
                    fact.direction for fact in cited_signals if horizon in fact.horizons and fact.direction is not None
                ] + [item.direction for item in cited_incoming if item.horizon == horizon]
                assessment_trends = [item.result for item in cited_assessments if horizon in item.horizons]
                derived = cls._reduce_trend([cls._trend_from_directions(directions), *assessment_trends])
                if derived != Trend.INSUFFICIENT_EVIDENCE:
                    updates[field] = derived
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
    def _chain_summary(cls, chain_name: str, nodes: list[NodeTrendView]) -> str:
        directional = [
            item
            for item in nodes
            if any(value != Trend.INSUFFICIENT_EVIDENCE for value in (item.short, item.medium, item.long))
        ]
        if not directional:
            return f"{chain_name}当前没有可追溯至有效 Signal 的方向结论。"
        fragments = [
            f"{item.node_name}短期{item.short.value}、中期{item.medium.value}、长期{item.long.value}"
            for item in directional
        ]
        return (f"{chain_name}由真实节点结果聚合：" + "；".join(fragments))[:1600]

    @classmethod
    def directional_lineage_issues(
        cls,
        context: InvestmentAnalysisContext,
        transmissions: list[AcceptedTransmission],
        draft: AnalysisDraft,
        industry_assessments: list[LayerAssessment] | None = None,
    ) -> list[str]:
        """Return output-reference failures for every directional node/horizon assessment."""

        active_signals = {item.uuid: item for item in context.facts if item.uuid in context.eligible_signal_fact_ids}
        transmissions_by_id = {item.transmission_id: item for item in transmissions}
        assessments_by_id = {item.assessment_id: item for item in (industry_assessments or [])}
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
                    layer_assessment = any(
                        assessment_id in assessments_by_id
                        and assessments_by_id[assessment_id].layer == ImpactLayer.INDUSTRY
                        and assessments_by_id[assessment_id].anchor_type == "ChainNode"
                        and assessments_by_id[assessment_id].anchor_id == node.node_id
                        and horizon in assessments_by_id[assessment_id].horizons
                        and set(assessments_by_id[assessment_id].direct_signal_fact_ids) <= set(active_signals)
                        for assessment_id in node.supporting_assessment_ids
                    )
                    if not direct and not propagated and not layer_assessment:
                        issues.append(
                            f"DIRECTIONAL_ASSESSMENT_WITHOUT_SIGNAL_LINEAGE:"
                            f"{chain.chain_id}:{node.node_id}:{horizon.value}"
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
    def build_reasoning_tree(
        context: InvestmentAnalysisContext,
        layer_results: list[LayerAnalysisResult],
        transmissions: list[AcceptedTransmission],
        draft: AnalysisDraft,
    ) -> list[ReasoningTraceNode]:
        """Materialize the accepted lineage as an auditable result tree."""

        nodes: list[ReasoningTraceNode] = [
            ReasoningTraceNode(
                node_id=event.event_id,
                node_type="EVENT",
                label=f"{event.title}：{event.summary}"[:1600],
            )
            for event in context.events
        ]
        active = {item.uuid: item for item in context.facts if item.uuid in context.eligible_signal_fact_ids}
        nodes.extend(
            ReasoningTraceNode(
                node_id=fact.uuid,
                node_type="SIGNAL",
                label=f"{fact.source_name} {fact.direction or ''} → {fact.target_name}：{fact.fact}"[:1600],
                parent_ids=[item for item in fact.source_event_ids if item],
            )
            for fact in active.values()
        )
        assessments = [assessment for result in layer_results for assessment in result.assessments]
        nodes.extend(
            ReasoningTraceNode(
                node_id=assessment.assessment_id,
                node_type="LAYER_ASSESSMENT",
                label=(
                    f"[{assessment.layer.value}] {assessment.anchor_name} "
                    f"{assessment.result.value}：{assessment.summary}"
                )[:1600],
                parent_ids=list(assessment.direct_signal_fact_ids),
            )
            for assessment in assessments
        )
        nodes.extend(
            ReasoningTraceNode(
                node_id=item.transmission_id,
                node_type="TRANSMISSION",
                label=f"{item.source_node_id} → {item.target_node_id}：{item.mechanism}"[:1600],
                parent_ids=list(
                    dict.fromkeys(
                        [*item.source_assessment_ids, *item.parent_transmission_ids, *item.root_signal_fact_ids]
                    )
                ),
            )
            for item in transmissions
        )
        for chain in draft.chains:
            for node in chain.nodes:
                conclusion_id = f"CONCLUSION:{chain.chain_id}:{node.node_id}"
                nodes.append(
                    ReasoningTraceNode(
                        node_id=conclusion_id,
                        node_type="NODE_CONCLUSION",
                        label=(
                            f"{chain.chain_name}/{node.node_name}："
                            f"短期{node.short.value}、中期{node.medium.value}、长期{node.long.value}；{node.rationale}"
                        )[:1600],
                        parent_ids=list(
                            dict.fromkeys(
                                [
                                    *node.supporting_assessment_ids,
                                    *node.supporting_transmission_ids,
                                    *node.supporting_fact_ids,
                                ]
                            )
                        ),
                    )
                )
        return nodes[:5000]

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
