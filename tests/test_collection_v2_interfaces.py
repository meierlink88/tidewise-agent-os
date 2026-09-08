"""Real REST/MCP transports against an isolated AgentOS with fake external I/O."""

import asyncio
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.os import AgentOS
from agno.registry import Registry
from agno.workflow import Workflow
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from test_collection_v2 import MemoryStore, StaticAdapter, candidate, channel

from capabilities.collection import AdapterKey
from capabilities.collection_v2.functions import collect_raw_v2, publish_raw_v2
from workflows.raw_collection_v2 import ensure_raw_collection_v2_workflow, raw_collection_v2


class InterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_rest_and_mcp_execute_the_new_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db = SqliteDb(db_file=str(Path(temporary) / "interface.db"))
            registry = Registry(functions=[collect_raw_v2, publish_raw_v2], dbs=[db])
            config = raw_collection_v2.to_dict()
            config["db"] = db.to_dict()
            workflow = Workflow.from_dict(config, db=db, registry=registry, strict=True)
            self.assertIsInstance(workflow.db, SqliteDb)
            with (
                patch("workflows.raw_collection_v2.get_postgres_db", return_value=db),
                patch.object(raw_collection_v2, "db", db),
            ):
                first = ensure_raw_collection_v2_workflow(registry)
                self.assertEqual(ensure_raw_collection_v2_workflow(registry), first)
                workflow.name = "Operator collection name"
                edited_version = workflow.save(db=db, stage="published")
                self.assertEqual(ensure_raw_collection_v2_workflow(registry), edited_version)
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
            adapter = StaticAdapter([candidate()])
            store = MemoryStore()
            with (
                patch.dict(os.environ, {"COLLECTOR_V2_ARTIFACT_ROOT": str(Path(temporary) / "raw")}),
                patch(
                    "capabilities.collection_v2.internal.acquisition.load_active_source_snapshot",
                    return_value=[channel()],
                ),
                patch(
                    "capabilities.collection_v2.internal.acquisition.SOURCE_ADAPTERS", {AdapterKey.GENERIC_RSS: adapter}
                ),
                patch(
                    "capabilities.collection_v2.functions.collection.configured_raw_document_store", return_value=store
                ),
                patch.object(Agent, "arun", side_effect=AssertionError("Model call forbidden")),
            ):
                task = asyncio.create_task(server.serve(sockets=[listener]))
                try:
                    async with asyncio.timeout(15):
                        while not server.started:
                            if task.done():
                                await task
                            await asyncio.sleep(0.01)
                    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
                        listing = await client.get("/components", params={"component_type": "workflow"})
                        listing.raise_for_status()
                        rows = listing.json()["data"]
                        component = next(item for item in rows if item["component_id"] == "raw-collection-v2")
                        self.assertEqual(component["name"], "Operator collection name")
                        self.assertEqual(component["current_version"], edited_version)
                        response = await client.post(
                            "/workflows/raw-collection-v2/runs", data={"message": "原查询", "stream": "false"}
                        )
                        response.raise_for_status()
                        payload = response.json()
                        self.assertEqual(payload["content"]["archived"], 1)
                        self.assertEqual(payload["content"]["outcome"], "completed")
                        self.assertEqual(len(payload["step_results"]), 2)
                    async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            config_result = await session.call_tool("get_agentos_config", {})
                            assert isinstance(config_result.content[0], TextContent)
                            visible = json.loads(config_result.content[0].text)
                            self.assertIn("raw-collection-v2", {item["id"] for item in visible["workflows"]})
                            result = await session.call_tool(
                                "run_workflow",
                                {
                                    "workflow_id": "raw-collection-v2",
                                    "message": "原查询",
                                },
                            )
                            self.assertFalse(result.isError, result)
                            assert isinstance(result.content[0], TextContent)
                            self.assertEqual(json.loads(result.content[0].text)["duplicates"], 1)
                    self.assertEqual(len(store.calls), 1)
                    self.assertEqual(len(list((Path(temporary) / "raw" / "runs").glob("*/manifest.json"))), 2)
                finally:
                    server.should_exit = True
                    await asyncio.wait_for(task, timeout=15)
                    listener.close()


if __name__ == "__main__":
    unittest.main()
