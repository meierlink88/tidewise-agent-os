"""Native batch Parallel execution, Event attribution and checkpoint recovery."""

import asyncio
import json
import unittest
from collections import Counter
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from agno.agent import Agent
from agno.run.agent import RunOutput
from agno.run.base import RunStatus

from capabilities.event import (
    BatchAssociationDecision,
    BatchIdentityDecision,
    BatchSignalDecision,
    ClassifiedEventDraft,
)
from capabilities.event.functions import batch as functions
from capabilities.event.functions import enqueue_evidence_artifact
from capabilities.event.internal.storage import (
    _load_frozen_batch,
    _load_lease,
    _runtime_batch,
    event_artifact_root,
    release_event_batch_lease,
)
from sematica.analysis.event.graphiti.storylines import GraphitiStorylineCatalog
from tests import test_event_storyline_workflow as fixtures
from workflows.event_extraction import _seed_workflow


class BatchWorkflowTest(unittest.IsolatedAsyncioTestCase):
    # Reuse fixtures, not the legacy behavior suite.
    async def respond(self, agent, *args, **kwargs):
        payload = json.loads(kwargs.get("input", args[0] if args else None))
        schema = agent.output_schema
        assert agent.skills is None and not agent.tools
        self.calls.append((schema.__name__, payload))
        if schema is ClassifiedEventDraft:
            draft = self.fixture.extraction_draft().model_dump(mode="json")
            for c in draft["candidates"]:
                c["classification"] = self.fixture.classification().model_dump(mode="json")
                c["classification"]["event_class"] = "INDUSTRY_CHAIN"
            content = schema.model_validate(self.draft_override or draft)
        elif schema is BatchIdentityDecision:
            content = schema(
                events=[
                    {
                        "candidate_key": e["candidate_key"],
                        "duplicate_of": None,
                        "decision": {
                            "decision": "NEW_EVENT",
                            "atomic": True,
                            "matched_event_ids": [],
                            "reason_codes": ["NEW"],
                            "summary": "new",
                        },
                    }
                    for e in payload["events"]
                ]
            )
        elif schema is BatchAssociationDecision:
            await asyncio.sleep(0)
            self.assertEqual(self.runtime.data_publications, 0)
            if self.fail_association:
                self.fail_association = False
                raise RuntimeError("synthetic association failure")
            content = schema(
                events=[
                    {
                        "candidate_key": e["candidate_key"],
                        "matches": []
                        if self.no_match
                        else [
                            {"uuid": p["uuid"], "reason": "explicit event activity"}
                            for p in payload["candidates"][: self.match_limit]
                            if p["uuid"] in e.get("allowed_uuids", [p["uuid"]])
                        ],
                        "no_match_reason": "No direct subject" if self.no_match or not payload["candidates"] else None,
                    }
                    for e in payload["events"]
                ]
            )
            if self.empty_first_match:
                response = content.model_dump(mode="json")
                response["events"][0]["matches"] = []
                response["events"][0].pop("no_match_reason")
                content = schema.model_validate_json(json.dumps(response))
        elif schema is BatchSignalDecision:
            content = schema(
                events=[
                    {
                        "candidate_key": e["candidate_key"],
                        "proposals": [],
                        "no_signal_reason": "No directly supported change",
                    }
                    for e in payload["events"]
                ]
            )
        else:
            raise AssertionError(schema)
        if self.response_transform:
            content = self.response_transform(schema, content)
        return RunOutput(agent_id=agent.id, content=content, status=RunStatus.completed)

    def setUp(self):
        self.harness = fixtures.StorylineWorkflowTest(methodName="runTest")
        self.harness.setUp()
        self.fixture = self.harness.fixture
        self.runtime = self.harness.runtime
        self.agents = self.harness.agents
        self.calls = []
        self.fail_association = False
        self.no_match = False
        self.empty_first_match = False
        self.match_limit = None
        self.draft_override = None
        self.response_transform = None
        self.evidence_input = self.fixture.evidences()

    def tearDown(self):
        self.harness.tearDown()

    def enqueue(self):
        with patch("capabilities.event.internal.queue.read_resolved_evidences", return_value=self.evidence_input):
            enqueue_evidence_artifact(str(self.fixture.evidence_manifest), [e.id for e in self.evidence_input])

    def release_synthetic_lease(self):
        from capabilities.event.internal.storage import event_artifact_root

        path = next((event_artifact_root() / ".pending").iterdir())
        lease = _load_lease(path)
        if lease is not None:
            release_event_batch_lease(_runtime_batch(_load_frozen_batch(path), lease, needs_analysis=False))

    async def run_flow(self, flow=None):
        async def response(agent, *args, **kwargs):
            return await self.respond(agent, *args, **kwargs)

        with (
            patch.object(Agent, "arun", new=response),
            patch("capabilities.event.internal.queue.read_resolved_evidences", return_value=self.evidence_input),
        ):
            return await (flow or self.workflow()).arun(input="test", stream=False)

    def workflow(self):
        flow = _seed_workflow(*self.agents.values(), agent_versions=fixtures.PINS)
        flow.db = None
        return flow

    async def test_batch_happy_path(self):
        self.enqueue()
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed, result.content)
        counts = Counter(name for name, _ in self.calls)
        self.assertEqual(
            counts,
            {
                "ClassifiedEventDraft": 1,
                "BatchIdentityDecision": 1,
                "BatchAssociationDecision": 2,
                "BatchSignalDecision": 1,
            },
        )
        self.assertEqual(self.runtime.data_publications, 1)

    async def test_batch_no_match_is_terminal_without_publication(self):
        self.no_match = True
        self.enqueue()
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed, result.content)
        self.assertEqual(self.runtime.data_publications, 0)

    async def test_invalid_identity_is_ignored_not_a_batch_failure(self):
        def transform(schema, content):
            if schema is BatchIdentityDecision:
                content.events[0].decision = content.events[0].decision.model_copy(
                    update={"decision": "SAME_EVENT", "matched_event_ids": ["invented"]}
                )
            return content

        self.response_transform = transform
        self.prepare_two_events()
        self.enqueue()
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed, result.content)
        self.assertEqual(self.runtime.data_publications, 1)

    async def test_invalid_duplicate_reference_does_not_block_valid_event(self):
        def transform(schema, content):
            if schema is BatchIdentityDecision:
                content.events[0].duplicate_of = "invented"
            return content

        self.response_transform = transform
        self.prepare_two_events()
        self.enqueue()
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed, result.content)
        self.assertEqual(self.runtime.data_publications, 1)

    async def test_invalid_and_duplicate_signals_do_not_block_valid_signal(self):
        draft = self.fixture.signal_draft()

        def transform(schema, content):
            if schema is BatchSignalDecision:
                item = content.events[0]
                content.events[0] = item.model_copy(
                    update={
                        "no_signal_reason": None,
                        "proposals": [
                            draft.model_copy(update={"anchor_uuid": "invented"}),
                            draft.model_copy(update={"variable_uuid": "invented"}),
                            draft,
                            draft,
                        ],
                    }
                )
            return content

        self.response_transform = transform
        self.enqueue()
        # Fail after normalization, then prove recovery retains both diagnostics and good signals.
        with patch.object(functions.legacy, "freeze_storyline_signal_page", side_effect=RuntimeError("compile failed")):
            with self.assertRaisesRegex(RuntimeError, "compile failed"):
                await self.run_flow()
        pending = next((event_artifact_root() / ".pending").iterdir())
        rejection_path = pending / "batch-v16" / "signal-chain-rejections.json"
        rejected_before = rejection_path.read_text()
        self.assertEqual(len(json.loads(rejected_before)["items"]), 3)
        self.release_synthetic_lease()
        self.calls.clear()
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed, result.content)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runtime.data_publications, 1)
        self.assertEqual(self.runtime.signal_projections, 1)
        completed = event_artifact_root() / "batches" / pending.name
        self.assertEqual((completed / "batch-v16" / rejection_path.name).read_text(), rejected_before)

    async def test_redundant_no_event_does_not_block_native_workflow(self):
        def transform(schema, content):
            if schema is ClassifiedEventDraft:
                payload = content.model_dump(mode="json")
                payload["no_event"] = [
                    {"evidence_id": eid, "reason": "上述处理重复"}
                    for candidate in payload["candidates"]
                    for eid in candidate["evidence_ids"]
                ]
                return payload
            return content

        self.response_transform = transform
        self.enqueue()
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed, result.content)
        self.assertEqual(self.runtime.data_publications, 1)

    async def test_signal_is_published_without_semantic_reviewer(self):
        def transform(schema, content):
            if schema is BatchSignalDecision:
                signal = self.fixture.signal_draft().model_dump()
                signal.update(impact_onset_days=1200, impact_peak_days=30, expected_duration_days=1500)
                content.events[0] = content.events[0].model_copy(
                    update={
                        "no_signal_reason": None,
                        "proposals": [type(self.fixture.signal_draft()).model_validate(signal)],
                    }
                )
            return content

        self.response_transform = transform
        self.enqueue()
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed, result.content)
        self.assertEqual(self.runtime.data_publications, 1)
        self.assertEqual(self.runtime.signal_projections, 1)

    async def test_invalid_match_does_not_block_valid_matches(self):
        def transform(schema, content):
            if schema is BatchAssociationDecision:
                content.events[0].matches.append(content.events[0].matches[0].model_copy(update={"uuid": "invented"}))
            return content

        self.response_transform = transform
        self.enqueue()
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed, result.content)
        self.assertEqual(self.runtime.data_publications, 1)

    def prepare_two_events(self):
        self.evidence_input = []
        self.draft_override = {"candidates": [], "no_event": []}
        for label in ("first", "second"):
            evidence = self.fixture.evidences()[0].model_copy(deep=True)
            evidence.id = "EVD" + str(uuid4())
            evidence.semantic.objects = [label]
            self.evidence_input.append(evidence)
            candidate = deepcopy(self.fixture.extraction_draft().model_dump(mode="json")["candidates"][0])
            candidate["evidence_ids"] = [evidence.id]
            candidate["event"]["semantic"]["objects"] = [label]
            candidate["classification"] = {
                **self.fixture.classification().model_dump(mode="json"),
                "event_class": "INDUSTRY_CHAIN",
            }
            self.draft_override["candidates"].append(candidate)

    async def test_empty_match_without_reason_does_not_block_valid_event(self):
        self.empty_first_match = True
        self.prepare_two_events()
        self.enqueue()
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed, result.content)
        self.assertEqual(self.runtime.data_publications, 1)
        self.assertEqual(self.runtime.signal_projections, 0)

    async def test_failed_match_has_no_publication_and_resume_reuses_extraction(self):
        self.fail_association = True
        self.enqueue()
        with self.assertRaisesRegex(ValueError, "missing batch checkpoint"):
            await self.run_flow()
        self.assertEqual(self.runtime.data_publications, 0)
        # Operator test fixture releases only the synthetic batch lease; production retains expiry fencing.
        self.release_synthetic_lease()
        self.calls.clear()
        result = await self.run_flow()
        self.assertEqual(result.status, RunStatus.completed, result.content)
        self.assertNotIn("ClassifiedEventDraft", [name for name, _ in self.calls])
        self.assertNotIn("BatchIdentityDecision", [name for name, _ in self.calls])

    async def test_full_708_chain_catalog_is_one_input_not_pages(self):
        self.runtime.catalog = [
            {**self.runtime.catalog[0], "uuid": f"chain-{i}", "business_id": f"ICH-{i}"} for i in range(708)
        ]
        self.match_limit = 1
        self.enqueue()
        await self.run_flow()
        inputs = [p for name, p in self.calls if name == "BatchAssociationDecision"]
        self.assertEqual(len(inputs), 2)
        self.assertEqual(len(inputs[0]["candidates"]), 708)

    async def test_chain_only_match_can_publish_without_node_signal(self):
        self.runtime.nodes = []
        self.enqueue()
        await self.run_flow()
        self.assertEqual(self.runtime.data_publications, 1)
        self.assertEqual(self.runtime.signal_projections, 0)
        self.assertNotIn("BatchSignalDecision", [name for name, _ in self.calls])

    async def test_signal_compilation_resume_reuses_frozen_model_result(self):
        self.enqueue()
        with patch.object(functions.legacy, "freeze_storyline_signal_page", side_effect=RuntimeError("compile failed")):
            with self.assertRaisesRegex(RuntimeError, "compile failed"):
                await self.run_flow()
        self.assertEqual(self.runtime.data_publications, 0)
        self.release_synthetic_lease()
        self.calls.clear()
        await self.run_flow()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runtime.data_publications, 1)

    async def test_publication_resume_never_repeats_semantic_calls(self):
        self.runtime.fail_graph_once = True
        self.enqueue()
        with self.assertRaises(Exception):
            await self.run_flow()
        self.assertEqual(self.runtime.data_publications, 1)
        self.release_synthetic_lease()
        self.calls.clear()
        await self.run_flow()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runtime.data_publications, 1)

    async def test_all_four_classes_have_isolated_shared_catalog_calls(self):
        classes = ["GEOPOLITICAL", "MACRO_ECONOMIC", "INDUSTRY_CHAIN", "COMPANY"]
        self.evidence_input = []
        self.draft_override = {"candidates": [], "no_event": []}
        for kind in classes:
            evidence = self.fixture.evidences()[0].model_copy(deep=True)
            evidence.id = "EVD" + str(uuid4())
            evidence.semantic.objects = [kind]
            self.evidence_input.append(evidence)
            candidate = deepcopy(self.fixture.extraction_draft().model_dump(mode="json")["candidates"][0])
            candidate["evidence_ids"] = [evidence.id]
            candidate["event"]["semantic"]["objects"] = [kind]
            candidate["classification"] = {**self.fixture.classification().model_dump(mode="json"), "event_class": kind}
            self.draft_override["candidates"].append(candidate)

        async def profiles(labels, *, terms=None, chain_uuids=None):
            if terms is not None:
                return []
            label = labels[0]
            return [{"uuid": label, "business_id": label, "name": label, "entity_type": label, "profile": {}}]

        async def companies(terms):
            return await profiles(["Company"])

        self.runtime.storyline_profiles = profiles
        self.runtime.company_profiles = companies
        self.enqueue()
        await self.run_flow()
        matches = [p for name, p in self.calls if name == "BatchAssociationDecision"]
        self.assertEqual(len(matches), 5)
        self.assertTrue(all(len(p["events"]) == 1 for p in matches))
        self.assertEqual(self.runtime.data_publications, 4)


class BatchBoundaryTest(unittest.IsolatedAsyncioTestCase):
    def test_wrong_event_or_duplicate_response_key_is_rejected(self):
        item = {"candidate_key": "one", "matches": [], "no_match_reason": "unrelated"}
        response = BatchAssociationDecision(events=[item, item])
        with self.assertRaisesRegex(ValueError, "exactly once"):
            functions._coverage(response.events, ["one", "two"])

    def test_node_from_other_events_chain_is_omitted_and_recorded(self):
        response = BatchAssociationDecision(
            events=[
                {
                    "candidate_key": "one",
                    "matches": [{"uuid": "other-node", "reason": "wrong chain"}],
                    "no_match_reason": None,
                }
            ]
        )
        request = {
            "events": [{"candidate_key": "one", "allowed_uuids": ["own-node"]}],
            "candidates": [{"uuid": "own-node"}, {"uuid": "other-node"}],
        }
        with (
            patch.object(functions, "_required", return_value=request),
            patch.object(functions, "_response", return_value=response),
            patch.object(functions, "_read", return_value=None),
            patch.object(functions, "_freeze") as freeze,
        ):
            functions._freeze_match(MagicMock(), MagicMock(), "match-node")
        saved = {call.args[1]: call.args[2] for call in freeze.call_args_list}
        self.assertEqual(saved["match-node-result"]["events"][0]["matches"], [])
        self.assertEqual(saved["match-node-rejections"]["items"][0]["reason"], "MATCH_OUTSIDE_EVENT_CANDIDATES")

    async def test_company_recall_combines_exact_and_vector_without_unowned_nodes(self):
        from types import SimpleNamespace

        from sematica.projection.runtime import GRAPHITI_GROUP_ID

        graph = MagicMock()
        catalog = GraphitiStorylineCatalog(graph)
        catalog.profiles = AsyncMock(return_value=[{"uuid": "exact", "name": "示例公司"}])
        nodes = [
            SimpleNamespace(
                uuid="vector",
                name="示例集团",
                group_id=GRAPHITI_GROUP_ID,
                labels=["Company"],
                attributes={"data_object_id": "COM-example", "aliases": ["示例公司"]},
            ),
            SimpleNamespace(
                uuid="unowned", name="临时公司", group_id=GRAPHITI_GROUP_ID, labels=["Company"], attributes={}
            ),
        ]
        graph.search_ = AsyncMock(return_value=SimpleNamespace(nodes=nodes))
        result = await catalog.companies(["示例公司"])
        self.assertEqual({p["uuid"] for p in result}, {"exact", "vector"})
        self.assertEqual(graph.search_.call_args.kwargs["config"].limit, 8)
        self.assertEqual(graph.search_.call_args.kwargs["search_filter"].node_labels, ["Company"])
