---
name: improve-agent
description: Autonomously harden an existing Tidewise AgentOS agent against its stated instructions and real usage, probing the live local container and iterating until behavior is reliable.
---

# Improve a Tidewise Agent

Read `AGENTS.md`, the target agent's `INSTRUCTIONS`, manifest prompts, existing evals, and recent sessions when available. Derive a small probe set covering the golden path, ambiguity, unavailable information, tool discipline and response contract. An existing response is a scenario, not the expected oracle.

Run probes with distinctive `user_id` and fixture data. Before probing agents with write tools, learning stores or external side effects, snapshot the affected state or use a disposable namespace; never assume a failed or timed-out run made no writes.

Judge each result against the source instructions, edit the smallest relevant instruction/code seam, reload `agentos`, and re-run failed plus regression probes. Stop after reliable passes or a concrete external blocker. Finish with MCP and static checks. Never affect other services in the shared `local` Compose project.
