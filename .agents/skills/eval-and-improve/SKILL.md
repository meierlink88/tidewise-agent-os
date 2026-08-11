---
name: eval-and-improve
description: Run the Tidewise AgentOS eval suite, diagnose every failure, fix in-scope causes, and repeat until cases pass or a concrete external blocker is proven.
---

# Evaluate and Improve

Confirm `.env`, the `agent_os` database and the `agentos` service are available. Run the narrowest failing case first (`python -m evals --name <case>`), preserve raw result metadata, and classify failure as code/instructions, test defect, dependency/configuration, external service, or cleanup/isolation.

Fix the production behavior when the contract is correct; fix the case only when its expectation is wrong. Never weaken criteria merely to turn a failure green. Re-run the failing case, its tag and relevant regressions. If a case writes state, verify teardown on pass, failure and timeout.

Finish with `ruff format --check .`, `./scripts/validate.sh`, and an Agent/MCP smoke check. Shared PostgreSQL and other `local` services remain out of scope for destructive operations.
