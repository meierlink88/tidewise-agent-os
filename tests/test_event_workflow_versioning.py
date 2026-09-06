"""Exact-version storage and direct-Agent rehydration for the linear Event Workflow."""

import unittest
from unittest.mock import MagicMock, patch

from agno.registry import Registry
from agno.workflow import Workflow

from capabilities.event.functions import event_extraction_complete, event_extraction_required
from capabilities.event.functions.batch import BATCH_FUNCTIONS
from capabilities.event.functions.linear import LINEAR_EVENT_FUNCTIONS
from capabilities.event.functions.storyline import STORYLINE_FUNCTIONS
from tests import test_event_batch_workflow as batch_fixtures
from tests import test_event_storyline_workflow as fixtures
from workflows.event_extraction import (
    EVENT_EXTRACTION_CONTRACT_VERSION,
    _publish_pinned_workflow,
    ensure_event_extraction_workflow,
)


class EventWorkflowVersionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.harness = batch_fixtures.BatchWorkflowTest(methodName="runTest")
        self.harness.setUp()
        self.database = MagicMock()
        self.database.upsert_config.return_value = {"version": 30}
        self.flow = self.harness.workflow()

    def tearDown(self):
        self.harness.tearDown()

    def publish(self):
        with patch("workflows.event_extraction.get_postgres_db", return_value=self.database):
            version = _publish_pinned_workflow(self.flow, agent_versions=fixtures.PINS, notes="test")
        self.assertEqual(version, 30)
        publication = self.database.upsert_config.call_args.kwargs
        self.database.get_component.return_value = {"current_version": 30}
        self.database.get_config.return_value = {"config": publication["config"], "version": 30}
        self.database.get_links.return_value = publication["links"]
        return publication

    def loaded(self, versions):
        return (*self.harness.agents.values(), versions)

    def test_current_pins_keep_published_version(self):
        self.publish()
        with (
            patch("workflows.event_extraction.get_postgres_db", return_value=self.database),
            patch("workflows.event_extraction._loaded_agents", return_value=self.loaded(fixtures.PINS)),
            patch.object(Workflow, "load", return_value=self.flow),
        ):
            self.assertEqual(ensure_event_extraction_workflow(MagicMock()), 30)
        self.assertEqual(self.database.upsert_config.call_count, 1)

    def test_changed_pins_publish_eight_exact_links(self):
        self.publish()
        self.flow.steps[7].max_iterations = 7
        newer = {**fixtures.PINS, "event-association": 3}
        with (
            patch("workflows.event_extraction.get_postgres_db", return_value=self.database),
            patch("workflows.event_extraction._loaded_agents", return_value=self.loaded(newer)),
            patch.object(Workflow, "load", return_value=self.flow),
        ):
            ensure_event_extraction_workflow(MagicMock())
        config = self.database.upsert_config.call_args.kwargs
        self.assertEqual(config["config"]["metadata"]["event_agent_versions"], newer)
        self.assertEqual(config["config"]["steps"][7]["max_iterations"], 7)
        self.assertEqual(len(config["links"]), 8)
        self.assertEqual({link["child_component_id"]: link["child_version"] for link in config["links"]}, newer)

    def test_contract_upgrade_replaces_legacy_topology(self):
        self.publish()
        config = self.database.get_config.return_value["config"]
        config["metadata"]["event_extraction_contract_version"] = 13
        with (
            patch("workflows.event_extraction.get_postgres_db", return_value=self.database),
            patch("workflows.event_extraction._loaded_agents", return_value=self.loaded(fixtures.PINS)),
        ):
            ensure_event_extraction_workflow(MagicMock())
        self.assertEqual(
            self.database.upsert_config.call_args.kwargs["config"]["metadata"]["event_extraction_contract_version"],
            EVENT_EXTRACTION_CONTRACT_VERSION,
        )

    async def test_published_roundtrip_resolves_exact_agents_and_executes_real_nested_steps(self):
        publication = self.publish()
        registry = Registry(
            functions=[
                *LINEAR_EVENT_FUNCTIONS,
                *STORYLINE_FUNCTIONS,
                *BATCH_FUNCTIONS,
                event_extraction_complete,
                event_extraction_required,
            ]
        )
        loads = []

        def load_agent(*, id, version, **kwargs):
            loads.append((id, version))
            return self.harness.agents[id]

        with patch("agno.agent.agent.get_agent_by_id", side_effect=load_agent):
            hydrated = Workflow.load(
                "event-extraction", db=self.database, registry=registry, version=30, strict=True, published_only=True
            )
        self.assertIsNotNone(hydrated)
        self.assertEqual(dict(loads), fixtures.PINS)
        self.assertEqual(len(loads), 8)
        self.assertEqual(hydrated.to_dict()["steps"], publication["config"]["steps"])
        hydrated.db = None
        self.harness.enqueue()
        result = await self.harness.run_flow(hydrated)
        self.assertEqual(result.status.value, "COMPLETED")
        self.assertEqual(self.harness.runtime.data_publications, 1)

    def test_application_registry_preserves_legacy_and_new_functions(self):
        from app.registry import registry

        for function in STORYLINE_FUNCTIONS:
            self.assertIsNotNone(registry.get_function(function.__name__))
        for name in ("extract_events", "resolve_events", "analyze_signals", "publish_events", "publish_signals"):
            self.assertIsNotNone(registry.get_function(name))
