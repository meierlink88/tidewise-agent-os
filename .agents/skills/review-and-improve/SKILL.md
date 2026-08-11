---
name: review-and-improve
description: Sweep Tidewise AgentOS for code, documentation, manifest, Compose, env and skill drift; automatically fix safe mechanical issues and report larger design gaps.
---

# Review and Improve the Repository

Read `AGENTS.md` and compare runtime truth across `app/main.py`, component modules, `app/config.yaml`, `example.env`, Compose, scripts, README and `.agents/skills`.

Check that every registered component has a source file and manifest entry; every manifest id is registered; every documented env var is read or intentionally reserved; every schedule targets a registered endpoint; Compose contains only `agentos`, project name `local`, and external network `tidewise-local`; no secret or obsolete official-template component remains.

Run formatting, lint, mypy, `/health`, `local-ping`, one prompt per safe agent, deployment check and MCP. Auto-fix mechanical drift; surface architecture or product decisions rather than guessing. Never use `--remove-orphans` or unscoped `docker compose down`.
