"""AgentOS composition for one shared Graphiti SDK client."""

from __future__ import annotations

import os
from typing import Any

from graphiti_core import Graphiti

from sematica.graphiti.agno_llm import AgnoGraphitiLLM, AgnoGraphitiReranker
from sematica.projection.runtime import GraphitiStorageConfig, create_graphiti, load_graphiti_storage_config


def create_agentos_graphiti(model: Any, config: GraphitiStorageConfig | None = None) -> Graphiti:
    """Create Graphiti with AgentOS's registered model and the configured embedder."""

    if config is None:
        aliases = {field.alias for field in GraphitiStorageConfig.model_fields.values()}
        environment = {key: value for key, value in os.environ.items() if key in aliases}
        provider = GraphitiStorageConfig.model_validate(environment) if environment else load_graphiti_storage_config()
    else:
        provider = config
    return create_graphiti(
        provider,
        llm_client=AgnoGraphitiLLM(model),
        cross_encoder=AgnoGraphitiReranker(model),
    )
