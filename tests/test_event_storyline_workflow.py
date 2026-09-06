"""Four semantic Steps exercised through real Agno Loops/Conditions and durable journals."""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from agno.agent import Agent
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from pydantic import ValidationError

from agents.event_association import build_event_association_agent
from app.workflow_runtime import install_raw_collection_session_compatibility
from capabilities.event import (
    AssociationDecision,
    IdentityClassificationDecision,
    SignalDecision,
    configure_event_workflow_runtime,
)
from capabilities.event.functions import enqueue_evidence_artifact
from capabilities.event.functions.storyline import _pages
from capabilities.event.internal.storyline_execution import StorylineOperationError
from capabilities.event.internal.storyline_models import AssociationProfile
from sematica.analysis.event.graphiti.storylines import GraphitiStorylineCatalog
from sematica.ingestion.episcode.event.stages.selected_episode import SelectedEventEpisodeStage
from tests import test_event_extraction as legacy
from tests.test_event_extraction import FakeEventWorkflowRuntime
from workflows.event_extraction import _seed_workflow

PINS = {"event-extractor": 11, "event-identity": 13, "event-association": 2, "event-signal-analyst": 17}


class Runtime(FakeEventWorkflowRuntime):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.catalog = [
            {
                "uuid": "chain-server",
                "business_id": "ICH-example",
                "name": "服务器",
                "entity_type": "IndustryChain",
                "profile": {},
            }
        ]
        self.nodes = [
            {
                "uuid": "anchor-server",
                "business_id": "CND-example",
                "name": "服务器",
                "entity_type": "ChainNode",
                "profile": {"definition": "服务器供给"},
            }
        ]
        self.fail_graph_once = False
        self.fail_node_catalog_once = False

    async def invoke_agent(self, *args, **kwargs):
        raise AssertionError("Functions must never invoke a semantic Agent")

    async def storyline_profiles(self, labels, *, terms=None, chain_uuids=None):
        self.calls.append(("catalog", labels, terms, chain_uuids))
        if chain_uuids is not None:
            if self.fail_node_catalog_once:
                self.fail_node_catalog_once = False
                raise ConnectionError("node catalog temporarily unavailable")
            return self.nodes
        return [] if terms is not None else self.catalog

    async def storyline_variables(self):
        return legacy.EventExtractionWorkflowTest.candidates().variables

    async def publish(self, *args, associations, **kwargs):
        self.calls.append(("publish", associations))
        result = await super().publish(*args, **kwargs)
        if self.fail_graph_once:
            self.fail_graph_once = False
            raise ConnectionError("graph unavailable after Data ACK")
        return result


class StorylineWorkflowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = legacy.EventExtractionWorkflowTest(methodName="runTest")
        self.fixture.setUp()
        self.runtime = Runtime()
        configure_event_workflow_runtime(self.runtime)
        self.agents = {key: Agent(id=key, name=key, instructions="test") for key in PINS}
        self.calls = []
        self.identity_override = None
        self.association_override = None
        self.signal_override = None
        install_raw_collection_session_compatibility()

    def tearDown(self):
        self.fixture.tearDown()

    async def respond(self, agent, *args, **kwargs):
        payload = kwargs.get("input", args[0] if args else None)
        # Real Agno direct Steps send serialized predecessor content, not private runtime invocations.
        if isinstance(payload, str):
            payload = json.loads(payload)
        self.calls.append((agent.id, payload))
        if agent.id == "event-extractor":
            content = legacy.EventExtractionWorkflowTest.extraction_draft()
        elif agent.id == "event-identity":
            classification = legacy.EventExtractionWorkflowTest.classification().model_copy(
                update={"event_class": "INDUSTRY_CHAIN"}
            )
            content = self.identity_override or IdentityClassificationDecision(
                decision="NEW_EVENT",
                atomic=True,
                matched_event_ids=[],
                reason_codes=["NEW"],
                summary="new",
                classification=classification,
            )
        elif agent.id == "event-association":
            content = self.association_override or AssociationDecision(
                matches=[{"uuid": p["uuid"], "reason": "explicit event object"} for p in payload["candidates"]]
            )
        else:
            content = self.signal_override or SignalDecision(
                proposals=[legacy.EventExtractionWorkflowTest.signal_draft()]
            )
        return RunOutput(agent_id=agent.id, content=content, status=RunStatus.completed)

    def workflow(self):
        flow = _seed_workflow(*self.agents.values(), agent_versions=PINS)
        flow.db = None
        return flow

    async def run_flow(self, flow=None):
        flow = flow or self.workflow()

        async def response(agent, *args, **kwargs):
            return await self.respond(agent, *args, **kwargs)

        with (
            patch.object(Agent, "arun", new=response),
            patch("capabilities.event.internal.queue.read_resolved_evidences", return_value=self.fixture.evidences()),
        ):
            return await flow.arun(input="run", stream=False)

    def enqueue(self):
        with patch("capabilities.event.internal.queue.read_resolved_evidences", return_value=self.fixture.evidences()):
            enqueue_evidence_artifact(str(self.fixture.evidence_manifest), [e.id for e in self.fixture.evidences()])

    async def test_visible_semantic_order_and_publication(self):
        self.enqueue()
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed)
        self.assertEqual(
            [key for key, _ in self.calls],
            ["event-extractor", "event-identity", "event-association", "event-association", "event-signal-analyst"],
        )
        self.assertEqual(self.runtime.data_publications, 1)
        self.assertEqual(self.runtime.signal_projections, 1)
        publications = [call for call in self.runtime.calls if call[0] == "publish"]
        self.assertEqual({p["uuid"] for p in publications[0][1]}, {"chain-server", "anchor-server"})

    async def test_renamed_steps_do_not_change_execution(self):
        self.enqueue()
        flow = self.workflow()
        legacy.EventExtractionWorkflowTest.rename_every_display_name(flow.steps)
        result = await self.run_flow(flow)
        self.assertEqual(result.status, RunStatus.completed)
        self.assertEqual(self.runtime.data_publications, 1)

    async def test_unknown_association_id_blocks_publication(self):
        self.enqueue()
        self.association_override = AssociationDecision(matches=[{"uuid": "invented", "reason": "guess"}])
        with self.assertRaisesRegex(StorylineOperationError, "freeze_association_page"):
            await self.run_flow()
        self.assertEqual(self.runtime.data_publications, 0)

    async def test_unrelated_entity_type_cannot_enter_primary_catalog(self):
        self.enqueue()
        self.runtime.catalog[0]["entity_type"] = "Company"
        with self.assertRaisesRegex(StorylineOperationError, "prepare_storyline_catalog"):
            await self.run_flow()
        self.assertEqual(self.runtime.data_publications, 0)

    async def test_explicit_zero_signal_outcome_still_publishes_event(self):
        self.enqueue()
        self.signal_override = SignalDecision(proposals=[], no_signal_reason="no directly supported change")
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed)
        self.assertEqual(self.runtime.data_publications, 1)
        self.assertEqual(self.runtime.signal_projections, 0)

    async def test_unknown_signal_id_blocks_publication(self):
        self.enqueue()
        self.signal_override = SignalDecision(
            proposals=[legacy.EventExtractionWorkflowTest.signal_draft().model_copy(update={"anchor_uuid": "invented"})]
        )
        with self.assertRaisesRegex(StorylineOperationError, "freeze_storyline_signal_page"):
            await self.run_flow()
        self.assertEqual(self.runtime.data_publications, 0)

    async def test_no_match_and_no_signal_are_valid(self):
        self.enqueue()
        self.association_override = AssociationDecision(matches=[], no_match_reason="not directly related")
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed)
        self.assertEqual(self.runtime.data_publications, 1)
        self.assertEqual(self.runtime.signal_projections, 0)
        self.assertNotIn("event-signal-analyst", [key for key, _ in self.calls])

    async def test_duplicate_has_no_catalog_or_write(self):
        self.enqueue()
        historical = legacy.EventExtractionWorkflowTest.historical_event()
        self.runtime.history = [historical]
        self.identity_override = IdentityClassificationDecision(
            decision="SAME_EVENT", atomic=True, matched_event_ids=[historical.id], reason_codes=["SAME"], summary="same"
        )
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed)
        self.assertEqual(self.runtime.calls, [])

    async def test_graph_failure_resumes_without_repeating_semantic_agents_or_data_post(self):
        self.enqueue()
        self.runtime.fail_graph_once = True
        with self.assertRaises(StorylineOperationError):
            await self.run_flow()
        semantic_calls = len(self.calls)
        self.assertEqual(self.runtime.data_publications, 1)
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed)
        self.assertEqual(len(self.calls), semantic_calls)
        self.assertEqual(self.runtime.data_publication_requests, 1)
        self.assertEqual(self.runtime.signal_projections, 1)

    async def test_node_catalog_failure_keeps_last_chain_association_decision(self):
        self.enqueue()
        self.runtime.fail_node_catalog_once = True
        with self.assertRaises(StorylineOperationError):
            await self.run_flow()
        self.assertEqual([key for key, _ in self.calls].count("event-association"), 1)
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed)
        self.assertEqual([key for key, _ in self.calls].count("event-association"), 2)
        self.assertEqual([key for key, _ in self.calls].count("event-identity"), 1)
        self.assertEqual(self.runtime.data_publications, 1)

    async def test_changed_agent_pins_cannot_resume_partial_batch(self):
        self.enqueue()
        self.runtime.fail_graph_once = True
        with self.assertRaises(StorylineOperationError):
            await self.run_flow()
        flow = self.workflow()
        flow.metadata["event_agent_versions"] = {**PINS, "event-identity": 99}
        with self.assertRaisesRegex(StorylineOperationError, "prepare_storyline_batch"):
            await self.run_flow(flow)

    async def test_legacy_pending_batch_fails_closed(self):
        from capabilities.event.internal.storage import claim_event_batch, freeze_draft, release_event_batch_lease

        self.enqueue()
        with patch("capabilities.event.internal.queue.read_resolved_evidences", return_value=self.fixture.evidences()):
            batch = claim_event_batch()
        freeze_draft(batch, self.fixture.extraction_draft())
        release_event_batch_lease(batch)
        with self.assertRaisesRegex(StorylineOperationError, "prepare_storyline_batch"):
            await self.run_flow()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runtime.data_publications, 0)

    def test_four_class_contract_and_no_match_schema(self):
        schema = IdentityClassificationDecision.model_json_schema()
        self.assertEqual(
            set(schema["$defs"]["StorylineEventClassification"]["properties"]["event_class"]["enum"]),
            {"GEOPOLITICAL", "MACRO_ECONOMIC", "INDUSTRY_CHAIN", "COMPANY"},
        )
        with self.assertRaises(ValidationError):
            IdentityClassificationDecision(
                decision="NEW_EVENT", atomic=True, matched_event_ids=[], reason_codes=["NEW"], summary="new"
            )
        with self.assertRaises(ValidationError):
            AssociationDecision(matches=[])

    def test_complete_catalog_pagination(self):
        rows = [
            AssociationProfile(
                uuid=str(i), business_id=str(i), name=f"story {i}", entity_type="IndustryChain", profile={}
            )
            for i in range(709)
        ]
        pages = _pages(rows)
        self.assertEqual(sum(map(len, pages)), 709)
        self.assertEqual(len({p.uuid for page in pages for p in page}), 709)

    def test_skill_rebind_on_nested_runtime_hydration(self):
        agent = build_event_association_agent()
        agent.skills = None
        flow = self.workflow()

        def replace(nodes):
            for node in nodes:
                if getattr(node, "agent", None) and node.agent.id == "event-association":
                    node.agent = agent
                replace(getattr(node, "steps", []) or [])

        replace(flow.steps)
        flow.update_agents_and_teams_session_info()
        self.assertIsNotNone(agent.skills)
        self.assertIsNone(agent.db)
        self.assertEqual(agent.workflow_id, "event-extraction")


class SelectedGraphTest(unittest.IsolatedAsyncioTestCase):
    async def test_profile_whitelist_excludes_downstream_assets(self):
        graph = MagicMock()
        graph.driver.execute_query = AsyncMock(
            return_value=(
                [
                    {
                        "labels": ["Entity", "MacroEconomic"],
                        "properties": {
                            "uuid": "m",
                            "data_object_id": "MEC-example",
                            "name": "利率",
                            "domain_name": "货币政策",
                            "candidate_assets": ["黄金"],
                            "main_transmission": "股票上涨",
                            "summary": "污染摘要",
                        },
                    }
                ],
                None,
                None,
            )
        )
        rows = await GraphitiStorylineCatalog(graph).profiles(["MacroEconomic"])
        self.assertEqual(rows[0]["profile"], {"domain_name": "货币政策"})
        self.assertNotIn("LIMIT", graph.driver.execute_query.call_args.args[0])

    async def test_selected_projection_never_calls_free_extraction(self):
        graph = MagicMock()
        from sematica.ingestion.episcode.event.provenance import event_episode_uuid

        history = legacy.EventExtractionWorkflowTest.historical_event()
        graph.driver.execute_query = AsyncMock(
            return_value=([{"uuid": event_episode_uuid(history.id), "mentions": 0}], None, None)
        )
        await SelectedEventEpisodeStage(graph).execute(history, [])
        graph.add_episode.assert_not_called()
        graph.add_triplet.assert_not_called()

    async def test_missing_or_changed_graph_identity_is_rejected(self):
        graph = MagicMock()
        graph.driver.execute_query = AsyncMock(return_value=([], None, None))
        with self.assertRaises(ValueError):
            await SelectedEventEpisodeStage(graph).execute(legacy.EventExtractionWorkflowTest.historical_event(), [])
