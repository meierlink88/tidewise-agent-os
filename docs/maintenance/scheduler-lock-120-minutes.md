# Schedule claim lock grace: 120 minutes

Issue #237 changes the project-owned PostgreSQL adapter's default
`claim_due_schedule(worker_id, lock_grace_seconds=7200)`. Agno's native
SchedulePoller passes only the worker ID, so it uses this default instead of 300
seconds. The adapter delegates the atomic claim operation to the original
PostgresDb implementation. Explicit caller-supplied grace values remain honored.

No persistent table or column is added. Existing `agno_schedules.locked_at` and
`locked_by` remain the source of claim ownership. This applies to all schedules
using `get_postgres_db()`, not just Raw Collection. Cron expressions, payloads,
execution timeouts, retries and enabled states are unchanged.

This is a grace-period extension, not heartbeating or an at-most-once guarantee.
Executions exceeding 120 minutes may still be reclaimed. A crashed worker's claim
may remain unavailable for up to 120 minutes. Executor completion or timeout can
release the lock sooner; a timed-out background Workflow may still be running.
Do not treat this value as a Workflow cancellation or execution-timeout setting.

## Verification

On local Agno 3.0.9, runtime inspection confirmed SchedulerPostgresDb and a 7200
second default. The opt-in test `tests/test_scheduler_lock_postgres.py` used a
fresh PostgreSQL schema and mocked only the claim clock:

- Initial due schedule was claimed.
- At 301 and 7199 seconds, neither the original worker nor a peer reclaimed it.
- At 7201 seconds, a peer recovered the same schedule.
- Normal release allowed an immediate new claim without waiting two hours.
- The isolated schema was removed in test cleanup.

Run in the configured local container with `RUN_SCHEDULER_PG_TEST=1`; without that
flag, normal unittest discovery skips the PostgreSQL integration test. No real
120-minute wall-clock test is claimed.

Additional checks: format and validation passed (115 typed source files); unittest
discovery passed 15 tests with the PostgreSQL test skipped by default, and that
PostgreSQL test passed separately in the live container. REST health and MCP
Local Ping returned COMPLETED with `Tidewise AgentOS OK`.

## Local rollout

The local trial mounts this worktree's `db` directory using
`/tmp/agno-237-local.override.yaml`, alongside the existing Reviewer override from
Issue #235. Existing schedules stay disabled. UAT deployment requires the normal
review and release process and was not performed here.

To revert only this local trial, recreate the agentos service using the normal
Compose file and the Reviewer override, omitting the Issue #237 override. There
is no database migration to reverse; the native default returns to 300 seconds.
