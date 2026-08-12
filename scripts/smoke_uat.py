"""Authenticated in-container smoke probe for a deployed UAT AgentOS."""

from __future__ import annotations

import asyncio
import time
import uuid

import httpx
from agno.db.schemas.service_accounts import ServiceAccount
from agno.os.service_accounts import generate_token

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
        schedule_names = {item["name"] for item in schedules["data"]}
        required_agents = {"tidewise-assistant", "raw-collector", "evidence-extractor"}
        required_workflows = {"local-ping", "raw-collection", "evidence-extraction"}
        required_schedules = {"raw-collection-hourly", "evidence-extraction-every-10-minutes"}
        if not required_agents <= agent_ids:
            raise RuntimeError(f"missing Agents: {sorted(required_agents - agent_ids)}")
        if not required_workflows <= workflow_ids:
            raise RuntimeError(f"missing Workflows: {sorted(required_workflows - workflow_ids)}")
        if not required_schedules <= schedule_names:
            raise RuntimeError(f"missing Schedules: {sorted(required_schedules - schedule_names)}")

        ping = await client.post(
            "/workflows/local-ping/runs",
            data={"message": "UAT deployment smoke", "stream": "false", "background": "false"},
        )
        ping.raise_for_status()
        if ping.json().get("status") != "COMPLETED":
            raise RuntimeError("local-ping did not complete")

        mcp = await client.post(
            "/mcp",
            headers={**headers, "Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "uat-smoke", "version": "1.0"},
                },
            },
        )
        mcp.raise_for_status()


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
