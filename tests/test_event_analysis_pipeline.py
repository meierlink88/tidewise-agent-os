"""Behavior tests for controlled Event-to-Signal analysis."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from graphiti_core.nodes import EntityNode

from sematica.analysis.event.contracts import (
    AnchorCandidate,
    CandidateSet,
    EventAnalysisInput,
    EventClass,
    EventClassification,
    SignalProposal,
    VariableCandidate,
)
from sematica.analysis.event.graphiti.candidates import GraphitiCandidateRetriever
from sematica.analysis.event.pipeline import EventAnalysisPipeline
from sematica.ingestion.episcode.event.contracts import EventCandidateDTO, HistoricalEvent


class EventAnalysisPipelineTest(unittest.IsolatedAsyncioTestCase):
    EVENT_TIME = datetime(2026, 8, 29, tzinfo=UTC)

    @classmethod
    def _event(cls) -> EventAnalysisInput:
        candidate = EventCandidateDTO.model_validate(
            {
                "title": "美国扩大高带宽内存出口限制",
                "summary": "美国宣布扩大对华高带宽内存出口限制。",
                "semantic": {
                    "actors": ["美国政府"],
                    "action": "扩大出口限制",
                    "objects": ["高带宽内存"],
                    "stage": "ANNOUNCED",
                    "jurisdictions": ["中国"],
                    "effective_at": None,
                    "time_precision": "DAY",
                },
                "modality": "FACT",
                "occurred_at": None,
                "announced_at": cls.EVENT_TIME,
            }
        )
        return EventAnalysisInput(
            event=HistoricalEvent(id="EVT15bec7e3-998c-4434-aa5d-29712c4c67cf", event=candidate),
            episode_uuid="episode-event",
            reference_time=cls.EVENT_TIME,
        )

    @staticmethod
    def _classification() -> EventClassification:
        return EventClassification(
            event_class=EventClass.CHAIN_NODE,
            confidence="HIGH",
            anchor_type_hints=["ChainNode"],
            variable_group_hints=["SUPPLY_CAPACITY"],
            retrieval_queries=["高带宽内存 出口限制"],
            rationale="事件直接发生在产业链节点。",
        )

    @staticmethod
    def _candidates() -> CandidateSet:
        return CandidateSet(
            anchors=[
                AnchorCandidate(
                    uuid="anchor-hbm",
                    name="高带宽内存",
                    entity_type="ChainNode",
                    business_id="node-hbm",
                ),
                AnchorCandidate(
                    uuid="anchor-ai-card",
                    name="AI加速卡",
                    entity_type="ChainNode",
                    business_id="node-ai-card",
                ),
            ],
            variables=[
                VariableCandidate(
                    uuid="variable-supply",
                    variable_id="effective_supply",
                    name="有效供给",
                    variable_group="SUPPLY_CAPACITY",
                    allowed_anchor_types=["ChainNode"],
                    definition="可向目标市场实际交付的供给能力。",
                )
            ],
        )

    @classmethod
    def _proposal(cls, anchor_uuid: str) -> SignalProposal:
        return SignalProposal(
            anchor_uuid=anchor_uuid,
            variable_uuid="variable-supply",
            fact="出口限制令有效供给下降。",
            direction="DOWN",
            magnitude="HIGH",
            derivation_type="DERIVED",
            assertion_modality="ACTUAL",
            valid_at=cls.EVENT_TIME,
            impact_onset_earliest=cls.EVENT_TIME,
            impact_onset_latest=cls.EVENT_TIME,
            impact_peak_earliest=cls.EVENT_TIME + timedelta(days=7),
            impact_peak_latest=cls.EVENT_TIME + timedelta(days=7),
            expected_end_earliest=cls.EVENT_TIME + timedelta(days=90),
            expected_end_latest=cls.EVENT_TIME + timedelta(days=90),
            horizon_tags=["SHORT"],
            mechanism="出口限制减少可交付供给。",
            duration_basis="政策约束持续期间。",
            assumptions=[],
            invalidation_conditions=["限制撤销"],
            provenance_confidence="HIGH",
            mechanism_confidence="HIGH",
            temporal_confidence="MEDIUM",
        )

    async def test_projects_valid_signal_when_a_sibling_proposal_is_rejected(self) -> None:
        classifier = AsyncMock()
        classifier.classify.return_value = self._classification()
        retriever = AsyncMock()
        retriever.retrieve.return_value = self._candidates()
        extractor = AsyncMock()
        extractor.extract.return_value = [self._proposal("anchor-hbm"), self._proposal("anchor-ai-card")]
        reviewer = AsyncMock()
        reviewer.review.side_effect = [True, False]
        projector = AsyncMock()
        projector.project.return_value = "fact-hbm"
        pipeline = EventAnalysisPipeline(classifier, retriever, extractor, reviewer, projector)

        outcome = await pipeline.analyze(self._event())

        self.assertEqual(outcome.status, "SUCCEEDED")
        self.assertEqual(outcome.signal_fact_uuids, ["fact-hbm"])
        self.assertEqual(
            outcome.reason_codes,
            ["DIRECT_SIGNAL_FACTS_PROJECTED", "SIGNAL_REVIEW_REJECTED"],
        )
        projector.project.assert_awaited_once()

    async def test_returns_no_signal_when_every_proposal_is_rejected(self) -> None:
        classifier = AsyncMock()
        classifier.classify.return_value = self._classification()
        retriever = AsyncMock()
        retriever.retrieve.return_value = self._candidates()
        extractor = AsyncMock()
        extractor.extract.return_value = [self._proposal("anchor-hbm")]
        reviewer = AsyncMock()
        reviewer.review.return_value = False
        projector = AsyncMock()
        pipeline = EventAnalysisPipeline(classifier, retriever, extractor, reviewer, projector)

        outcome = await pipeline.analyze(self._event())

        self.assertEqual(outcome.status, "NO_SIGNAL")
        self.assertEqual(outcome.signal_fact_uuids, [])
        self.assertEqual(outcome.reason_codes, ["SIGNAL_REVIEW_REJECTED"])
        projector.project.assert_not_awaited()

    async def test_retrieval_keeps_every_fundamental_variable_in_the_hinted_groups(self) -> None:
        graphiti = MagicMock()
        graphiti.search_ = AsyncMock(return_value=MagicMock(nodes=[]))
        graphiti.driver.execute_query = AsyncMock()
        demand_rows = [
            {
                "uuid": f"variable-demand-{index}",
                "variable_id": f"demand_{index}",
                "name": f"需求变量{index}",
                "variable_group": "DEMAND",
                "allowed_anchor_types": ["IndustryChain"],
                "definition": f"需求定义{index}",
            }
            for index in range(1, 8)
        ]
        unrelated_rows = [
            {
                "uuid": "variable-supply",
                "variable_id": "supply_1",
                "name": "供给变量",
                "variable_group": "SUPPLY_CAPACITY",
                "allowed_anchor_types": ["IndustryChain"],
                "definition": "供给定义",
            }
        ]

        async def execute_query(query, **kwargs):
            del kwargs
            if "mentioned_anchor_candidates" in query:
                return ([{"uuid": "anchor-chain"}], None, None)
            if "fundamental_variable_candidates" in query:
                return ([*demand_rows, *unrelated_rows], None, None)
            return ([], None, None)

        graphiti.driver.execute_query.side_effect = execute_query
        anchor = EntityNode(
            uuid="anchor-chain",
            name="AI计算芯片产业链",
            group_id="neo4j",
            labels=["Entity", "IndustryChain"],
            attributes={"data_object_id": "chain-ai-compute"},
        )
        classification = EventClassification(
            event_class=EventClass.INDUSTRY_CHAIN,
            confidence="HIGH",
            anchor_type_hints=["IndustryChain"],
            variable_group_hints=["DEMAND"],
            retrieval_queries=["AI计算芯片需求"],
            rationale="产业链需求事件。",
        )

        with patch.object(EntityNode, "get_by_uuid", new=AsyncMock(return_value=anchor)):
            candidates = await GraphitiCandidateRetriever(graphiti).retrieve(self._event(), classification)

        self.assertEqual(len(candidates.variables), 7)
        self.assertEqual({item.variable_group.value for item in candidates.variables}, {"DEMAND"})

    async def test_anchor_limit_is_applied_per_entity_type(self) -> None:
        graphiti = MagicMock()
        graphiti.search_ = AsyncMock(return_value=MagicMock(nodes=[]))
        graphiti.driver.execute_query = AsyncMock()
        nodes = {
            **{
                f"node-{index}": EntityNode(
                    uuid=f"node-{index}",
                    name=f"产业链节点{index}",
                    group_id="neo4j",
                    labels=["Entity", "ChainNode"],
                    attributes={"data_object_id": f"node-id-{index}"},
                )
                for index in range(6)
            },
            **{
                f"chain-{index}": EntityNode(
                    uuid=f"chain-{index}",
                    name=f"产业链{index}",
                    group_id="neo4j",
                    labels=["Entity", "IndustryChain"],
                    attributes={"data_object_id": f"chain-id-{index}"},
                )
                for index in range(3)
            },
        }

        async def execute_query(query, **kwargs):
            del kwargs
            if "mentioned_anchor_candidates" in query:
                return ([{"uuid": uuid} for uuid in nodes], None, None)
            return ([], None, None)

        async def get_by_uuid(_driver, uuid):
            return nodes[uuid]

        graphiti.driver.execute_query.side_effect = execute_query
        classification = EventClassification(
            event_class=EventClass.INDUSTRY_CHAIN,
            confidence="HIGH",
            anchor_type_hints=["IndustryChain", "ChainNode"],
            variable_group_hints=["DEMAND"],
            retrieval_queries=["产业链"],
            rationale="产业链事件。",
        )

        with patch.object(EntityNode, "get_by_uuid", new=get_by_uuid):
            candidates = await GraphitiCandidateRetriever(graphiti).retrieve(self._event(), classification)

        type_counts = {
            entity_type: sum(item.entity_type.value == entity_type for item in candidates.anchors)
            for entity_type in ("ChainNode", "IndustryChain")
        }
        self.assertEqual(type_counts, {"ChainNode": 4, "IndustryChain": 3})


if __name__ == "__main__":
    unittest.main()
