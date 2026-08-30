"""Published Event Workflow version-pin rehydration tests."""

import unittest
from unittest.mock import MagicMock, patch

from agno.agent import Agent
from agno.registry import Registry
from agno.workflow import Workflow

from agents.event_extractor import LoadedEventExtractorAgent
from agents.event_identity import LoadedEventIdentityAgent
from agents.event_signal_analyst import LoadedEventSignalAnalystAgent
from capabilities.event.functions import (
    event_extraction_required,
    event_resolution_complete,
    freeze_event_extraction,
    has_pending_event_resolution,
    has_pending_signal_analysis,
    persist_event_resolution,
    persist_signal_task,
    prepare_event_extraction,
    prepare_event_resolution,
    prepare_signal_task,
    publish_events,
    publish_signals,
    signal_analysis_complete,
)
from workflows.event_extraction import ensure_event_extraction_workflow


class EventWorkflowVersioningTest(unittest.TestCase):
    def test_published_workflow_strictly_rehydrates_the_three_exact_agent_versions(self) -> None:
        versions = {
            "event-extractor": 11,
            "event-identity": 13,
            "event-signal-analyst": 17,
        }
        agents = {
            agent_id: Agent(id=agent_id, name=agent_id, instructions=f"published {agent_id}") for agent_id in versions
        }
        loaded_agents = (
            LoadedEventExtractorAgent(agents["event-extractor"], 11, "a" * 64),
            LoadedEventIdentityAgent(agents["event-identity"], 13, "b" * 64),
            LoadedEventSignalAnalystAgent(agents["event-signal-analyst"], 17, "c" * 64),
        )
        database = MagicMock()
        database.get_component.return_value = None
        database.upsert_config.return_value = {"version": 29}

        with (
            patch("workflows.event_extraction.get_postgres_db", return_value=database),
            patch("workflows.event_extraction.load_event_extractor_agent", return_value=loaded_agents[0]),
            patch("workflows.event_extraction.load_event_identity_agent", return_value=loaded_agents[1]),
            patch("workflows.event_extraction.load_event_signal_analyst_agent", return_value=loaded_agents[2]),
        ):
            self.assertEqual(ensure_event_extraction_workflow(MagicMock()), 29)

        publication = database.upsert_config.call_args.kwargs
        database.get_config.return_value = {
            "config": publication["config"],
            "version": 29,
        }
        database.get_links.return_value = publication["links"]
        function_registry = Registry(
            functions=[
                prepare_event_extraction,
                event_extraction_required,
                freeze_event_extraction,
                has_pending_event_resolution,
                prepare_event_resolution,
                persist_event_resolution,
                event_resolution_complete,
                publish_events,
                has_pending_signal_analysis,
                prepare_signal_task,
                persist_signal_task,
                signal_analysis_complete,
                publish_signals,
            ]
        )
        observed: list[tuple[str, int | None]] = []

        def load_pinned_agent(**kwargs):
            observed.append((kwargs["id"], kwargs["version"]))
            return agents[kwargs["id"]]

        with patch("agno.agent.agent.get_agent_by_id", side_effect=load_pinned_agent):
            rehydrated = Workflow.load(
                "event-extraction",
                db=database,
                registry=function_registry,
                version=29,
                strict=True,
                published_only=True,
            )

        self.assertIsNotNone(rehydrated)
        self.assertEqual(
            observed,
            [
                ("event-extractor", 11),
                ("event-identity", 13),
                ("event-signal-analyst", 17),
            ],
        )
        assert rehydrated is not None
        assert rehydrated.metadata is not None
        self.assertEqual(rehydrated.metadata["event_agent_versions"], versions)


if __name__ == "__main__":
    unittest.main()
