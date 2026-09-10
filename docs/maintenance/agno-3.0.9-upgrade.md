# Agno 3.0.9 local upgrade and scheduler verification

Checked on 2026-09-10 for Issue #233. Upgrade from Agno 3.0.1 to 3.0.9;
UAT deployment and custom scheduler fixes are outside this change.

## Dependency compatibility

The generated lock moves FastMCP 3.4.3 to 4.0.3 and MCP 1.28.1 to 2.2.0,
including their required HTTP dependencies. Existing unrelated pins are retained.
MCP smoke clients now use `streamable_http_client`, an explicit `httpx2.AsyncClient`
for headers/timeouts, two transport streams, snake-case result attributes, and
numeric `read_timeout_seconds`. The existing REST/MCP collection interface test
was updated to the same SDK contract.

## Long-running schedule experiment

The local container ran Agno 3.0.9 with unmodified native AgentOS, SchedulePoller,
ScheduleExecutor, PostgreSQL, and real loopback HTTP on isolated port 18081.
No collection code, fake executor, patched clock, or lock override was used.
A disposable PostgreSQL schema separated all diagnostic state from real schedules.

The Workflow contained one async step waiting 365 real seconds. Schedule settings:
`0 */2 * * *`, Asia/Shanghai, 900-second timeout, no retries. After the HTTP server
was listening, only the initial `next_run_at` was set to now to avoid waiting for
the next cron occurrence. The original 3.0.1 experiment used the same procedure.

Observed on 3.0.9 (Asia/Shanghai):

- 15:57:13: first Workflow started.
- 16:02:13: second Workflow started while the first was still running.
- 16:03:18: first Workflow naturally completed after 365 seconds.
- 16:03:58 snapshot: first schedule record was success; next due time was 18:00.
- 16:04:28: second Workflow was cancelled during test shutdown.
- Before completion, both schedule records were `running`; `locked_at` moved forward by 300 seconds,
  while `next_run_at` retained the initial due time.

This reproduces duplicate execution on the full native stack. Agno 3.0.9 is not a
fix for the long-running Schedule lease problem. Keep affected schedules disabled
until a separate fix has been reviewed and verified. No custom lock strategy is
introduced in this upgrade.

## Verification and rollout boundary

- Local container reports Agno 3.0.9; REST `/health` succeeds.
- MCP handshake, component visibility and an actual assistant call succeed.
- `./scripts/format.sh` and `./scripts/validate.sh` pass (113 source files).
- `python -m unittest discover -s tests`: 15 tests pass.
- Local existing schedules remain disabled. UAT is unchanged.
- The diagnostic process, second Workflow and disposable schema were cleaned up.

The local rollback image is `tidewise-agent-os:rollback-233-agno301`.
For rollback, retag that image as `tidewise-agent-os:latest` and recreate only the
`agentos` Compose service with `docker compose up -d --no-build agentos`.
The MCP smoke script changes must also be reverted together with dependencies;
they use the MCP 2.x API.

Upstream: [Agno 3.0.9 release](https://github.com/agno-agi/agno/releases/tag/v3.0.9).
