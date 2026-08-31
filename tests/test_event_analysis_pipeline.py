"""Behavior tests for bounded Graphiti-native Signal candidate retrieval."""

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from graphiti_core.nodes import EntityNode
from pydantic import ValidationError

from capabilities.event.internal.review import ControlledSignalReviewer
from sematica.analysis.event.contracts import (
    AnchorCandidate,
    DirectSignalDraft,
    EventAnalysisInput,
    EventClass,
    EventClassification,
    VariableCandidate,
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
                    "modality": "FACT",
                    "time": {
                        "occurred_at": None,
                        "announced_at": cls.EVENT_TIME,
                        "effective_at": None,
                        "precision": "DAY",
                    },
                    "jurisdictions": ["中国"],
                    "reason": "国家安全限制",
                    "method": "扩大出口管制范围",
                    "metrics": [],
                },
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

        with patch.object(EntityNode, "get_by_uuids", new=AsyncMock(return_value=[anchor])):
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
            patch.object(EntityNode, "get_by_uuids", new=AsyncMock(return_value=[anchor])),
            self.assertRaisesRegex(ValidationError, "at most 64"),
        ):
            await GraphitiCandidateRetriever(graphiti).retrieve(self._event(), classification)

    async def test_anchor_hints_rank_without_excluding_cross_layer_candidates(self) -> None:
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
            "contextual-chain": EntityNode(
                uuid="contextual-chain",
                name="LLM 临时产业链",
                group_id="neo4j",
                labels=["Entity", "IndustryChain"],
                attributes={},
            ),
            "country-us": EntityNode(
                uuid="country-us",
                name="美国",
                group_id="neo4j",
                labels=["Entity", "Country"],
                attributes={"data_object_id": "COUNTRY-US"},
            ),
            "foreign-chain": EntityNode(
                uuid="foreign-chain",
                name="跨组产业链",
                group_id="another-tenant",
                labels=["Entity", "IndustryChain"],
                attributes={"data_object_id": "ICH-FOREIGN"},
            ),
        }

        async def execute_query(query, **kwargs):
            del kwargs
            if "mentioned_anchor_candidates" in query:
                return ([{"uuid": uuid} for uuid in nodes], None, None)
            return ([], None, None)

        async def get_by_uuids(_driver, uuids, group_id=None):
            self.assertEqual(group_id, "neo4j")
            return [nodes[uuid] for uuid in uuids]

        graphiti.driver.execute_query.side_effect = execute_query
        classification = EventClassification(
            event_class=EventClass.INDUSTRY_CHAIN,
            confidence="HIGH",
            anchor_type_hints=["IndustryChain", "ChainNode"],
            variable_group_hints=["DEMAND"],
            retrieval_queries=["产业链"],
            rationale="产业链事件。",
        )

        with patch.object(EntityNode, "get_by_uuids", new=get_by_uuids):
            candidates = await GraphitiCandidateRetriever(graphiti).retrieve(self._event(), classification)

        type_counts = {
            entity_type: sum(item.entity_type.value == entity_type for item in candidates.anchors)
            for entity_type in ("ChainNode", "IndustryChain")
        }
        self.assertEqual(type_counts, {"ChainNode": 6, "IndustryChain": 3})
        self.assertNotIn("country-us", {item.uuid for item in candidates.anchors})
        self.assertNotIn("foreign-chain", {item.uuid for item in candidates.anchors})
        self.assertNotIn("contextual-chain", {item.uuid for item in candidates.anchors})

        narrowed = classification.model_copy(update={"anchor_type_hints": [classification.anchor_type_hints[0]]})
        with patch.object(EntityNode, "get_by_uuids", new=get_by_uuids):
            narrowed_candidates = await GraphitiCandidateRetriever(graphiti).retrieve(self._event(), narrowed)
        self.assertEqual(
            {item.entity_type.value for item in narrowed_candidates.anchors},
            {"IndustryChain", "ChainNode"},
        )
        self.assertEqual(narrowed_candidates.anchors[0].entity_type.value, "IndustryChain")

    async def test_exact_alias_match_recalls_authoritative_anchor_without_semantic_hit(self) -> None:
        graphiti = MagicMock()
        graphiti.search_ = AsyncMock(return_value=MagicMock(nodes=[]))
        graphiti.driver.execute_query = AsyncMock()
        anchor = EntityNode(
            uuid="node-hbm",
            name="高带宽存储器",
            group_id="neo4j",
            labels=["Entity", "ChainNode"],
            attributes={
                "data_object_id": "CND00000000-0000-4000-8000-000000000002",
                "aliases": ["HBM"],
            },
        )

        async def execute_query(query, **kwargs):
            if "exact_anchor_candidates" in query:
                self.assertIn("高带宽内存", kwargs["terms"])
                self.assertLessEqual(len(kwargs["terms"]), 5)
                return ([{"uuid": anchor.uuid}], None, None)
            return ([], None, None)

        graphiti.driver.execute_query.side_effect = execute_query
        classification = self._classification().model_copy(update={"retrieval_queries": ["HBM"]})
        batch_load = AsyncMock(return_value=[anchor])
        with patch.object(EntityNode, "get_by_uuids", new=batch_load):
            candidates = await GraphitiCandidateRetriever(graphiti).retrieve(self._event(), classification)

        batch_load.assert_awaited_once_with(graphiti.driver, [anchor.uuid], group_id="neo4j")
        self.assertEqual([item.business_id for item in candidates.anchors], [anchor.attributes["data_object_id"]])
        self.assertEqual(candidates.anchors[0].retrieval_sources, ["EXACT"])

    async def test_semantic_cutoff_preserves_highest_reranker_scores(self) -> None:
        graphiti = MagicMock()
        semantic_nodes = [
            EntityNode(
                uuid=f"semantic-{index}",
                name=f"{chr(ord('Z') - index)}-候选节点",
                group_id="neo4j",
                labels=["Entity", "ChainNode"],
                attributes={"data_object_id": f"CND{index:032d}"},
            )
            for index in range(9)
        ]
        scores = [float(index) for index in range(9)]

        async def search(_query, **kwargs):
            if kwargs["search_filter"].node_labels == ["Variable"]:
                return MagicMock(nodes=[], node_reranker_scores=[])
            return MagicMock(nodes=semantic_nodes, node_reranker_scores=scores)

        graphiti.search_ = AsyncMock(side_effect=search)
        graphiti.driver.execute_query = AsyncMock(return_value=([], None, None))

        candidates = await GraphitiCandidateRetriever(graphiti).retrieve(
            self._event(),
            self._classification(),
        )

        returned = [item.uuid for item in candidates.anchors]
        self.assertEqual(len(returned), 8)
        self.assertEqual(returned[0], "semantic-8")
        self.assertNotIn("semantic-0", returned)

    async def test_agent_search_intents_take_priority_over_verbose_event_objects(self) -> None:
        graphiti = MagicMock()
        graphiti.search_ = AsyncMock(return_value=MagicMock(nodes=[], node_reranker_scores=[]))
        exact_terms: list[str] = []

        async def execute_query(query, **kwargs):
            if "exact_anchor_candidates" in query:
                exact_terms.extend(kwargs["terms"])
            return ([], None, None)

        graphiti.driver.execute_query = AsyncMock(side_effect=execute_query)
        base = self._event()
        verbose_event = base.event.event.model_copy(
            update={
                "semantic": base.event.event.semantic.model_copy(
                    update={"objects": [f"宽泛对象{index}" for index in range(8)]}
                )
            }
        )
        event = base.model_copy(update={"event": base.event.model_copy(update={"event": verbose_event})})
        classification = self._classification().model_copy(
            update={"retrieval_queries": ["精确产业链节点", "精确政策主题"]}
        )

        await GraphitiCandidateRetriever(graphiti).retrieve(event, classification)

        self.assertEqual(exact_terms[:2], ["精确产业链节点", "精确政策主题"])
        self.assertLessEqual(len(exact_terms), 5)

    async def test_labeled_anchor_recall_at_10_20_and_30(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "event_anchor_recall.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        graphiti = MagicMock()
        batches = {
            batch["query"]: (
                [
                    EntityNode(
                        uuid=item["uuid"],
                        name=item["name"],
                        group_id="neo4j",
                        labels=["Entity", item["entity_type"]],
                        attributes={"data_object_id": item["business_id"]},
                    )
                    for item in batch["candidates"]
                ],
                [float(item["score"]) for item in batch["candidates"]],
            )
            for batch in fixture["search_batches"]
        }

        async def search(query, **kwargs):
            if kwargs["search_filter"].node_labels == ["Variable"]:
                return MagicMock(nodes=[], node_reranker_scores=[])
            nodes, scores = batches[query]
            return MagicMock(nodes=nodes, node_reranker_scores=scores)

        graphiti.search_ = AsyncMock(side_effect=search)
        graphiti.driver.execute_query = AsyncMock(return_value=([], None, None))
        classification = EventClassification(
            event_class=EventClass.GEOPOLITICAL,
            confidence="HIGH",
            anchor_type_hints=["Country", "MacroEconomic", "IndustryChain", "ChainNode"],
            variable_group_hints=["GEOPOLITICAL"],
            retrieval_queries=fixture["retrieval_queries"],
            rationale="出口管制事件需在地缘、宏观与产业层召回既有锚点。",
        )

        candidates = await GraphitiCandidateRetriever(graphiti).retrieve(self._event(), classification)

        ranked_ids = [item.business_id for item in candidates.anchors]
        expected = set(fixture["expected_business_ids"])
        recalls = {cutoff: len(expected.intersection(ranked_ids[:cutoff])) / len(expected) for cutoff in (10, 20, 30)}
        self.assertEqual(recalls, {10: 0.5, 20: 0.75, 30: 1.0})
        self.assertEqual(len(ranked_ids), 30)

    async def test_candidate_exposes_all_retrieval_sources_in_strength_order(self) -> None:
        graphiti = MagicMock()
        anchor = EntityNode(
            uuid="node-hbm",
            name="HBM",
            group_id="neo4j",
            labels=["Entity", "ChainNode"],
            attributes={"data_object_id": "CND00000000-0000-4000-8000-000000000002"},
        )
        graphiti.search_ = AsyncMock(return_value=MagicMock(nodes=[anchor]))

        async def execute_query(query, **kwargs):
            del kwargs
            if "exact_anchor_candidates" in query or "mentioned_anchor_candidates" in query:
                return ([{"uuid": anchor.uuid}], None, None)
            return ([], None, None)

        graphiti.driver.execute_query = AsyncMock(side_effect=execute_query)
        with patch.object(EntityNode, "get_by_uuids", new=AsyncMock(return_value=[anchor])):
            candidates = await GraphitiCandidateRetriever(graphiti).retrieve(
                self._event(),
                self._classification(),
            )

        self.assertEqual(candidates.anchors[0].retrieval_sources, ["EXACT", "MENTION", "SEMANTIC"])

    async def test_variable_semantic_ranking_runs_once_for_multiple_anchor_search_intents(self) -> None:
        graphiti = MagicMock()
        graphiti.search_ = AsyncMock(return_value=MagicMock(nodes=[]))
        graphiti.driver.execute_query = AsyncMock(return_value=([], None, None))
        classification = self._classification().model_copy(
            update={"retrieval_queries": ["HBM", "高带宽存储器", "AI加速卡"]}
        )

        await GraphitiCandidateRetriever(graphiti).retrieve(self._event(), classification)

        variable_searches = [
            call
            for call in graphiti.search_.await_args_list
            if call.kwargs["search_filter"].node_labels == ["Variable"]
        ]
        self.assertEqual(len(variable_searches), 1)

    async def test_semantic_variable_recall_is_not_erased_by_incomplete_group_hints(self) -> None:
        graphiti = MagicMock()
        semantic_variable = EntityNode(
            uuid="variable-demand",
            name="市场需求",
            group_id="neo4j",
            labels=["Entity", "Variable"],
            attributes={
                "variable_id": "market_demand",
                "variable_group": "DEMAND",
                "variable_role": "FUNDAMENTAL",
                "allowed_anchor_types": ["ChainNode"],
                "definition": "直接需求。",
            },
        )

        async def search(_query, **kwargs):
            if kwargs["search_filter"].node_labels == ["Variable"]:
                return MagicMock(nodes=[semantic_variable])
            return MagicMock(nodes=[])

        graphiti.search_ = AsyncMock(side_effect=search)
        graphiti.driver.execute_query = AsyncMock(return_value=([], None, None))
        classification = EventClassification.model_validate(
            {
                **self._classification().model_dump(),
                "variable_group_hints": ["COMPANY_FINANCIAL"],
            }
        )

        candidates = await GraphitiCandidateRetriever(graphiti).retrieve(self._event(), classification)

        self.assertIn("market_demand", [item.variable_id for item in candidates.variables])

    async def test_company_event_can_recall_existing_chain_anchors(self) -> None:
        graphiti = MagicMock()
        graphiti.search_ = AsyncMock(return_value=MagicMock(nodes=[]))
        graphiti.driver.execute_query = AsyncMock()
        anchors = {
            "node-server": EntityNode(
                uuid="node-server",
                name="AI服务器",
                group_id="neo4j",
                labels=["Entity", "ChainNode"],
                attributes={"data_object_id": "CND00000000-0000-4000-8000-000000000001"},
            ),
            "chain-compute": EntityNode(
                uuid="chain-compute",
                name="AI计算产业链",
                group_id="neo4j",
                labels=["Entity", "IndustryChain"],
                attributes={"data_object_id": "ICH00000000-0000-4000-8000-000000000001"},
            ),
        }

        async def execute_query(query, **kwargs):
            del kwargs
            if "mentioned_anchor_candidates" in query:
                return ([{"uuid": uuid} for uuid in anchors], None, None)
            return ([], None, None)

        async def get_by_uuids(_driver, uuids, group_id=None):
            self.assertEqual(group_id, "neo4j")
            return [anchors[uuid] for uuid in uuids]

        graphiti.driver.execute_query.side_effect = execute_query
        classification = EventClassification(
            event_class=EventClass.COMPANY,
            confidence="HIGH",
            anchor_type_hints=["ChainNode"],
            variable_group_hints=["DEMAND"],
            retrieval_queries=["AI服务器"],
            rationale="公司订单直接影响AI服务器节点与产业链。",
        )

        with patch.object(EntityNode, "get_by_uuids", new=get_by_uuids):
            candidates = await GraphitiCandidateRetriever(graphiti).retrieve(self._event(), classification)

        self.assertEqual(
            {item.entity_type.value for item in candidates.anchors},
            {"IndustryChain", "ChainNode"},
        )


class ControlledSignalReviewerTest(unittest.IsolatedAsyncioTestCase):
    async def test_company_event_direct_chain_node_signal_is_not_categorically_rejected(self) -> None:
        event = GraphitiCandidateRetrieverTest._event()
        classification = EventClassification(
            event_class="COMPANY",
            confidence="HIGH",
            anchor_type_hints=["ChainNode"],
            variable_group_hints=["DEMAND"],
            retrieval_queries=["AI服务器"],
            rationale="公司订单直接影响AI服务器需求。",
        )
        anchor = AnchorCandidate(
            uuid="anchor-server",
            name="AI服务器",
            entity_type="ChainNode",
            business_id="CND00000000-0000-4000-8000-000000000001",
        )
        variable = VariableCandidate(
            uuid="variable-demand",
            variable_id="market_demand",
            name="市场需求",
            variable_group="DEMAND",
            allowed_anchor_types=["ChainNode"],
            definition="特定产业链节点面向客户的真实需求。",
        )
        proposal = DirectSignalDraft(
            anchor_uuid=anchor.uuid,
            variable_uuid=variable.uuid,
            fact="新增订单提高AI服务器市场需求。",
            direction="UP",
            magnitude="MEDIUM",
            impact_onset_days=0,
            impact_peak_days=30,
            expected_duration_days=90,
            mechanism="公司披露的新增订单直接增加AI服务器需求。",
            duration_basis="订单履行周期。",
            assumptions=[],
            invalidation_conditions=["订单取消"],
            provenance_confidence="HIGH",
            mechanism_confidence="HIGH",
            temporal_confidence="MEDIUM",
        ).proposal(
            event_time=GraphitiCandidateRetrieverTest.EVENT_TIME,
            reference_time=event.reference_time,
            assertion_modality="ACTUAL",
        )

        accepted = await ControlledSignalReviewer().review(
            event,
            classification,
            proposal,
            variable,
            anchor,
        )

        self.assertTrue(accepted)


class DirectSignalTemporalSemanticsTest(unittest.TestCase):
    def test_signal_is_usable_at_analysis_time_while_impact_window_tracks_event_time(self) -> None:
        event_time = datetime(2026, 8, 29, tzinfo=UTC)
        reference_time = datetime(2026, 8, 31, 8, tzinfo=UTC)
        proposal = DirectSignalDraft(
            anchor_uuid="anchor-server",
            variable_uuid="variable-demand",
            fact="订单增加预计将在未来提高服务器需求。",
            direction="UP",
            magnitude="MEDIUM",
            impact_onset_days=30,
            impact_peak_days=90,
            expected_duration_days=180,
            mechanism="订单履约直接增加服务器采购需求。",
            duration_basis="订单履约周期。",
            assumptions=["订单正常履约"],
            invalidation_conditions=["订单取消"],
            provenance_confidence="HIGH",
            mechanism_confidence="HIGH",
            temporal_confidence="MEDIUM",
        ).proposal(
            event_time=event_time,
            reference_time=reference_time,
            assertion_modality="ANTICIPATED",
        )

        self.assertEqual(proposal.valid_at, reference_time)
        self.assertEqual(proposal.impact_onset_earliest, event_time + timedelta(days=30))
        self.assertEqual(proposal.impact_peak_earliest, event_time + timedelta(days=90))
        self.assertEqual(proposal.expected_end_earliest, event_time + timedelta(days=210))


if __name__ == "__main__":
    unittest.main()
