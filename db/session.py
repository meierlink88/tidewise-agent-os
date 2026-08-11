"""PostgreSQL connection helper for AgentOS state."""

from functools import cache

from agno.db.postgres import PostgresDb

from db.url import db_url

DB_ID = "tidewise-agent-os-db"


@cache
def get_postgres_db() -> PostgresDb:
    """Return the shared, memoized AgentOS Postgres database adapter."""
    return PostgresDb(id=DB_ID, db_url=db_url)
