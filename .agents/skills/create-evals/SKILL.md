---
name: create-evals
description: Add Agno eval coverage for a Tidewise AgentOS agent by mapping its observable promises, mining representative sessions, writing Case entries, and running them locally.
---

# Create Agent Evals

Read `AGENTS.md`, the target agent, its manifest and current `evals/cases.py`. Map only behaviors the agent explicitly promises. Use `ReliabilityEval`/`expected_tool_calls` for required tool use and judge criteria for answer quality; combine them when both matter. Criteria must be falsifiable and specific to the agent.

Use `smoke` for fast deterministic cases, `release` for broader gates, and `live` for facts that depend on current external state. Any case that can write components, learning state or external systems needs setup/teardown isolation before it may run.

Add cases to `evals/cases.py`, run by name first and then by tag using `python -m evals`, inspect persisted results in `agent_os`, and run static checks. Do not schedule model-cost evals by default.
