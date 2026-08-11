---
name: setup-platform
description: Set up Tidewise AgentOS locally using the existing local PostgreSQL container, an isolated agent_os database, DeepSeek, and the local Compose project. Use for first-time setup or bringing this repo up on a new Tidewise development machine.
---

# Set Up Tidewise AgentOS

Read `AGENTS.md` before acting. Never print secrets and never run an unscoped `docker compose down` from this repository.

## 1. Preconditions

- `docker info` succeeds.
- `local-postgres-1` is running.
- external Docker network `tidewise-local` exists and PostgreSQL has alias `postgres`.
- host port `8000` is free.

## 2. Isolated database

Reuse PostgreSQL 5432; do not add a database service to `compose.yaml`.

- role: `agent_os_runtime`
- database: `agent_os`
- owner: `agent_os_runtime`

Create missing role/database with the PostgreSQL admin inside `local-postgres-1`. Generate a strong role password, do not alter an existing role password, and do not grant access to other Tidewise databases.

## 3. Environment

Create ignored `.env` with mode `600` from `example.env`. Required:

- `DEEPSEEK_API_KEY`
- `DEEPSEEK_MODEL=deepseek-v4-flash`
- `DEEPSEEK_BASE_URL=https://api.deepseek.com`
- `DB_HOST=postgres`, `DB_PORT=5432`
- `DB_USER=agent_os_runtime`, generated `DB_PASS`, `DB_DATABASE=agent_os`

Never bake `.env` into the image or echo its values.

## 4. Boot

Validate that Compose resolves to project `local`, one service `agentos`, and external network `tidewise-local`. Start only this service:

```bash
docker compose up -d --build agentos
```

The orphan warning is expected because this repository intentionally shares the `local` project label. Never add `--remove-orphans`.

## 5. Prove the platform

In order:

1. `GET /health` returns 200.
2. `/agents` contains `tidewise-assistant`.
3. `/workflows` contains `local-ping` and `deployment-check`.
4. Run `local-ping`; expect `Tidewise AgentOS OK`.
5. Run the assistant quick prompt; expect a non-empty DeepSeek response.
6. Run `./scripts/mcp_check.sh`; expect eight MCP tools and a non-empty response.
7. Restart only `agentos`, then verify sessions and schedules remain in `agent_os`.
8. Run `ruff format --check .` and `./scripts/validate.sh` from the project venv.

## 6. UI handoff

Connect [AgentOS UI](https://os.agno.com) using connection type **Local** and endpoint `http://localhost:8000`. The UI is optional; local Agents, Workflows and Schedules execute without the Agno cloud control plane.
