"""Injectable model/vector boundary for the document-only Workflow."""

import asyncio
import json
import os
from typing import Any, Protocol

from graphiti_core.embedder.openai import OpenAIEmbedder
from pydantic import BaseModel

from capabilities.event_v2.internal.models import DocumentEventAnalysis
from capabilities.event_v2.internal.skills import (
    ANALYST_ID,
    ANALYST_REVISION,
    bind_extraction_skill,
    extraction_skill_snapshot,
)
from capabilities.event_v2.tools.search import EventSearch
from sematica.graphiti.runtime import create_agentos_graphiti
from sematica.ingestion.event_vectors import EventVectorStore


class DocumentEventRuntime(Protocol):
    async def ready(self) -> None: ...
    def versions(self) -> dict[str, Any]: ...
    async def analyze(self, source: dict, versions: dict[str, Any], session: str) -> dict: ...
    async def stage(self, event: dict, vector: dict) -> None: ...


class LocalDocumentEventRuntime:
    def __init__(self, model, db, registry):
        self.db, self.registry = db, registry
        self.graphiti = create_agentos_graphiti(model)
        if not isinstance(self.graphiti.embedder, OpenAIEmbedder):
            raise TypeError("Document Events require the configured OpenAI-compatible embedder")
        config = self.graphiti.embedder.config
        self.vectors = EventVectorStore(self.graphiti, model=config.embedding_model, dimension=config.embedding_dim)

    def versions(self) -> dict[str, Any]:
        return {ANALYST_ID: ANALYST_REVISION, "skill": extraction_skill_snapshot()}

    async def analyze(self, source: dict, versions: dict, session: str) -> dict:
        if versions != self.versions():
            raise ValueError("Event analyst or Skill changed during the batch; start a new batch")
        registered = self.registry.get_agent(ANALYST_ID)
        if registered is None:
            raise ValueError("Event analyst is not registered")
        search = EventSearch(self.vectors, source["article_key"])
        schema = DocumentEventAnalysis
        agent = bind_extraction_skill(registered.deep_copy(), tools=[search.search_similar_events], schema=schema)
        timeout = float(os.getenv("EVENT_V2_AGENT_TIMEOUT_SECONDS", "180"))
        if timeout <= 0:
            raise ValueError("EVENT_V2_AGENT_TIMEOUT_SECONDS must be positive")
        result = await asyncio.wait_for(
            agent.arun(
                json.dumps(source, ensure_ascii=False),
                session_id=f"{session}:{ANALYST_ID}:analysis",
                stream=False,
            ),
            timeout=timeout,
        )
        content = result.content
        if isinstance(content, BaseModel):
            content = content.model_dump()
        analysis = schema.model_validate_json(content) if isinstance(content, str) else schema.model_validate(content)
        receipt = search.verify(analysis)
        return {"analysis": analysis.model_dump(mode="json"), "search": receipt}

    async def ready(self):
        await self.vectors.ready()

    async def stage(self, event, vector):
        await self.vectors.stage(event, vector)

    async def close(self):
        await self.graphiti.close()


_runtime: Any = None


def configure_document_event_runtime(runtime: DocumentEventRuntime | None) -> None:
    global _runtime
    _runtime = runtime


def document_event_runtime() -> DocumentEventRuntime:
    if _runtime is None:
        raise RuntimeError("Document Event runtime is not configured")
    return _runtime
