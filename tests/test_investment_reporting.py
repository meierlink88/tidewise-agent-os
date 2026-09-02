"""Fixed local Report Artifact projection contracts for investment reasoning."""

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from capabilities.investment import (
    AcceptedCrossLayerTransmission,
    AcceptedTransmission,
    AnalysisDraft,
    ChainNodeSnapshot,
    ChainTrendView,
    Confidence,
    Direction,
    EventSnapshot,
    FactSnapshot,
    Horizon,
    ImpactLayer,
    IndustryChainSnapshot,
    InvestmentAnalysisContext,
    InvestmentAnalysisRequest,
    InvestmentAssessment,
    InvestmentConclusionArtifact,
    LayerAnalysisResult,
    LayerAssessment,
    NodeTrendView,
    ReasoningOntologyContext,
    ReviewResult,
    TopologyEdgeSnapshot,
    Trend,
)
from capabilities.investment.internal.reporting import EventEvidenceIndex, InvestmentReportAssembler


class InvestmentReportingTest(unittest.IsolatedAsyncioTestCase):
    GEO_EVIDENCE = "EVD11111111-1111-4111-8111-111111111111"
    MACRO_EVIDENCE = "EVD22222222-2222-4222-8222-222222222222"
    NODE_EVIDENCE = "EVD33333333-3333-4333-8333-333333333333"
    NOW = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.event_root = Path(self.temporary.name) / "event"
        batch = self.event_root / "batches" / "batch-a"
        batch.mkdir(parents=True)
        requests = []
        publications = []
        for index, (event_id, evidence_id) in enumerate(
            [
                ("event-geo", self.GEO_EVIDENCE),
                ("event-macro", self.MACRO_EVIDENCE),
                ("event-node", self.NODE_EVIDENCE),
            ]
        ):
            key = f"candidate-{index}"
            requests.append({"candidate_key": key, "candidate": {"evidence_ids": [evidence_id]}})
            publications.append({"candidate_key": key, "event_id": event_id})
        (batch / "identity-requests.json").write_text(json.dumps({"requests": requests}))
        (batch / "publications.json").write_text(json.dumps({"publications": publications}))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @classmethod
    def _signal(cls, uuid: str, event_id: str, anchor_id: str, anchor_name: str, anchor_type: str) -> FactSnapshot:
        return FactSnapshot(
            uuid=uuid,
            kind="SIGNAL",
            name="SIGNAL_ON",
            fact=f"{anchor_name} 市场需求上升",
            source_uuid=f"variable-{uuid}",
            source_name="市场需求",
            source_business_id="VAR-DEMAND",
            target_uuid=f"uuid-{anchor_id}",
            target_name=anchor_name,
            target_business_id=anchor_id,
            target_labels=["Entity", anchor_type],
            source_event_ids=[event_id],
            anchor_type=anchor_type,
            variable_id="market_demand",
            direction=Direction.UP,
            horizons=[Horizon.MEDIUM],
            confidence=Confidence.MEDIUM,
            valid_at=cls.NOW,
        )

    @classmethod
    def _assessment(
        cls,
        assessment_id: str,
        layer: ImpactLayer,
        anchor_id: str,
        anchor_name: str,
        anchor_type: str,
        fact: FactSnapshot,
    ) -> LayerAssessment:
        return LayerAssessment(
            anchor_id=anchor_id,
            result=Trend.WARMING,
            confidence=Confidence.MEDIUM,
            summary=f"{anchor_name}升温。",
            reasoning=f"{fact.fact}，因此{anchor_name}升温。",
            direct_signal_fact_ids=[fact.uuid],
            assessment_id=assessment_id,
            layer=layer,
            anchor_name=anchor_name,
            anchor_type=anchor_type,
            horizons=[Horizon.MEDIUM],
            root_event_ids=fact.source_event_ids,
        )

    def _fixture(self) -> tuple[InvestmentConclusionArtifact, InvestmentAnalysisContext]:
        geo_fact = self._signal("signal-geo", "event-geo", "geo-1", "海湾安全对抗", "GeopoliticRivalry")
        macro_fact = self._signal("signal-macro", "event-macro", "macro-1", "增长预期修正", "MacroEconomic")
        node_fact = self._signal("signal-node", "event-node", "node-1", "油品运输服务", "ChainNode")
        facts = [geo_fact, macro_fact, node_fact]
        geo_assessment = self._assessment(
            "assessment-geo", ImpactLayer.GEOPOLITICAL, "geo-1", "海湾安全对抗", "GeopoliticRivalry", geo_fact
        )
        macro_assessment = self._assessment(
            "assessment-macro", ImpactLayer.MACRO_ECONOMIC, "macro-1", "增长预期修正", "MacroEconomic", macro_fact
        )
        node_assessment = self._assessment(
            "assessment-node", ImpactLayer.INDUSTRY, "node-1", "油品运输服务", "ChainNode", node_fact
        )
        chain = IndustryChainSnapshot(
            uuid="chain-uuid",
            business_id="chain-1",
            name="油品石化贸易服务产业链",
            anchor_match_count=1,
            matched_node_ids=["node-1"],
            signal_root_fact_ids=[node_fact.uuid],
            signal_root_node_ids=["node-1"],
            nodes=[
                ChainNodeSnapshot(uuid="node-uuid-1", business_id="node-1", name="油品运输服务"),
                ChainNodeSnapshot(uuid="node-uuid-2", business_id="node-2", name="成品油批发交付服务"),
            ],
            edges=[
                TopologyEdgeSnapshot(
                    uuid="edge-uuid",
                    business_id="edge-1",
                    name="ChainNodeInputTo",
                    source_node_id="node-1",
                    source_name="油品运输服务",
                    target_node_id="node-2",
                    target_name="成品油批发交付服务",
                    fact="油品运输服务投入成品油批发交付服务",
                )
            ],
        )
        request = InvestmentAnalysisRequest(question="分析最近48小时事件", decision_at=self.NOW, max_chains=100)
        context = InvestmentAnalysisContext(
            request=request,
            events=[
                EventSnapshot(
                    episode_uuid=f"episode-{index}",
                    event_id=event_id,
                    title=event_id,
                    summary=event_id,
                    modality="FACT",
                    occurred_at=self.NOW,
                )
                for index, event_id in enumerate(("event-geo", "event-macro", "event-node"), 1)
            ],
            facts=facts,
            chains=[chain],
            ontology=ReasoningOntologyContext(
                entity_types={"Event": "事件", "ChainNode": "产业链节点"},
                fact_types={"SIGNAL_ON": "变量信号"},
                relationship_types={"ChainNodeInputTo": "投入传导"},
                usage_rules=["直接 Signal 不转换为 Claim。"],
            ),
        )
        topology = AcceptedTransmission(
            chain_id="chain-1",
            topology_edge_id="edge-1",
            source_node_id="node-1",
            target_node_id="node-2",
            flow="ALONG_EDGE",
            target_variable="market_demand",
            direction=Direction.UP,
            horizon=Horizon.MEDIUM,
            confidence=Confidence.LOW,
            mechanism="运输受限会继续影响批发交付。",
            source_fact_ids=[node_fact.uuid],
            transmission_id="tx-1",
            hop=1,
            root_signal_fact_ids=[node_fact.uuid],
        )
        cross = [
            AcceptedCrossLayerTransmission(
                source_assessment_id="assessment-geo",
                target_assessment_id="assessment-macro",
                logic="海湾安全风险通过能源运输条件强化增长预期下修。",
                confidence=Confidence.LOW,
                status="已形成解释闭环；仍需连续数据验证",
                transmission_id="xlt-1",
                source_layer=ImpactLayer.GEOPOLITICAL,
                target_layer=ImpactLayer.MACRO_ECONOMIC,
                relation_type="CROSS_LAYER",
            ),
            AcceptedCrossLayerTransmission(
                source_assessment_id="assessment-macro",
                target_assessment_id="assessment-node",
                logic="增长预期变化影响油品运输需求。",
                confidence=Confidence.LOW,
                status="目标节点已有直接 Signal",
                transmission_id="xlt-2",
                source_layer=ImpactLayer.MACRO_ECONOMIC,
                target_layer=ImpactLayer.INDUSTRY,
                relation_type="CROSS_LAYER",
            ),
        ]
        direct_node = NodeTrendView(
            chain_id="chain-1",
            node_id="node-1",
            node_name="油品运输服务",
            short=Trend.INSUFFICIENT_EVIDENCE,
            medium=Trend.WARMING,
            long=Trend.INSUFFICIENT_EVIDENCE,
            confidence=Confidence.MEDIUM,
            investment_assessment=InvestmentAssessment.OPPORTUNITY_CANDIDATE,
            rationale="运输需求上升使该节点升温。",
            supporting_fact_ids=[node_fact.uuid],
            supporting_assessment_ids=[node_assessment.assessment_id],
        )
        inferred_node = NodeTrendView(
            chain_id="chain-1",
            node_id="node-2",
            node_name="成品油批发交付服务",
            short=Trend.INSUFFICIENT_EVIDENCE,
            medium=Trend.WARMING,
            long=Trend.INSUFFICIENT_EVIDENCE,
            confidence=Confidence.LOW,
            investment_assessment=InvestmentAssessment.OPPORTUNITY_CANDIDATE,
            rationale="运输环节变化可能继续传至批发交付。",
            supporting_transmission_ids=[topology.transmission_id],
        )
        draft = AnalysisDraft(
            one_sentence_conclusion="油品链条出现升温影响。",
            chains=[
                ChainTrendView(
                    chain_id="chain-1",
                    chain_name=chain.name,
                    short=Trend.INSUFFICIENT_EVIDENCE,
                    medium=Trend.WARMING,
                    long=Trend.INSUFFICIENT_EVIDENCE,
                    confidence=Confidence.MEDIUM,
                    summary="运输节点直接升温，并可能向批发交付传导。",
                    nodes=[direct_node, inferred_node],
                )
            ],
        )
        analysis = InvestmentConclusionArtifact(
            executor="test",
            status="SUCCEEDED",
            context_fingerprint="fingerprint",
            geopolitical=LayerAnalysisResult(
                layer=ImpactLayer.GEOPOLITICAL,
                assessments=[geo_assessment],
                supporting_facts=[geo_fact],
                summary="海湾安全对抗升温。",
            ),
            macro=LayerAnalysisResult(
                layer=ImpactLayer.MACRO_ECONOMIC,
                assessments=[macro_assessment],
                supporting_facts=[macro_fact],
                summary="增长预期修正升温。",
            ),
            industry=LayerAnalysisResult(
                layer=ImpactLayer.INDUSTRY,
                assessments=[node_assessment],
                supporting_facts=[node_fact],
                summary="油品运输服务升温。",
            ),
            cross_layer_transmissions=cross,
            transmissions=[topology],
            draft=draft,
            review=ReviewResult(
                accepted=True,
                confidence=Confidence.MEDIUM,
                review_summary="通过",
            ),
            stage_metrics={},
            workflow_run_id="run-report-test",
            artifact_path="/tmp/audit.json",
            decision_at=self.NOW,
            question=request.question,
            event_window_hours=48,
            conclusion_status="SUPPORTED",
        )
        return analysis, context

    def test_fixed_report_uses_evidence_only_for_direct_conclusions(self) -> None:
        analysis, context = self._fixture()

        package = InvestmentReportAssembler(EventEvidenceIndex(self.event_root)).assemble(analysis, context)

        self.assertEqual(package.schema_version, "investment-report-artifact/v1")
        self.assertEqual(package.content.included_layers, ["geopolitics", "macroeconomics", "industry_chain"])
        self.assertEqual(package.content.status, "generated")
        self.assertEqual(package.content.statistics.industry_chain_count, 1)
        self.assertEqual(package.content.statistics.signaled_chain_node_count, 1)
        self.assertEqual(package.content.geopolitics.anchors[0].evidence_refs[0].evidence_id, self.GEO_EVIDENCE)
        direct, inferred = package.content.industry_chains[0].nodes
        self.assertEqual(direct.nature.code, "direct_evidence")
        self.assertEqual(direct.evidence_refs[0].evidence_id, self.NODE_EVIDENCE)
        self.assertEqual(inferred.nature.code, "reasoning_hypothesis")
        self.assertEqual(inferred.evidence_refs, [])
        self.assertEqual(package.content.geopolitics.downward_transmission.published_paths[0].evidence_refs, [])
        self.assertIn(
            "上述传导为经路径评分筛选的推理假设",
            package.content.industry_chains[0].uncertainty.counterevidence_and_gap or "",
        )

    def test_missing_upper_layer_anchors_keeps_fixed_sections_without_inventing_cards(self) -> None:
        analysis, context = self._fixture()
        analysis = analysis.model_copy(
            update={
                "geopolitical": LayerAnalysisResult(
                    layer=ImpactLayer.GEOPOLITICAL,
                    summary="地缘政治层未形成方向结论。",
                    limitations=["NO_GEOPOLITICAL_SIGNAL"],
                ),
                "macro": LayerAnalysisResult(
                    layer=ImpactLayer.MACRO_ECONOMIC,
                    summary="宏观经济层未形成方向结论。",
                    limitations=["NO_MACRO_SIGNAL"],
                ),
                "cross_layer_transmissions": [],
            }
        )

        artifact = InvestmentReportAssembler(EventEvidenceIndex(self.event_root)).assemble(analysis, context)

        self.assertEqual(artifact.content.geopolitics.anchors, [])
        self.assertEqual(artifact.content.macroeconomics.anchors, [])
        self.assertEqual([card.kind for card in artifact.content.report_cards], ["industry_chain"])
        self.assertEqual(artifact.content.statistics.geopolitic_anchor_count, 0)
        self.assertEqual(artifact.content.statistics.macroeconomic_anchor_count, 0)

    def test_chain_uncertainty_uses_readable_counterevidence_and_real_pending_nodes(self) -> None:
        analysis, context = self._fixture()
        chain = analysis.draft.chains[0]
        direct, inferred = chain.nodes
        direct = direct.model_copy(update={"risks": ["订单取消会削弱需求结论", "NO_SIGNAL_LINEAGE"]})
        inferred = inferred.model_copy(
            update={
                "short": Trend.INSUFFICIENT_EVIDENCE,
                "medium": Trend.INSUFFICIENT_EVIDENCE,
                "long": Trend.INSUFFICIENT_EVIDENCE,
                "supporting_transmission_ids": [],
            }
        )
        analysis = analysis.model_copy(
            update={
                "draft": analysis.draft.model_copy(
                    update={"chains": [chain.model_copy(update={"nodes": [direct, inferred]})]}
                )
            }
        )

        artifact = InvestmentReportAssembler(EventEvidenceIndex(self.event_root)).assemble(analysis, context)
        uncertainty = artifact.content.industry_chains[0].uncertainty.counterevidence_and_gap or ""

        self.assertIn("订单取消会削弱需求结论", uncertainty)
        self.assertIn("同链相邻节点缺少直接 Variable Signal 与经营观测", uncertainty)
        self.assertNotIn("NO_SIGNAL_LINEAGE", uncertainty)


if __name__ == "__main__":
    unittest.main()
