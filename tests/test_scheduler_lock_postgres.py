"""Opt-in integration coverage against the configured local PostgreSQL instance.

Run with RUN_SCHEDULER_PG_TEST=1; only a fresh, disposable schema is modified.
"""

import os
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from agno.scheduler import ScheduleManager
from sqlalchemy import text

from db.scheduler_postgres import SchedulerPostgresDb
from db.url import db_url


@unittest.skipUnless(os.getenv("RUN_SCHEDULER_PG_TEST") == "1", "requires configured local PostgreSQL")
class SchedulerLockPostgresTests(unittest.TestCase):
    def test_long_claim_is_not_reclaimed_until_two_hours_and_release_still_works(self):
        schema = f"diag_lock_{uuid4().hex}"
        db = SchedulerPostgresDb(id=schema, db_url=db_url, db_schema=schema)
        try:
            schedule = ScheduleManager(db).create(
                name="two-hour-lock-test",
                cron="0 */2 * * *",
                endpoint="/workflows/test/runs",
            )
            now = int(time.time())
            db.update_schedule(schedule.id, next_run_at=now)
            with patch("agno.db.postgres.postgres.time.time", return_value=now):
                first = db.claim_due_schedule("worker-a")
            self.assertIsNotNone(first)
            for elapsed in (301, 7199):
                with patch("agno.db.postgres.postgres.time.time", return_value=now + elapsed):
                    self.assertIsNone(db.claim_due_schedule("worker-a"))
                    self.assertIsNone(db.claim_due_schedule("worker-b"))
            with patch("agno.db.postgres.postgres.time.time", return_value=now + 7201):
                recovered = db.claim_due_schedule("worker-b")
            assert recovered is not None
            self.assertEqual(recovered["id"], schedule.id)
            self.assertEqual(recovered["locked_by"], "worker-b")

            # Normal completion releases immediately; it does not wait two hours.
            self.assertTrue(db.release_schedule(schedule.id, next_run_at=now))
            self.assertIsNotNone(db.claim_due_schedule("worker-c"))
        finally:
            with db.db_engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            db.db_engine.dispose()
