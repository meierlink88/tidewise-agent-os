"""Tests for code-owned AgentOS schedule registration."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.schedules import (
    EVIDENCE_EXTRACTION_SCHEDULE_NAME,
    EVIDENCE_EXTRACTION_SCHEDULE_PROMPT,
    register_schedules,
)


class ScheduleRegistrationTest(unittest.TestCase):
    @patch("app.schedules.get_postgres_db", return_value=object())
    @patch("app.schedules.ScheduleManager")
    def test_registers_evidence_extraction_every_ten_minutes(self, manager_type, _get_db) -> None:
        manager = manager_type.return_value
        manager.create.return_value = SimpleNamespace(id="schedule-id", updated_at=None)

        with patch.dict(os.environ, {"ENABLE_DEPLOY_CHECK": "false"}):
            register_schedules()

        calls = [call.kwargs for call in manager.create.call_args_list]
        evidence_schedule = next(call for call in calls if call["name"] == EVIDENCE_EXTRACTION_SCHEDULE_NAME)
        self.assertEqual(evidence_schedule["cron"], "*/10 * * * *")
        self.assertEqual(evidence_schedule["endpoint"], "/workflows/evidence-extraction/runs")
        self.assertEqual(evidence_schedule["payload"], {"message": EVIDENCE_EXTRACTION_SCHEDULE_PROMPT})
        self.assertEqual(evidence_schedule["timezone"], "Asia/Shanghai")
        self.assertEqual(evidence_schedule["if_exists"], "update")


if __name__ == "__main__":
    unittest.main()
