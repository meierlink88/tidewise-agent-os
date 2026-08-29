"""Provider compatibility tests for Graphiti embedding."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from graphiti_core.embedder.openai import OpenAIEmbedderConfig

from sematica.projection.runtime import ProviderCompatibleOpenAIEmbedder


class ProviderCompatibleOpenAIEmbedderTest(unittest.IsolatedAsyncioTestCase):
    async def test_large_graphiti_batches_are_split_without_reordering(self) -> None:
        embedder = ProviderCompatibleOpenAIEmbedder(
            OpenAIEmbedderConfig(api_key="test", embedding_model="test", embedding_dim=1)
        )
        create = AsyncMock()
        create.side_effect = [
            SimpleNamespace(data=[SimpleNamespace(embedding=[float(index)]) for index in range(start, end)])
            for start, end in ((0, 10), (10, 20), (20, 23))
        ]
        embedder.client = MagicMock()
        embedder.client.embeddings.create = create

        result = await embedder.create_batch([f"entity-{index}" for index in range(23)])

        self.assertEqual(result, [[float(index)] for index in range(23)])
        self.assertEqual([len(call.kwargs["input"]) for call in create.await_args_list], [10, 10, 3])

    async def test_empty_batch_never_calls_the_provider(self) -> None:
        embedder = ProviderCompatibleOpenAIEmbedder(OpenAIEmbedderConfig(api_key="test"))
        embedder.client = MagicMock()
        embedder.client.embeddings.create = AsyncMock()

        self.assertEqual(await embedder.create_batch([]), [])
        embedder.client.embeddings.create.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
