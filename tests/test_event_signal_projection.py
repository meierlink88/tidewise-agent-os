"""Graphiti public-ingestion behavior tests for reviewed Event Signals."""

import json
import unittest
from datetime import UTC, datetime, timedelta

from graphiti_core.edges import EntityEdge
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.graphiti import AddTripletResults
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode

from sematica.analysis.event.contracts import (
    AnchorCandidate,
    EventAnalysisInput,
    EventClassification,
    SignalProposal,
    VariableCandidate,
)
from sematica.analysis.event.errors import PermanentEventAnalysisFailure
from sematica.analysis.event.graphiti import GraphitiSignalFactProjector
from sematica.ingestion.episcode.event.contracts import EventCandidateDTO, HistoricalEvent
from sematica.ingestion.episcode.event.provenance import (
    EVENT_SOURCE_DESCRIPTION,
    event_episode_uuid,
)
from sematica.projection.runtime import GRAPHITI_GROUP_ID


class _GraphState:
    def __init__(self) -> None:
        self.nodes: dict[str, EntityNode] = {}
        self.episodes: dict[str, EpisodicNode] = {}
        self.event_ids: dict[str, str] = {}
        self.facts: dict[str, EntityEdge] = {}


class _GraphOperations:
    """Public Graphiti persistence seam backed by one in-memory graph state."""

    def __init__(self, state: _GraphState) -> None:
        self.state = state

    async def node_get_by_uuid(self, _cls, _driver, uuid: str) -> EntityNode:
        try:
            return self.state.nodes[uuid].model_copy(deep=True)
        except KeyError as exc:
            raise NodeNotFoundError(uuid) from exc

    async def episodic_node_get_by_uuid(self, _cls, _driver, uuid: str) -> EpisodicNode:
        try:
            return self.state.episodes[uuid].model_copy(deep=True)
        except KeyError as exc:
            raise NodeNotFoundError(uuid) from exc

    async def edge_save(self, edge: EntityEdge, _driver) -> None:
        self.state.facts[edge.uuid] = edge.model_copy(deep=True)


class _StatefulDriver:
    _database = "neo4j"

    def __init__(self, state: _GraphState) -> None:
        self.state = state
        self.graph_operations_interface = _GraphOperations(state)

    async def execute_query(self, query: str, **parameters):
        if "signal_fact_require_formal_event_episode" in query:
            uuid = parameters["episode_uuid"]
            episode = self.state.episodes.get(uuid)
            if (
                episode is None
                or episode.group_id != parameters["group_id"]
                or episode.source_description != parameters["source_description"]
                or self.state.event_ids.get(uuid) != parameters["event_id"]
            ):
                return [], None, None
            return [{"uuid": uuid}], None, None
        if "signal_fact_existing_event_provenance" in query:
            records = [
                {"uuid": uuid, "content": self.state.episodes[uuid].content}
                for uuid in parameters["episode_uuids"]
                if uuid in self.state.episodes
                and self.state.episodes[uuid].group_id == parameters["group_id"]
                and self.state.episodes[uuid].source_description == parameters["source_description"]
            ]
            return records, None, None
        if "signal_fact_link_event_episode" in query:
            uuid = parameters["episode_uuid"]
            episode = self.state.episodes.get(uuid)
            if (
                episode is None
                or episode.group_id != parameters["group_id"]
                or episode.source_description != parameters["source_description"]
                or self.state.event_ids.get(uuid) != parameters["event_id"]
            ):
                return [], None, None
            if parameters["fact_uuid"] not in episode.entity_edges:
                episode.entity_edges.append(parameters["fact_uuid"])
            return (
                [
                    {
                        "uuid": uuid,
                        "episode_kind": "EVENT",
                        "domain_object_id": self.state.event_ids[uuid],
                        "entity_edges": list(episode.entity_edges),
                    }
                ],
                None,
                None,
            )
        raise AssertionError("unexpected Signal projection query")


class _StatefulGraphiti:
    """Native add_triplet seam with controllable resolution and ACK behavior."""

    def __init__(
        self,
        driver: _StatefulDriver,
        *,
        resolved_uuid: str | None = None,
        resolved_source_uuid: str | None = None,
        resolved_target_uuid: str | None = None,
        lose_first_ack: bool = False,
    ) -> None:
        self.driver = driver
        self.resolved_uuid = resolved_uuid
        self.resolved_source_uuid = resolved_source_uuid
        self.resolved_target_uuid = resolved_target_uuid
        self.lose_first_ack = lose_first_ack
        self.add_triplet_calls = 0
        self.triplets: list[tuple[EntityNode, EntityEdge, EntityNode]] = []

    async def add_triplet(
        self,
        source: EntityNode,
        edge: EntityEdge,
        target: EntityNode,
    ) -> AddTripletResults:
        self.add_triplet_calls += 1
        self.triplets.append((source.model_copy(deep=True), edge.model_copy(deep=True), target.model_copy(deep=True)))
        resolved = edge.model_copy(
            deep=True,
            update={
                "uuid": self.resolved_uuid or edge.uuid,
                "source_node_uuid": self.resolved_source_uuid or edge.source_node_uuid,
                "target_node_uuid": self.resolved_target_uuid or edge.target_node_uuid,
            },
        )
        persisted = self.driver.state.facts.setdefault(resolved.uuid, resolved)
        if self.lose_first_ack and self.add_triplet_calls == 1:
            raise ConnectionError("acknowledgement lost after native add_triplet write")
        return AddTripletResults(
            nodes=[source, target],
            edges=[persisted.model_copy(deep=True)],
        )


class GraphitiSignalFactProjectionTest(unittest.IsolatedAsyncioTestCase):
    EVENT_ID = "EVT15bec7e3-998c-4434-aa5d-29712c4c67cf"
    EVENT_TIME = datetime(2026, 8, 29, tzinfo=UTC)
    VARIABLE_UUID = "variable-supply"
    ANCHOR_UUID = "anchor-hbm"

    @classmethod
    def event(cls) -> EventAnalysisInput:
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
            event=HistoricalEvent(id=cls.EVENT_ID, event=candidate),
            episode_uuid=event_episode_uuid(cls.EVENT_ID),
            reference_time=cls.EVENT_TIME,
        )

    @staticmethod
    def classification() -> EventClassification:
        return EventClassification(
            event_class="CHAIN_NODE",
            confidence="HIGH",
            anchor_type_hints=["ChainNode"],
            variable_group_hints=["SUPPLY_CAPACITY"],
            retrieval_queries=["高带宽内存 出口限制"],
            rationale="事件直接发生在产业链节点。",
        )

    @classmethod
    def variable(cls) -> VariableCandidate:
        return VariableCandidate(
            uuid=cls.VARIABLE_UUID,
            variable_id="effective_supply",
            name="有效供给",
            variable_group="SUPPLY_CAPACITY",
            allowed_anchor_types=["ChainNode"],
            definition="可向目标市场实际交付的供给能力。",
        )

    @classmethod
    def anchor(cls) -> AnchorCandidate:
        return AnchorCandidate(
            uuid=cls.ANCHOR_UUID,
            name="高带宽内存",
            entity_type="ChainNode",
            business_id="node-hbm",
        )

    @classmethod
    def proposal(cls) -> SignalProposal:
        return SignalProposal(
            anchor_uuid=cls.ANCHOR_UUID,
            variable_uuid=cls.VARIABLE_UUID,
            fact="出口限制令高带宽内存有效供给下降。",
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

    @classmethod
    def graph(cls, **graphiti_options) -> tuple[_GraphState, _StatefulGraphiti]:
        state = _GraphState()
        state.nodes[cls.VARIABLE_UUID] = EntityNode(
            uuid=cls.VARIABLE_UUID,
            name="有效供给",
            group_id=GRAPHITI_GROUP_ID,
            labels=["Entity", "Variable"],
            attributes={"variable_id": "effective_supply", "variable_role": "FUNDAMENTAL"},
        )
        state.nodes[cls.ANCHOR_UUID] = EntityNode(
            uuid=cls.ANCHOR_UUID,
            name="高带宽内存",
            group_id=GRAPHITI_GROUP_ID,
            labels=["Entity", "ChainNode"],
            attributes={"data_object_id": "node-hbm"},
        )
        episode_uuid = event_episode_uuid(cls.EVENT_ID)
        state.episodes[episode_uuid] = EpisodicNode(
            uuid=episode_uuid,
            name="美国扩大高带宽内存出口限制",
            group_id=GRAPHITI_GROUP_ID,
            labels=[],
            source=EpisodeType.json,
            source_description=EVENT_SOURCE_DESCRIPTION,
            content=json.dumps({"id": cls.EVENT_ID}),
            valid_at=cls.EVENT_TIME,
            entity_edges=[],
        )
        state.event_ids[episode_uuid] = cls.EVENT_ID
        return state, _StatefulGraphiti(_StatefulDriver(state), **graphiti_options)

    async def test_native_dedup_reuses_canonical_fact_and_merges_event_provenance(self) -> None:
        canonical_fact_uuid = "graphiti-canonical-fact-uuid"
        previous_event_id = "EVT25bec7e3-998c-4434-aa5d-29712c4c67cf"
        previous_episode_uuid = event_episode_uuid(previous_event_id)
        state, graphiti = self.graph(resolved_uuid=canonical_fact_uuid)
        state.episodes[previous_episode_uuid] = EpisodicNode(
            uuid=previous_episode_uuid,
            name="既有高带宽内存出口限制",
            group_id=GRAPHITI_GROUP_ID,
            labels=[],
            source=EpisodeType.json,
            source_description=EVENT_SOURCE_DESCRIPTION,
            content=json.dumps({"id": previous_event_id}),
            valid_at=self.EVENT_TIME - timedelta(days=1),
            entity_edges=[canonical_fact_uuid],
        )
        state.event_ids[previous_episode_uuid] = previous_event_id
        state.facts[canonical_fact_uuid] = EntityEdge(
            uuid=canonical_fact_uuid,
            group_id=GRAPHITI_GROUP_ID,
            source_node_uuid=self.VARIABLE_UUID,
            target_node_uuid=self.ANCHOR_UUID,
            created_at=self.EVENT_TIME - timedelta(days=1),
            name="REDUCES",
            fact=self.proposal().fact,
            episodes=[previous_episode_uuid],
            valid_at=self.EVENT_TIME - timedelta(days=1),
            reference_time=self.EVENT_TIME - timedelta(days=1),
            attributes={"source_event_ids": [previous_event_id]},
        )

        fact_uuid = await GraphitiSignalFactProjector(graphiti).project(  # type: ignore[arg-type]
            self.event(),
            self.classification(),
            self.variable(),
            self.anchor(),
            self.proposal(),
        )

        self.assertEqual(fact_uuid, canonical_fact_uuid)
        submitted = graphiti.triplets[0][1]
        self.assertNotEqual(submitted.uuid, canonical_fact_uuid)
        stored = state.facts[canonical_fact_uuid]
        self.assertEqual(stored.name, "SIGNAL_ON")
        self.assertEqual(
            stored.attributes["source_event_ids"],
            sorted([previous_event_id, self.EVENT_ID]),
        )
        self.assertEqual(set(stored.episodes), {previous_episode_uuid, self.event().episode_uuid})
        self.assertEqual(state.episodes[previous_episode_uuid].entity_edges, [canonical_fact_uuid])
        self.assertEqual(state.episodes[self.event().episode_uuid].entity_edges, [canonical_fact_uuid])

    async def test_native_add_triplet_preserves_controlled_signal_and_event_provenance(self) -> None:
        state, graphiti = self.graph()

        fact_uuid = await GraphitiSignalFactProjector(graphiti).project(  # type: ignore[arg-type]
            self.event(),
            self.classification(),
            self.variable(),
            self.anchor(),
            self.proposal(),
        )

        self.assertEqual(fact_uuid, "2b70b159-fe8a-5031-92fd-40f3c5634062")
        self.assertEqual(graphiti.add_triplet_calls, 1)
        self.assertEqual(set(state.nodes), {self.VARIABLE_UUID, self.ANCHOR_UUID})
        self.assertEqual(set(state.episodes), {self.event().episode_uuid})
        self.assertEqual(set(state.facts), {fact_uuid})

        source, submitted, target = graphiti.triplets[0]
        self.assertEqual((source.uuid, target.uuid), (self.VARIABLE_UUID, self.ANCHOR_UUID))
        self.assertEqual(submitted.uuid, fact_uuid)
        self.assertEqual(submitted.name, "SIGNAL_ON")
        self.assertEqual(submitted.episodes, [self.event().episode_uuid])

        stored = state.facts[fact_uuid]
        self.assertEqual((stored.source_node_uuid, stored.target_node_uuid), (self.VARIABLE_UUID, self.ANCHOR_UUID))
        self.assertEqual(stored.episodes, [self.event().episode_uuid])
        self.assertEqual(
            stored.attributes,
            {
                "source_event_ids": [self.EVENT_ID],
                "event_class": "CHAIN_NODE",
                "variable_id": "effective_supply",
                "anchor_type": "ChainNode",
                "anchor_business_id": "node-hbm",
                "direction": "DOWN",
                "magnitude": "HIGH",
                "derivation_type": "DERIVED",
                "assertion_modality": "ACTUAL",
                "review_status": "REVIEWED",
                "impact_onset_earliest": "2026-08-29T00:00:00Z",
                "impact_onset_latest": "2026-08-29T00:00:00Z",
                "impact_peak_earliest": "2026-09-05T00:00:00Z",
                "impact_peak_latest": "2026-09-05T00:00:00Z",
                "expected_end_earliest": "2026-11-27T00:00:00Z",
                "expected_end_latest": "2026-11-27T00:00:00Z",
                "horizon_tags": ["SHORT"],
                "mechanism": "出口限制减少可交付供给。",
                "duration_basis": "政策约束持续期间。",
                "assumptions": [],
                "invalidation_conditions": ["限制撤销"],
                "provenance_confidence": "HIGH",
                "mechanism_confidence": "HIGH",
                "temporal_confidence": "MEDIUM",
                "methodology_version": "event-analysis/v1",
            },
        )
        self.assertEqual(state.episodes[self.event().episode_uuid].entity_edges, [fact_uuid])

    async def test_retry_after_lost_add_triplet_ack_does_not_create_a_second_fact(self) -> None:
        state, graphiti = self.graph(lose_first_ack=True)
        projector = GraphitiSignalFactProjector(graphiti)  # type: ignore[arg-type]

        with self.assertRaisesRegex(ConnectionError, "acknowledgement lost"):
            await projector.project(
                self.event(),
                self.classification(),
                self.variable(),
                self.anchor(),
                self.proposal(),
            )
        self.assertEqual(set(state.facts), {"2b70b159-fe8a-5031-92fd-40f3c5634062"})
        self.assertEqual(state.episodes[self.event().episode_uuid].entity_edges, [])

        fact_uuid = await projector.project(
            self.event(),
            self.classification(),
            self.variable(),
            self.anchor(),
            self.proposal(),
        )

        self.assertEqual(fact_uuid, "2b70b159-fe8a-5031-92fd-40f3c5634062")
        self.assertEqual(graphiti.add_triplet_calls, 2)
        self.assertEqual(set(state.facts), {fact_uuid})
        self.assertEqual(state.facts[fact_uuid].attributes["source_event_ids"], [self.EVENT_ID])
        self.assertEqual(state.episodes[self.event().episode_uuid].entity_edges, [fact_uuid])

    async def test_requires_existing_variable_anchor_and_formal_event_episode(self) -> None:
        missing_identities = (
            ("Variable", "nodes", self.VARIABLE_UUID),
            ("Anchor", "nodes", self.ANCHOR_UUID),
            ("Event Episode", "episodes", self.event().episode_uuid),
        )
        for identity, collection_name, uuid in missing_identities:
            with self.subTest(identity=identity):
                state, graphiti = self.graph()
                del getattr(state, collection_name)[uuid]

                with self.assertRaisesRegex(PermanentEventAnalysisFailure, "existing graph identity"):
                    await GraphitiSignalFactProjector(graphiti).project(  # type: ignore[arg-type]
                        self.event(),
                        self.classification(),
                        self.variable(),
                        self.anchor(),
                        self.proposal(),
                    )

                self.assertEqual(graphiti.add_triplet_calls, 0)
                self.assertEqual(state.facts, {})

    async def test_missing_formal_episode_metadata_is_rejected_before_add_triplet(self) -> None:
        state, graphiti = self.graph()
        del state.event_ids[self.event().episode_uuid]

        with self.assertRaisesRegex(PermanentEventAnalysisFailure, "formal Event Episode metadata"):
            await GraphitiSignalFactProjector(graphiti).project(  # type: ignore[arg-type]
                self.event(),
                self.classification(),
                self.variable(),
                self.anchor(),
                self.proposal(),
            )

        self.assertEqual(graphiti.add_triplet_calls, 0)
        self.assertEqual(state.facts, {})
        self.assertEqual(state.episodes[self.event().episode_uuid].entity_edges, [])

    async def test_rejects_graphiti_resolved_fact_with_different_endpoints(self) -> None:
        mismatches = (
            {"resolved_source_uuid": "different-variable"},
            {"resolved_target_uuid": "different-anchor"},
        )
        for graphiti_options in mismatches:
            with self.subTest(**graphiti_options):
                state, graphiti = self.graph(**graphiti_options)

                with self.assertRaisesRegex(PermanentEventAnalysisFailure, "unexpected endpoints"):
                    await GraphitiSignalFactProjector(graphiti).project(  # type: ignore[arg-type]
                        self.event(),
                        self.classification(),
                        self.variable(),
                        self.anchor(),
                        self.proposal(),
                    )

                self.assertEqual(state.episodes[self.event().episode_uuid].entity_edges, [])


if __name__ == "__main__":
    unittest.main()
