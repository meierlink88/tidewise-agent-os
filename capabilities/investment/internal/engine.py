"""Deterministic safety gates for Agent-produced investment reasoning."""

from __future__ import annotations

import hashlib
import json

from capabilities.investment.internal.models import (
    AcceptedCrossLayerTransmission,
    AcceptedImpactClaim,
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
    LayerImpactBatch,
    NodeTrendView,
    ReasoningTraceNode,
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
    def validate_layer_batch(
        cls,
        context: LayerAnalysisContext,
        previous_claims: list[AcceptedImpactClaim],
        batch: LayerImpactBatch,
        *,
        layer: ImpactLayer,
    ) -> list[AcceptedImpactClaim]:
        """Accept only claims with an explicit active Signal root and bounded lineage."""

        if context.layer != layer:
            raise ValueError(f"layer context mismatch: expected {layer.value}, got {context.layer.value}")
        anchors = {item.business_id: item for item in context.anchors}
        facts = {item.uuid: item for item in context.facts}
        active_signals = {
            item.uuid: item
            for item in context.facts
            if item.uuid in context.direct_signal_fact_ids
            and item.is_active_signal(context.decision_at)
            and bool({event.event_id for event in context.events}.intersection(item.source_event_ids))
        }
        parents = {item.claim_id: item for item in previous_claims}
        allowed_parent_layers = {
            ImpactLayer.GEOPOLITICAL: set(),
            ImpactLayer.MACRO_ECONOMIC: {ImpactLayer.GEOPOLITICAL},
            ImpactLayer.INDUSTRY: {ImpactLayer.GEOPOLITICAL, ImpactLayer.MACRO_ECONOMIC},
        }[layer]
        accepted: list[AcceptedImpactClaim] = []
        seen: set[tuple[str, str, Direction, tuple[Horizon, ...]]] = set()
        for proposal in batch.proposals:
            if proposal.direction == Direction.UNKNOWN:
                continue
            anchor = anchors.get(proposal.anchor_id)
            if anchor is None:
                continue
            if layer == ImpactLayer.INDUSTRY and anchor.entity_type != "ChainNode":
                continue
            cited_signals = [active_signals[item] for item in proposal.source_fact_ids if item in active_signals]
            referenced_parents = [parents[item] for item in proposal.parent_claim_ids if item in parents]
            if any(item.layer not in allowed_parent_layers for item in referenced_parents):
                continue
            cited_parents = [item for item in referenced_parents if item.layer in allowed_parent_layers]
            valid_mechanisms = [
                facts[item]
                for item in proposal.mechanism_fact_ids
                if item in facts
                and facts[item].kind == "ORDINARY"
                and cls._fact_is_valid_at(facts[item], context.decision_at)
            ]

            derivation: str
            root_signal_ids: list[str]
            root_event_ids: list[str]
            confidence_cap: Confidence
            validated_source_facts: list[str]
            validated_mechanism_facts: list[str]
            validated_parent_claims: list[str]
            accepted_variable_id = proposal.variable_id
            accepted_direction = proposal.direction
            accepted_horizons = proposal.horizons
            if cited_signals:
                attached = [
                    item
                    for item in cited_signals
                    if anchor.uuid in {item.source_uuid, item.target_uuid}
                    or anchor.business_id in {item.source_business_id, item.target_business_id}
                ]
                if not attached:
                    continue
                by_variable: dict[str, list[FactSnapshot]] = {}
                for item in attached:
                    if item.variable_id:
                        by_variable.setdefault(item.variable_id, []).append(item)
                same_variable = by_variable.get(proposal.variable_id)
                if not same_variable:
                    continue
                supported_horizons = sorted(
                    {horizon for item in same_variable for horizon in item.horizons},
                    key=lambda item: item.value,
                )
                supported_directions = {
                    item.direction for item in same_variable if item.direction not in {None, Direction.UNKNOWN}
                }
                if not supported_horizons or not supported_directions:
                    continue
                supported_direction = (
                    next(iter(supported_directions)) if len(supported_directions) == 1 else Direction.MIXED
                )
                # Do not silently rewrite a model narrative around different
                # structured semantics. A mismatch is rejected so summary,
                # mechanism, direction and horizon cannot contradict each other.
                if proposal.direction != supported_direction or set(proposal.horizons) != set(supported_horizons):
                    continue
                accepted_horizons = supported_horizons
                accepted_direction = supported_direction
                root_signal_ids = list(dict.fromkeys(item.uuid for item in same_variable))
                scoped_event_ids = {event.event_id for event in context.events}
                root_event_ids = list(
                    dict.fromkeys(
                        event_id
                        for item in same_variable
                        for event_id in item.source_event_ids
                        if event_id in scoped_event_ids
                    )
                )
                if not root_event_ids:
                    continue
                confidence_cap = cls._minimum_confidence([item.confidence for item in same_variable])
                derivation = "DIRECT_SIGNAL"
                validated_source_facts = root_signal_ids
                validated_mechanism_facts = []
                validated_parent_claims = []
            else:
                if layer == ImpactLayer.GEOPOLITICAL or not cited_parents or not valid_mechanisms:
                    continue
                if not set(proposal.horizons) <= {horizon for parent in cited_parents for horizon in parent.horizons}:
                    continue
                # A cross-layer mechanism must be an actual bridge: the same
                # ordinary Fact touches both the proposed anchor and a cited
                # parent anchor. Merely co-occurring in the prompt is not lineage.
                parent_anchor_ids = {item.anchor_id for item in cited_parents}
                if not any(
                    cls._fact_touches_anchor(fact, anchor.uuid, anchor.business_id)
                    and bool(
                        parent_anchor_ids.intersection(
                            {
                                fact.source_business_id,
                                fact.target_business_id,
                            }
                        )
                    )
                    for fact in valid_mechanisms
                ):
                    continue
                root_signal_ids = list(
                    dict.fromkeys(item for parent in cited_parents for item in parent.root_signal_fact_ids)
                )
                root_event_ids = list(dict.fromkeys(item for parent in cited_parents for item in parent.root_event_ids))
                if not root_signal_ids or not root_event_ids:
                    continue
                confidence_cap = cls._degrade_confidence(
                    cls._minimum_confidence([parent.confidence for parent in cited_parents])
                )
                derivation = "CROSS_LAYER"
                validated_source_facts = []
                validated_mechanism_facts = list(dict.fromkeys(item.uuid for item in valid_mechanisms))
                validated_parent_claims = list(dict.fromkeys(item.claim_id for item in cited_parents))

            if proposal.assumptions:
                confidence_cap = cls._degrade_confidence(confidence_cap)
            confidence = cls._minimum_confidence([proposal.confidence, confidence_cap])
            key = (
                proposal.anchor_id,
                accepted_variable_id,
                accepted_direction,
                tuple(sorted(accepted_horizons, key=lambda item: item.value)),
            )
            if key in seen:
                continue
            seen.add(key)
            canonical_proposal = proposal.model_copy(
                update={
                    "variable_id": accepted_variable_id,
                    "direction": accepted_direction,
                    "horizons": accepted_horizons,
                    "source_fact_ids": validated_source_facts,
                    "mechanism_fact_ids": validated_mechanism_facts,
                    "parent_claim_ids": validated_parent_claims,
                }
            )
            claim_id = cls._claim_id(layer, canonical_proposal)
            accepted.append(
                AcceptedImpactClaim(
                    **proposal.model_dump(
                        exclude={
                            "confidence",
                            "variable_id",
                            "direction",
                            "horizons",
                            "source_fact_ids",
                            "mechanism_fact_ids",
                            "parent_claim_ids",
                        }
                    ),
                    variable_id=accepted_variable_id,
                    direction=accepted_direction,
                    horizons=accepted_horizons,
                    confidence=confidence,
                    source_fact_ids=validated_source_facts,
                    mechanism_fact_ids=validated_mechanism_facts,
                    parent_claim_ids=validated_parent_claims,
                    claim_id=claim_id,
                    layer=layer,
                    anchor_name=anchor.name,
                    anchor_type=anchor.entity_type,
                    derivation=derivation,
                    root_event_ids=root_event_ids,
                    root_signal_fact_ids=root_signal_ids,
                )
            )
        return accepted

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

    @staticmethod
    def _claim_id(layer: ImpactLayer, proposal) -> str:
        payload = proposal.model_dump(mode="json")
        payload["layer"] = layer.value
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:20]
        return f"CLAIM-{digest}"

    @classmethod
    def validate_cross_layer_batch(
        cls,
        context: LayerAnalysisContext,
        source_claims: list[AcceptedImpactClaim],
        target_claims: list[AcceptedImpactClaim],
        batch: CrossLayerTransmissionBatch,
    ) -> CrossLayerAnalysisResult:
        """Separate causal bridges from same-source context and unresolved hypotheses."""

        sources = {item.claim_id: item for item in source_claims}
        targets = {item.claim_id: item for item in target_claims}
        facts = {item.uuid: item for item in context.facts}
        accepted: list[AcceptedCrossLayerTransmission] = []
        candidates: list[CandidateCrossLayerMechanism] = []
        seen: set[tuple[str, str]] = set()
        for proposal in batch.proposals:
            source = sources.get(proposal.source_claim_id)
            target = targets.get(proposal.target_claim_id)
            if source is None or target is None or target.layer != context.layer:
                continue
            key = (source.claim_id, target.claim_id)
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
            bridge = next(
                (
                    fact
                    for fact in mechanisms
                    if cls._fact_touches_anchor(fact, source.anchor_id, source.anchor_id)
                    and cls._fact_touches_anchor(fact, target.anchor_id, target.anchor_id)
                ),
                None,
            )
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
                "source_claim_id": source.claim_id,
                "target_claim_id": target.claim_id,
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
        root_claims: list[AcceptedImpactClaim] | None = None,
    ) -> list[AcceptedTransmission]:
        chains = {item.business_id: item for item in context.chains}
        eligible_signals = {fact.uuid: fact for fact in context.facts if fact.uuid in context.eligible_signal_fact_ids}
        accepted_by_id = {item.transmission_id: item for item in accepted}
        claims_by_id = {item.claim_id: item for item in (root_claims or [])}
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
                cited_claims = [
                    claims_by_id[item]
                    for item in proposal.source_claim_ids
                    if item in claims_by_id
                    and claims_by_id[item].anchor_type == "ChainNode"
                    and claims_by_id[item].anchor_id == proposal.source_node_id
                    and proposal.horizon in claims_by_id[item].horizons
                ]
                if not cited and not cited_claims:
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
                        + [root for claim in cited_claims for root in claim.root_signal_fact_ids]
                    )
                )
                confidence_cap = cls._degrade_confidence(
                    cls._minimum_confidence(
                        [item.confidence for item in cited] + [item.confidence for item in cited_claims]
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
        industry_claims: list[AcceptedImpactClaim] | None = None,
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
                        industry_claims or [],
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
        industry_claims: list[AcceptedImpactClaim] | None = None,
    ) -> NodeTrendView:
        direct_signals = [
            fact
            for fact in context.facts
            if fact.uuid in context.eligible_signal_fact_ids
            and (fact.source_business_id == node_id or fact.target_business_id == node_id)
        ]
        incoming = [item for item in transmissions if item.chain_id == chain_id and item.target_node_id == node_id]
        direct_claims = [
            item
            for item in (industry_claims or [])
            if item.layer == ImpactLayer.INDUSTRY
            and item.anchor_type == "ChainNode"
            and item.anchor_id == node_id
            and set(item.root_signal_fact_ids) <= context.eligible_signal_fact_ids
        ]
        cited_signal_ids = set(node.supporting_fact_ids) & {fact.uuid for fact in direct_signals}
        cited_transmission_ids = set(node.supporting_transmission_ids) & {item.transmission_id for item in incoming}
        cited_claim_ids = set(node.supporting_claim_ids) & {item.claim_id for item in direct_claims}
        cited_signals = [item for item in direct_signals if item.uuid in cited_signal_ids]
        cited_incoming = [item for item in incoming if item.transmission_id in cited_transmission_ids]
        cited_claims = [item for item in direct_claims if item.claim_id in cited_claim_ids]
        supported_horizons = (
            {horizon for fact in cited_signals for horizon in fact.horizons}
            | {item.horizon for item in cited_incoming}
            | {horizon for claim in cited_claims for horizon in claim.horizons}
        )
        updates: dict[str, object] = {
            "chain_id": chain_id,
            "node_id": node_id,
            "supporting_fact_ids": list(
                dict.fromkeys(item for item in node.supporting_fact_ids if item in cited_signal_ids)
            ),
            "supporting_claim_ids": list(
                dict.fromkeys(item for item in node.supporting_claim_ids if item in cited_claim_ids)
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
        industry_claims: list[AcceptedImpactClaim] | None = None,
    ) -> list[str]:
        """Return hard-gate failures for every directional node/horizon claim."""

        active_signals = {item.uuid: item for item in context.facts if item.uuid in context.eligible_signal_fact_ids}
        transmissions_by_id = {item.transmission_id: item for item in transmissions}
        claims_by_id = {item.claim_id: item for item in (industry_claims or [])}
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
                    layer_claim = any(
                        claim_id in claims_by_id
                        and claims_by_id[claim_id].layer == ImpactLayer.INDUSTRY
                        and claims_by_id[claim_id].anchor_type == "ChainNode"
                        and claims_by_id[claim_id].anchor_id == node.node_id
                        and horizon in claims_by_id[claim_id].horizons
                        and set(claims_by_id[claim_id].root_signal_fact_ids) <= set(active_signals)
                        for claim_id in node.supporting_claim_ids
                    )
                    if not direct and not propagated and not layer_claim:
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
        claims = [claim for result in layer_results for claim in result.claims]
        mechanism_ids = {fact_id for claim in claims for fact_id in claim.mechanism_fact_ids}
        nodes.extend(
            ReasoningTraceNode(
                node_id=fact.uuid,
                node_type="FACT",
                label=f"{fact.source_name} —{fact.name}→ {fact.target_name}：{fact.fact}"[:1600],
                parent_ids=[item for item in fact.source_event_ids if item],
            )
            for fact in context.facts
            if fact.uuid in mechanism_ids
        )
        nodes.extend(
            ReasoningTraceNode(
                node_id=claim.claim_id,
                node_type="LAYER_CLAIM",
                label=f"[{claim.layer.value}] {claim.anchor_name} {claim.direction.value}：{claim.summary}"[:1600],
                parent_ids=list(
                    dict.fromkeys([*claim.parent_claim_ids, *claim.mechanism_fact_ids, *claim.root_signal_fact_ids])
                ),
            )
            for claim in claims
        )
        nodes.extend(
            ReasoningTraceNode(
                node_id=item.transmission_id,
                node_type="TRANSMISSION",
                label=f"{item.source_node_id} → {item.target_node_id}：{item.mechanism}"[:1600],
                parent_ids=list(
                    dict.fromkeys([*item.source_claim_ids, *item.parent_transmission_ids, *item.root_signal_fact_ids])
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
                                    *node.supporting_claim_ids,
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
