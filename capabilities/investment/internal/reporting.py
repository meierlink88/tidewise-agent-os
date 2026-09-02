"""Deterministically project reviewed reasoning into an AgentOS Report Artifact."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

from capabilities.event.internal.storage import event_artifact_root
from capabilities.investment.internal.models import (
    TRANSMISSION_CONTINUATION_THRESHOLD,
    TRANSMISSION_INCLUSION_THRESHOLD,
    Confidence,
    Direction,
    FactSnapshot,
    Horizon,
    ImpactLayer,
    InvestmentAnalysisContext,
    InvestmentConclusionArtifact,
    LayerAssessment,
    NodeTrendView,
    Trend,
)
from capabilities.investment.internal.report_contract import (
    InvestmentReportArtifact,
    ReportAnchor,
    ReportCandidateMechanism,
    ReportCard,
    ReportChainUncertainty,
    ReportCompanyBoundary,
    ReportConfidence,
    ReportContent,
    ReportDownwardTransmission,
    ReportEvidenceReference,
    ReportImpactItem,
    ReportIndustryChain,
    ReportIndustryChainEdge,
    ReportIndustryChainNode,
    ReportLayer,
    ReportLayerUncertainty,
    ReportNature,
    ReportReasoningStep,
    ReportResult,
    ReportStatistics,
    ReportTargetReference,
    ReportTransmissionPath,
    ReportTransmissionTarget,
)


class ReportNotPublishable(ValueError):
    """The reviewed run cannot satisfy the fixed product contract without invention."""


def _key(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _confidence(value: Confidence) -> ReportConfidence:
    return ReportConfidence(label={Confidence.LOW: "低", Confidence.MEDIUM: "中", Confidence.HIGH: "高"}[value])


def _direction(value: Direction) -> str:
    return {
        Direction.UP: "上行",
        Direction.DOWN: "下行",
        Direction.MIXED: "分化",
        Direction.STABLE: "平稳",
        Direction.UNKNOWN: "方向待验证",
    }[value]


def _minimum_confidence(values: list[Confidence]) -> Confidence:
    rank = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
    return min(values, key=rank.__getitem__) if values else Confidence.LOW


def _result_from_directions(values: list[Direction]) -> ReportResult:
    present = set(values)
    if Direction.MIXED in present or {Direction.UP, Direction.DOWN} <= present:
        return ReportResult(code="diverging", label="分化")
    if Direction.UP in present:
        return ReportResult(code="warming", label="升温")
    if Direction.DOWN in present:
        return ReportResult(code="cooling", label="降温")
    return ReportResult(code="pending", label="待验证")


def _result_from_trends(values: list[Trend]) -> ReportResult:
    present = {item for item in values if item not in {Trend.INSUFFICIENT_EVIDENCE, Trend.NO_MATERIAL_CHANGE}}
    if Trend.DIVERGENT in present or {Trend.WARMING, Trend.COOLING} <= present:
        return ReportResult(code="diverging", label="分化")
    if Trend.WARMING in present:
        return ReportResult(code="warming", label="升温")
    if Trend.COOLING in present:
        return ReportResult(code="cooling", label="降温")
    return ReportResult(code="pending", label="待验证")


def _horizon(values: list[Horizon]) -> str:
    labels = {Horizon.SHORT: "短期", Horizon.MEDIUM: "中期", Horizon.LONG: "长期"}
    ordered = [item for item in (Horizon.SHORT, Horizon.MEDIUM, Horizon.LONG) if item in set(values)]
    return "–".join(labels[item] for item in ordered) or "后续周期"


def _node_horizon(node: NodeTrendView) -> str:
    values = []
    for horizon, trend in ((Horizon.SHORT, node.short), (Horizon.MEDIUM, node.medium), (Horizon.LONG, node.long)):
        if trend != Trend.INSUFFICIENT_EVIDENCE:
            values.append(horizon)
    return _horizon(values)


def _nature(code: str) -> ReportNature:
    return {
        "direct_evidence": ReportNature(code="direct_evidence", label="直接证据"),
        "reasoning_hypothesis": ReportNature(code="reasoning_hypothesis", label="推理假设"),
        "pending_validation": ReportNature(code="pending_validation", label="待验证"),
    }[code]


def _refs(values: list[str], role: str = "直接依据") -> list[ReportEvidenceReference]:
    return [
        ReportEvidenceReference(evidence_id=value, role=role, display_order=index)
        for index, value in enumerate(dict.fromkeys(values), start=1)
    ]


class EventEvidenceIndex:
    """Resolve formal Event roots to the Evidence IDs frozen by Event Workflow artifacts."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or event_artifact_root()).resolve()

    def load(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        batches = self._root / "batches"
        if not batches.exists():
            return result
        for directory in sorted(item for item in batches.iterdir() if item.is_dir()):
            requests_path = directory / "identity-requests.json"
            publications_path = directory / "publications.json"
            if not requests_path.exists() or not publications_path.exists():
                continue
            try:
                requests = json.loads(requests_path.read_text(encoding="utf-8"))
                publications = json.loads(publications_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            evidence_by_key = {
                item.get("candidate_key"): item.get("candidate", {}).get("evidence_ids", [])
                for item in requests.get("requests", [])
                if isinstance(item, dict)
            }
            for item in publications.get("publications", []):
                if not isinstance(item, dict) or not item.get("event_id"):
                    continue
                evidence_ids = [
                    value
                    for value in evidence_by_key.get(item.get("candidate_key"), [])
                    if isinstance(value, str) and value.startswith("EVD")
                ]
                if evidence_ids:
                    result[item["event_id"]] = list(dict.fromkeys([*result.get(item["event_id"], []), *evidence_ids]))
        return result


class InvestmentReportAssembler:
    """Pure projection from reviewed audit + frozen graph snapshot to product Report."""

    def __init__(self, evidence_index: EventEvidenceIndex | None = None) -> None:
        self._evidence_index = evidence_index or EventEvidenceIndex()

    def assemble(
        self,
        analysis: InvestmentConclusionArtifact,
        context: InvestmentAnalysisContext,
    ) -> InvestmentReportArtifact:
        event_evidence = self._evidence_index.load()
        for event in context.events:
            if event.evidence_ids:
                event_evidence[event.event_id] = list(
                    dict.fromkeys([*event.evidence_ids, *event_evidence.get(event.event_id, [])])
                )
        fact_by_id = {item.uuid: item for item in context.facts}
        assessment_by_id = {
            item.assessment_id: item
            for item in [
                *analysis.geopolitical.assessments,
                *analysis.macro.assessments,
                *analysis.industry.assessments,
            ]
        }
        chain_keys = {item.business_id: _key("chain", item.business_id) for item in context.chains}
        node_keys: dict[tuple[str, str], str] = {
            (chain.business_id, node.business_id): _key("node", f"{chain.business_id}:{node.business_id}")
            for chain in context.chains
            for node in chain.nodes
        }
        node_refs: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for (chain_id, node_id), node_key in node_keys.items():
            node_refs[node_id].append((chain_keys[chain_id], node_key))
        node_membership_counts = Counter(node.business_id for chain in context.chains for node in chain.nodes)

        geo = self._layer(
            ImpactLayer.GEOPOLITICAL,
            analysis.geopolitical.assessments,
            analysis.geopolitical.summary,
            analysis.geopolitical.limitations,
            analysis,
            event_evidence,
            fact_by_id,
            assessment_by_id,
            chain_keys,
            node_refs,
        )
        macro = self._layer(
            ImpactLayer.MACRO_ECONOMIC,
            analysis.macro.assessments,
            analysis.macro.summary,
            analysis.macro.limitations,
            analysis,
            event_evidence,
            fact_by_id,
            assessment_by_id,
            chain_keys,
            node_refs,
        )
        chains = self._chains(
            analysis,
            context,
            event_evidence,
            fact_by_id,
            assessment_by_id,
            chain_keys,
            node_keys,
            node_membership_counts,
        )
        cards = self._cards(geo, macro, chains)
        pending_nodes = sum(1 for chain in chains for node in chain.nodes if node.result.code == "pending")
        signaled_nodes = {
            business_id
            for fact in context.facts
            if fact.kind == "SIGNAL" and fact.uuid in context.eligible_signal_fact_ids
            for business_id, labels in (
                (fact.source_business_id, fact.source_labels),
                (fact.target_business_id, fact.target_labels),
            )
            if business_id and "ChainNode" in labels
        }
        content = ReportContent(
            title="每日投研推理报告",
            generated_at=analysis.decision_at.astimezone(ZoneInfo("Asia/Shanghai")),
            included_layers=["geopolitics", "macroeconomics", "industry_chain"],
            statistics=ReportStatistics(
                event_count=len(context.events),
                ordinary_fact_count=sum(item.kind == "ORDINARY" for item in context.facts),
                signal_fact_count=sum(item.kind == "SIGNAL" for item in context.facts),
                transmission_hypothesis_count=len(analysis.transmissions),
                remaining_topology_pending_count=pending_nodes,
                adaptive_inclusion_threshold=float(
                    analysis.stage_metrics.get(
                        "transmission_inclusion_threshold",
                        TRANSMISSION_INCLUSION_THRESHOLD,
                    )
                ),
                adaptive_continuation_threshold=float(
                    analysis.stage_metrics.get(
                        "transmission_continuation_threshold",
                        TRANSMISSION_CONTINUATION_THRESHOLD,
                    )
                ),
                adaptive_hard_max_hops=context.request.max_hops,
                adaptive_observed_max_hops=max((item.hop for item in analysis.transmissions), default=0),
                adaptive_stopped_by_confidence=analysis.stage_metrics.get("transmission_stopped_by_confidence", 0),
                adaptive_stopped_by_no_unvisited_neighbor=analysis.stage_metrics.get(
                    "transmission_stopped_by_no_neighbor", 0
                ),
                adaptive_rejected_below_inclusion=analysis.stage_metrics.get(
                    "transmission_rejected_below_inclusion", 0
                ),
                geopolitic_anchor_count=len(geo.anchors),
                macroeconomic_anchor_count=len(macro.anchors),
                signaled_chain_node_count=len({item for item in signaled_nodes if item}),
                industry_chain_count=len(chains),
                unmapped_chain_node_count=0,
            ),
            report_cards=cards,
            geopolitics=geo,
            macroeconomics=macro,
            industry_chains=chains,
            company=ReportCompanyBoundary(),
        )
        return InvestmentReportArtifact(
            source_report_id=f"agentos-investment-{analysis.workflow_run_id}",
            content=content,
        )

    def _assessment_evidence(
        self,
        assessment: LayerAssessment,
        event_evidence: dict[str, list[str]],
    ) -> list[str]:
        return list(
            dict.fromkeys(
                evidence_id
                for event_id in assessment.root_event_ids
                for evidence_id in event_evidence.get(event_id, [])
            )
        )

    def _layer(
        self,
        layer: ImpactLayer,
        assessments: list[LayerAssessment],
        summary: str,
        limitations: list[str],
        analysis: InvestmentConclusionArtifact,
        event_evidence: dict[str, list[str]],
        fact_by_id: dict[str, FactSnapshot],
        assessment_by_id: dict[str, LayerAssessment],
        chain_keys: dict[str, str],
        node_refs: dict[str, list[tuple[str, str]]],
    ) -> ReportLayer:
        grouped: dict[str, list[LayerAssessment]] = defaultdict(list)
        for assessment in assessments:
            grouped[assessment.anchor_id].append(assessment)
        anchors: list[ReportAnchor] = []
        reasoning_steps: list[ReportReasoningStep] = []
        anchor_key_by_id = {anchor_id: _key("anchor", anchor_id) for anchor_id in grouped}
        for index, (anchor_id, items) in enumerate(sorted(grouped.items(), key=lambda pair: pair[1][0].anchor_name), 1):
            evidence = list(
                dict.fromkeys(
                    evidence_id
                    for assessment in items
                    for evidence_id in self._assessment_evidence(assessment, event_evidence)
                )
            )
            if not evidence:
                raise ReportNotPublishable(f"直接锚点 {items[0].anchor_name} 无法映射到正式 Evidence ID")
            states = [self._assessment_state(item, fact_by_id) for item in items]
            anchors.append(
                ReportAnchor(
                    key=anchor_key_by_id[anchor_id],
                    display_order=index,
                    name=items[0].anchor_name,
                    current_state="；".join(states),
                    result=_result_from_trends([item.result for item in items]),
                    nature=_nature("direct_evidence"),
                    reasoning="；".join(item.reasoning for item in items)[:10_000],
                    time_window=_horizon([value for item in items for value in item.horizons]),
                    confidence=_confidence(_minimum_confidence([item.confidence for item in items])),
                    evidence_refs=_refs(evidence),
                )
            )
            for assessment in items:
                assessment_evidence = self._assessment_evidence(assessment, event_evidence)
                reasoning_steps.append(
                    ReportReasoningStep(
                        key=_key("reason", assessment.assessment_id),
                        display_order=len(reasoning_steps) + 1,
                        input=assessment.summary,
                        mechanism=assessment.reasoning,
                        output=f"{assessment.anchor_name}：{self._assessment_state(assessment, fact_by_id)}",
                        type="Event → Signal → 锚点评估",
                        confidence=_confidence(assessment.confidence),
                        evidence_refs=_refs(assessment_evidence),
                    )
                )
        downward = self._downward(
            layer,
            analysis,
            assessment_by_id,
            anchor_key_by_id,
            chain_keys,
            node_refs,
        )
        all_evidence = list(
            dict.fromkeys(reference.evidence_id for anchor in anchors for reference in anchor.evidence_refs)
        )
        results = [item.result for item in assessments]
        confidence = _minimum_confidence([item.confidence for item in assessments])
        layer_key = "geopolitics" if layer == ImpactLayer.GEOPOLITICAL else "macroeconomics"
        return ReportLayer(
            key=layer_key,
            display_order=1 if layer == ImpactLayer.GEOPOLITICAL else 2,
            title="地缘政治" if layer == ImpactLayer.GEOPOLITICAL else "宏观经济",
            conclusion=summary,
            result=_result_from_trends(results),
            confidence=_confidence(confidence),
            time_window=_horizon([value for item in assessments for value in item.horizons]),
            anchors=anchors,
            reasoning_steps=reasoning_steps,
            related_anchor_keys=[item.key for item in anchors],
            related_chain_keys=list(
                dict.fromkeys(
                    target.ref.key
                    for path in downward.published_paths
                    for target in path.target_refs
                    if target.ref.type == "industry_chain"
                )
            ),
            downward_transmission=downward,
            uncertainty=ReportLayerUncertainty(
                counterevidence=None,
                evidence_gap=None,
                boundary="仅纳入具有真实图谱锚点和受控谱系的结论。",
                reversal_condition="若根 Signal 失效、方向反转或机制事实被否定，则修正本层结论。",
                checkpoints=[],
            ),
            evidence_refs=_refs(all_evidence),
        )

    def _downward(
        self,
        layer: ImpactLayer,
        analysis: InvestmentConclusionArtifact,
        assessment_by_id: dict[str, LayerAssessment],
        anchor_key_by_id: dict[str, str],
        chain_keys: dict[str, str],
        node_refs: dict[str, list[tuple[str, str]]],
    ) -> ReportDownwardTransmission:
        paths: list[ReportTransmissionPath] = []
        for item in analysis.cross_layer_transmissions:
            source = assessment_by_id.get(item.source_assessment_id)
            target = assessment_by_id.get(item.target_assessment_id)
            if source is None or target is None or source.layer != layer:
                continue
            targets: list[ReportTransmissionTarget] = []
            if target.layer == ImpactLayer.MACRO_ECONOMIC:
                target_key = _key("anchor", target.anchor_id)
                targets.append(
                    ReportTransmissionTarget(
                        ref=ReportTargetReference(type="anchor", key=target_key),
                        label=f"宏观经济 · {target.anchor_name}",
                        result=_result_from_trends([target.result]),
                    )
                )
            elif target.layer == ImpactLayer.INDUSTRY:
                for chain_key, node_key in node_refs.get(target.anchor_id, []):
                    targets.extend(
                        [
                            ReportTransmissionTarget(
                                ref=ReportTargetReference(type="industry_chain", key=chain_key),
                                label="产业链",
                                result=_result_from_trends([target.result]),
                            ),
                            ReportTransmissionTarget(
                                ref=ReportTargetReference(type="industry_chain_node", key=node_key),
                                label=target.anchor_name,
                                result=_result_from_trends([target.result]),
                            ),
                        ]
                    )
            unique: dict[tuple[str, str], ReportTransmissionTarget] = {}
            for target_ref in targets:
                unique[(target_ref.ref.type, target_ref.ref.key)] = target_ref
            if not unique:
                continue
            paths.append(
                ReportTransmissionPath(
                    key=_key("path", item.transmission_id),
                    display_order=len(paths) + 1,
                    source_conclusion=source.summary,
                    target_refs=list(unique.values()),
                    logic=item.logic,
                    relation_nature="跨层推理" if item.relation_type == "CROSS_LAYER" else "同源信号",
                    evidence_role="推导背景",
                    confidence=_confidence(item.confidence),
                    status=item.status,
                    evidence_refs=[],
                )
            )
        candidates: list[ReportCandidateMechanism] = []
        for candidate in analysis.cross_layer_candidates:
            source = assessment_by_id.get(candidate.source_assessment_id)
            if source is None or source.layer != layer:
                continue
            candidates.append(
                ReportCandidateMechanism(
                    key=_key(
                        "candidate",
                        f"{candidate.source_assessment_id}:{candidate.target_assessment_id}",
                    ),
                    display_order=len(candidates) + 1,
                    mechanism=candidate.logic,
                    evidence_gap=None,
                    confidence=_confidence(candidate.confidence),
                    evidence_refs=[],
                )
            )
        return ReportDownwardTransmission(
            summary="已闭合的跨层关系如下；其余仅保留为待验证机制。" if paths else "本期没有形成闭合的跨层传导关系。",
            published_paths=paths,
            candidate_mechanisms=candidates,
            boundary_notes=[],
        )

    def _chains(
        self,
        analysis: InvestmentConclusionArtifact,
        context: InvestmentAnalysisContext,
        event_evidence: dict[str, list[str]],
        fact_by_id: dict[str, FactSnapshot],
        assessment_by_id: dict[str, LayerAssessment],
        chain_keys: dict[str, str],
        node_keys: dict[tuple[str, str], str],
        node_membership_counts: Counter[str],
    ) -> list[ReportIndustryChain]:
        chain_snapshot = {item.business_id: item for item in context.chains}
        output: list[ReportIndustryChain] = []
        for order, chain_view in enumerate(analysis.draft.chains, 1):
            snapshot = chain_snapshot.get(chain_view.chain_id)
            if snapshot is None:
                continue
            supported_node_ids = {
                node.node_id
                for node in chain_view.nodes
                if node.supporting_fact_ids or node.supporting_assessment_ids or node.supporting_transmission_ids
            }
            adjacent_gap_ids: list[str] = []
            for edge in snapshot.edges:
                if edge.source_node_id in supported_node_ids and edge.target_node_id not in supported_node_ids:
                    adjacent_gap_ids.append(edge.target_node_id)
                if edge.target_node_id in supported_node_ids and edge.source_node_id not in supported_node_ids:
                    adjacent_gap_ids.append(edge.source_node_id)
            displayed_node_ids = supported_node_ids | set(dict.fromkeys(adjacent_gap_ids[:3]))
            if not supported_node_ids:
                continue
            nodes: list[ReportIndustryChainNode] = []
            displayed_source_nodes: list[NodeTrendView] = []
            for node in chain_view.nodes:
                if node.node_id not in displayed_node_ids:
                    continue
                displayed_source_nodes.append(node)
                node_order = len(nodes) + 1
                direct_fact_ids = [
                    fact_id
                    for fact_id in node.supporting_fact_ids
                    if fact_id in fact_by_id and fact_by_id[fact_id].kind == "SIGNAL"
                ]
                direct_assessments = [
                    assessment_by_id[assessment_id]
                    for assessment_id in node.supporting_assessment_ids
                    if assessment_id in assessment_by_id
                ]
                evidence = list(
                    dict.fromkeys(
                        evidence_id
                        for event_id in [
                            *[event for fact_id in direct_fact_ids for event in fact_by_id[fact_id].source_event_ids],
                            *[event for assessment in direct_assessments for event in assessment.root_event_ids],
                        ]
                        for evidence_id in event_evidence.get(event_id, [])
                    )
                )
                if (direct_fact_ids or direct_assessments) and not evidence:
                    raise ReportNotPublishable(f"直接产业链节点 {node.node_name} 无法映射到正式 Evidence ID")
                result = _result_from_trends([node.short, node.medium, node.long])
                if direct_fact_ids or direct_assessments:
                    nature = _nature("direct_evidence")
                elif node.supporting_transmission_ids:
                    nature = _nature("reasoning_hypothesis")
                else:
                    nature = _nature("pending_validation")
                impact = self._node_impact(node, direct_fact_ids, fact_by_id)
                nodes.append(
                    ReportIndustryChainNode(
                        key=node_keys[(chain_view.chain_id, node.node_id)],
                        display_order=node_order,
                        name=node.node_name,
                        impact=impact,
                        result=result,
                        nature=nature,
                        reasoning=node.rationale,
                        time_window=_node_horizon(node),
                        confidence=_confidence(node.confidence),
                        evidence_refs=_refs(evidence),
                    )
                )
            edge_items: list[ReportIndustryChainEdge] = []
            for edge in snapshot.edges:
                from_key = node_keys.get((snapshot.business_id, edge.source_node_id))
                to_key = node_keys.get((snapshot.business_id, edge.target_node_id))
                if (
                    edge.source_node_id not in displayed_node_ids
                    or edge.target_node_id not in displayed_node_ids
                    or from_key is None
                    or to_key is None
                    or from_key == to_key
                ):
                    continue
                edge_items.append(
                    ReportIndustryChainEdge(
                        key=_key("edge", f"{snapshot.business_id}:{edge.business_id}"),
                        display_order=len(edge_items) + 1,
                        from_node_key=from_key,
                        to_node_key=to_key,
                        relation_label={
                            "ChainNodeInputTo": "投入",
                            "ChainNodeIsComponentOf": "组成",
                            "ChainNodeDependsOn": "依赖",
                        }.get(edge.name, edge.name),
                    )
                )
            direct_nodes = [item.name for item in nodes if item.nature.code == "direct_evidence"]
            hypothesis_nodes = [item.name for item in nodes if item.nature.code == "reasoning_hypothesis"]
            chain_evidence = list(
                dict.fromkeys(reference.evidence_id for node in nodes for reference in node.evidence_refs)
            )
            output.append(
                ReportIndustryChain(
                    key=chain_keys[chain_view.chain_id],
                    claim_key=_key("claim", chain_view.chain_id),
                    display_order=order,
                    name=chain_view.chain_name,
                    conclusion=chain_view.summary,
                    status=self._chain_status(direct_nodes, hypothesis_nodes),
                    result=_result_from_trends([chain_view.short, chain_view.medium, chain_view.long]),
                    confidence=_confidence(chain_view.confidence),
                    time_window=self._chain_horizon(chain_view.nodes),
                    path_summary=(
                        "、".join(direct_nodes) + "（直接 Signal 节点）→ 真实同链拓扑节点" if direct_nodes else None
                    ),
                    accepted_hypothesis_summary="；".join(hypothesis_nodes) or None,
                    evidence_refs=_refs(chain_evidence),
                    nodes=nodes,
                    edges=edge_items,
                    uncertainty=ReportChainUncertainty(
                        counterevidence_and_gap=self._chain_counterevidence_and_gap(
                            displayed_source_nodes,
                            nodes,
                            node_membership_counts,
                        ),
                        stop_condition="若根 Signal 失效、方向反转或链语境不成立，则关闭或修正本链结论。",
                        checkpoints=[],
                    ),
                )
            )
        return output

    @staticmethod
    def _chain_counterevidence_and_gap(
        source_nodes: list[NodeTrendView],
        report_nodes: list[ReportIndustryChainNode],
        node_membership_counts: Counter[str],
    ) -> str:
        counterevidence = list(
            dict.fromkeys(
                risk.strip()
                for node in source_nodes
                for risk in node.risks
                if risk.strip()
                and not all(character in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in risk.strip())
            )
        )
        pending = [node.name for node in report_nodes if node.result.code == "pending"]
        has_hypothesis = any(node.nature.code == "reasoning_hypothesis" for node in report_nodes)
        has_shared_node = any(node_membership_counts[node.node_id] > 1 for node in source_nodes)
        parts = list(counterevidence[:5])
        if has_shared_node:
            parts.append("共享节点的具体链语境尚未解析")
        if has_hypothesis:
            parts.append("上述传导为经路径评分筛选的推理假设，仍需目标节点的订单、价格、产能或经营数据验证")
            parts.append("未被推导的相邻节点继续作为 Evidence Gap")
        elif pending:
            parts.append("同链相邻节点缺少直接 Variable Signal 与经营观测")
        if not parts:
            parts.append("本期未形成独立反证或额外 Evidence Gap")
        return "；".join(parts) + "。"

    @staticmethod
    def _node_impact(node: NodeTrendView, fact_ids: list[str], facts: dict[str, FactSnapshot]) -> str:
        values = []
        for fact_id in fact_ids:
            fact = facts[fact_id]
            if fact.variable_id and fact.direction is not None and fact.confidence is not None:
                values.append(
                    f"{fact.source_name}：{_direction(fact.direction)}（置信度{_confidence(fact.confidence).label}）"
                )
        if values:
            return "；".join(dict.fromkeys(values))
        if node.supporting_transmission_ids:
            return "由同链上游或下游节点影响传导至此"
        return "真实同链拓扑相邻，尚无直接 Signal"

    @staticmethod
    def _assessment_state(assessment: LayerAssessment, facts: dict[str, FactSnapshot]) -> str:
        values = []
        for fact_id in assessment.direct_signal_fact_ids:
            fact = facts.get(fact_id)
            if fact is None or fact.kind != "SIGNAL" or fact.direction is None:
                continue
            variable = fact.source_name or fact.variable_id or "Variable"
            values.append(f"{variable}：{_direction(fact.direction)}")
        return "；".join(dict.fromkeys(values)) or f"综合结果：{assessment.result.value}"

    @staticmethod
    def _chain_status(direct_nodes: list[str], hypothesis_nodes: list[str]) -> str:
        if direct_nodes and hypothesis_nodes:
            return "直接节点 Signal 明确；已形成传导假设，其余相邻节点继续待验证"
        if direct_nodes:
            return "直接节点 Signal 明确；其余相邻节点继续待验证"
        if hypothesis_nodes:
            return "仅形成传导假设，仍需直接证据验证"
        return "待验证"

    @staticmethod
    def _chain_horizon(nodes: list[NodeTrendView]) -> str:
        horizons = []
        for node in nodes:
            if node.short != Trend.INSUFFICIENT_EVIDENCE:
                horizons.append(Horizon.SHORT)
            if node.medium != Trend.INSUFFICIENT_EVIDENCE:
                horizons.append(Horizon.MEDIUM)
            if node.long != Trend.INSUFFICIENT_EVIDENCE:
                horizons.append(Horizon.LONG)
        return _horizon(horizons)

    @staticmethod
    def _cards(
        geo: ReportLayer,
        macro: ReportLayer,
        chains: list[ReportIndustryChain],
    ) -> list[ReportCard]:
        cards: list[ReportCard] = []
        for layer, kind, subtitle in (
            (geo, "geopolitics", "地缘政治层"),
            (macro, "macroeconomics", "宏观经济层"),
        ):
            if not layer.anchors:
                continue
            cards.append(
                ReportCard(
                    key=f"{layer.key}-card",
                    kind=kind,
                    display_order=len(cards) + 1,
                    detail_ref=ReportTargetReference(type="layer", key=layer.key),
                    title=layer.title,
                    subtitle=subtitle,
                    conclusion=layer.conclusion,
                    result=layer.result,
                    confidence=layer.confidence,
                    time_window=layer.time_window,
                    impact_items=[
                        ReportImpactItem(
                            ref=ReportTargetReference(type="anchor", key=anchor.key),
                            name=anchor.name,
                            result=anchor.result,
                            confidence=anchor.confidence,
                            time_window=anchor.time_window,
                        )
                        for anchor in layer.anchors
                    ],
                    evidence_refs=layer.evidence_refs,
                )
            )
        rank = {"warming": 0, "cooling": 0, "diverging": 1, "pending": 2}
        for chain in sorted(chains, key=lambda item: (rank[item.result.code], item.name))[:5]:
            supported = [item for item in chain.nodes if item.result.code != "pending"]
            if not supported:
                continue
            cards.append(
                ReportCard(
                    key=f"{chain.key}-card",
                    kind="industry_chain",
                    display_order=len(cards) + 1,
                    detail_ref=ReportTargetReference(type="industry_chain", key=chain.key),
                    title=chain.name,
                    subtitle="产业链",
                    conclusion=chain.conclusion,
                    result=chain.result,
                    confidence=chain.confidence,
                    time_window=chain.time_window,
                    impact_items=[
                        ReportImpactItem(
                            ref=ReportTargetReference(type="industry_chain_node", key=node.key),
                            name=node.name,
                            result=node.result,
                            confidence=node.confidence,
                            time_window=node.time_window,
                        )
                        for node in supported[:5]
                    ],
                    evidence_refs=chain.evidence_refs,
                )
            )
        return cards
