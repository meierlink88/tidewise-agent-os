"""Authenticated in-container smoke probe for a deployed UAT AgentOS."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import timedelta

import httpx
from agno.db.schemas.service_accounts import ServiceAccount
from agno.os.service_accounts import generate_token
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent

from app.schedules import (
    EVENT_EXTRACTION_SCHEDULE_ENDPOINT,
    EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT,
    INVESTMENT_REASONING_SCHEDULE_ENDPOINT,
    RAW_COLLECTION_SCHEDULE_ENDPOINT,
)
from db import get_postgres_db

BASE_URL = "http://127.0.0.1:9081"

# Least-privilege scopes for the deployment probe. Agno's default service-account
# scopes allow runs but intentionally do not allow component or schedule listing.
UAT_SMOKE_SERVICE_ACCOUNT_SCOPES = [
    "agents:read",
    "agents:run",
    "workflows:read",
    "workflows:run",
    "config:read",
    "registry:read",
]

# System Schedules are deliberately unowned. Agno service accounts always
# self-scope unless they are admins, so use a separate, short-lived principal
# for this operator-level read instead of widening the workflow smoke account.
UAT_SCHEDULE_PROBE_SERVICE_ACCOUNT_SCOPES = ["agent_os:admin"]

EXPECTED_AGENT_MODEL_ID = "gpt-5.6-sol"
RETIRED_AGENT_ID = "investment-planner"
REQUIRED_GPT_AGENT_IDS = {
    "tidewise-assistant",
    "title-curator",
    "evidence-extractor",
    "event-extractor",
    "event-identity",
    "event-signal-analyst",
    "investment-reasoner",
    "investment-report-writer",
    "investment-reviewer",
}


def _is_expected_agent_model(model: object) -> bool:
    """Match the model summary shape returned by Agno's REST Agent list."""
    return isinstance(model, dict) and (
        model.get("name") == "OpenAIResponses"
        and model.get("model") == EXPECTED_AGENT_MODEL_ID
        and model.get("provider") == "OpenAI"
    )


async def _probe(token: str, schedule_token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=BASE_URL, headers=headers, timeout=20.0) as client:
        agents = (await client.get("/agents")).raise_for_status().json()
        workflows = (await client.get("/workflows")).raise_for_status().json()
        agent_ids = {item["id"] for item in agents}
        workflow_ids = {item["id"] for item in workflows}
        required_agents = REQUIRED_GPT_AGENT_IDS
        required_workflows = {
            "local-ping",
            "raw-collection",
            "evidence-extraction",
            "event-extraction",
            "investment-reasoning",
        }
        required_schedule_endpoints = {
            RAW_COLLECTION_SCHEDULE_ENDPOINT,
            EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT,
            EVENT_EXTRACTION_SCHEDULE_ENDPOINT,
            INVESTMENT_REASONING_SCHEDULE_ENDPOINT,
        }
        if not required_agents <= agent_ids:
            raise RuntimeError(f"missing Agents: {sorted(required_agents - agent_ids)}")
        if RETIRED_AGENT_ID in agent_ids:
            raise RuntimeError(f"retired Agent is still active: {RETIRED_AGENT_ID}")
        agents_by_id = {item["id"]: item for item in agents}
        for agent_id in sorted(required_agents):
            if not _is_expected_agent_model(agents_by_id[agent_id].get("model")):
                raise RuntimeError(f"Agent {agent_id} is not using OpenAI {EXPECTED_AGENT_MODEL_ID}")
        if not required_workflows <= workflow_ids:
            raise RuntimeError(f"missing Workflows: {sorted(required_workflows - workflow_ids)}")

        registry = (
            (await client.get("/registry", params={"resource_type": "model", "limit": 100, "page": 1}))
            .raise_for_status()
            .json()
        )
        registered_models = {item["name"]: item for item in registry["data"]}
        sol = registered_models.get(EXPECTED_AGENT_MODEL_ID)
        if sol is None:
            raise RuntimeError(f"registry is missing model: {EXPECTED_AGENT_MODEL_ID}")
        sol_metadata = sol.get("metadata") or {}
        if (
            sol_metadata.get("class_path") != "agno.models.openai.responses.OpenAIResponses"
            or sol_metadata.get("provider") != "OpenAI"
            or sol_metadata.get("model_id") != EXPECTED_AGENT_MODEL_ID
        ):
            raise RuntimeError(f"registry model metadata is invalid: {EXPECTED_AGENT_MODEL_ID}")
        if "deepseek-v4-flash" not in registered_models:
            raise RuntimeError("registry is missing the Graphiti DeepSeek model")

        schedule_headers = {"Authorization": f"Bearer {schedule_token}"}
        schedules = (
            (
                await client.get(
                    "/schedules",
                    params={"limit": 100, "page": 1},
                    headers=schedule_headers,
                )
            )
            .raise_for_status()
            .json()
        )
        schedule_endpoints = [item["endpoint"] for item in schedules["data"]]
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
            if RETIRED_AGENT_ID in mcp_agent_ids:
                raise RuntimeError(f"MCP exposes retired Agent: {RETIRED_AGENT_ID}")
            if not required_workflows <= mcp_workflow_ids:
                raise RuntimeError(f"MCP missing Workflows: {sorted(required_workflows - mcp_workflow_ids)}")
            run_result = await session.call_tool(
                "run_agent",
                {
                    "agent_id": "tidewise-assistant",
                    "message": "Reply with exactly UAT_GPT_OK and nothing else.",
                },
                read_timeout_seconds=timedelta(seconds=120),
            )
            if run_result.isError:
                raise RuntimeError("GPT Agent smoke returned an MCP error")
            if not run_result.content or not isinstance(run_result.content[0], TextContent):
                raise RuntimeError("GPT Agent smoke response is not text")
            if run_result.content[0].text.strip() != "UAT_GPT_OK":
                raise RuntimeError("GPT Agent smoke returned an unexpected response")
            structured = run_result.structuredContent
            if not isinstance(structured, dict) or structured.get("status") != "COMPLETED":
                raise RuntimeError("GPT Agent smoke did not complete")


async def main() -> None:
    db = get_postgres_db()
    now = int(time.time())
    accounts: list[ServiceAccount] = []
    tokens: list[str] = []
    try:
        for name, scopes in (
            (f"uat-deploy-smoke-{now}", UAT_SMOKE_SERVICE_ACCOUNT_SCOPES),
            (f"uat-schedule-probe-{now}", UAT_SCHEDULE_PROBE_SERVICE_ACCOUNT_SCOPES),
        ):
            plaintext, token_hash, token_prefix = generate_token()
            account = ServiceAccount(
                id=str(uuid.uuid4()),
                name=name,
                token_hash=token_hash,
                token_prefix=token_prefix,
                scopes=list(scopes),
                created_at=now,
                expires_at=now + 300,
                created_by="uat-deploy",
                user_id=None,
            )
            db.create_service_account(account.to_dict())
            accounts.append(account)
            tokens.append(plaintext)
        await _probe(tokens[0], tokens[1])
    finally:
        for account in accounts:
            db.delete_service_account(account.id)
    print("PASS authenticated-agentos-smoke")


if __name__ == "__main__":
    asyncio.run(main())
