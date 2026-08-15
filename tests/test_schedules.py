"""Tests for explicit Schedule seeding and read-only runtime validation."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.schedules import (
    EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT,
    EVIDENCE_EXTRACTION_SCHEDULE_PROMPT,
    RAW_COLLECTION_SCHEDULE_ENDPOINT,
    inspect_schedules,
    seed_schedules,
)


class ScheduleSeedTest(unittest.TestCase):
    def test_preserves_existing_schedules_after_control_panel_rename(self) -> None:
        manager = Mock()
        manager.list.return_value = [
            SimpleNamespace(
                id="raw-id",
                name="raw-collection-schedule",
                endpoint=RAW_COLLECTION_SCHEDULE_ENDPOINT,
                enabled=False,
            ),
            SimpleNamespace(
                id="evidence-id",
                name="evidence-extraction-schedule",
                endpoint=EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT,
                enabled=True,
            ),
        ]

        with patch.dict(os.environ, {"ENABLE_DEPLOY_CHECK": "false"}):
            self.assertTrue(seed_schedules(manager=manager))

        manager.create.assert_not_called()
        manager.update.assert_not_called()
        manager.enable.assert_not_called()
        manager.disable.assert_not_called()

    def test_refuses_to_seed_a_duplicate_workflow_endpoint(self) -> None:
        manager = Mock()
        manager.list.return_value = [
            SimpleNamespace(
                id="raw-id",
                name="Raw Collection",
                endpoint=RAW_COLLECTION_SCHEDULE_ENDPOINT,
                enabled=True,
            ),
            SimpleNamespace(
                id="evidence-a",
                name="Evidence A",
                endpoint=EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT,
                enabled=True,
            ),
            SimpleNamespace(
                id="evidence-b",
                name="Evidence B",
                endpoint=EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT,
                enabled=True,
            ),
        ]

        with patch.dict(os.environ, {"ENABLE_DEPLOY_CHECK": "false"}):
            self.assertFalse(seed_schedules(manager=manager))

        manager.create.assert_not_called()
        manager.update.assert_not_called()

    def test_creates_only_missing_default_schedules(self) -> None:
        manager = Mock()
        manager.list.return_value = []
        manager.create.side_effect = [
            SimpleNamespace(id="raw-id"),
            SimpleNamespace(id="evidence-id"),
        ]

        with patch.dict(os.environ, {"ENABLE_DEPLOY_CHECK": "false"}):
            self.assertTrue(seed_schedules(manager=manager))

        calls = [call.kwargs for call in manager.create.call_args_list]
        self.assertEqual(
            {call["endpoint"] for call in calls},
            {RAW_COLLECTION_SCHEDULE_ENDPOINT, EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT},
        )
        evidence = next(call for call in calls if call["endpoint"] == EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT)
        self.assertEqual(evidence["cron"], "*/10 * * * *")
        self.assertEqual(evidence["payload"], {"message": EVIDENCE_EXTRACTION_SCHEDULE_PROMPT})
        self.assertEqual(evidence["timezone"], "Asia/Shanghai")
        self.assertEqual(evidence["if_exists"], "raise")


class ScheduleInspectionTest(unittest.TestCase):
    def test_reports_missing_and_duplicate_endpoints_without_writes(self) -> None:
        manager = Mock()
        manager.list.return_value = [
            SimpleNamespace(
                id="evidence-a",
                name="Evidence A",
                endpoint=EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT,
                enabled=True,
            ),
            SimpleNamespace(
                id="evidence-b",
                name="Evidence B",
                endpoint=EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT,
                enabled=False,
            ),
        ]

        with patch.dict(os.environ, {"ENABLE_DEPLOY_CHECK": "false"}):
            states = inspect_schedules(manager=manager)

        by_endpoint = {state.endpoint: state for state in states}
        self.assertEqual(by_endpoint[RAW_COLLECTION_SCHEDULE_ENDPOINT].status, "missing")
        self.assertEqual(by_endpoint[EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT].status, "duplicate")
        manager.create.assert_not_called()
        manager.update.assert_not_called()
        manager.enable.assert_not_called()
        manager.disable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
