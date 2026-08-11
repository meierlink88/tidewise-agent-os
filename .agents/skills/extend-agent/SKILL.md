---
name: extend-agent
description: Make a concrete user-requested change to an existing Tidewise AgentOS agent, then verify it against the live local container. Use for adding tools, knowledge, memory, capabilities, or explicit behavior changes.
---

# Extend a Tidewise Agent

Read `AGENTS.md`, identify the registered agent file and its manifest entry, and restate the requested behavior as an observable contract. Ground new Agno integrations in current official docs; record imports, constructor arguments, environment variables and package dependencies.

Implement the smallest cohesive change in the agent module. Add secrets only to ignored `.env`; document variable names in `example.env`. If dependencies change, update `pyproject.toml`, regenerate `requirements.txt`, and run `docker compose up -d --build agentos`; otherwise use hot reload or `docker compose restart agentos`.

Verify `/health`, the target REST run with a relevant quick prompt, tool activity when applicable, MCP regression via `./scripts/mcp_check.sh`, and `ruff format --check .` plus `./scripts/validate.sh`. Never run unscoped `docker compose down` or `--remove-orphans`.
