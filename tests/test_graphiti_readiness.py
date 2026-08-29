"""Contract tests for Graphiti embedding readiness."""

import unittest
from unittest.mock import AsyncMock, patch

from sematica.graphiti.readiness import verify_embedding_provider
from sematica.projection.runtime import GraphitiStorageConfig


def config(dimension: int = 3) -> GraphitiStorageConfig:
    return GraphitiStorageConfig.model_validate(
        {
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": "secret",
            "NEO4J_BOLT_PORT": 7687,
            "NEO4J_HTTP_PORT": 7474,
            "NEO4J_URI": "bolt://neo4j:7687",
            "GRAPHITI_EMBEDDING_API_KEY": "embedding-secret",
            "GRAPHITI_EMBEDDING_BASE_URL": "https://embedding.example/v1",
            "GRAPHITI_EMBEDDING_MODEL": "embedding-model",
            "GRAPHITI_EMBEDDING_DIM": dimension,
        }
    )


class GraphitiReadinessTest(unittest.IsolatedAsyncioTestCase):
    def test_agentos_storage_contract_has_no_second_llm_configuration(self) -> None:
        aliases = {field.alias for field in GraphitiStorageConfig.model_fields.values() if field.alias is not None}

        self.assertFalse(any(alias.startswith("GRAPHITI_LLM_") for alias in aliases))

    async def test_calls_provider_and_accepts_exact_dimension(self) -> None:
        with patch(
            "sematica.graphiti.readiness.ProviderCompatibleOpenAIEmbedder.create",
            new=AsyncMock(return_value=[0.1, 0.2, 0.3]),
        ) as create:
            await verify_embedding_provider(config())

        create.assert_awaited_once_with("Tidewise Graphiti embedding readiness probe")

    async def test_rejects_wrong_dimension(self) -> None:
        with patch(
            "sematica.graphiti.readiness.ProviderCompatibleOpenAIEmbedder.create",
            new=AsyncMock(return_value=[0.1]),
        ):
            with self.assertRaisesRegex(RuntimeError, "dimension mismatch"):
                await verify_embedding_provider(config())


if __name__ == "__main__":
    unittest.main()
