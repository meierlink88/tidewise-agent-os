"""Contract tests for the Schedule-driven investment reasoning Workflow."""

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from agno.agent import Agent
from agno.run import RunContext
from agno.workflow import Step, StepInput, Workflow
from pydantic import ValidationError

from agents.investment_reasoner import (
    INVESTMENT_REASONER_CONTRACT_VERSION,
    ensure_investment_reasoner_agent,
)
from agents.investment_reviewer import (
    INVESTMENT_REVIEWER_CONTRACT_VERSION,
    ensure_investment_reviewer_agent,
)
from app.registry import TidewiseRegistry, registry
from capabilities.investment import (
    AcceptedTransmission,
    AnalysisAnchorSnapshot,
    AnalysisDraft,
    ChainNodeSnapshot,
    ChainTrendView,
    Confidence,
    Direction,
    EventSnapshot,
    FactSnapshot,
    GeopoliticalAnalysisState,
    Horizon,
    ImpactLayer,
    IndustryAnalysisState,
    IndustryChainSnapshot,
    InvestmentAnalysisContext,
    InvestmentAnalysisRequest,
    InvestmentAssessment,
    InvestmentConclusionArtifact,
    InvestmentReasoningInput,
    LayerAnalysisContext,
    LayerAnalysisResult,
    LayerAssessment,
    LayerAssessmentBatch,
    LayerAssessmentProposal,
    MacroAnalysisState,
    NodeAnalysisBatch,
    NodeTrendView,
    PreparedInvestmentContext,
    ReasoningOntologyContext,
    RetrievalReceipt,
    ReviewedInvestmentState,
    ReviewResult,
    TopologyEdgeSnapshot,
    TransmissionBatch,
    TransmissionProposal,
    Trend,
    configure_investment_workflow_runtime,
)
from capabilities.investment.functions import (
    analyze_geopolitical_impact,
    analyze_industry_impact,
    analyze_macro_impact,
    generate_investment_report,
    prepare_investment_context,
    publish_investment_report,
    review_and_finalize,
)
from capabilities.investment.internal.context import InvestmentContextBuilder
from capabilities.investment.internal.engine import InvestmentReasoningEngine
from capabilities.investment.internal.local_runtime import LocalInvestmentWorkflowRuntime, _bounded_text, _payload
from capabilities.investment.internal.storage import write_conclusion_artifact
from sematica.graphiti.investment import GraphitiInvestmentReader
from workflows.investment_reasoning import (
    INVESTMENT_REASONING_CONTRACT_VERSION,
    _seed_workflow,
    ensure_investment_reasoning_workflow,
)


class InvestmentReasoningGateTest(unittest.TestCase):
    def _context(self, fact: FactSnapshot) -> InvestmentAnalysisContext:
        return InvestmentAnalysisContext(
            request=InvestmentAnalysisRequest(
                question="分析最近48小时事件对相关产业链节点投资价值的影响",
                decision_at=datetime(2026, 8, 29, 0, 0, tzinfo=UTC),
            ),
            events=[
                EventSnapshot(
                    episode_uuid="episode-1",
                    event_id="event-1",
                    title="测试事件",
                    summary="测试事件摘要",
                    modality="FACT",
                    occurred_at=datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
                )
            ],
            facts=[fact],
            ontology=LayerAssessmentContractTest._ontology(),
            retrieval_receipts=[
                RetrievalReceipt(
                    stage=stage,
                    layer=ImpactLayer(stage) if stage != "PREPARE" else None,
                    required_actions=["load_test_context"],
                    completed_actions=["load_test_context"],
                    event_ids=["event-1"],
                    fact_ids=[fact.uuid],
                    direct_signal_fact_ids=[fact.uuid] if fact.kind == "SIGNAL" else [],
                )
                for stage in ["PREPARE", "GEOPOLITICAL", "MACRO_ECONOMIC", "INDUSTRY"]
            ],
            chains=[
                IndustryChainSnapshot(
                    uuid="chain-uuid",
                    business_id="chain-1",
                    name="测试产业链",
                    anchor_match_count=1,
                    matched_node_ids=["node-a"],
                    signal_root_fact_ids=[fact.uuid] if fact.kind == "SIGNAL" else [],
                    signal_root_node_ids=["node-a"] if fact.kind == "SIGNAL" else [],
                    nodes=[
                        ChainNodeSnapshot(uuid="node-a-uuid", business_id="node-a", name="上游"),
                        ChainNodeSnapshot(uuid="node-b-uuid", business_id="node-b", name="下游"),
                    ],
                    edges=[
                        TopologyEdgeSnapshot(
                            uuid="edge-uuid",
                            business_id="edge-1",
                            name="ChainNodeInputTo",
                            source_node_id="node-a",
                            source_name="上游",
                            target_node_id="node-b",
                            target_name="下游",
                            fact="上游向下游提供投入品",
                        )
                    ],
                )
            ],
        )

    def _proposal(self, fact_id: str) -> TransmissionBatch:
        return TransmissionBatch(
            proposals=[
                TransmissionProposal(
                    chain_id="chain-1",
                    topology_edge_id="edge-1",
                    source_node_id="node-a",
                    target_node_id="node-b",
                    flow="ALONG_EDGE",
                    target_variable="下游供给",
                    direction=Direction.DOWN,
                    horizon=Horizon.SHORT,
                    confidence=Confidence.MEDIUM,
                    mechanism="上游收缩沿真实投入关系传导至下游。",
                    source_fact_ids=[fact_id],
                )
            ]
        )

    def test_ordinary_fact_cannot_start_directional_transmission(self) -> None:
        fact = FactSnapshot(
            uuid="ordinary-1",
            kind="ORDINARY",
            name="SUPPLIES",
            fact="上游向下游供货",
            source_uuid="node-a-uuid",
            source_name="上游",
            source_business_id="node-a",
            source_labels=["Entity", "ChainNode"],
            target_uuid="node-b-uuid",
            target_name="下游",
            target_business_id="node-b",
            target_labels=["Entity", "ChainNode"],
            source_event_ids=["event-1"],
        )

        accepted = InvestmentReasoningEngine.validate_round(
            self._context(fact), [], self._proposal(fact.uuid), round_number=1
        )

        self.assertEqual(accepted, [])

    def test_active_signal_fact_can_start_and_carries_root_lineage(self) -> None:
        fact = FactSnapshot(
            uuid="signal-1",
            kind="SIGNAL",
            name="SIGNAL_ON",
            fact="有效产能下降作用于上游节点",
            source_uuid="variable-uuid",
            source_name="有效产能",
            source_business_id="variable-1",
            source_labels=["Entity", "Variable"],
            target_uuid="node-a-uuid",
            target_name="上游",
            target_business_id="node-a",
            target_labels=["Entity", "ChainNode"],
            source_event_ids=["event-1"],
            direction=Direction.DOWN,
            horizons=[Horizon.SHORT],
            confidence=Confidence.HIGH,
            valid_at=datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
        )

        accepted = InvestmentReasoningEngine.validate_round(
            self._context(fact), [], self._proposal(fact.uuid), round_number=1
        )

        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].root_signal_fact_ids, [fact.uuid])
        self.assertEqual(accepted[0].confidence, Confidence.MEDIUM)

    def test_candidate_enumeration_covers_every_real_adjacent_edge(self) -> None:
        fact = self._active_signal()
        context = self._context(fact)
        chain = context.chains[0]
        extended = chain.model_copy(
            update={
                "nodes": [
                    *chain.nodes,
                    ChainNodeSnapshot(uuid="node-c-uuid", business_id="node-c", name="另一邻点"),
                ],
                "edges": [
                    *chain.edges,
                    TopologyEdgeSnapshot(
                        uuid="edge-2-uuid",
                        business_id="edge-2",
                        name="ChainNodeDependsOn",
                        source_node_id="node-c",
                        source_name="另一邻点",
                        target_node_id="node-a",
                        target_name="上游",
                        fact="另一邻点依赖上游",
                    ),
                ],
            }
        )
        context = context.model_copy(update={"chains": [extended]})

        candidates = InvestmentReasoningEngine.enumerate_transmission_candidates(context, [], round_number=1)

        self.assertEqual({item.target_node_id for item in candidates}, {"node-b", "node-c"})
        self.assertEqual({item.flow for item in candidates}, {"ALONG_EDGE", "AGAINST_EDGE"})

    def test_medium_root_uses_path_score_to_continue_even_when_enum_confidence_is_low(self) -> None:
        fact = self._active_signal().model_copy(update={"confidence": Confidence.MEDIUM})
        context = self._context(fact)
        chain = context.chains[0]
        chain = chain.model_copy(
            update={
                "nodes": [
                    *chain.nodes,
                    ChainNodeSnapshot(uuid="node-c-uuid", business_id="node-c", name="终端"),
                ],
                "edges": [
                    *chain.edges,
                    TopologyEdgeSnapshot(
                        uuid="edge-2-uuid",
                        business_id="edge-2",
                        name="ChainNodeInputTo",
                        source_node_id="node-b",
                        source_name="下游",
                        target_node_id="node-c",
                        target_name="终端",
                        fact="下游向终端提供投入品",
                    ),
                ],
            }
        )
        context = context.model_copy(update={"chains": [chain]})
        first_candidate = InvestmentReasoningEngine.enumerate_transmission_candidates(context, [], round_number=1)[0]
        proposal = (
            self._proposal(fact.uuid)
            .proposals[0]
            .model_copy(update={"candidate_id": first_candidate.candidate_id, "confidence": Confidence.MEDIUM})
        )
        first = InvestmentReasoningEngine.validate_round(
            context,
            [],
            TransmissionBatch(proposals=[proposal]),
            round_number=1,
            candidates=[first_candidate],
        )[0]

        second_candidates = InvestmentReasoningEngine.enumerate_transmission_candidates(
            context, [first], round_number=2
        )

        self.assertEqual(first.confidence, Confidence.LOW)
        self.assertEqual(first.path_score, 0.7)
        self.assertEqual([item.target_node_id for item in second_candidates], ["node-c"])

    def test_chain_trend_aggregation_preserves_mixed_node_directions(self) -> None:
        self.assertEqual(
            InvestmentReasoningEngine._reduce_trend([Trend.WARMING, Trend.COOLING]),
            Trend.DIVERGENT,
        )

    def test_run_level_batch_can_merge_more_than_one_hundred_chain_results(self) -> None:
        proposal = self._proposal(self._active_signal().uuid).proposals[0]

        batch = TransmissionBatch(proposals=[proposal] * 134)

        self.assertEqual(len(batch.proposals), 134)

    def test_industry_chain_signal_cannot_start_directional_reasoning(self) -> None:
        fact = self._active_signal().model_copy(
            update={
                "uuid": "legacy-chain-signal",
                "anchor_type": "IndustryChain",
                "target_uuid": "chain-uuid",
                "target_name": "测试产业链",
                "target_business_id": "chain-1",
                "target_labels": ["Entity", "IndustryChain"],
            }
        )

        context = self._context(fact)

        self.assertFalse(fact.is_active_signal(context.request.decision_at))
        self.assertEqual(context.eligible_signal_fact_ids, set())
        self.assertEqual(
            InvestmentReasoningEngine.validate_round(
                context,
                [],
                self._proposal(fact.uuid),
                round_number=1,
            ),
            [],
        )

    def test_first_hop_may_express_an_inferred_horizon_without_rewriting_the_signal(self) -> None:
        fact = self._active_signal()
        proposal = self._proposal(fact.uuid).proposals[0].model_copy(update={"horizon": Horizon.LONG})

        accepted = InvestmentReasoningEngine.validate_round(
            self._context(fact), [], TransmissionBatch(proposals=[proposal]), round_number=1
        )

        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].horizon, Horizon.LONG)
        self.assertEqual(accepted[0].root_signal_fact_ids, [fact.uuid])

    def test_topology_flow_must_match_the_real_edge_direction(self) -> None:
        fact = self._active_signal()
        proposal = self._proposal(fact.uuid).proposals[0]
        wrong_flow = proposal.model_copy(update={"flow": "AGAINST_EDGE"})

        accepted = InvestmentReasoningEngine.validate_round(
            self._context(fact), [], TransmissionBatch(proposals=[wrong_flow]), round_number=1
        )

        self.assertEqual(accepted, [])

    def test_unscoped_event_signals_cannot_start_directional_transmission(self) -> None:
        for label, context, signal in self._unscoped_signal_contexts():
            with self.subTest(source_scope=label):
                accepted = InvestmentReasoningEngine.validate_round(
                    context,
                    [],
                    self._proposal(signal.uuid),
                    round_number=1,
                )

                self.assertEqual(accepted, [])

    def test_later_hop_requires_previous_same_chain_same_horizon_parent(self) -> None:
        fact = self._active_signal()
        context = self._context(fact)
        first = InvestmentReasoningEngine.validate_round(context, [], self._proposal(fact.uuid), round_number=1)[0]
        reverse = TransmissionProposal(
            chain_id="chain-1",
            topology_edge_id="edge-1",
            source_node_id="node-b",
            target_node_id="node-a",
            flow="AGAINST_EDGE",
            target_variable="上游需求",
            direction=Direction.DOWN,
            horizon=Horizon.SHORT,
            confidence=Confidence.HIGH,
            mechanism="下游减产反向压低上游需求。",
            parent_transmission_ids=[first.transmission_id],
        )

        second = InvestmentReasoningEngine.validate_round(
            context, [first], TransmissionBatch(proposals=[reverse]), round_number=2
        )
        jump = InvestmentReasoningEngine.validate_round(
            context, [first], TransmissionBatch(proposals=[reverse]), round_number=3
        )
        wrong_chain_parent = first.model_copy(update={"chain_id": "other-chain"})
        cross_chain = InvestmentReasoningEngine.validate_round(
            context, [wrong_chain_parent], TransmissionBatch(proposals=[reverse]), round_number=2
        )

        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].confidence, Confidence.LOW)
        self.assertEqual(jump, [])
        self.assertEqual(cross_chain, [])

    def test_three_hop_fixture_keeps_lineage_and_rejects_a_fabricated_edge(self) -> None:
        fact = self._active_signal()
        nodes = [
            ChainNodeSnapshot(uuid=f"node-{name}-uuid", business_id=f"node-{name}", name=name.upper())
            for name in ("a", "b", "c", "d")
        ]
        edges = [
            TopologyEdgeSnapshot(
                uuid=f"edge-{index}-uuid",
                business_id=f"edge-{index}",
                name="ChainNodeInputTo",
                source_node_id=f"node-{source}",
                source_name=source.upper(),
                target_node_id=f"node-{target}",
                target_name=target.upper(),
                fact=f"{source.upper()} 向 {target.upper()} 提供投入品",
            )
            for index, (source, target) in enumerate((("a", "b"), ("b", "c"), ("c", "d")), 1)
        ]
        chain = IndustryChainSnapshot(
            uuid="chain-uuid",
            business_id="chain-1",
            name="三跳测试链",
            anchor_match_count=1,
            matched_node_ids=["node-a"],
            signal_root_fact_ids=[fact.uuid],
            signal_root_node_ids=["node-a"],
            nodes=nodes,
            edges=edges,
        )
        context = self._context(fact).model_copy(update={"chains": [chain]})
        accepted: list[AcceptedTransmission] = []
        parent_id = None
        for hop, (source, target) in enumerate((("a", "b"), ("b", "c"), ("c", "d")), 1):
            proposal = TransmissionProposal(
                chain_id="chain-1",
                topology_edge_id=f"edge-{hop}",
                source_node_id=f"node-{source}",
                target_node_id=f"node-{target}",
                flow="ALONG_EDGE",
                target_variable="需求传导",
                direction=Direction.DOWN,
                horizon=Horizon.SHORT,
                confidence=Confidence.HIGH,
                mechanism=f"第 {hop} 跳沿真实拓扑传导。",
                source_fact_ids=[fact.uuid] if hop == 1 else [],
                parent_transmission_ids=[parent_id] if parent_id else [],
            )
            new_items = InvestmentReasoningEngine.validate_round(
                context, accepted, TransmissionBatch(proposals=[proposal]), round_number=hop
            )
            self.assertEqual(len(new_items), 1)
            accepted.extend(new_items)
            parent_id = new_items[0].transmission_id

        fabricated = accepted[-1].model_copy(
            update={"topology_edge_id": "edge-does-not-exist", "transmission_id": "ignored"}
        )
        fabricated_proposal = TransmissionProposal.model_validate(
            fabricated.model_dump(exclude={"transmission_id", "hop", "root_signal_fact_ids", "path_score"})
        )
        rejected = InvestmentReasoningEngine.validate_round(
            context, accepted[:-1], TransmissionBatch(proposals=[fabricated_proposal]), round_number=3
        )

        self.assertEqual([item.hop for item in accepted], [1, 2, 3])
        self.assertTrue(all(item.root_signal_fact_ids == [fact.uuid] for item in accepted))
        self.assertEqual(rejected, [])

    def test_direct_node_signal_is_bound_even_when_the_model_omits_its_id(self) -> None:
        fact = self._active_signal()
        context = self._context(fact)
        node = NodeTrendView(
            chain_id="chain-1",
            node_id="node-a",
            node_name="上游",
            short=Trend.WARMING,
            medium=Trend.INSUFFICIENT_EVIDENCE,
            long=Trend.INSUFFICIENT_EVIDENCE,
            confidence=Confidence.HIGH,
            investment_assessment=InvestmentAssessment.OPPORTUNITY_CANDIDATE,
            rationale="供给收缩。",
        )

        normalized = InvestmentReasoningEngine._normalize_node(context, [], "chain-1", "node-a", node)

        self.assertEqual(normalized.short, Trend.WARMING)
        self.assertEqual(normalized.supporting_fact_ids, [fact.uuid])

    def test_unscoped_event_signals_cannot_preserve_directional_node_trends(self) -> None:
        for label, context, signal in self._unscoped_signal_contexts():
            with self.subTest(source_scope=label):
                node = NodeTrendView(
                    chain_id="chain-1",
                    node_id="node-a",
                    node_name="上游",
                    short=Trend.WARMING,
                    medium=Trend.INSUFFICIENT_EVIDENCE,
                    long=Trend.INSUFFICIENT_EVIDENCE,
                    confidence=Confidence.HIGH,
                    investment_assessment=InvestmentAssessment.OPPORTUNITY_CANDIDATE,
                    rationale="模型尝试使用不在当前 Event 窗口的 Signal 保留升温结论。",
                    supporting_fact_ids=[signal.uuid],
                )
                draft = AnalysisDraft(
                    one_sentence_conclusion="上游节点升温。",
                    chains=[
                        ChainTrendView(
                            chain_id="chain-1",
                            chain_name="测试产业链",
                            short=Trend.WARMING,
                            medium=Trend.INSUFFICIENT_EVIDENCE,
                            long=Trend.INSUFFICIENT_EVIDENCE,
                            confidence=Confidence.HIGH,
                            summary="模型候选结论。",
                            nodes=[node],
                        )
                    ],
                )

                normalized = InvestmentReasoningEngine.normalize_draft(context, [], draft)
                normalized_node = next(
                    item for chain in normalized.chains for item in chain.nodes if item.node_id == "node-a"
                )

                self.assertEqual(normalized_node.short, Trend.INSUFFICIENT_EVIDENCE)
                self.assertEqual(
                    normalized_node.investment_assessment,
                    InvestmentAssessment.INSUFFICIENT_EVIDENCE,
                )
                self.assertEqual(normalized_node.supporting_fact_ids, [])

    def test_signal_from_an_event_outside_the_requested_window_is_not_eligible(self) -> None:
        current_signal = self._active_signal()
        context = self._context(current_signal)
        old_event = context.events[0].model_copy(
            update={
                "episode_uuid": "episode-old",
                "event_id": "event-old",
                "occurred_at": datetime(2026, 8, 26, 0, 0, tzinfo=UTC),
            }
        )
        old_signal = current_signal.model_copy(
            update={
                "uuid": "signal-from-old-event",
                "source_event_ids": [old_event.event_id],
            }
        )
        scoped = context.model_copy(
            update={
                "events": [context.events[0], old_event],
                "facts": [current_signal, old_signal],
            }
        )

        self.assertEqual(scoped.eligible_signal_fact_ids, {current_signal.uuid})

    def test_signal_without_a_source_event_in_the_current_context_is_not_eligible(self) -> None:
        current_signal = self._active_signal()
        foreign_signal = current_signal.model_copy(
            update={
                "uuid": "signal-from-foreign-event",
                "source_event_ids": ["event-not-in-current-context"],
            }
        )
        scoped = self._context(current_signal).model_copy(update={"facts": [current_signal, foreign_signal]})

        self.assertEqual(scoped.eligible_signal_fact_ids, {current_signal.uuid})

    def _unscoped_signal_contexts(self) -> list[tuple[str, InvestmentAnalysisContext, FactSnapshot]]:
        current = self._context(self._active_signal())
        old_event = current.events[0].model_copy(
            update={
                "episode_uuid": "episode-old",
                "event_id": "event-old",
                "occurred_at": datetime(2026, 8, 26, 0, 0, tzinfo=UTC),
            }
        )
        old_signal = self._active_signal().model_copy(
            update={
                "uuid": "signal-from-old-event",
                "source_event_ids": [old_event.event_id],
            }
        )
        outside_window = self._context(old_signal).model_copy(update={"events": [current.events[0], old_event]})
        foreign_signal = self._active_signal().model_copy(
            update={
                "uuid": "signal-from-foreign-event",
                "source_event_ids": ["event-not-in-current-context"],
            }
        )
        foreign_event = self._context(foreign_signal)
        return [
            ("OUTSIDE_EVENT_WINDOW", outside_window, old_signal),
            ("FOREIGN_EVENT", foreign_event, foreign_signal),
        ]

    @staticmethod
    def _active_signal() -> FactSnapshot:
        return FactSnapshot(
            uuid="signal-1",
            kind="SIGNAL",
            name="SIGNAL_ON",
            fact="有效产能下降作用于上游节点",
            source_uuid="variable-uuid",
            source_name="有效产能",
            source_business_id="variable-1",
            source_labels=["Entity", "Variable"],
            target_uuid="node-a-uuid",
            target_name="上游",
            target_business_id="node-a",
            target_labels=["Entity", "ChainNode"],
            source_event_ids=["event-1"],
            direction=Direction.DOWN,
            horizons=[Horizon.SHORT],
            confidence=Confidence.HIGH,
            valid_at=datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
        )


class LayerAssessmentContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.decision_at = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
        self.event = EventSnapshot(
            episode_uuid="episode-1",
            event_id="event-1",
            title="美伊冲突风险上升",
            summary="中东军事冲突风险上升。",
            modality="FACT",
            occurred_at=datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
        )
        self.geo_anchor = AnalysisAnchorSnapshot(
            uuid="geo-uuid",
            business_id="GPR00000000-0000-4000-8000-000000000001",
            name="美伊地缘冲突",
            entity_type="GeopoliticRivalry",
        )
        self.macro_anchor = AnalysisAnchorSnapshot(
            uuid="macro-uuid",
            business_id="MEC00000000-0000-4000-8000-000000000001",
            name="通胀预警",
            entity_type="MacroEconomic",
        )
        self.signal = FactSnapshot(
            uuid="geo-signal-1",
            kind="SIGNAL",
            name="SIGNAL_ON",
            fact="地缘政治风险上升作用于美伊地缘冲突",
            source_uuid="variable-uuid",
            source_name="地缘政治风险",
            source_business_id="geopolitical_risk",
            source_labels=["Entity", "Variable"],
            target_uuid=self.geo_anchor.uuid,
            target_name=self.geo_anchor.name,
            target_business_id=self.geo_anchor.business_id,
            target_labels=["Entity", "GeopoliticRivalry"],
            source_event_ids=["event-1"],
            event_class="GEOPOLITICAL",
            anchor_type="GeopoliticRivalry",
            variable_id="geopolitical_risk",
            direction=Direction.UP,
            horizons=[Horizon.SHORT],
            confidence=Confidence.MEDIUM,
            valid_at=datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
        )
        self.mechanism = FactSnapshot(
            uuid="geo-to-macro-mechanism",
            kind="ORDINARY",
            name="TRANSMITS_TO",
            fact="中东能源供应风险通过油价影响通胀预期。",
            source_uuid=self.geo_anchor.uuid,
            source_name=self.geo_anchor.name,
            source_business_id=self.geo_anchor.business_id,
            source_labels=["Entity", "GeopoliticRivalry"],
            target_uuid=self.macro_anchor.uuid,
            target_name=self.macro_anchor.name,
            target_business_id=self.macro_anchor.business_id,
            target_labels=["Entity", "MacroEconomic"],
        )

    @staticmethod
    def _ontology() -> ReasoningOntologyContext:
        return ReasoningOntologyContext(
            entity_types={"Event": "事件", "Variable": "变量", "GeopoliticRivalry": "地缘锚点"},
            fact_types={"SIGNAL_ON": "直接变量信号", "ORDINARY": "普通事实"},
            relationship_types={"MENTIONS": "提及"},
            usage_rules=["直接 Signal 不转换为 Claim。"],
        )

    def _layer_context(self, layer: ImpactLayer) -> LayerAnalysisContext:
        receipt = RetrievalReceipt(
            stage=layer.value,
            layer=layer,
            required_actions=["search_anchor_nodes", "load_anchor_facts"],
            completed_actions=["search_anchor_nodes", "load_anchor_facts"],
            event_ids=[self.event.event_id],
            anchor_ids=[self.geo_anchor.business_id, self.macro_anchor.business_id],
            fact_ids=[self.signal.uuid, self.mechanism.uuid],
            direct_signal_fact_ids=[self.signal.uuid],
        )
        return LayerAnalysisContext(
            layer=layer,
            decision_at=self.decision_at,
            question="逐层分析最近48小时事件的投研影响",
            events=[self.event],
            anchors=[self.geo_anchor, self.macro_anchor],
            facts=[self.signal, self.mechanism],
            parent_assessments=[],
            direct_signal_fact_ids=[self.signal.uuid],
            ontology=self._ontology(),
            retrieval_receipt=receipt,
        )

    def _geo_batch(self) -> LayerAssessmentBatch:
        return LayerAssessmentBatch(
            proposals=[
                LayerAssessmentProposal(
                    anchor_id=self.geo_anchor.business_id,
                    result=Trend.WARMING,
                    confidence=Confidence.MEDIUM,
                    summary="美伊冲突的短期地缘政治风险上升。",
                    reasoning="军事冲突概率上升提高区域不确定性。",
                )
            ],
            summary="地缘政治风险上升。",
        )

    def test_direct_signal_becomes_an_assessment_reference_without_claim_conversion(self) -> None:
        accepted = InvestmentReasoningEngine.build_layer_assessments(
            self._layer_context(ImpactLayer.GEOPOLITICAL),
            self._geo_batch(),
            layer=ImpactLayer.GEOPOLITICAL,
        )

        self.assertEqual(len(accepted), 1)
        self.assertIsInstance(accepted[0], LayerAssessment)
        self.assertEqual(accepted[0].root_event_ids, ["event-1"])
        self.assertEqual(accepted[0].direct_signal_fact_ids, [self.signal.uuid])
        self.assertEqual(accepted[0].layer, ImpactLayer.GEOPOLITICAL)
        self.assertEqual(accepted[0].result, Trend.WARMING)

    def test_model_cannot_self_declare_signal_references(self) -> None:
        payload = self._geo_batch().proposals[0].model_dump(mode="json")
        payload["direct_signal_fact_ids"] = ["invented-signal"]

        with self.assertRaises(ValidationError):
            LayerAssessmentProposal.model_validate(payload)

    def test_merged_signal_keeps_only_current_event_roots(self) -> None:
        merged_signal = self.signal.model_copy(
            update={"source_event_ids": [self.event.event_id, "event-before-current-window"]}
        )
        context = self._layer_context(ImpactLayer.GEOPOLITICAL).model_copy(
            update={"facts": [merged_signal, self.mechanism]}
        )

        accepted = InvestmentReasoningEngine.build_layer_assessments(
            context,
            self._geo_batch(),
            layer=ImpactLayer.GEOPOLITICAL,
        )

        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].root_event_ids, [self.event.event_id])
        self.assertNotIn("event-before-current-window", accepted[0].root_event_ids)

    def test_model_proposal_cannot_self_declare_root_lineage(self) -> None:
        with self.assertRaises(ValidationError):
            LayerAssessmentProposal.model_validate(
                {
                    "anchor_id": self.geo_anchor.business_id,
                    "result": "WARMING",
                    "confidence": "MEDIUM",
                    "summary": "模型试图自报根谱。",
                    "reasoning": "根谱只能由工作流绑定。",
                    "root_event_ids": ["event-1"],
                }
            )

    def test_ordinary_fact_alone_cannot_create_a_layer_assessment(self) -> None:
        context = self._layer_context(ImpactLayer.GEOPOLITICAL).model_copy(
            update={"direct_signal_fact_ids": [], "facts": [self.mechanism]}
        )
        accepted = InvestmentReasoningEngine.build_layer_assessments(
            context,
            self._geo_batch(),
            layer=ImpactLayer.GEOPOLITICAL,
        )

        self.assertEqual(accepted, [])


class InvestmentWorkflowShapeTest(unittest.TestCase):
    def test_malformed_node_trend_is_normalized_without_aborting_the_workflow(self) -> None:
        raw = SimpleNamespace(
            content=json.dumps(
                {
                    "nodes": [
                        {
                            "chain_id": "chain-1",
                            "node_id": "node-1",
                            "node_name": "刻蚀设备",
                            "short": {"WARMING": "订单增加"},
                            "medium": {"WARMING": "产能扩张"},
                            "long": {"INSUFFICIENT_EVIDENCE": "无长期信号"},
                            "confidence": "MEDIUM",
                            "investment_assessment": "OPPORTUNITY_CANDIDATE",
                            "rationale": "直接订单 Signal 支持短中期升温。",
                            "supporting_fact_ids": ["fact-1"],
                        },
                        {"node_id": "invalid-node"},
                    ]
                },
                ensure_ascii=False,
            )
        )

        result = _payload(raw, NodeAnalysisBatch)

        self.assertEqual(len(result.nodes), 1)
        self.assertEqual(result.nodes[0].short, Trend.WARMING)
        self.assertEqual(result.nodes[0].long, Trend.INSUFFICIENT_EVIDENCE)

    def test_application_registry_can_rehydrate_every_workflow_function(self) -> None:
        functions = [
            prepare_investment_context,
            analyze_geopolitical_impact,
            analyze_macro_impact,
            analyze_industry_impact,
            review_and_finalize,
            generate_investment_report,
            publish_investment_report,
        ]

        self.assertEqual(
            {function.__name__ for function in functions},
            {function.__name__ for function in functions if registry.get_function(function.__name__) is not None},
        )

    def test_schedule_natural_language_is_a_first_class_reasoning_input(self) -> None:
        message = "获取最近72小时全部Event，逐层分析对产业链的投研影响。"

        parsed = InvestmentReasoningInput.model_validate(message)

        self.assertEqual(parsed.question, message)
        self.assertEqual(parsed.event_window_hours, 72)
        self.assertFalse(parsed.include_company)

    def test_validation_run_can_request_one_year_of_event_history(self) -> None:
        parsed = InvestmentReasoningInput.model_validate(
            {
                "question": "重放历史 Event 并验证投研推理。",
                "event_window_hours": 24 * 365,
                "include_company": False,
            }
        )

        self.assertEqual(parsed.event_window_hours, 8760)

    def test_overlong_transmission_stop_reason_is_tolerantly_truncated(self) -> None:
        parsed = _payload(
            SimpleNamespace(content={"proposals": [], "stopped_reason": "证据不足" * 200}),
            TransmissionBatch,
        )

        self.assertEqual(len(parsed.stopped_reason or ""), 500)
        self.assertEqual(len(_bounded_text("A" * 900, limit=500) or ""), 500)

    def test_workflow_has_six_fixed_business_stages(self) -> None:
        workflow = _seed_workflow(cast(Agent, object()), cast(Agent, object()))
        steps = cast(list[Step], workflow.steps)
        self.assertEqual(
            [step.name for step in steps],
            [
                "prepare-investment-context",
                "analyze-geopolitical-impact",
                "analyze-macro-impact",
                "analyze-industry-impact",
                "review-and-finalize",
                "generate-investment-report",
            ],
        )

    def test_http_natural_language_contract_is_owned_by_prepare_not_workflow_input_schema(self) -> None:
        workflow = _seed_workflow(cast(Agent, object()), cast(Agent, object()))

        self.assertEqual(INVESTMENT_REASONING_CONTRACT_VERSION, 8)
        self.assertIsNone(workflow.input_schema)
        self.assertIs(cast(list[Step], workflow.steps)[0].executor, prepare_investment_context)

    def test_workflow_has_no_planner_or_company_dependency(self) -> None:
        reasoner = Agent(id="investment-reasoner")
        reviewer = Agent(id="investment-reviewer")

        workflow = _seed_workflow(reasoner, reviewer)

        self.assertEqual(
            workflow.dependencies,
            {
                "reasoner_agent_id": "investment-reasoner",
                "reviewer_agent_id": "investment-reviewer",
            },
        )
        dependencies = workflow.dependencies or {}
        self.assertNotIn("planner_agent_id", dependencies)
        self.assertNotIn("company_agent_id", dependencies)


class InvestmentComponentLifecycleTest(unittest.TestCase):
    def test_each_agent_migrates_an_old_contract_to_the_current_version(self) -> None:
        cases = [
            (
                "agents.investment_reasoner",
                ensure_investment_reasoner_agent,
                "investment_reasoner_contract_version",
                INVESTMENT_REASONER_CONTRACT_VERSION,
                "Investment Reasoner",
            ),
            (
                "agents.investment_reviewer",
                ensure_investment_reviewer_agent,
                "investment_reviewer_contract_version",
                INVESTMENT_REVIEWER_CONTRACT_VERSION,
                "Investment Reviewer",
            ),
        ]
        for module, ensure, metadata_key, contract_version, name in cases:
            with self.subTest(agent=name):
                db = MagicMock()
                db.get_component.return_value = {"current_version": 3}
                current = MagicMock()
                current.metadata = {metadata_key: 0}
                current.save.return_value = 4
                with (
                    patch(f"{module}.get_postgres_db", return_value=db),
                    patch(f"{module}.Agent.load", return_value=current),
                ):
                    version = ensure(MagicMock())

                self.assertEqual(version, 4)
                self.assertEqual(current.metadata[metadata_key], contract_version)
                current.save.assert_called_once_with(
                    db=db,
                    stage="published",
                    notes=f"{name} contract migration {contract_version}",
                )

    def test_registry_resolves_only_the_two_runtime_investment_agents(self) -> None:
        registry = TidewiseRegistry(name="Investment Registry Test")
        reasoner = Agent(id="investment-reasoner")
        reviewer = Agent(id="investment-reviewer")
        with (
            patch("app.registry.load_investment_reasoner_agent", return_value=reasoner),
            patch("app.registry.load_investment_reviewer_agent", return_value=reviewer),
        ):
            self.assertIs(registry.get_agent("investment-reasoner"), reasoner)
            self.assertIs(registry.get_agent("investment-reviewer"), reviewer)
            self.assertIsNone(registry.get_agent("investment-planner"))

    def test_workflow_migration_preserves_reasoner_and_reviewer_bindings(self) -> None:
        db = MagicMock()
        db.get_component.return_value = {"current_version": 7}
        db.get_config.return_value = {
            "config": {
                "id": "investment-reasoning",
                "name": "Investment Reasoning",
                "metadata": {"investment_reasoning_contract_version": 0},
            }
        }
        reasoner = Agent(id="investment-reasoner", db=None)
        reviewer = Agent(id="investment-reviewer", db=None)
        with (
            patch("workflows.investment_reasoning.get_postgres_db", return_value=db),
            patch("workflows.investment_reasoning.load_investment_reasoner_agent", return_value=reasoner),
            patch("workflows.investment_reasoning.load_investment_reviewer_agent", return_value=reviewer),
            patch.object(Workflow, "save", autospec=True, return_value=8) as saved,
        ):
            version = ensure_investment_reasoning_workflow(MagicMock())

        self.assertEqual(version, 8)
        migrated = cast(Workflow, saved.call_args.args[0])
        self.assertEqual(
            migrated.metadata,
            {"investment_reasoning_contract_version": INVESTMENT_REASONING_CONTRACT_VERSION},
        )
        self.assertEqual(
            migrated.dependencies,
            {
                "reasoner_agent_id": "investment-reasoner",
                "reviewer_agent_id": "investment-reviewer",
            },
        )
        self.assertEqual(cast(list[Step], migrated.steps)[0].name, "prepare-investment-context")


class _LayeredRuntime:
    def __init__(self, context: InvestmentAnalysisContext) -> None:
        self.context = context
        self.prepared_request: InvestmentAnalysisRequest | None = None
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.geopolitical = LayerAnalysisResult(
            layer=ImpactLayer.GEOPOLITICAL,
            assessments=[],
            summary="地缘政治层未形成方向结论。",
            limitations=["NO_GEOPOLITICAL_SIGNAL"],
        )
        self.macro = LayerAnalysisResult(
            layer=ImpactLayer.MACRO_ECONOMIC,
            assessments=[],
            summary="宏观经济层未形成方向结论。",
            limitations=["NO_MACRO_TRANSMISSION"],
        )
        self.industry = LayerAnalysisResult(
            layer=ImpactLayer.INDUSTRY,
            assessments=[],
            summary="产业链层证据不足。",
            limitations=["NO_INDUSTRY_SIGNAL"],
        )

    async def prepare(self, request: InvestmentAnalysisRequest) -> InvestmentAnalysisContext:
        self.prepared_request = request
        return self.context.model_copy(update={"request": request, "chains": []})

    async def analyze_geopolitical(self, prepared: PreparedInvestmentContext) -> LayerAnalysisResult:
        self.calls.append(("geopolitical", (prepared,)))
        return self.geopolitical

    async def analyze_macro(
        self,
        prepared: PreparedInvestmentContext,
        geopolitical: LayerAnalysisResult,
    ) -> LayerAnalysisResult:
        self.calls.append(("macro", (prepared, geopolitical)))
        return self.macro

    async def analyze_industry(
        self,
        prepared: PreparedInvestmentContext,
        geopolitical: LayerAnalysisResult,
        macro: LayerAnalysisResult,
        macro_transmission=None,
    ) -> IndustryAnalysisState:
        self.calls.append(("industry", (prepared, geopolitical, macro, macro_transmission)))
        return IndustryAnalysisState(
            prepared=prepared,
            geopolitical=geopolitical,
            macro=macro,
            industry=self.industry,
            industry_context=prepared.context,
            transmissions=[],
            rounds_executed=0,
            draft=AnalysisDraft(
                one_sentence_conclusion="当前没有足够证据形成方向性产业结论。",
                limitations=["NO_INDUSTRY_SIGNAL"],
            ),
        )


class InvestmentWorkflowExecutionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            os.environ,
            {"INVESTMENT_ARTIFACT_ROOT": str(Path(self.temporary.name) / "investment")},
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    @staticmethod
    def _run_context(run_id: str) -> RunContext:
        return RunContext(run_id=run_id, session_id="investment-session", dependencies={})

    @staticmethod
    def _base_context() -> InvestmentAnalysisContext:
        ordinary = FactSnapshot(
            uuid="ordinary-1",
            kind="ORDINARY",
            name="MENTIONS",
            fact="事件与锚点相关",
            source_uuid="event-entity",
            source_name="事件主题",
            target_uuid="anchor-uuid",
            target_name="测试锚点",
            target_business_id="anchor-1",
            target_labels=["Entity", "GeopoliticRivalry"],
        )
        return InvestmentReasoningGateTest()._context(ordinary).model_copy(update={"chains": []})

    @staticmethod
    def _accepted_assessment(layer: ImpactLayer, fact: FactSnapshot) -> LayerAssessment:
        return LayerAssessment(
            anchor_id="node-a",
            result=Trend.COOLING,
            confidence=Confidence.MEDIUM,
            summary=f"{layer.value} 层形成一条评估。",
            reasoning="当前有效 Signal 支持该评估。",
            direct_signal_fact_ids=[fact.uuid],
            assessment_id=f"assessment-{layer.value.lower()}",
            layer=layer,
            anchor_name="上游",
            anchor_type="ChainNode",
            horizons=[Horizon.SHORT],
            root_event_ids=["event-1"],
        )

    @classmethod
    def _finalization_state(
        cls,
        *,
        assessment_layers: tuple[ImpactLayer, ...] = (),
        include_transmission: bool = False,
        invalid_root: bool = False,
    ) -> IndustryAnalysisState:
        gate = InvestmentReasoningGateTest()
        fact = gate._active_signal()
        context = gate._context(fact)
        assessments = {layer: cls._accepted_assessment(layer, fact) for layer in assessment_layers}
        if invalid_root and assessments:
            first_layer = assessment_layers[0]
            assessments[first_layer] = assessments[first_layer].model_copy(
                update={"direct_signal_fact_ids": ["missing-signal-root"]}
            )

        def layer_result(layer: ImpactLayer) -> LayerAnalysisResult:
            assessment = assessments.get(layer)
            return LayerAnalysisResult(
                layer=layer,
                assessments=[assessment] if assessment is not None else [],
                supporting_facts=[fact] if assessment is not None else [],
                summary=f"{layer.value} 层测试结果。",
            )

        transmissions = (
            InvestmentReasoningEngine.validate_round(
                context,
                [],
                gate._proposal(fact.uuid),
                round_number=1,
            )
            if include_transmission
            else []
        )
        prepared = PreparedInvestmentContext(
            context=context,
            context_fingerprint=InvestmentReasoningEngine.context_fingerprint(context),
        )
        return IndustryAnalysisState(
            prepared=prepared,
            geopolitical=layer_result(ImpactLayer.GEOPOLITICAL),
            macro=layer_result(ImpactLayer.MACRO_ECONOMIC),
            industry=layer_result(ImpactLayer.INDUSTRY),
            industry_context=context,
            transmissions=transmissions,
            rounds_executed=1 if transmissions else 0,
            draft=AnalysisDraft(
                one_sentence_conclusion="当前节点方向仍为证据不足。",
                chains=[InvestmentReasoningEngine.insufficient_chain(context.chains[0])],
            ),
        )

    async def test_schedule_payload_reaches_prepare_without_a_planner_output(self) -> None:
        runtime = _LayeredRuntime(self._base_context())
        payload = InvestmentReasoningInput(
            question="分析最近事件对三层锚点的影响",
            event_window_hours=36,
            include_company=False,
        )
        configure_investment_workflow_runtime(runtime)
        try:
            output = await prepare_investment_context(
                StepInput(input=payload.model_dump_json()),
                cast(Any, object()),
            )
        finally:
            configure_investment_workflow_runtime(None)

        self.assertIsInstance(output.content, PreparedInvestmentContext)
        self.assertIsNotNone(runtime.prepared_request)
        prepared_request = cast(InvestmentAnalysisRequest, runtime.prepared_request)
        self.assertEqual(prepared_request.question, payload.question)
        self.assertEqual(prepared_request.event_window_hours, 36)
        self.assertFalse(prepared_request.include_company)

    async def test_each_layer_receives_all_accepted_preceding_results(self) -> None:
        runtime = _LayeredRuntime(self._base_context())
        prepared = PreparedInvestmentContext(
            context=runtime.context,
            context_fingerprint=InvestmentReasoningEngine.context_fingerprint(runtime.context),
        )
        configure_investment_workflow_runtime(runtime)
        try:
            geo_output = await analyze_geopolitical_impact(StepInput(previous_step_content=prepared))
            geo_state = cast(GeopoliticalAnalysisState, geo_output.content)
            macro_output = await analyze_macro_impact(StepInput(previous_step_content=geo_state))
            macro_state = cast(MacroAnalysisState, macro_output.content)
            industry_output = await analyze_industry_impact(StepInput(previous_step_content=macro_state))
            industry_state = cast(IndustryAnalysisState, industry_output.content)
        finally:
            configure_investment_workflow_runtime(None)

        self.assertIs(geo_state.prepared, prepared)
        self.assertIs(geo_state.geopolitical, runtime.geopolitical)
        self.assertIs(macro_state.geopolitical, runtime.geopolitical)
        self.assertIs(macro_state.macro, runtime.macro)
        self.assertIs(industry_state.geopolitical, runtime.geopolitical)
        self.assertIs(industry_state.macro, runtime.macro)
        self.assertIs(industry_state.industry, runtime.industry)
        self.assertEqual(
            [name for name, _ in runtime.calls],
            ["geopolitical", "macro", "industry"],
        )
        self.assertIs(runtime.calls[1][1][1], runtime.geopolitical)
        self.assertIs(runtime.calls[2][1][1], runtime.geopolitical)
        self.assertIs(runtime.calls[2][1][2], runtime.macro)

    async def test_missing_retrieved_reference_blocks_report_without_erasing_audit_data(self) -> None:
        state = self._finalization_state(
            assessment_layers=(ImpactLayer.GEOPOLITICAL, ImpactLayer.MACRO_ECONOMIC, ImpactLayer.INDUSTRY),
            include_transmission=True,
            invalid_root=True,
        )
        reviewer = AsyncMock()
        configure_investment_workflow_runtime(SimpleNamespace(review=reviewer))
        try:
            output = await review_and_finalize(
                StepInput(previous_step_content=state),
                self._run_context("run-hard-gate"),
            )
        finally:
            configure_investment_workflow_runtime(None)

        result = cast(ReviewedInvestmentState, output.content).analysis
        reviewer.assert_not_awaited()
        for layer in (result.geopolitical, result.macro, result.industry):
            self.assertTrue(layer.assessments)
        self.assertFalse(result.review.accepted)
        self.assertIn("ASSESSMENT_REFERENCE_OUTSIDE_CONTEXT", ";".join(result.review.issue_codes))

    async def test_reviewer_rejection_blocks_when_runtime_cannot_repair(self) -> None:
        state = self._finalization_state(
            assessment_layers=(ImpactLayer.GEOPOLITICAL, ImpactLayer.MACRO_ECONOMIC, ImpactLayer.INDUSTRY),
            include_transmission=True,
        )
        reviewer = AsyncMock(
            return_value=ReviewResult(
                accepted=False,
                confidence=Confidence.LOW,
                issue_codes=["SEMANTIC_REVIEW_REJECTED"],
                review_summary="语义审核拒绝当前结论。",
            )
        )
        configure_investment_workflow_runtime(SimpleNamespace(review=reviewer))
        try:
            output = await review_and_finalize(
                StepInput(previous_step_content=state),
                self._run_context("run-review-rejection"),
            )
        finally:
            configure_investment_workflow_runtime(None)

        result = cast(ReviewedInvestmentState, output.content).analysis
        reviewer.assert_awaited_once_with(state)
        self.assertEqual(result.status, "NEEDS_REVIEW")
        self.assertFalse(result.review.accepted)
        self.assertTrue(result.geopolitical.assessments)
        self.assertTrue(result.macro.assessments)
        self.assertTrue(result.industry.assessments)
        self.assertTrue(result.transmissions)
        self.assertTrue(
            {"LAYER_ASSESSMENT", "TRANSMISSION"}.intersection(item.node_type for item in result.reasoning_tree)
        )

    async def test_reviewer_runs_one_repair_and_rechecks_the_same_frozen_state(self) -> None:
        state = self._finalization_state(
            assessment_layers=(ImpactLayer.GEOPOLITICAL, ImpactLayer.MACRO_ECONOMIC, ImpactLayer.INDUSTRY),
            include_transmission=True,
        )
        first = ReviewResult(
            accepted=False,
            confidence=Confidence.LOW,
            issue_codes=["REASONING_INCONSISTENCY"],
            review_summary="节点与链级方向冲突。",
        )
        second = ReviewResult(
            accepted=True,
            confidence=Confidence.MEDIUM,
            issue_codes=[],
            review_summary="返工后方向一致。",
        )
        runtime = SimpleNamespace(
            review=AsyncMock(side_effect=[first, second]),
            repair=AsyncMock(return_value=state),
        )
        configure_investment_workflow_runtime(runtime)
        try:
            output = await review_and_finalize(
                StepInput(previous_step_content=state),
                self._run_context("run-review-repair"),
            )
        finally:
            configure_investment_workflow_runtime(None)

        result = cast(ReviewedInvestmentState, output.content).analysis
        self.assertEqual(runtime.review.await_count, 2)
        runtime.repair.assert_awaited_once_with(state, first)
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertTrue(result.review.accepted)

    async def test_invalid_reviewer_output_blocks_report_generation(self) -> None:
        state = self._finalization_state(assessment_layers=(ImpactLayer.GEOPOLITICAL,))
        reviewer = AsyncMock(
            return_value=ReviewResult(
                accepted=False,
                confidence=Confidence.LOW,
                issue_codes=["REVIEW_OUTPUT_INVALID"],
                review_summary="Reviewer 输出无法解析。",
            )
        )
        configure_investment_workflow_runtime(SimpleNamespace(review=reviewer))
        try:
            output = await review_and_finalize(
                StepInput(previous_step_content=state),
                self._run_context("run-invalid-review-output"),
            )
        finally:
            configure_investment_workflow_runtime(None)

        result = cast(ReviewedInvestmentState, output.content).analysis
        self.assertFalse(result.review.accepted)
        self.assertEqual(result.status, "NEEDS_REVIEW")

    async def test_any_layer_assessment_or_transmission_requires_semantic_review(self) -> None:
        cases = [
            ("GEOPOLITICAL_CLAIM", (ImpactLayer.GEOPOLITICAL,), False),
            ("MACRO_CLAIM", (ImpactLayer.MACRO_ECONOMIC,), False),
            ("INDUSTRY_CLAIM", (ImpactLayer.INDUSTRY,), False),
            ("TRANSMISSION", (), True),
        ]
        for label, assessment_layers, include_transmission in cases:
            with self.subTest(material_result=label):
                state = self._finalization_state(
                    assessment_layers=assessment_layers,
                    include_transmission=include_transmission,
                )
                reviewer = AsyncMock(
                    return_value=ReviewResult(
                        accepted=True,
                        confidence=Confidence.MEDIUM,
                        issue_codes=[],
                        review_summary="已审核当前有实质内容的推导结果。",
                    )
                )
                configure_investment_workflow_runtime(SimpleNamespace(review=reviewer))
                try:
                    await review_and_finalize(
                        StepInput(previous_step_content=state),
                        self._run_context(f"run-{label.lower()}"),
                    )
                finally:
                    configure_investment_workflow_runtime(None)

                reviewer.assert_awaited_once_with(state)

    async def test_upper_layer_mechanism_facts_survive_into_industry_review_context(self) -> None:
        fixture = LayerAssessmentContractTest()
        fixture.setUp()
        geo_assessment = InvestmentReasoningEngine.build_layer_assessments(
            fixture._layer_context(ImpactLayer.GEOPOLITICAL),
            fixture._geo_batch(),
            layer=ImpactLayer.GEOPOLITICAL,
        )[0]
        macro_assessment = geo_assessment.model_copy(
            update={
                "assessment_id": "assessment-macro",
                "layer": ImpactLayer.MACRO_ECONOMIC,
                "anchor_id": fixture.macro_anchor.business_id,
                "anchor_name": fixture.macro_anchor.name,
                "anchor_type": "MacroEconomic",
                "summary": "能源供给风险影响通胀预期。",
            }
        )
        base = self._finalization_state().industry_context.model_copy(
            update={
                "facts": [fixture.signal, fixture.mechanism],
                "anchors": [fixture.geo_anchor, fixture.macro_anchor],
            }
        )
        base = base.model_copy(
            update={
                "retrieval_receipts": [
                    receipt.model_copy(
                        update={
                            "fact_ids": [fixture.signal.uuid, fixture.mechanism.uuid],
                            "direct_signal_fact_ids": [fixture.signal.uuid],
                        }
                    )
                    for receipt in base.retrieval_receipts
                ]
            }
        )
        prepared = PreparedInvestmentContext(
            context=base,
            context_fingerprint=InvestmentReasoningEngine.context_fingerprint(base),
        )
        geopolitical = LayerAnalysisResult(
            layer=ImpactLayer.GEOPOLITICAL,
            assessments=[geo_assessment],
            supporting_facts=[fixture.signal],
            summary="地缘政治结论。",
        )
        macro = LayerAnalysisResult(
            layer=ImpactLayer.MACRO_ECONOMIC,
            assessments=[macro_assessment],
            supporting_facts=[fixture.signal, fixture.mechanism],
            summary="宏观经济结论。",
        )
        industry = LayerAnalysisResult(
            layer=ImpactLayer.INDUSTRY,
            assessments=[],
            supporting_facts=[],
            summary="产业层未形成方向结论。",
        )
        industry_layer_context = LayerAnalysisContext(
            layer=ImpactLayer.INDUSTRY,
            decision_at=fixture.decision_at,
            question=base.request.question,
            events=base.events,
            anchors=[],
            facts=[fixture.signal],
            parent_assessments=[geo_assessment, macro_assessment],
            ontology=fixture._ontology(),
            retrieval_receipt=RetrievalReceipt(
                stage="INDUSTRY",
                layer=ImpactLayer.INDUSTRY,
                required_actions=["search_anchor_nodes", "load_anchor_facts"],
                completed_actions=["search_anchor_nodes", "load_anchor_facts"],
                event_ids=[fixture.event.event_id],
                fact_ids=[fixture.signal.uuid],
                direct_signal_fact_ids=[fixture.signal.uuid],
            ),
        )
        runtime = LocalInvestmentWorkflowRuntime(
            cast(Any, None),
            cast(Agent, object()),
            cast(Agent, object()),
        )
        runtime._analyze_layer = AsyncMock(  # type: ignore[method-assign]
            return_value=(industry, industry_layer_context)
        )
        runtime._provider = SimpleNamespace(  # type: ignore[assignment]
            expand_industry_context=AsyncMock(return_value=base)
        )
        runtime._synthesize = AsyncMock(  # type: ignore[method-assign]
            return_value=AnalysisDraft(
                one_sentence_conclusion="当前没有产业方向性结论。",
                limitations=["NO_INDUSTRY_SIGNAL"],
            )
        )
        runtime._propagate = AsyncMock(  # type: ignore[method-assign]
            return_value=TransmissionBatch(proposals=[])
        )
        reviewer = AsyncMock(
            return_value=ReviewResult(
                accepted=True,
                confidence=Confidence.MEDIUM,
                issue_codes=[],
                review_summary="已审核上层跨层机制证据。",
            )
        )
        runtime.review = reviewer  # type: ignore[method-assign]

        state = await runtime.analyze_industry(prepared, geopolitical, macro)
        self.assertIn(fixture.mechanism.uuid, {fact.uuid for fact in state.industry_context.facts})

        configure_investment_workflow_runtime(runtime)
        try:
            output = await review_and_finalize(
                StepInput(previous_step_content=state),
                self._run_context("run-upper-layer-mechanism"),
            )
        finally:
            configure_investment_workflow_runtime(None)

        result = cast(ReviewedInvestmentState, output.content).analysis
        reviewer.assert_awaited_once()
        await_args = reviewer.await_args
        assert await_args is not None
        reviewed_state = await_args.args[0]
        self.assertIn(fixture.mechanism.uuid, {fact.uuid for fact in reviewed_state.industry_context.facts})
        self.assertTrue(result.review.accepted)

    async def test_final_result_is_an_idempotent_standalone_artifact(self) -> None:
        state = self._finalization_state()
        context = self._run_context("run-artifact-idempotency")
        configure_investment_workflow_runtime(SimpleNamespace(review=AsyncMock()))
        try:
            first = await review_and_finalize(
                StepInput(previous_step_content=state),
                context,
            )
            second = await review_and_finalize(
                StepInput(previous_step_content=state),
                context,
            )
        finally:
            configure_investment_workflow_runtime(None)

        reviewed = cast(ReviewedInvestmentState, first.content)
        artifact = reviewed.analysis
        self.assertEqual(second.content, reviewed)
        self.assertEqual(artifact.schema_version, "investment-conclusion-artifact/v1")
        self.assertEqual(artifact.workflow_run_id, context.run_id)
        self.assertEqual(artifact.conclusion_status, "INSUFFICIENT_EVIDENCE")
        path = Path(artifact.artifact_path)
        self.assertTrue(path.is_file())
        self.assertEqual(InvestmentConclusionArtifact.model_validate_json(path.read_text()), artifact)
        encoded = json.loads(path.read_text())
        self.assertNotIn("step_results", encoded)
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            write_conclusion_artifact(artifact.model_copy(update={"question": "冲突命题"}))

    def test_company_layer_is_rejected_by_the_current_input_contract(self) -> None:
        with self.assertRaises(ValidationError):
            InvestmentReasoningInput(
                question="分析公司层影响",
                event_window_hours=48,
                include_company=True,
            )


class _SearchOnlyGraphiti:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, **kwargs):
        self.queries.append(query)
        return [SimpleNamespace(uuid="fact-1")]


class GraphitiInvestmentRetrievalTest(unittest.IsolatedAsyncioTestCase):
    async def test_industry_layer_is_signal_rooted_and_skips_broad_anchor_fact_loading(self) -> None:
        gate = InvestmentReasoningGateTest()
        signal = gate._active_signal().model_copy(update={"anchor_type": "ChainNode"})
        node_anchor = AnalysisAnchorSnapshot(
            uuid="node-a-uuid",
            business_id="node-a",
            name="上游",
            entity_type="ChainNode",
            source_event_ids=["event-1"],
        )
        relevant = FactSnapshot(
            uuid="mechanism-1",
            kind="ORDINARY",
            name="AFFECTS",
            fact="上层风险通过投入成本作用于上游节点",
            source_uuid="macro-uuid",
            source_name="投入成本",
            source_business_id="macro-1",
            source_labels=["Entity", "MacroEconomic"],
            target_uuid="node-a-uuid",
            target_name="上游",
            target_business_id="node-a",
            target_labels=["Entity", "ChainNode"],
            source_event_ids=["event-1"],
        )
        unrelated = FactSnapshot(
            uuid="unrelated-1",
            kind="ORDINARY",
            name="AFFECTS",
            fact="其他事件作用于其他节点",
            source_uuid="other-source-uuid",
            source_name="其他主体",
            source_business_id="other-source",
            source_labels=["Entity"],
            target_uuid="other-node-uuid",
            target_name="其他节点",
            target_business_id="other-node",
            target_labels=["Entity", "ChainNode"],
            source_event_ids=["event-1"],
        )
        base = gate._context(signal).model_copy(
            update={"facts": [signal, relevant, unrelated], "anchors": [node_anchor], "chains": []}
        )
        reader = MagicMock()
        reader.search_anchor_nodes = AsyncMock(side_effect=AssertionError("industry must not use broad anchor search"))
        reader.load_anchor_facts = AsyncMock(side_effect=AssertionError("industry must not bulk-load anchor facts"))
        builder = InvestmentContextBuilder(cast(Any, reader))

        context = await builder.build_layer_context(base, ImpactLayer.INDUSTRY, [])

        self.assertEqual([item.business_id for item in context.anchors], ["node-a"])
        self.assertEqual(context.direct_signal_fact_ids, [signal.uuid])
        self.assertEqual([item.uuid for item in context.facts], [signal.uuid, relevant.uuid])
        self.assertEqual(
            context.retrieval_receipt.required_actions,
            ["select_signal_root_anchors", "select_signal_scoped_facts"],
        )
        reader.search_anchor_nodes.assert_not_awaited()
        reader.load_anchor_facts.assert_not_awaited()

    async def test_layer_queries_have_a_strict_budget_under_maximum_context(self) -> None:
        gate = InvestmentReasoningGateTest()
        base_signal = gate._active_signal()
        base = gate._context(base_signal).model_copy(
            update={
                "events": [
                    EventSnapshot(
                        episode_uuid=f"episode-{index}",
                        event_id=f"event-{index}",
                        title=f"极端压力事件{index:03d}",
                        summary="该事件描述包含需要进入分层检索的语义上下文。" * 8,
                        modality="FACT",
                        occurred_at=datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
                    )
                    for index in range(500)
                ],
                "facts": [
                    base_signal.model_copy(
                        update={
                            "uuid": f"signal-{index}",
                            "fact": f"变量 Signal {index:03d} " + "影响机制" * 40,
                            "source_event_ids": [f"event-{index % 500}"],
                        }
                    )
                    for index in range(125)
                ],
                "chains": [],
            }
        )
        parent = InvestmentWorkflowExecutionTest._accepted_assessment(ImpactLayer.GEOPOLITICAL, base_signal)
        parents = [
            parent.model_copy(
                update={
                    "assessment_id": f"parent-assessment-{index}",
                    "summary": f"父层结论 {index:03d} " + "传导背景" * 30,
                    "reasoning": f"父层机制 {index:03d} " + "机制说明" * 30,
                }
            )
            for index in range(125)
        ]

        queries = InvestmentContextBuilder._layer_queries(
            base,
            parents,
            [f"补充查询 {index} " + "补充语义" * 100 for index in range(8)],
        )

        self.assertGreater(len(queries), 0)
        self.assertLessEqual(len(queries), 25)
        self.assertTrue(all(len(query) <= 500 for query in queries))

    async def test_graphiti_fact_queries_exclude_relationships_created_after_decision_time(self) -> None:
        driver = SimpleNamespace(execute_query=AsyncMock(return_value=([], None, None)))
        reader = GraphitiInvestmentReader(cast(Any, SimpleNamespace(driver=driver)))
        decision_at = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
        latest_considered = datetime(2027, 8, 29, 0, 0, tzinfo=UTC)

        await reader.load_facts(
            ["event-1"],
            ["episode-1"],
            decision_at,
            latest_considered,
            limit=20,
        )
        await reader.load_anchor_facts(
            ["anchor-uuid"],
            decision_at,
            latest_considered,
            limit=20,
        )

        self.assertEqual(driver.execute_query.await_count, 2)
        for call in driver.execute_query.await_args_list:
            query = call.args[0]
            self.assertIn("fact.created_at IS NULL OR fact.created_at <= $decision_at", query)

    async def test_unscoped_event_signals_do_not_even_select_industry_chains(self) -> None:
        gate = InvestmentReasoningGateTest()
        for label, base, signal in gate._unscoped_signal_contexts():
            with self.subTest(source_scope=label):
                node_anchor = AnalysisAnchorSnapshot(
                    uuid="node-a-uuid",
                    business_id="node-a",
                    name="上游",
                    entity_type="ChainNode",
                )
                base = base.model_copy(update={"anchors": [node_anchor]})
                layer_context = LayerAnalysisContext(
                    layer=ImpactLayer.INDUSTRY,
                    decision_at=base.request.decision_at,
                    question=base.request.question,
                    events=base.events,
                    anchors=[node_anchor],
                    facts=[signal],
                    direct_signal_fact_ids=[signal.uuid],
                    ontology=LayerAssessmentContractTest._ontology(),
                    retrieval_receipt=RetrievalReceipt(
                        stage="INDUSTRY",
                        layer=ImpactLayer.INDUSTRY,
                        required_actions=["search_anchor_nodes", "load_anchor_facts"],
                        completed_actions=["search_anchor_nodes", "load_anchor_facts"],
                        event_ids=[item.event_id for item in base.events],
                        anchor_ids=[node_anchor.business_id],
                        fact_ids=[signal.uuid],
                        direct_signal_fact_ids=[signal.uuid],
                    ),
                )
                reader = MagicMock()
                reader.load_chain_candidates = AsyncMock(
                    return_value=[
                        {
                            "uuid": "chain-uuid",
                            "business_id": "chain-1",
                            "name": "测试产业链",
                            "matched_node_ids": ["node-a"],
                        }
                    ]
                )
                reader.load_chain_nodes = AsyncMock(
                    return_value=[
                        {
                            "chain_id": "chain-1",
                            "uuid": "node-a-uuid",
                            "business_id": "node-a",
                            "name": "上游",
                            "stage": "UPSTREAM",
                            "position": 1,
                        },
                        {
                            "chain_id": "chain-1",
                            "uuid": "node-b-uuid",
                            "business_id": "node-b",
                            "name": "下游",
                            "stage": "DOWNSTREAM",
                            "position": 2,
                        },
                    ]
                )
                reader.load_topology_edges = AsyncMock(return_value=[])
                builder = InvestmentContextBuilder(cast(Any, reader))

                expanded = await builder.expand_industry_context(base, layer_context, [])

                self.assertEqual(expanded.chains, [])
                reader.load_chain_candidates.assert_not_awaited()

    async def test_layer_context_carries_the_schedule_question_and_current_events(self) -> None:
        reader = MagicMock()
        reader.search_anchor_nodes = AsyncMock(return_value=[])
        reader.load_anchor_facts = AsyncMock(return_value=[])
        builder = InvestmentContextBuilder(cast(Any, reader))
        base = InvestmentWorkflowExecutionTest._base_context()

        layer_context = await builder.build_layer_context(
            base,
            ImpactLayer.GEOPOLITICAL,
            [],
        )

        self.assertEqual(layer_context.question, base.request.question)
        self.assertEqual(layer_context.events, base.events)

    async def test_prepare_keeps_anchor_mentions_but_never_loads_industry_topology(self) -> None:
        reader = MagicMock()
        reader.load_events = AsyncMock(
            return_value=[
                {
                    "episode_uuid": "episode-1",
                    "event_id": "event-1",
                    "name": "测试事件",
                    "content": json.dumps(
                        {
                            "title": "测试事件",
                            "summary": "事件提及一个标准产业链节点。",
                            "modality": "FACT",
                            "occurred_at": "2026-08-28T00:00:00Z",
                        }
                    ),
                    "valid_at": datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
                }
            ]
        )
        reader.load_mentions = AsyncMock(
            return_value=[
                {
                    "episode_uuid": "episode-1",
                    "uuid": "node-a-uuid",
                    "business_id": "node-a",
                    "name": "上游节点",
                    "labels": ["Entity", "ChainNode"],
                }
            ]
        )
        reader.load_facts = AsyncMock(return_value=[])
        reader.search_fact_ids = AsyncMock(return_value=[])
        reader.load_chain_candidates = AsyncMock(side_effect=AssertionError("prepare must not load chains"))
        reader.load_chain_nodes = AsyncMock(side_effect=AssertionError("prepare must not load nodes"))
        reader.load_topology_edges = AsyncMock(side_effect=AssertionError("prepare must not load topology"))
        builder = InvestmentContextBuilder(cast(Any, reader))

        context = await builder.build(
            InvestmentAnalysisRequest(
                question="逐层分析最近48小时事件",
                event_window_hours=48,
                decision_at=datetime(2026, 8, 29, 0, 0, tzinfo=UTC),
            )
        )

        self.assertEqual(context.chains, [])
        self.assertEqual([anchor.business_id for anchor in context.anchors], ["node-a"])
        reader.load_chain_candidates.assert_not_awaited()
        reader.load_chain_nodes.assert_not_awaited()
        reader.load_topology_edges.assert_not_awaited()

    async def test_native_search_batches_question_and_events_instead_of_one_large_query(self) -> None:
        graphiti = _SearchOnlyGraphiti()
        reader = GraphitiInvestmentReader(cast(Any, graphiti))
        events = [
            EventSnapshot(
                episode_uuid=f"episode-{index}",
                event_id=f"event-{index}",
                title=f"事件{index}",
                summary="摘要" * 20,
                modality="FACT",
                occurred_at=datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
            )
            for index in range(25)
        ]

        queries = InvestmentContextBuilder.build_native_queries("分析命题", events)
        ids = await reader.search_fact_ids(queries, {"fact-1"})

        self.assertEqual(ids, ["fact-1"])
        self.assertEqual(len(graphiti.queries), 3)
        self.assertTrue(all(len(query) <= 2000 for query in graphiti.queries))
        event_queries = "\n".join(graphiti.queries[1:])
        self.assertTrue(all(event.title in event_queries for event in events))

    async def test_native_search_controls_ordinary_context_but_never_drops_signal_roots(self) -> None:
        gate = InvestmentReasoningGateTest()
        signal = gate._active_signal()
        ordinary_selected = signal.model_copy(update={"uuid": "ordinary-selected", "kind": "ORDINARY"})
        ordinary_ignored = signal.model_copy(update={"uuid": "ordinary-ignored", "kind": "ORDINARY"})

        selected = InvestmentContextBuilder.select_retrieved_facts(
            [signal, ordinary_selected, ordinary_ignored], [ordinary_selected.uuid]
        )

        self.assertEqual([item.uuid for item in selected], [signal.uuid, ordinary_selected.uuid])

    async def test_five_hundred_long_events_have_a_strict_twenty_six_query_budget(self) -> None:
        events = [
            EventSnapshot(
                episode_uuid=f"episode-{index}",
                event_id=f"event-{index}",
                title=f"唯一事件{index:03d}",
                summary="很长的事件摘要" * 200,
                modality="FACT",
                occurred_at=datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
            )
            for index in range(500)
        ]

        queries = InvestmentContextBuilder.build_native_queries("分析命题", events)

        self.assertEqual(len(queries), 26)
        self.assertTrue(all(len(query) <= 500 for query in queries[1:]))
        combined = "\n".join(queries[1:])
        self.assertTrue(all(event.title in combined for event in events))


class InvestmentLifespanTest(unittest.IsolatedAsyncioTestCase):
    async def test_startup_failure_closes_already_created_event_runtime(self) -> None:
        with patch.dict(os.environ, {"RUNTIME_ENV": "dev"}):
            import app.main as main

        event_runtime = SimpleNamespace(close=AsyncMock())
        ensure_names = [
            "ensure_title_curator_agent",
            "ensure_evidence_extractor_agent",
            "ensure_event_extractor_agent",
            "ensure_event_identity_agent",
            "ensure_event_signal_analyst_agent",
            "ensure_investment_reasoner_agent",
            "ensure_investment_reviewer_agent",
            "ensure_raw_collection_workflow",
            "retire_collection_query_planner_agent",
            "ensure_evidence_extraction_workflow",
            "ensure_event_extraction_workflow",
            "ensure_investment_reasoning_workflow",
        ]
        with ExitStack() as stack:
            for name in ensure_names:
                stack.enter_context(patch.object(main, name))
            stack.enter_context(patch.object(main.registry, "get_model", return_value=object()))
            stack.enter_context(patch.object(main, "create_local_event_workflow_runtime", return_value=event_runtime))
            stack.enter_context(patch.object(main, "load_investment_reasoner_agent", return_value=Agent(id="reasoner")))
            stack.enter_context(patch.object(main, "load_investment_reviewer_agent", return_value=Agent(id="reviewer")))
            stack.enter_context(
                patch.object(
                    main,
                    "create_local_investment_workflow_runtime",
                    side_effect=RuntimeError("investment runtime failed"),
                )
            )
            with self.assertRaisesRegex(RuntimeError, "investment runtime failed"):
                async with main.lifespan(None):
                    pass

        event_runtime.close.assert_awaited_once()

    async def test_one_close_failure_does_not_block_the_other_runtime(self) -> None:
        with patch.dict(os.environ, {"RUNTIME_ENV": "dev"}):
            import app.main as main

        event_runtime = SimpleNamespace(close=AsyncMock())
        investment_runtime = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close failed")))
        ensure_names = [
            "ensure_title_curator_agent",
            "ensure_evidence_extractor_agent",
            "ensure_event_extractor_agent",
            "ensure_event_identity_agent",
            "ensure_event_signal_analyst_agent",
            "ensure_investment_reasoner_agent",
            "ensure_investment_reviewer_agent",
            "ensure_raw_collection_workflow",
            "retire_collection_query_planner_agent",
            "ensure_evidence_extraction_workflow",
            "ensure_event_extraction_workflow",
            "ensure_investment_reasoning_workflow",
        ]
        with ExitStack() as stack:
            for name in ensure_names:
                stack.enter_context(patch.object(main, name))
            stack.enter_context(patch.object(main.registry, "get_model", return_value=object()))
            stack.enter_context(patch.object(main, "create_local_event_workflow_runtime", return_value=event_runtime))
            stack.enter_context(patch.object(main, "load_investment_reasoner_agent", return_value=Agent(id="reasoner")))
            stack.enter_context(patch.object(main, "load_investment_reviewer_agent", return_value=Agent(id="reviewer")))
            stack.enter_context(
                patch.object(main, "create_local_investment_workflow_runtime", return_value=investment_runtime)
            )
            stack.enter_context(patch.object(main, "validate_schedules"))
            with self.assertRaisesRegex(ExceptionGroup, "AgentOS runtime shutdown failed"):
                async with main.lifespan(None):
                    pass

        investment_runtime.close.assert_awaited_once()
        event_runtime.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
