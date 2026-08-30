"""Event runtime composition tests for the single AgentOS-to-Graphiti model seam."""

import os
import unittest
from unittest.mock import MagicMock, patch

from capabilities.event.internal.local_runtime import create_local_event_workflow_runtime


class EventRuntimeCompositionTest(unittest.TestCase):
    def test_native_event_and_signal_ingestion_share_the_agentos_graphiti_client(self) -> None:
        model = object()
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
            patch("capabilities.event.internal.local_runtime.GraphitiEpisodeStage") as episode_stage,
            patch("capabilities.event.internal.local_runtime.GraphitiCandidateRetriever") as candidate_retriever,
            patch("capabilities.event.internal.local_runtime.GraphitiSignalFactProjector") as signal_projector,
        ):
            runtime = create_local_event_workflow_runtime(model)

        create_graphiti.assert_called_once_with(model)
        data_client.assert_called_once_with("http://data.example", "configured-service-token")
        episode_stage.assert_called_once_with(graphiti)
        candidate_retriever.assert_called_once_with(graphiti)
        signal_projector.assert_called_once_with(graphiti)
        self.assertIs(runtime._graphiti, graphiti)


if __name__ == "__main__":
    unittest.main()
