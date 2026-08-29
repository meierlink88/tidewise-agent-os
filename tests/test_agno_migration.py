from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from scripts.migrate_agno_db import migrate_agno_database


class AgnoMigrationTests(unittest.TestCase):
    def test_migrates_all_agno_tables_with_the_shared_database(self) -> None:
        db = MagicMock()
        manager = MagicMock()
        manager.up = AsyncMock()

        with (
            patch("scripts.migrate_agno_db.get_postgres_db", return_value=db),
            patch("scripts.migrate_agno_db.MigrationManager", return_value=manager) as manager_type,
        ):
            asyncio.run(migrate_agno_database())

        manager_type.assert_called_once_with(db)
        manager.up.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
