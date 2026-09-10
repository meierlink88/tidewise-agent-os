"""Project-owned default for Agno schedule claim recovery."""

from typing import Any

from agno.db.postgres import PostgresDb

SCHEDULE_LOCK_GRACE_SECONDS = 120 * 60


class SchedulerPostgresDb(PostgresDb):
    """Keep native claim semantics with a two-hour stale-lock threshold."""

    def claim_due_schedule(
        self,
        worker_id: str,
        lock_grace_seconds: int = SCHEDULE_LOCK_GRACE_SECONDS,
    ) -> dict[str, Any] | None:
        return super().claim_due_schedule(worker_id, lock_grace_seconds=lock_grace_seconds)
