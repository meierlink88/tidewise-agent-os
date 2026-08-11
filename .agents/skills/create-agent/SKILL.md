---
name: create-agent
description: Add a code-defined agent to Tidewise AgentOS, register it, document its quick prompts, reload local-agentos-1, and prove it over REST and MCP. Use whenever the user wants to create a new AgentOS agent.
---

# Create a Tidewise AgentOS Agent

Read `AGENTS.md`. If the user already provided a concrete job, build without extra discovery. Ask only when a missing business choice would materially change the result.

## 1. Design gate

Confirm Agent is the right primitive:

- use an Agent for open-ended judgment or conversation;
- use a Workflow for fixed steps, retries, idempotency and side effects;
- use a Team only for genuine multi-role delegation.

Ground any toolkit/model integration in current official Agno documentation. Check required env vars and packages without printing secret values.

## 2. Implement

Create `agents/<slug>.py` with this baseline:

```python
from agno.agent import Agent

from app.settings import default_model
from db import get_postgres_db

INSTRUCTIONS = """<job, tool rules, limits, and response contract>"""

my_agent = Agent(
    id="my-agent",
    name="My Agent",
    model=default_model(),
    db=get_postgres_db(),
    instructions=INSTRUCTIONS,
    add_datetime_to_context=True,
    add_history_to_context=True,
    num_history_runs=5,
    markdown=True,
)
```

Add tools, knowledge and memory only when required. Do not place business logic in `app/main.py`.

## 3. Register

- Import and append the agent in `app/main.py`.
- Add its description and three representative quick prompts in `app/config.yaml`.
- Add dependencies to `pyproject.toml`, regenerate `requirements.txt`, and rebuild when needed.

## 4. Verify

For code-only changes, hot reload is normally enough; use `docker compose restart agentos` for a clean reload. For dependencies, use `docker compose up -d --build agentos`.

1. `/health` returns 200.
2. `/agents` contains the new id.
3. One manifest quick prompt returns HTTP 200 and non-empty content.
4. Relevant tools actually fire when the contract requires them.
5. `./scripts/mcp_check.sh` still passes.
6. `ruff format --check .` and `./scripts/validate.sh` pass.

Do not run `docker compose down` or `--remove-orphans`; other Tidewise services share the `local` Compose project.
