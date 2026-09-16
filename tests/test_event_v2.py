"""Document Workflow behavior at the archive/model/vector persistence seam."""

import os
import tempfile
import unittest
from unittest.mock import patch

from agno.db.sqlite import SqliteDb
from agno.registry import Registry
from agno.run import RunContext
from agno.workflow import StepInput, Workflow

from capabilities.collection import Candidate
from capabilities.collection_v2 import archived_article_keys
from capabilities.collection_v2.internal.storage import archive_candidate
from capabilities.event_v2 import DocumentEventDraft, DuplicateDecision, configure_document_event_runtime
from capabilities.event_v2.functions import (
    DOCUMENT_EVENT_FUNCTIONS,
    extract_next_document_event,
    prepare_document_events,
    summarize_document_events,
)
from capabilities.event_v2.internal.storage import path, read
from workflows.event_extraction_v2 import ensure_document_event_workflow


class Store:
    def publish_markdown(self, **kwargs):
        pass


class Runtime:
    def __init__(self):
        self.staged = {}
        self.inputs = []
        self.decisions = 0
        self.same = set()
        self.bad = set()
        self.fail_stage = False
        self.bad_identity = False

    async def ready(self):
        pass

    def versions(self):
        return {"document-event-extractor": 1, "document-event-identity": 1}

    async def extract(self, source, versions, session):
        self.inputs.append(source)
        key = source["article_key"]
        if key in self.bad:
            raise ValueError("model failed")
        return DocumentEventDraft(
            title="同一公告" if key in self.same else source["title"],
            summary=source["content"],
            semantic=[],
            keywords=["收入100亿元"],
        )

    async def embed(self, title, summary):
        return {"values": [1.0, 0.0], "title": title, "model": "test", "version": "title-summary.v1", "hash": "test"}

    async def recall(self, vector, article_key):
        return [
            {
                "candidate_id": e["candidate_id"],
                "title": e["event"]["title"],
                "summary": e["event"]["summary"],
                "semantic": [],
                "score": 0.99,
            }
            for e in self.staged.values()
            if e["article_key"] != article_key
        ]

    async def decide(self, event, candidates, versions, session):
        self.decisions += 1
        if self.bad_identity:
            return DuplicateDecision(duplicate=True, matched_id="invented", reason="invalid selection")
        match = next((c for c in candidates if c["title"] == event["title"]), None)
        return DuplicateDecision(
            duplicate=bool(match), matched_id=match["candidate_id"] if match else None, reason="比较整篇事实"
        )

    async def stage(self, event, vector):
        if self.fail_stage:
            raise ValueError("storage unavailable")
        self.staged[event["candidate_id"]] = event


class EventV2Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "COLLECTOR_V2_ARTIFACT_ROOT": self.temp.name + "/raw",
                "EVENT_V2_ARTIFACT_ROOT": self.temp.name + "/event",
            },
        )
        self.env.start()
        self.runtime = Runtime()
        configure_document_event_runtime(self.runtime)

    def tearDown(self):
        configure_document_event_runtime(None)
        self.env.stop()
        self.temp.cleanup()

    def archive(self, count):
        for i in range(count):
            article = Candidate(
                candidate_id=f"candidate-{i}",
                connector="test",
                query="query",
                title=f"Article {i}",
                url=f"https://example.com/{i}",
                content=f"完整原文{i}：收入100亿元。" + ("文本" * 300),
                source_name="Test",
                source_level="L1_OFFICIAL",
                published_at=None,
                collected_at="2026-09-16T00:00:00Z",
            )
            archive_candidate(article, Store())
        return archived_article_keys()

    async def execute(self, run="test-run", input="next"):
        ctx = RunContext(run_id=run, session_id=run, session_state={})
        prepared = await prepare_document_events(StepInput(input=input), ctx)
        if prepared.stop:
            return prepared.content
        for _ in range(prepared.content["selected"]):
            await extract_next_document_event(StepInput(), ctx)
        return summarize_document_events(StepInput(), ctx).content

    async def test_twenty_cap_full_text_duplicate_and_next_batch(self):
        keys = self.archive(22)
        self.runtime.same = set(keys[:2])
        self.runtime.bad = {keys[2]}
        result = await self.execute()
        self.assertEqual(
            (result["selected"], result["accepted"], result["duplicates"], result["failed"]), (20, 18, 1, 1)
        )
        self.assertEqual(len(self.runtime.inputs), 20)
        self.assertTrue(all(len(i["content"]) > 600 for i in self.runtime.inputs))
        staged = next(iter(self.runtime.staged.values()))
        self.assertIsNone(staged["published_at"])
        self.assertEqual(staged["event"]["semantic"], [])
        self.assertFalse(result["published"])
        next_result = await self.execute("next-run")
        self.assertEqual(next_result["selected"], 2)
        self.assertEqual((await self.execute("empty-run"))["selected"], 0)
        self.assertIsNone(read(path("articles", keys[1], "result"))["candidate_id"])

    async def test_checkpoint_retry_does_not_reextract_or_rejudge(self):
        key = self.archive(1)[0]
        self.runtime.fail_stage = True
        result = await self.execute()
        self.assertEqual(result["failed"], 1)
        frozen = read(path("articles", key, "event"))
        self.runtime.fail_stage = False
        result = await self.execute("retry", {"retry_failed": True})
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(len(self.runtime.inputs), 1)
        self.assertEqual(read(path("articles", key, "event")), frozen)
        self.assertEqual(len(self.runtime.staged), 1)

    async def test_foreign_match_cannot_discard_article(self):
        keys = self.archive(2)
        self.runtime.bad_identity = True
        result = await self.execute()
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(read(path("articles", keys[1], "result"))["stage"], "identity")

    async def test_workflow_loop_and_studio_roundtrip(self):
        self.archive(3)
        db = SqliteDb(db_file=self.temp.name + "/studio.db")
        registry = Registry(functions=DOCUMENT_EVENT_FUNCTIONS, dbs=[db])
        with patch("workflows.event_extraction_v2.get_postgres_db", return_value=db):
            version = ensure_document_event_workflow(registry)
            self.assertEqual(ensure_document_event_workflow(registry), version)
            workflow = Workflow.load("event-extraction-v2", db=db, registry=registry)
        self.assertIsNotNone(workflow)
        self.assertEqual(workflow.name, "事件提取")
        result = await workflow.arun("next", session_id="native-loop")
        self.assertEqual(result.content["accepted"], 3)
        self.assertEqual(len(self.runtime.inputs), 3)

    async def test_repeated_run_and_concurrent_selection_are_idempotent(self):
        self.archive(1)
        a = RunContext(run_id="a", session_id="a", session_state={})
        b = RunContext(run_id="b", session_id="b", session_state={})
        await prepare_document_events(StepInput(input="next"), a)
        await prepare_document_events(StepInput(input="next"), b)
        import asyncio

        outputs = await asyncio.gather(
            extract_next_document_event(StepInput(), a), extract_next_document_event(StepInput(), b)
        )
        self.assertEqual({o.content["status"] for o in outputs}, {"accepted", "already_processed"})
        self.assertEqual(len(self.runtime.inputs), 1)

    async def test_rest_and_mcp_transport(self):
        import asyncio
        import json
        import socket

        import httpx
        import uvicorn
        from agno.os import AgentOS
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        self.archive(2)
        db = SqliteDb(db_file=self.temp.name + "/api.db")
        registry = Registry(functions=DOCUMENT_EVENT_FUNCTIONS, dbs=[db])
        with patch("workflows.event_extraction_v2.get_postgres_db", return_value=db):
            ensure_document_event_workflow(registry)
        os_app = AgentOS(
            workflows=[], db=db, registry=registry, mcp_server=True, tracing=False, scheduler=False, authorization=False
        )
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(os_app.get_app(), log_level="error", lifespan="on"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(15):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
                response = await client.get("/components", params={"component_type": "workflow"})
                response.raise_for_status()
                self.assertEqual(
                    next(c for c in response.json()["data"] if c["component_id"] == "event-extraction-v2")["name"],
                    "事件提取",
                )
                response = await client.post(
                    "/workflows/event-extraction-v2/runs", data={"message": "next", "stream": "false"}
                )
                response.raise_for_status()
                self.assertEqual(response.json()["content"]["accepted"], 2)
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (reader, writer, *_):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    response = await session.call_tool("get_agentos_config", {})
                    self.assertIn("event-extraction-v2", response.content[0].text)
                    response = await session.call_tool(
                        "run_workflow", {"workflow_id": "event-extraction-v2", "message": "next"}
                    )
                    self.assertFalse(response.isError)
                    self.assertEqual(json.loads(response.content[0].text)["selected"], 0)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 15)
            listener.close()


if __name__ == "__main__":
    unittest.main()
