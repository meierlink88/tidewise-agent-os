"""
AgentOS Entrypoint
==================
"""

from contextlib import asynccontextmanager
from os import getenv
from pathlib import Path

from agno.os import AgentOS
from agno.utils.log import log_info

from agents.event_extractor import ensure_event_extractor_agent
from agents.evidence_extractor import ensure_evidence_extractor_agent
from agents.investment_planner import ensure_investment_planner_agent
from agents.investment_reasoner import ensure_investment_reasoner_agent, load_investment_reasoner_agent
from agents.investment_reviewer import ensure_investment_reviewer_agent, load_investment_reviewer_agent
from agents.raw_collector import ensure_collector_agent
from agents.tidewise_assistant import tidewise_assistant
from agents.title_curator import ensure_title_curator_agent
from app.registry import registry
from app.schedules import validate_schedules
from capabilities.event import configure_event_workflow_runtime, create_local_event_workflow_runtime
from capabilities.investment import (
    configure_investment_workflow_runtime,
    create_local_investment_workflow_runtime,
)
from db import get_postgres_db
from workflows.deployment_check import deployment_check
from workflows.event_extraction import ensure_event_extraction_workflow
from workflows.evidence_extraction import ensure_evidence_extraction_workflow
from workflows.investment_reasoning import ensure_investment_reasoning_workflow
from workflows.local_ping import local_ping
from workflows.raw_collection import ensure_raw_collection_workflow

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
runtime_env = getenv("RUNTIME_ENV", "prd")
# The external URL is advertised to remote clients and OAuth. The internal URL
# stays on loopback so Scheduler callbacks never leave the container.
agentos_external_url = getenv("AGENTOS_EXTERNAL_URL", "http://127.0.0.1:8000")
agentos_internal_url = getenv("AGENTOS_INTERNAL_URL", "http://127.0.0.1:8000")

# ---------------------------------------------------------------------------
# Interfaces
# - Tidewise Assistant becomes available on Slack when both env vars are set
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN = getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = getenv("SLACK_SIGNING_SECRET", "")

interfaces: list = []
if SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET:
    from agno.os.interfaces.slack import Slack

    interfaces.append(
        Slack(
            agent=tidewise_assistant,
            streaming=True,
            token=SLACK_BOT_TOKEN,
            signing_secret=SLACK_SIGNING_SECRET,
            resolve_user_identity=True,
            loading_text="处理中...",
        )
    )


# ---------------------------------------------------------------------------
# MCP OAuth — enabled by setting the MCP_CONNECT_SECRET environment variable.
# Connect your favorite AI apps and coding agents to a secure /mcp using OAuth.
# ---------------------------------------------------------------------------
MCP_CONNECT_SECRET = getenv("MCP_CONNECT_SECRET", "")

mcp_auth = None
if MCP_CONNECT_SECRET:
    from agno.os import AgentOSBuiltinAuth

    mcp_auth = AgentOSBuiltinAuth(
        url=agentos_external_url,
        secret=MCP_CONNECT_SECRET,
        signing_key_material=getenv("AGENTOS_MCP_SIGNING_KEY"),
    )


# ---------------------------------------------------------------------------
# Lifespan — app-level startup / teardown.
#
# AgentOS handles the MCP lifecycle (connect on startup, close on shutdown)
# for agent-attached and registry tools. Keep this hook to plug in your own setup.
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app):  # type: ignore[no-untyped-def]
    log_info("AgentOS lifespan: startup")
    ensure_collector_agent(registry)
    ensure_title_curator_agent(registry)
    ensure_evidence_extractor_agent(registry)
    ensure_event_extractor_agent(registry)
    ensure_investment_planner_agent(registry)
    ensure_investment_reasoner_agent(registry)
    ensure_investment_reviewer_agent(registry)
    ensure_raw_collection_workflow(registry)
    ensure_evidence_extraction_workflow(registry)
    ensure_event_extraction_workflow(registry)
    ensure_investment_reasoning_workflow(registry)
    model_id = getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    model = registry.get_model(model_id)
    if model is None:
        raise RuntimeError(f"registered Event Workflow model is unavailable: {model_id}")
    event_runtime = None
    investment_runtime = None
    try:
        event_runtime = create_local_event_workflow_runtime(model)
        configure_event_workflow_runtime(event_runtime)
        investment_runtime = create_local_investment_workflow_runtime(
            model,
            load_investment_reasoner_agent(registry),
            load_investment_reviewer_agent(registry),
        )
        configure_investment_workflow_runtime(investment_runtime)
        # Schedule rows are runtime configuration owned by PostgreSQL/Control Panel.
        # Startup only validates them; new environments use the explicit seed command.
        validate_schedules()
        yield
    finally:
        configure_event_workflow_runtime(None)
        configure_investment_workflow_runtime(None)
        close_errors: list[Exception] = []
        if investment_runtime is not None:
            try:
                await investment_runtime.close()
            except Exception as exc:
                close_errors.append(exc)
        if event_runtime is not None:
            try:
                await event_runtime.close()
            except Exception as exc:
                close_errors.append(exc)
        log_info("AgentOS lifespan: shutdown")
        if close_errors:
            raise ExceptionGroup("AgentOS runtime shutdown failed", close_errors)


# ---------------------------------------------------------------------------
# Create AgentOS
# ---------------------------------------------------------------------------
agent_os = AgentOS(
    name="Tidewise AgentOS",
    tracing=True,
    scheduler=True,
    scheduler_base_url=agentos_internal_url,
    authorization=runtime_env != "dev",
    mcp_server=True,
    mcp_auth=mcp_auth,
    lifespan=lifespan,
    db=get_postgres_db(),
    agents=[tidewise_assistant],
    workflows=[local_ping, deployment_check],
    interfaces=interfaces,
    registry=registry,
    config=str(Path(__file__).parent / "config.yaml"),
)
app = agent_os.get_app()


if __name__ == "__main__":
    agent_os.serve(app="app.main:app", reload=False)
