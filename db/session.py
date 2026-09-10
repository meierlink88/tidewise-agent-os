"""PostgreSQL connection helper for AgentOS state."""

from functools import cache

from db.scheduler_postgres import SchedulerPostgresDb
from db.url import db_url

DB_ID = "tidewise-agent-os-db"


@cache
def get_postgres_db() -> SchedulerPostgresDb:
    """Return the shared, memoized AgentOS Postgres database adapter."""
    return SchedulerPostgresDb(id=DB_ID, db_url=db_url)
