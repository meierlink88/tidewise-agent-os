"""Published Workflow migration and real REST/MCP transports with isolated storage."""

import asyncio
import json
import os
import socket
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import uvicorn
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.os import AgentOS
from agno.registry import Registry
from agno.workflow import Step, Workflow
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from test_geopolitical_research import END, REPORT, FakeResearch, row

from capabilities.geopolitical_research.functions import (
    geopolitical_research_complete,
    research_next_geopolitical_story,
    select_geopolitical_stories,
)
from workflows.investment_reasoning import ensure_investment_reasoning_workflow


def old_executor(step_input):
    raise AssertionError("Old investment pipeline must never execute")


FUNCTIONS: list[Callable[..., Any]] = [
    select_geopolitical_stories,
    research_next_geopolitical_story,
    geopolitical_research_complete,
    old_executor,
]


def result_dict(value):
    """Agno Loop outputs may be nested in a list depending on the transport."""
    if isinstance(value, dict) and value.get("schema_version") == "geopolitical-research-result/v1":
        return value
    if isinstance(value, dict):
        for child in value.values():
            found = result_dict(child)
            if found:
                return found
    if isinstance(value, list):
        for child in reversed(value):
            found = result_dict(child)
            if found:
                return found
    return None


class InterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_published_migration_and_rest_mcp_execute_same_workflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = SqliteDb(db_file=str(Path(temporary) / "interface.db"))
            registry = Registry(functions=FUNCTIONS, dbs=[db])
            previous = Workflow(
                id="investment-reasoning",
                name="Investment Reasoning",
                db=db,
                metadata={"investment_reasoning_contract_version": 12, "operator_tag": "preserve"},
                steps=[Step(name="old", executor=old_executor)],
            )
            old_version = previous.save(db=db, stage="published")
            assert isinstance(old_version, int)
            with patch("workflows.investment_reasoning.get_postgres_db", return_value=db):
                new_version = ensure_investment_reasoning_workflow(registry)
                self.assertGreater(new_version, old_version)
                self.assertEqual(ensure_investment_reasoning_workflow(registry), new_version)
            current = Workflow.load("investment-reasoning", db=db, registry=registry, version=new_version)
            assert current is not None
            assert isinstance(current.steps, list)
            assert isinstance(current.metadata, dict)
            self.assertEqual(current.name, "地缘冲突研究")
            self.assertEqual(len(current.steps), 2)
            self.assertEqual(current.metadata["operator_tag"], "preserve")
            self.assertEqual(current.to_dict()["steps"][1]["type"], "Loop")
            historical = Workflow.load("investment-reasoning", db=db, registry=registry, version=old_version)
            assert historical is not None
            self.assertEqual(historical.name, "Investment Reasoning")
            agent_os = AgentOS(
                workflows=[],
                db=db,
                registry=registry,
                mcp_server=True,
                tracing=False,
                scheduler=False,
                authorization=False,
            )
            app = agent_os.get_app()
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
            fake = FakeResearch()
            with (
                patch.dict(
                    os.environ,
                    {
                        "GEOPOLITICAL_RESEARCH_ARTIFACT_ROOT": str(Path(temporary) / "artifacts"),
                        "TIDEWISE_RESEARCH_BASE_URL": "http://research.test",
                    },
                ),
                patch("capabilities.geopolitical_research.internal.execution.ResearchClient", fake.client),
                patch(
                    "capabilities.geopolitical_research.internal.selection.load_geopolitical_event_window",
                    AsyncMock(return_value=[row(), row("GPR-2", "EVT-2")]),
                ),
                patch.object(Agent, "arun", side_effect=AssertionError("Model call forbidden")),
            ):
                task = asyncio.create_task(server.serve(sockets=[listener]))
                try:
                    async with asyncio.timeout(20):
                        while not server.started:
                            if task.done():
                                await task
                            await asyncio.sleep(0.01)
                    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
                        listing = await client.get("/components", params={"component_type": "workflow"})
                        listing.raise_for_status()
                        component = next(
                            x for x in listing.json()["data"] if x["component_id"] == "investment-reasoning"
                        )
                        self.assertEqual(component["name"], "地缘冲突研究")
                        response = await client.post(
                            "/workflows/investment-reasoning/runs",
                            data={
                                "message": json.dumps({"cutoff_at": END.isoformat()}),
                                "stream": "false",
                            },
                        )
                        response.raise_for_status()
                        payload = response.json()
                        result = result_dict(payload["content"])
                        self.assertIsNotNone(result, payload)
                        self.assertEqual(result["outcome"], "completed")
                        self.assertEqual(result["completed"], 2)
                        self.assertEqual(Path(result["items"][0]["report_path"]).read_bytes(), REPORT.encode())
                        self.assertEqual(len(payload["step_results"]), 2)
                    async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as transport:
                        async with ClientSession(transport[0], transport[1]) as session:
                            await session.initialize()
                            result = await session.call_tool(
                                "run_workflow",
                                {
                                    "workflow_id": "investment-reasoning",
                                    "message": json.dumps({"cutoff_at": END.isoformat()}),
                                },
                            )
                            self.assertFalse(result.is_error, result)
                            assert isinstance(result.content[0], TextContent)
                            content = result_dict(json.loads(result.content[0].text))
                            self.assertIsNotNone(content, result)
                            self.assertEqual(content["outcome"], "completed")
                            self.assertTrue(content["items"][0]["reused"])
                    self.assertEqual(len(fake.creates), 2)
                    self.assertEqual(len(list((Path(temporary) / "artifacts/runs").glob("*/result.json"))), 2)
                finally:
                    server.should_exit = True
                    await asyncio.wait_for(task, timeout=15)
                    listener.close()


if __name__ == "__main__":
    unittest.main()
