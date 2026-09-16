"""Injectable model/vector boundary for the document-only Workflow."""

import asyncio
import json
import os
from typing import Any, Protocol

from agno.agent import Agent
from agno.db.base import ComponentType
from graphiti_core.embedder.openai import OpenAIEmbedder
from pydantic import BaseModel

from capabilities.event_v2.internal.models import DocumentEventDraft, DuplicateDecision
from sematica.graphiti.runtime import create_agentos_graphiti
from sematica.ingestion.event_vectors import EventVectorStore

EXTRACTOR = "document-event-extractor"
IDENTITY = "document-event-identity"


class DocumentEventRuntime(Protocol):
    async def ready(self) -> None: ...
    def versions(self) -> dict[str, int]: ...
    async def extract(self, source: dict, versions: dict[str, int], session: str) -> DocumentEventDraft: ...
    async def embed(self, title: str, summary: str) -> dict: ...
    async def recall(self, vector: dict, article_key: str) -> list[dict]: ...
    async def decide(
        self, event: dict, candidates: list[dict], versions: dict[str, int], session: str
    ) -> DuplicateDecision: ...
    async def stage(self, event: dict, vector: dict) -> None: ...


class LocalDocumentEventRuntime:
    def __init__(self, model, db, registry):
        self.db, self.registry = db, registry
        self.graphiti = create_agentos_graphiti(model)
        if not isinstance(self.graphiti.embedder, OpenAIEmbedder):
            raise TypeError("Document Events require the configured OpenAI-compatible embedder")
        config = self.graphiti.embedder.config
        self.vectors = EventVectorStore(self.graphiti, model=config.embedding_model, dimension=config.embedding_dim)

    def versions(self) -> dict[str, int]:
        versions = {}
        for key in (EXTRACTOR, IDENTITY):
            component = self.db.get_component(key, component_type=ComponentType.AGENT)
            version = component.get("current_version") if component else None
            if not isinstance(version, int):
                raise ValueError("Document Event Agent has no published version")
            versions[key] = version
        return versions

    async def _invoke(self, key: str, payload: dict, versions: dict[str, int], session: str, schema):
        agent = Agent.load(key, db=self.db, registry=self.registry, version=versions[key])
        if agent is None:
            raise ValueError("Document Event Agent cannot be loaded")
        timeout = float(os.getenv("EVENT_V2_AGENT_TIMEOUT_SECONDS", "180"))
        if timeout <= 0:
            raise ValueError("EVENT_V2_AGENT_TIMEOUT_SECONDS must be positive")
        result = await asyncio.wait_for(
            agent.arun(json.dumps(payload, ensure_ascii=False), session_id=f"{session}:{key}", stream=False),
            timeout=timeout,
        )
        content = result.content
        if isinstance(content, str):
            return schema.model_validate_json(content)
        if isinstance(content, BaseModel):
            content = content.model_dump()
        return schema.model_validate(content)

    async def extract(self, source, versions, session):
        return await self._invoke(EXTRACTOR, source, versions, session, DocumentEventDraft)

    async def decide(self, event, candidates, versions, session):
        return await self._invoke(
            IDENTITY, {"event": event, "candidates": candidates}, versions, session, DuplicateDecision
        )

    async def ready(self):
        await self.vectors.ready()

    async def embed(self, title, summary):
        return await self.vectors.embed(title, summary)

    async def recall(self, vector, article_key):
        return await self.vectors.recall(vector, article_key)

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
