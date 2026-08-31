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
from agno.workflow import Step, StepInput, StepOutput, Workflow
from pydantic import ValidationError

from agents.investment_reasoner import (
    INVESTMENT_REASONER_CONTRACT_VERSION,
    ensure_investment_reasoner_agent,
)
from agents.investment_reviewer import (
    INVESTMENT_REVIEWER_CONTRACT_VERSION,
    ensure_investment_reviewer_agent,
)
from app.registry import TidewiseRegistry
from capabilities.investment import (
    AcceptedImpactClaim,
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
    ImpactClaimProposal,
    ImpactLayer,
    IndustryAnalysisState,
    IndustryChainSnapshot,
    InvestmentAnalysisContext,
    InvestmentAnalysisRequest,
    InvestmentAnalysisResult,
    InvestmentAssessment,
    InvestmentConclusionArtifact,
    InvestmentReasoningInput,
    LayerAnalysisContext,
    LayerAnalysisResult,
    LayerImpactBatch,
    MacroAnalysisState,
    NodeTrendView,
    PreparedInvestmentContext,
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
    prepare_investment_context,
    review_and_finalize,
)
from capabilities.investment.internal.context import InvestmentContextBuilder
from capabilities.investment.internal.engine import InvestmentReasoningEngine
from capabilities.investment.internal.local_runtime import LocalInvestmentWorkflowRuntime
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

    def test_first_hop_cannot_escape_signal_horizon(self) -> None:
        fact = self._active_signal()
        proposal = self._proposal(fact.uuid).proposals[0].model_copy(update={"horizon": Horizon.LONG})

        accepted = InvestmentReasoningEngine.validate_round(
            self._context(fact), [], TransmissionBatch(proposals=[proposal]), round_number=1
        )

        self.assertEqual(accepted, [])

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
            fabricated.model_dump(exclude={"transmission_id", "hop", "root_signal_fact_ids"})
        )
        rejected = InvestmentReasoningEngine.validate_round(
            context, accepted[:-1], TransmissionBatch(proposals=[fabricated_proposal]), round_number=3
        )

        self.assertEqual([item.hop for item in accepted], [1, 2, 3])
        self.assertTrue(all(item.root_signal_fact_ids == [fact.uuid] for item in accepted))
        self.assertEqual(rejected, [])

    def test_uncited_directional_node_claim_is_normalized_to_insufficient(self) -> None:
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

        self.assertEqual(normalized.short, Trend.INSUFFICIENT_EVIDENCE)
        self.assertEqual(normalized.investment_assessment, InvestmentAssessment.INSUFFICIENT_EVIDENCE)

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


class LayeredImpactLineageGateTest(unittest.TestCase):
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

    def _layer_context(
        self,
        layer: ImpactLayer,
        *,
        parent_claims: list[AcceptedImpactClaim] | None = None,
    ) -> LayerAnalysisContext:
        return LayerAnalysisContext(
            layer=layer,
            decision_at=self.decision_at,
            question="逐层分析最近48小时事件的投研影响",
            events=[self.event],
            anchors=[self.geo_anchor, self.macro_anchor],
            facts=[self.signal, self.mechanism],
            parent_claims=parent_claims or [],
            direct_signal_fact_ids=[self.signal.uuid],
        )

    def _geo_batch(self, *, source_fact_ids: list[str] | None = None) -> LayerImpactBatch:
        return LayerImpactBatch(
            proposals=[
                ImpactClaimProposal(
                    anchor_id=self.geo_anchor.business_id,
                    variable_id="geopolitical_risk",
                    direction=Direction.UP,
                    horizons=[Horizon.SHORT],
                    confidence=Confidence.MEDIUM,
                    summary="美伊冲突的短期地缘政治风险上升。",
                    mechanism="军事冲突概率上升提高区域不确定性。",
                    source_fact_ids=source_fact_ids or [self.signal.uuid],
                )
            ],
            summary="地缘政治风险上升。",
        )

    def test_direct_geo_claim_gets_root_lineage_from_the_cited_signal(self) -> None:
        accepted = InvestmentReasoningEngine.validate_layer_batch(
            self._layer_context(ImpactLayer.GEOPOLITICAL),
            [],
            self._geo_batch(),
            layer=ImpactLayer.GEOPOLITICAL,
        )

        self.assertEqual(len(accepted), 1)
        self.assertIsInstance(accepted[0], AcceptedImpactClaim)
        self.assertEqual(accepted[0].derivation, "DIRECT_SIGNAL")
        self.assertEqual(accepted[0].root_event_ids, ["event-1"])
        self.assertEqual(accepted[0].root_signal_fact_ids, [self.signal.uuid])
        self.assertEqual(accepted[0].layer, ImpactLayer.GEOPOLITICAL)

    def test_merged_signal_claim_keeps_only_current_event_roots(self) -> None:
        merged_signal = self.signal.model_copy(
            update={"source_event_ids": [self.event.event_id, "event-before-current-window"]}
        )
        context = self._layer_context(ImpactLayer.GEOPOLITICAL).model_copy(
            update={"facts": [merged_signal, self.mechanism]}
        )

        accepted = InvestmentReasoningEngine.validate_layer_batch(
            context,
            [],
            self._geo_batch(),
            layer=ImpactLayer.GEOPOLITICAL,
        )

        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].root_event_ids, [self.event.event_id])
        self.assertNotIn("event-before-current-window", accepted[0].root_event_ids)

    def test_model_proposal_cannot_self_declare_accepted_lineage(self) -> None:
        with self.assertRaises(ValidationError):
            ImpactClaimProposal.model_validate(
                {
                    "anchor_id": self.geo_anchor.business_id,
                    "variable_id": "geopolitical_risk",
                    "direction": "UP",
                    "horizons": ["SHORT"],
                    "confidence": "MEDIUM",
                    "summary": "模型试图自报根谱。",
                    "mechanism": "根谱只能由代码解析。",
                    "source_fact_ids": [self.signal.uuid],
                    "root_event_ids": ["event-1"],
                    "root_signal_fact_ids": [self.signal.uuid],
                }
            )

    def test_ordinary_fact_cannot_be_declared_as_a_direct_layer_root(self) -> None:
        accepted = InvestmentReasoningEngine.validate_layer_batch(
            self._layer_context(ImpactLayer.GEOPOLITICAL),
            [],
            self._geo_batch(source_fact_ids=[self.mechanism.uuid]),
            layer=ImpactLayer.GEOPOLITICAL,
        )

        self.assertEqual(accepted, [])

    def test_macro_cross_layer_claim_inherits_roots_and_caps_confidence(self) -> None:
        parent = InvestmentReasoningEngine.validate_layer_batch(
            self._layer_context(ImpactLayer.GEOPOLITICAL),
            [],
            self._geo_batch(),
            layer=ImpactLayer.GEOPOLITICAL,
        )[0]
        batch = LayerImpactBatch(
            proposals=[
                ImpactClaimProposal(
                    anchor_id=self.macro_anchor.business_id,
                    variable_id="inflation_expectation",
                    direction=Direction.UP,
                    horizons=[Horizon.SHORT],
                    confidence=Confidence.HIGH,
                    summary="能源风险使短期通胀预期上升。",
                    mechanism="能源供给风险经油价传导至通胀。",
                    mechanism_fact_ids=[self.mechanism.uuid],
                    parent_claim_ids=[parent.claim_id],
                )
            ],
            summary="通胀预期受到上行压力。",
        )

        accepted = InvestmentReasoningEngine.validate_layer_batch(
            self._layer_context(ImpactLayer.MACRO_ECONOMIC, parent_claims=[parent]),
            [parent],
            batch,
            layer=ImpactLayer.MACRO_ECONOMIC,
        )

        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].derivation, "CROSS_LAYER")
        self.assertEqual(accepted[0].root_event_ids, parent.root_event_ids)
        self.assertEqual(accepted[0].root_signal_fact_ids, parent.root_signal_fact_ids)
        self.assertEqual(accepted[0].parent_claim_ids, [parent.claim_id])
        self.assertEqual(accepted[0].mechanism_fact_ids, [self.mechanism.uuid])
        self.assertNotEqual(accepted[0].confidence, Confidence.HIGH)

    def test_macro_cross_layer_claim_without_a_mechanism_fact_is_rejected(self) -> None:
        parent = InvestmentReasoningEngine.validate_layer_batch(
            self._layer_context(ImpactLayer.GEOPOLITICAL),
            [],
            self._geo_batch(),
            layer=ImpactLayer.GEOPOLITICAL,
        )[0]
        batch = LayerImpactBatch(
            proposals=[
                ImpactClaimProposal(
                    anchor_id=self.macro_anchor.business_id,
                    variable_id="inflation_expectation",
                    direction=Direction.UP,
                    horizons=[Horizon.SHORT],
                    confidence=Confidence.MEDIUM,
                    summary="通胀预期上升。",
                    mechanism="缺少图谱机制事实。",
                    parent_claim_ids=[parent.claim_id],
                )
            ],
            summary="宏观层候选结论。",
        )

        accepted = InvestmentReasoningEngine.validate_layer_batch(
            self._layer_context(ImpactLayer.MACRO_ECONOMIC, parent_claims=[parent]),
            [parent],
            batch,
            layer=ImpactLayer.MACRO_ECONOMIC,
        )

        self.assertEqual(accepted, [])

    def test_cross_layer_mechanism_must_connect_parent_and_current_anchors(self) -> None:
        parent = InvestmentReasoningEngine.validate_layer_batch(
            self._layer_context(ImpactLayer.GEOPOLITICAL),
            [],
            self._geo_batch(),
            layer=ImpactLayer.GEOPOLITICAL,
        )[0]
        one_sided_mechanism = self.mechanism.model_copy(
            update={
                "uuid": "only-touches-current-anchor",
                "source_uuid": "unrelated-uuid",
                "source_name": "无关锚点",
                "source_business_id": "unrelated-anchor",
                "source_labels": ["Entity", "MacroEconomic"],
            }
        )
        context = self._layer_context(ImpactLayer.MACRO_ECONOMIC, parent_claims=[parent]).model_copy(
            update={"facts": [self.signal, one_sided_mechanism]}
        )
        batch = LayerImpactBatch(
            proposals=[
                ImpactClaimProposal(
                    anchor_id=self.macro_anchor.business_id,
                    variable_id="inflation_expectation",
                    direction=Direction.UP,
                    horizons=[Horizon.SHORT],
                    confidence=Confidence.MEDIUM,
                    summary="单边触及当前锚点不足以证明跨层传导。",
                    mechanism="该 Fact 没有连接地缘政治父结论锚点。",
                    mechanism_fact_ids=[one_sided_mechanism.uuid],
                    parent_claim_ids=[parent.claim_id],
                )
            ],
            summary="应拒绝单边机制 Fact。",
        )

        accepted = InvestmentReasoningEngine.validate_layer_batch(
            context,
            [parent],
            batch,
            layer=ImpactLayer.MACRO_ECONOMIC,
        )

        self.assertEqual(accepted, [])

    def test_future_mechanism_fact_cannot_support_a_cross_layer_claim(self) -> None:
        parent = InvestmentReasoningEngine.validate_layer_batch(
            self._layer_context(ImpactLayer.GEOPOLITICAL),
            [],
            self._geo_batch(),
            layer=ImpactLayer.GEOPOLITICAL,
        )[0]
        future_mechanism = self.mechanism.model_copy(
            update={
                "uuid": "future-mechanism",
                "valid_at": datetime(2026, 8, 30, 0, 0, tzinfo=UTC),
            }
        )
        context = self._layer_context(ImpactLayer.MACRO_ECONOMIC, parent_claims=[parent]).model_copy(
            update={"facts": [self.signal, future_mechanism]}
        )
        batch = LayerImpactBatch(
            proposals=[
                ImpactClaimProposal(
                    anchor_id=self.macro_anchor.business_id,
                    variable_id="inflation_expectation",
                    direction=Direction.UP,
                    horizons=[Horizon.SHORT],
                    confidence=Confidence.MEDIUM,
                    summary="未生效的机制 Fact 不能支撑当前结论。",
                    mechanism="机制关系晚于本次决策时点才生效。",
                    mechanism_fact_ids=[future_mechanism.uuid],
                    parent_claim_ids=[parent.claim_id],
                )
            ],
            summary="应拒绝未生效的机制 Fact。",
        )

        accepted = InvestmentReasoningEngine.validate_layer_batch(
            context,
            [parent],
            batch,
            layer=ImpactLayer.MACRO_ECONOMIC,
        )

        self.assertEqual(accepted, [])

    def test_a_layer_cannot_use_a_same_layer_claim_as_its_parent(self) -> None:
        geo_parent = InvestmentReasoningEngine.validate_layer_batch(
            self._layer_context(ImpactLayer.GEOPOLITICAL),
            [],
            self._geo_batch(),
            layer=ImpactLayer.GEOPOLITICAL,
        )[0]
        same_layer_parent = geo_parent.model_copy(update={"layer": ImpactLayer.MACRO_ECONOMIC})
        batch = LayerImpactBatch(
            proposals=[
                ImpactClaimProposal(
                    anchor_id=self.macro_anchor.business_id,
                    variable_id="inflation_expectation",
                    direction=Direction.UP,
                    horizons=[Horizon.SHORT],
                    confidence=Confidence.LOW,
                    summary="同层结论不应成为自己的上游依据。",
                    mechanism="宏观同层循环引用。",
                    mechanism_fact_ids=[self.mechanism.uuid],
                    parent_claim_ids=[same_layer_parent.claim_id],
                )
            ],
            summary="非法同层传导。",
        )

        accepted = InvestmentReasoningEngine.validate_layer_batch(
            self._layer_context(ImpactLayer.MACRO_ECONOMIC, parent_claims=[same_layer_parent]),
            [same_layer_parent],
            batch,
            layer=ImpactLayer.MACRO_ECONOMIC,
        )

        self.assertEqual(accepted, [])


class InvestmentWorkflowShapeTest(unittest.TestCase):
    def test_schedule_natural_language_is_a_first_class_reasoning_input(self) -> None:
        message = "获取最近72小时全部Event，逐层分析对产业链的投研影响。"

        parsed = InvestmentReasoningInput.model_validate(message)

        self.assertEqual(parsed.question, message)
        self.assertEqual(parsed.event_window_hours, 72)
        self.assertFalse(parsed.include_company)

    def test_workflow_has_five_fixed_business_stages(self) -> None:
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
            ],
        )

    def test_http_natural_language_contract_is_owned_by_prepare_not_workflow_input_schema(self) -> None:
        workflow = _seed_workflow(cast(Agent, object()), cast(Agent, object()))

        self.assertEqual(INVESTMENT_REASONING_CONTRACT_VERSION, 4)
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
            claims=[],
            summary="地缘政治层未形成方向结论。",
            limitations=["NO_GEOPOLITICAL_SIGNAL"],
        )
        self.macro = LayerAnalysisResult(
            layer=ImpactLayer.MACRO_ECONOMIC,
            claims=[],
            summary="宏观经济层未形成方向结论。",
            limitations=["NO_MACRO_TRANSMISSION"],
        )
        self.industry = LayerAnalysisResult(
            layer=ImpactLayer.INDUSTRY,
            claims=[],
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
    ) -> IndustryAnalysisState:
        self.calls.append(("industry", (prepared, geopolitical, macro)))
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
    def _accepted_claim(layer: ImpactLayer, fact: FactSnapshot) -> AcceptedImpactClaim:
        return AcceptedImpactClaim(
            anchor_id="node-a",
            variable_id="effective_capacity",
            direction=Direction.DOWN,
            horizons=[Horizon.SHORT],
            confidence=Confidence.MEDIUM,
            summary=f"{layer.value} 层形成一条已接受结论。",
            mechanism="当前有效 Signal 支持该结论。",
            source_fact_ids=[fact.uuid],
            claim_id=f"claim-{layer.value.lower()}",
            layer=layer,
            anchor_name="上游",
            anchor_type="ChainNode",
            derivation="DIRECT_SIGNAL",
            root_event_ids=["event-1"],
            root_signal_fact_ids=[fact.uuid],
        )

    @classmethod
    def _finalization_state(
        cls,
        *,
        claim_layers: tuple[ImpactLayer, ...] = (),
        include_transmission: bool = False,
        invalid_root: bool = False,
    ) -> IndustryAnalysisState:
        gate = InvestmentReasoningGateTest()
        fact = gate._active_signal()
        context = gate._context(fact)
        claims = {layer: cls._accepted_claim(layer, fact) for layer in claim_layers}
        if invalid_root and claims:
            first_layer = claim_layers[0]
            claims[first_layer] = claims[first_layer].model_copy(
                update={"root_signal_fact_ids": ["missing-signal-root"]}
            )

        def layer_result(layer: ImpactLayer) -> LayerAnalysisResult:
            claim = claims.get(layer)
            return LayerAnalysisResult(
                layer=layer,
                claims=[claim] if claim is not None else [],
                supporting_facts=[fact] if claim is not None else [],
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
            geo_output = await analyze_geopolitical_impact(
                StepInput(previous_step_outputs={"prepare-investment-context": StepOutput(content=prepared)})
            )
            geo_state = cast(GeopoliticalAnalysisState, geo_output.content)
            macro_output = await analyze_macro_impact(
                StepInput(previous_step_outputs={"analyze-geopolitical-impact": StepOutput(content=geo_state)})
            )
            macro_state = cast(MacroAnalysisState, macro_output.content)
            industry_output = await analyze_industry_impact(
                StepInput(previous_step_outputs={"analyze-macro-impact": StepOutput(content=macro_state)})
            )
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

    async def test_hard_gate_safe_fallback_clears_all_claims_facts_and_transmissions(self) -> None:
        state = self._finalization_state(
            claim_layers=(ImpactLayer.GEOPOLITICAL, ImpactLayer.MACRO_ECONOMIC, ImpactLayer.INDUSTRY),
            include_transmission=True,
            invalid_root=True,
        )
        reviewer = AsyncMock()
        configure_investment_workflow_runtime(SimpleNamespace(review=reviewer))
        try:
            output = await review_and_finalize(
                StepInput(previous_step_outputs={"analyze-industry-impact": StepOutput(content=state)}),
                self._run_context("run-hard-gate"),
            )
        finally:
            configure_investment_workflow_runtime(None)

        result = cast(InvestmentAnalysisResult, output.content)
        reviewer.assert_not_awaited()
        for layer in (result.geopolitical, result.macro, result.industry):
            self.assertEqual(layer.claims, [])
            self.assertEqual(layer.supporting_facts, [])
        self.assertEqual(result.transmissions, [])
        self.assertFalse({"LAYER_CLAIM", "TRANSMISSION"}.intersection(item.node_type for item in result.reasoning_tree))

    async def test_reviewer_rejection_clears_all_claims_facts_and_transmissions(self) -> None:
        state = self._finalization_state(
            claim_layers=(ImpactLayer.GEOPOLITICAL, ImpactLayer.MACRO_ECONOMIC, ImpactLayer.INDUSTRY),
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
                StepInput(previous_step_outputs={"analyze-industry-impact": StepOutput(content=state)}),
                self._run_context("run-review-rejection"),
            )
        finally:
            configure_investment_workflow_runtime(None)

        result = cast(InvestmentAnalysisResult, output.content)
        reviewer.assert_awaited_once_with(state)
        for layer in (result.geopolitical, result.macro, result.industry):
            self.assertEqual(layer.claims, [])
            self.assertEqual(layer.supporting_facts, [])
        self.assertEqual(result.transmissions, [])
        self.assertFalse({"LAYER_CLAIM", "TRANSMISSION"}.intersection(item.node_type for item in result.reasoning_tree))

    async def test_any_layer_claim_or_transmission_requires_semantic_review(self) -> None:
        cases = [
            ("GEOPOLITICAL_CLAIM", (ImpactLayer.GEOPOLITICAL,), False),
            ("MACRO_CLAIM", (ImpactLayer.MACRO_ECONOMIC,), False),
            ("INDUSTRY_CLAIM", (ImpactLayer.INDUSTRY,), False),
            ("TRANSMISSION", (), True),
        ]
        for label, claim_layers, include_transmission in cases:
            with self.subTest(material_result=label):
                state = self._finalization_state(
                    claim_layers=claim_layers,
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
                        StepInput(previous_step_outputs={"analyze-industry-impact": StepOutput(content=state)}),
                        self._run_context(f"run-{label.lower()}"),
                    )
                finally:
                    configure_investment_workflow_runtime(None)

                reviewer.assert_awaited_once_with(state)

    async def test_upper_layer_mechanism_facts_survive_into_industry_review_and_reasoning_tree(self) -> None:
        fixture = LayeredImpactLineageGateTest()
        fixture.setUp()
        geo_claim = InvestmentReasoningEngine.validate_layer_batch(
            fixture._layer_context(ImpactLayer.GEOPOLITICAL),
            [],
            fixture._geo_batch(),
            layer=ImpactLayer.GEOPOLITICAL,
        )[0]
        macro_batch = LayerImpactBatch(
            proposals=[
                ImpactClaimProposal(
                    anchor_id=fixture.macro_anchor.business_id,
                    variable_id="inflation_expectation",
                    direction=Direction.UP,
                    horizons=[Horizon.SHORT],
                    confidence=Confidence.MEDIUM,
                    summary="能源供给风险把地缘影响传导至通胀预期。",
                    mechanism="中东能源供给风险通过油价传导至通胀预期。",
                    mechanism_fact_ids=[fixture.mechanism.uuid],
                    parent_claim_ids=[geo_claim.claim_id],
                )
            ],
            summary="宏观层形成一条可审计的跨层结论。",
        )
        macro_claim = InvestmentReasoningEngine.validate_layer_batch(
            fixture._layer_context(ImpactLayer.MACRO_ECONOMIC, parent_claims=[geo_claim]),
            [geo_claim],
            macro_batch,
            layer=ImpactLayer.MACRO_ECONOMIC,
        )[0]
        base = InvestmentAnalysisContext(
            request=InvestmentAnalysisRequest(
                question="逐层分析最近48小时事件",
                decision_at=fixture.decision_at,
            ),
            events=[fixture.event],
            facts=[fixture.signal],
            anchors=[fixture.geo_anchor, fixture.macro_anchor],
        )
        prepared = PreparedInvestmentContext(
            context=base,
            context_fingerprint=InvestmentReasoningEngine.context_fingerprint(base),
        )
        geopolitical = LayerAnalysisResult(
            layer=ImpactLayer.GEOPOLITICAL,
            claims=[geo_claim],
            supporting_facts=[fixture.signal],
            summary="地缘政治结论。",
        )
        macro = LayerAnalysisResult(
            layer=ImpactLayer.MACRO_ECONOMIC,
            claims=[macro_claim],
            supporting_facts=[fixture.signal, fixture.mechanism],
            summary="宏观经济结论。",
        )
        industry = LayerAnalysisResult(
            layer=ImpactLayer.INDUSTRY,
            claims=[],
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
            parent_claims=[geo_claim, macro_claim],
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
                StepInput(previous_step_outputs={"analyze-industry-impact": StepOutput(content=state)}),
                self._run_context("run-upper-layer-mechanism"),
            )
        finally:
            configure_investment_workflow_runtime(None)

        result = cast(InvestmentAnalysisResult, output.content)
        reviewer.assert_awaited_once()
        await_args = reviewer.await_args
        assert await_args is not None
        reviewed_state = await_args.args[0]
        self.assertIn(fixture.mechanism.uuid, {fact.uuid for fact in reviewed_state.industry_context.facts})
        fact_nodes = {node.node_id: node for node in result.reasoning_tree if node.node_type == "FACT"}
        self.assertIn(fixture.mechanism.uuid, fact_nodes)
        macro_node = next(node for node in result.reasoning_tree if node.node_id == macro_claim.claim_id)
        self.assertIn(fixture.mechanism.uuid, macro_node.parent_ids)

    async def test_final_result_is_an_idempotent_standalone_artifact(self) -> None:
        state = self._finalization_state()
        context = self._run_context("run-artifact-idempotency")
        configure_investment_workflow_runtime(SimpleNamespace(review=AsyncMock()))
        try:
            first = await review_and_finalize(
                StepInput(previous_step_outputs={"analyze-industry-impact": StepOutput(content=state)}),
                context,
            )
            second = await review_and_finalize(
                StepInput(previous_step_outputs={"analyze-industry-impact": StepOutput(content=state)}),
                context,
            )
        finally:
            configure_investment_workflow_runtime(None)

        artifact = cast(InvestmentConclusionArtifact, first.content)
        self.assertEqual(second.content, artifact)
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
        parent = InvestmentWorkflowExecutionTest._accepted_claim(ImpactLayer.GEOPOLITICAL, base_signal)
        parents = [
            parent.model_copy(
                update={
                    "claim_id": f"parent-claim-{index}",
                    "summary": f"父层结论 {index:03d} " + "传导背景" * 30,
                    "mechanism": f"父层机制 {index:03d} " + "机制说明" * 30,
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
