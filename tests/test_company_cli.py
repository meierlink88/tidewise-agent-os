"""Tests for the Company projection operator boundary."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sematica.projection.company_cli import _graph_write_exclusive, _runtime_config
from sematica.projection.runtime import ProjectionError


class CompanyCLIConfigTest(unittest.TestCase):
    def test_reuses_container_data_service_environment_without_a_second_secret_file(self) -> None:
        environment = {
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": "password",
            "NEO4J_BOLT_PORT": "7687",
            "NEO4J_HTTP_PORT": "7474",
            "NEO4J_URI": "bolt://neo4j:7687",
            "GRAPHITI_EMBEDDING_API_KEY": "embedding-key",
            "GRAPHITI_EMBEDDING_BASE_URL": "https://embedding.example/v1",
            "GRAPHITI_EMBEDDING_MODEL": "embedding-model",
            "GRAPHITI_EMBEDDING_DIM": "1024",
            "GRAPHITI_LLM_API_KEY": "llm-key",
            "GRAPHITI_LLM_BASE_URL": "https://llm.example/v1",
            "GRAPHITI_LLM_MODEL": "llm-model",
            "DATA_SERVICE_BASE_URL": "http://data:9011",
            "DATA_SERVICE_TOKEN": "service-token",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = _runtime_config(None)

        self.assertEqual(str(config.tidewise_data_base_url), "http://data:9011/")
        self.assertEqual(config.tidewise_data_service_token.get_secret_value(), "service-token")

    def test_missing_runtime_fails_without_echoing_secret_values(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ProjectionError, "runtime is incomplete") as raised:
                _runtime_config(None)

        self.assertNotIn("service-token", str(raised.exception))

    def test_graph_write_lock_is_shared_across_run_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "company-graph.lock"
            with patch("sematica.projection.company_cli.GRAPH_WRITE_LOCK_PATH", lock_path):
                with _graph_write_exclusive(True):
                    with self.assertRaisesRegex(RuntimeError, "another Company graph projection write"):
                        with _graph_write_exclusive(True):
                            self.fail("nested graph writer unexpectedly acquired the global lock")


if __name__ == "__main__":
    unittest.main()
