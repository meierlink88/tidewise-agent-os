"""Event runtime composition tests for the single AgentOS-to-Graphiti model seam."""

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

from agno.run import RunContext
from agno.run.agent import RunOutput

from capabilities.event.internal.local_runtime import (
    LocalEventWorkflowRuntime,
    create_local_event_workflow_runtime,
)


class EventRuntimeCompositionTest(unittest.TestCase):
    def test_native_event_and_signal_ingestion_share_the_agentos_graphiti_client(self) -> None:
        model = object()
        registry = MagicMock(name="registry")
        db = MagicMock(name="agentos-db")
        graphiti = MagicMock(name="agentos-graphiti")
        data = MagicMock(name="data-event-client")

        with (
            patch.dict(
                os.environ,
                {
                    "DATA_SERVICE_BASE_URL": "http://data.example",
                    "DATA_SERVICE_TOKEN": "configured-service-token",
                },
            ),
            patch(
                "capabilities.event.internal.local_runtime.create_agentos_graphiti",
                return_value=graphiti,
            ) as create_graphiti,
            patch(
                "capabilities.event.internal.local_runtime.DataEventClient",
                return_value=data,
            ) as data_client,
            patch("db.get_postgres_db", return_value=db) as get_postgres_db,
            patch("capabilities.event.internal.local_runtime.GraphitiEventHistory") as event_history,
            patch("capabilities.event.internal.local_runtime.GraphitiEpisodeStage") as episode_stage,
            patch("capabilities.event.internal.local_runtime.GraphitiCandidateRetriever") as candidate_retriever,
            patch("capabilities.event.internal.local_runtime.GraphitiSignalFactProjector") as signal_projector,
        ):
            runtime = create_local_event_workflow_runtime(model, registry)

        create_graphiti.assert_called_once_with(model)
        data_client.assert_called_once_with("http://data.example", "configured-service-token")
        get_postgres_db.assert_called_once_with()
        event_history.assert_called_once_with(graphiti)
        episode_stage.assert_called_once_with(graphiti)
        candidate_retriever.assert_called_once_with(graphiti)
        signal_projector.assert_called_once_with(graphiti)
        self.assertIs(runtime._graphiti, graphiti)
        self.assertIs(runtime._data, data)
        self.assertIs(runtime._db, db)
        self.assertIs(runtime._registry, registry)


class PinnedEventAgentExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_consecutive_pinned_agent_runs_preserve_workflow_metadata(self) -> None:
        db = MagicMock(name="agentos-db")
        registry = MagicMock(name="registry")
        loaded_agent = MagicMock(name="pinned-agent")
        loaded_agent.id = "event-identity"
        loaded_agent.db = db

        async def agent_run(**kwargs):
            child_context = kwargs.get("run_context")
            if child_context is not None:
                child_context.metadata = dict(kwargs["metadata"])
            return RunOutput(content={"decision": "SAME_EVENT"}, metadata={"trace": "preserved"})

        loaded_agent.arun = AsyncMock(side_effect=agent_run)
        runtime = LocalEventWorkflowRuntime(
            MagicMock(name="agentos-graphiti"),
            MagicMock(name="data-event-client"),
            db,
            registry,
        )
        workflow_metadata = {
            "event_agent_versions": {
                "event-extractor": 10,
                "event-identity": 17,
                "event-signal-analyst": 2,
            }
        }
        run_context = RunContext(
            run_id="workflow-run-1",
            session_id="event-session-1",
            user_id="event-user-1",
            dependencies={},
            metadata=dict(workflow_metadata),
        )

        with patch(
            "capabilities.event.internal.local_runtime.Agent.load",
            return_value=loaded_agent,
        ) as load_agent:
            first = await runtime.invoke_agent(
                "event-identity",
                17,
                {"candidate_key": "candidate-1"},
                run_context,
            )
            second = await runtime.invoke_agent(
                "event-identity",
                17,
                {"candidate_key": "candidate-2"},
                run_context,
            )

        load_agent.assert_has_calls(
            [
                call(
                    "event-identity",
                    db=db,
                    registry=registry,
                    version=17,
                    strict=True,
                    published_only=True,
                ),
                call(
                    "event-identity",
                    db=db,
                    registry=registry,
                    version=17,
                    strict=True,
                    published_only=True,
                ),
            ]
        )
        loaded_agent.arun.assert_has_awaits(
            [
                call(
                    input={"candidate_key": "candidate-1"},
                    stream=False,
                    session_id="event-session-1:event-identity",
                    user_id="event-user-1",
                    metadata={"event_agent_id": "event-identity", "event_agent_version": 17},
                ),
                call(
                    input={"candidate_key": "candidate-2"},
                    stream=False,
                    session_id="event-session-1:event-identity",
                    user_id="event-user-1",
                    metadata={"event_agent_id": "event-identity", "event_agent_version": 17},
                ),
            ]
        )
        self.assertEqual(run_context.metadata, workflow_metadata)
        self.assertIsNone(loaded_agent.db)
        for response in (first, second):
            self.assertEqual(
                response.metadata,
                {
                    "trace": "preserved",
                    "event_agent_id": "event-identity",
                    "event_agent_version": 17,
                },
            )


if __name__ == "__main__":
    unittest.main()
