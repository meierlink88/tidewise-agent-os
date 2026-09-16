"""Neo4j candidate recall and staged vectors; no formal Event publication."""

import hashlib
import json
import math

from sematica.ingestion.episcode.event.provenance import EVENT_SOURCE_DESCRIPTION
from sematica.projection.runtime import GRAPHITI_GROUP_ID

INDEX = "document_event_embedding_v1"
VERSION = "title-summary.v1"


def embedding_text(title: str, summary: str) -> str:
    return f"{title.strip()}\n\n{summary.strip()}"


class EventVectorStore:
    def __init__(self, graphiti, *, model: str, dimension: int, group_id: str = GRAPHITI_GROUP_ID):
        self.group_id = group_id
        self.graphiti = graphiti
        self.model = model
        self.dimension = dimension

    async def _query(self, query: str, **kwargs):
        rows, _, _ = await self.graphiti.driver.execute_query(query, **kwargs)
        return rows

    async def ensure_index(self) -> None:
        await self._query(
            "CREATE CONSTRAINT document_event_candidate_id IF NOT EXISTS "
            "FOR (n:EventExtractionCandidate) REQUIRE n.uuid IS UNIQUE"
        )
        await self._query(
            f"CREATE VECTOR INDEX {INDEX} IF NOT EXISTS FOR (n:EventVector) ON n.event_embedding "
            "OPTIONS {indexConfig: {`vector.dimensions`: $dimension, `vector.similarity_function`: 'cosine'}}",
            dimension=self.dimension,
        )
        await self._query("CALL db.awaitIndex($name, 60)", name=INDEX)
        rows = await self._query("SHOW VECTOR INDEXES YIELD name, options WHERE name=$name RETURN options", name=INDEX)
        config = rows[0]["options"]["indexConfig"] if rows else {}
        if (
            config.get("vector.dimensions") != self.dimension
            or str(config.get("vector.similarity_function", "")).lower() != "cosine"
        ):
            raise ValueError("Event vector index configuration mismatch")

    async def ready(self) -> None:
        await self.ensure_index()
        rows = await self._query(
            "MATCH (n:EventExtractionCandidate {group_id:$group}) "
            "WHERE NOT n:EventVector OR n.event_embedding IS NULL OR size(n.event_embedding)<>$dimension "
            "OR n.event_embedding_model IS NULL OR n.event_embedding_model<>$model "
            "OR n.event_embedding_version IS NULL OR n.event_embedding_version<>$version "
            "RETURN count(n) AS missing",
            group=self.group_id,
            dimension=self.dimension,
            model=self.model,
            version=VERSION,
        )
        if rows[0]["missing"]:
            raise ValueError("Staged Event vectors use an incompatible model/version")

    async def embed(self, title: str, summary: str) -> dict:
        text = embedding_text(title, summary)
        values = await self.graphiti.embedder.create(text)
        if len(values) != self.dimension or not all(math.isfinite(v) for v in values) or not any(values):
            raise ValueError("Event embedding has invalid dimensions or values")
        return {
            "values": values,
            "hash": hashlib.sha256(text.encode()).hexdigest(),
            "model": self.model,
            "version": VERSION,
        }

    def _validate_vector(self, vector: dict) -> None:
        values = vector.get("values", [])
        if vector.get("model") != self.model or vector.get("version") != VERSION:
            raise ValueError("Frozen Event vector uses an incompatible model/version")
        if len(values) != self.dimension or not all(math.isfinite(v) for v in values) or not any(values):
            raise ValueError("Frozen Event vector has invalid dimensions or values")

    async def recall(self, vector: dict, article_key: str) -> list[dict]:
        self._validate_vector(vector)
        rows = await self._query(
            f"CALL db.index.vector.queryNodes('{INDEX}', 100, $vector) YIELD node AS n, score "
            "WHERE n.group_id=$group AND n.event_embedding_model=$model AND n.event_embedding_version=$version "
            "AND coalesce(n.article_key,'')<>$key "
            "AND (n:EventExtractionCandidate OR (n:Episodic AND n.source_description=$source)) "
            "RETURN n.uuid AS candidate_id,n.event_title AS title,n.event_summary AS summary,"
            "n.event_semantic_json AS semantic_json,score ORDER BY score DESC LIMIT 30",
            vector=vector["values"],
            group=self.group_id,
            model=self.model,
            version=VERSION,
            key=article_key,
            source=EVENT_SOURCE_DESCRIPTION,
        )
        return [
            {
                "candidate_id": r["candidate_id"],
                "title": r["title"],
                "summary": r["summary"],
                "semantic": json.loads(r["semantic_json"] or "[]"),
                "score": r["score"],
            }
            for r in rows
        ]

    async def stage(self, event: dict, vector: dict) -> None:
        self._validate_vector(vector)
        draft = event["event"]
        if vector["hash"] != hashlib.sha256(embedding_text(draft["title"], draft["summary"]).encode()).hexdigest():
            raise ValueError("Frozen Event vector content hash mismatch")
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
        rows = await self._query(
            "MERGE (n:EventExtractionCandidate {uuid:$id}) "
            "ON CREATE SET n.payload=$payload,n.created_at=datetime() "
            "WITH n WHERE n.payload=$payload "
            "SET n:EventVector,n.group_id=$group,n.article_key=$key,n.event_title=$title,"
            "n.event_summary=$summary,n.event_semantic_json=$semantic,n.event_embedding=$vector,"
            "n.event_embedding_hash=$hash,n.event_embedding_model=$model,n.event_embedding_version=$version "
            "RETURN n.uuid AS uuid",
            id=event["candidate_id"],
            payload=payload,
            group=self.group_id,
            key=event["article_key"],
            title=draft["title"],
            summary=draft["summary"],
            semantic=json.dumps(draft["semantic"], ensure_ascii=False),
            vector=vector["values"],
            hash=vector["hash"],
            model=self.model,
            version=VERSION,
        )
        if not rows:
            raise ValueError("Immutable staged Event conflict")
