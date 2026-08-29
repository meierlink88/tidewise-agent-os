from pathlib import Path
from unittest import TestCase

import yaml  # type: ignore[import-untyped]

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class LocalComposeContractTest(TestCase):
    def setUp(self) -> None:
        self.compose = yaml.safe_load((REPOSITORY_ROOT / "compose.yaml").read_text())

    def test_agentos_owns_neo4j_in_an_independent_project(self) -> None:
        self.assertEqual(self.compose["name"], "agent-os")
        self.assertEqual(set(self.compose["services"]), {"agentos", "neo4j"})
        self.assertEqual(
            self.compose["services"]["agentos"]["depends_on"]["neo4j"]["condition"],
            "service_healthy",
        )
        self.assertEqual(self.compose["services"]["agentos"]["container_name"], "agent-os")
        self.assertEqual(self.compose["services"]["neo4j"]["container_name"], "agent-os-neo4j")

    def test_neo4j_reuses_the_existing_graph_volumes(self) -> None:
        self.assertTrue(self.compose["volumes"]["graphiti-neo4j-data"]["external"])
        self.assertTrue(self.compose["volumes"]["graphiti-neo4j-logs"]["external"])
        self.assertEqual(
            self.compose["volumes"]["graphiti-neo4j-data"]["name"],
            "tidewise-reason_graphiti-neo4j-data",
        )
        self.assertEqual(
            self.compose["volumes"]["graphiti-neo4j-logs"]["name"],
            "tidewise-reason_graphiti-neo4j-logs",
        )
        self.assertEqual(
            self.compose["services"]["agentos"]["environment"]["NEO4J_URI"],
            "bolt://neo4j:7687",
        )

    def test_neo4j_ports_are_loopback_only(self) -> None:
        ports = self.compose["services"]["neo4j"]["ports"]
        self.assertTrue(all(str(port).startswith("127.0.0.1:") for port in ports))
