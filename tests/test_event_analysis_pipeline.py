"""Behavior tests for bounded Graphiti-native Signal candidate retrieval."""

import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from graphiti_core.nodes import EntityNode
from pydantic import ValidationError

from sematica.analysis.event.contracts import (
    EventAnalysisInput,
    EventClass,
    EventClassification,
)
from sematica.analysis.event.graphiti.candidates import GraphitiCandidateRetriever
from sematica.ingestion.episcode.event.contracts import EventCandidateDTO, HistoricalEvent


class GraphitiCandidateRetrieverTest(unittest.IsolatedAsyncioTestCase):
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
            for index in range(1, 36)
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

        self.assertEqual(len(candidates.variables), 35)
        self.assertEqual({item.variable_group.value for item in candidates.variables}, {"DEMAND"})

    async def test_retrieval_fails_closed_when_selected_variable_groups_exceed_public_bound(self) -> None:
        graphiti = MagicMock()
        graphiti.search_ = AsyncMock(return_value=MagicMock(nodes=[]))
        graphiti.driver.execute_query = AsyncMock()
        demand_rows = [
            {
                "uuid": f"variable-demand-{index}",
                "variable_id": f"demand_{index:02d}",
                "name": f"需求变量{index}",
                "variable_group": "DEMAND",
                "allowed_anchor_types": ["IndustryChain"],
                "definition": f"需求定义{index}",
            }
            for index in range(1, 66)
        ]

        async def execute_query(query, **kwargs):
            del kwargs
            if "mentioned_anchor_candidates" in query:
                return ([{"uuid": "anchor-chain"}], None, None)
            if "fundamental_variable_candidates" in query:
                return (demand_rows, None, None)
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

        with (
            patch.object(EntityNode, "get_by_uuid", new=AsyncMock(return_value=anchor)),
            self.assertRaisesRegex(ValidationError, "at most 64"),
        ):
            await GraphitiCandidateRetriever(graphiti).retrieve(self._event(), classification)

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

        narrowed = classification.model_copy(update={"anchor_type_hints": [classification.anchor_type_hints[0]]})
        with patch.object(EntityNode, "get_by_uuid", new=get_by_uuid):
            narrowed_candidates = await GraphitiCandidateRetriever(graphiti).retrieve(self._event(), narrowed)
        self.assertEqual(
            {item.entity_type.value for item in narrowed_candidates.anchors},
            {"IndustryChain"},
        )


if __name__ == "__main__":
    unittest.main()
