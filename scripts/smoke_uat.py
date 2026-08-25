"""Authenticated in-container smoke probe for a deployed UAT AgentOS."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import httpx
from agno.db.schemas.service_accounts import ServiceAccount
from agno.os.service_accounts import generate_token
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent

from app.schedules import (
    EVENT_EXTRACTION_SCHEDULE_ENDPOINT,
    EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT,
    RAW_COLLECTION_SCHEDULE_ENDPOINT,
)
from db import get_postgres_db

BASE_URL = "http://127.0.0.1:9081"

# Least-privilege scopes for the deployment probe. Agno's default service-account
# scopes allow runs but intentionally do not allow component or schedule listing.
UAT_SMOKE_SERVICE_ACCOUNT_SCOPES = [
    "agents:read",
    "workflows:read",
    "workflows:run",
    "schedules:read",
    "config:read",
]


async def _probe(token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=BASE_URL, headers=headers, timeout=20.0) as client:
        agents = (await client.get("/agents")).raise_for_status().json()
        workflows = (await client.get("/workflows")).raise_for_status().json()
        schedules = (await client.get("/schedules", params={"limit": 100, "page": 1})).raise_for_status().json()
        agent_ids = {item["id"] for item in agents}
        workflow_ids = {item["id"] for item in workflows}
        schedule_endpoints = [item["endpoint"] for item in schedules["data"]]
        required_agents = {
            "tidewise-assistant",
            "raw-collector",
            "evidence-extractor",
            "event-extractor",
        }
        required_workflows = {
            "local-ping",
            "raw-collection",
            "evidence-extraction",
            "event-extraction",
        }
        required_schedule_endpoints = {
            RAW_COLLECTION_SCHEDULE_ENDPOINT,
            EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT,
            EVENT_EXTRACTION_SCHEDULE_ENDPOINT,
        }
        if not required_agents <= agent_ids:
            raise RuntimeError(f"missing Agents: {sorted(required_agents - agent_ids)}")
        if not required_workflows <= workflow_ids:
            raise RuntimeError(f"missing Workflows: {sorted(required_workflows - workflow_ids)}")
        missing_endpoints = required_schedule_endpoints - set(schedule_endpoints)
        if missing_endpoints:
            raise RuntimeError(f"missing Schedule endpoints: {sorted(missing_endpoints)}")
        duplicate_endpoints = sorted(
            endpoint for endpoint in required_schedule_endpoints if schedule_endpoints.count(endpoint) > 1
        )
        if duplicate_endpoints:
            raise RuntimeError(f"duplicate Schedule endpoints: {duplicate_endpoints}")

        ping = await client.post(
            "/workflows/local-ping/runs",
            data={"message": "UAT deployment smoke", "stream": "false", "background": "false"},
        )
        ping.raise_for_status()
        if ping.json().get("status") != "COMPLETED":
            raise RuntimeError("local-ping did not complete")

    async with streamablehttp_client(f"{BASE_URL}/mcp", headers=headers, timeout=20) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            config_result = await session.call_tool("get_agentos_config", {})
            if not config_result.content or not isinstance(config_result.content[0], TextContent):
                raise RuntimeError("MCP AgentOS config response is not text")
            config = json.loads(config_result.content[0].text)
            mcp_agent_ids = {item["id"] for item in config["agents"]}
            mcp_workflow_ids = {item["id"] for item in config["workflows"]}
            if not required_agents <= mcp_agent_ids:
                raise RuntimeError(f"MCP missing Agents: {sorted(required_agents - mcp_agent_ids)}")
            if not required_workflows <= mcp_workflow_ids:
                raise RuntimeError(f"MCP missing Workflows: {sorted(required_workflows - mcp_workflow_ids)}")


async def main() -> None:
    db = get_postgres_db()
    plaintext, token_hash, token_prefix = generate_token()
    now = int(time.time())
    account = ServiceAccount(
        id=str(uuid.uuid4()),
        name=f"uat-deploy-smoke-{now}",
        token_hash=token_hash,
        token_prefix=token_prefix,
        scopes=list(UAT_SMOKE_SERVICE_ACCOUNT_SCOPES),
        created_at=now,
        expires_at=now + 300,
        created_by="uat-deploy",
        user_id=None,
    )
    db.create_service_account(account.to_dict())
    try:
        await _probe(plaintext)
    finally:
        db.delete_service_account(account.id)
    print("PASS authenticated-agentos-smoke")


if __name__ == "__main__":
    asyncio.run(main())
