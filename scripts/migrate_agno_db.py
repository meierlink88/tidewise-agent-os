"""Apply the idempotent Agno database migrations required by the runtime."""

from __future__ import annotations

import asyncio

from agno.db.migrations.manager import MigrationManager

from db import get_postgres_db


async def migrate_agno_database() -> None:
    """Migrate every Agno-owned table to the schema expected by this release."""
    await MigrationManager(get_postgres_db()).up()


def main() -> None:
    """Run migrations before the upgraded AgentOS starts serving traffic."""
    asyncio.run(migrate_agno_database())


if __name__ == "__main__":
    main()
