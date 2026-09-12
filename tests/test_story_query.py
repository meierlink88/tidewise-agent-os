"""Contract tests for live daily storyline queries and public transports."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import Client, FastMCP

from app.story_queries import router
from capabilities.event.internal import story_query as q
from capabilities.event.tools.story_query import get_story_evidence, query_story_events

STORY = "GPRtest"
DAY = "2026-09-12"


def event(eid):
    return {
        "event": {
            "domain_object_id": eid,
            "created_at": DAY,
            "content": json.dumps({"id": eid, "title": eid, "summary": "fact", "semantic": {"modality": "FACT"}}),
        }
    }


class StoryQueryTests(unittest.TestCase):
    def test_day_is_shanghai_created_at_not_occurrence(self):
        window = q.query_window(STORY, DAY)
        self.assertEqual(window["start"], "2026-09-12T00:00:00+08:00")
        self.assertEqual(window["end"], "2026-09-13T00:00:00+08:00")
        self.assertEqual(window["selection_time_field"], "created_at")
        for value in ("tomorrow", "2999-01-01", "20260912"):
            with self.assertRaises(ValueError):
                q.query_window(STORY, value)

    def test_keyset_pages_and_cross_page_signal_closure(self):
        signal = {"signal": {"uuid": "S1", "fact": "supported", "source_event_ids": ["E1", "E2"]}}
        reads = [
            [{"story": {"name": "story"}}],
            [{"total": 2}],
            [event("E1"), event("E2")],
            [{"id": "E1"}, {"id": "E2"}],
            [signal],
        ]
        with (
            patch.object(q, "_read", side_effect=reads) as read,
            patch.object(q, "_provenance", return_value=({"E1": {"evidence_ids": ["V1"]}}, {})),
        ):
            result = q.query_events(STORY, DAY, limit=5)
        self.assertEqual(read.call_args_list[2].args[1]["limit"], 2)
        self.assertEqual(result["next_after_event_id"], "E1")
        self.assertEqual(len(result["events"]), 1)
        self.assertTrue(result["variable_signals"][0]["usable_as_complete_fact"])
        self.assertIn("e.created_at < datetime($end)", read.call_args_list[1].args[0])
        self.assertIn("e.domain_object_id > $after", read.call_args_list[2].args[0])

    def test_mixed_source_signal_does_not_leak_historical_assertion(self):
        reads = [
            [{"story": {}}],
            [{"total": 1}],
            [event("E1")],
            [{"id": "E1"}],
            [{"signal": {"uuid": "S1", "fact": "historical assertion", "source_event_ids": ["E1", "OLD"]}}],
        ]
        with patch.object(q, "_read", side_effect=reads), patch.object(q, "_provenance", return_value=({"E1": {}}, {})):
            result = q.query_events(STORY, DAY)
        item = result["variable_signals"][0]
        self.assertFalse(item["usable_as_complete_fact"])
        self.assertEqual(item["missing_source_event_ids"], ["OLD"])
        self.assertNotIn("fact", item["signal"])

    def test_empty_day_distinct_from_unknown_story_and_outage(self):
        with patch.object(q, "_read", side_effect=[[{"story": {}}], [{"total": 0}], [], []]):
            self.assertEqual(q.query_events(STORY, DAY)["events"], [])
        with patch.object(q, "_read", return_value=[]), self.assertRaises(ValueError):
            q.query_events(STORY, DAY)
        with patch.object(q, "_read", side_effect=RuntimeError("offline")), self.assertRaises(RuntimeError):
            q.query_events(STORY, DAY)

    def test_evidence_scope(self):
        with patch.object(q, "_read", return_value=[]), self.assertRaises(ValueError):
            q.query_evidence(STORY, DAY, "OLD", "V1")
        with (
            patch.object(q, "_read", return_value=[{"id": "E1"}]),
            patch.object(q, "_provenance", return_value=({"E1": {"evidence_ids": ["V1"]}}, {"V1": {"id": "V1"}})),
        ):
            self.assertEqual(q.query_evidence(STORY, DAY, "E1", "V1")["evidence"]["id"], "V1")
            with self.assertRaises(ValueError):
                q.query_evidence(STORY, DAY, "E1", "UNRELATED")

    def test_journal_missing_and_pending_provenance(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"EVENT_ARTIFACT_ROOT": root}):
            batch = Path(root) / ".pending/batch"
            batch.mkdir(parents=True)
            (batch / "storyline_journal.json").write_text(
                json.dumps(
                    {
                        "candidates": {
                            "c": {
                                "publication": {"event_id": "E1"},
                                "done": False,
                                "identity_request": {"candidate": {"evidence_ids": ["V1"]}},
                            }
                        }
                    }
                )
            )
            (batch / "input.json").write_text(json.dumps({"evidences": [{"id": "V1", "summary": "source"}]}))
            refs, evidence = q._provenance({"E1", "E2"})
            self.assertFalse(refs["E1"]["publication_complete"])
            self.assertEqual(refs["E2"]["provenance_status"], "missing")
            self.assertEqual(evidence["V1"]["summary"], "source")

    def test_rest_auth_and_contract(self):
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client, patch.dict(os.environ, {"RUNTIME_ENV": "prd"}):
            self.assertEqual(
                client.get("/research/story-events", params={"story_id": STORY, "research_date": DAY}).status_code, 401
            )
        with (
            TestClient(app) as client,
            patch.dict(os.environ, {"RUNTIME_ENV": "dev"}),
            patch("capabilities.event.tools.story_query.query_events", return_value={"events": []}),
        ):
            response = client.get("/research/story-events", params={"story_id": STORY, "research_date": DAY})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"events": []})

    def test_rest_router_precedes_agentos_mcp_root_mount(self):
        from agno.agent import Agent
        from agno.os import AgentOS
        from agno.os.config import MCPServerConfig

        base = FastAPI()
        base.include_router(router)
        host = AgentOS(
            base_app=base,
            agents=[Agent(name="unused-test-agent")],
            authorization=False,
            mcp_server=MCPServerConfig(tools=[query_story_events, get_story_evidence]),
        )
        with (
            patch.dict(os.environ, {"RUNTIME_ENV": "dev"}),
            patch("capabilities.event.tools.story_query.query_events", return_value={"events": []}),
        ):
            with TestClient(host.get_app()) as client:
                response = client.get("/research/story-events", params={"story_id": STORY, "research_date": DAY})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"events": []})

    def test_mcp_schema_identity_injection_and_call(self):
        from agno.os.config import MCPServerConfig
        from agno.os.mcp import _register_custom_tools

        async def run():
            server = FastMCP("contract test")
            import inspect

            entries = [query_story_events, get_story_evidence]
            if "entries" in inspect.signature(_register_custom_tools).parameters:
                _register_custom_tools(server, entries)
            else:
                _register_custom_tools(server, MCPServerConfig(tools=entries))
            async with Client(server) as client:
                tools = await client.list_tools()
                self.assertEqual({t.name for t in tools}, {"query_story_events", "get_story_evidence"})
                self.assertTrue(all("user_id" not in t.inputSchema["properties"] for t in tools))
                with (
                    patch.dict(os.environ, {"RUNTIME_ENV": "dev"}),
                    patch("capabilities.event.tools.story_query.query_events", return_value={"events": [], "total": 0}),
                ):
                    result = await client.call_tool("query_story_events", {"story_id": STORY, "research_date": DAY})
                    self.assertFalse(result.is_error)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
