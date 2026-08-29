"""Fail-closed readiness probe for AgentOS's external Graphiti embedding provider."""

from __future__ import annotations

import asyncio
import os

from graphiti_core.embedder.openai import OpenAIEmbedderConfig

from sematica.projection.runtime import GraphitiStorageConfig, ProviderCompatibleOpenAIEmbedder


def _config_from_environment() -> GraphitiStorageConfig:
    aliases = {field.alias for field in GraphitiStorageConfig.model_fields.values()}
    return GraphitiStorageConfig.model_validate({key: value for key, value in os.environ.items() if key in aliases})


async def verify_embedding_provider(config: GraphitiStorageConfig | None = None) -> None:
    """Call the configured provider and reject an unexpected vector dimension."""

    provider = config or _config_from_environment()
    embedder = ProviderCompatibleOpenAIEmbedder(
        OpenAIEmbedderConfig(
            api_key=provider.graphiti_embedding_api_key.get_secret_value(),
            base_url=str(provider.graphiti_embedding_base_url).rstrip("/"),
            embedding_model=provider.graphiti_embedding_model,
            embedding_dim=provider.graphiti_embedding_dim,
        )
    )
    vector = await embedder.create("Tidewise Graphiti embedding readiness probe")
    if len(vector) != provider.graphiti_embedding_dim:
        raise RuntimeError(
            f"embedding dimension mismatch: expected {provider.graphiti_embedding_dim}, received {len(vector)}"
        )


def main() -> None:
    asyncio.run(verify_embedding_provider())


if __name__ == "__main__":
    main()
