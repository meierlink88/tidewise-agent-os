"""Persisted identity dispositions retain complete association explanations."""

import unittest

from pydantic import ValidationError

from capabilities.event.internal.models import EventResolutionJournal, EventResolutionRecord


class ResolutionSummaryTests(unittest.TestCase):
    def test_long_association_reason_survives_journal_roundtrip(self):
        summary = "; ".join(["No supplied authoritative entity matches this occurrence." * 12] * 2)
        record = EventResolutionRecord(
            candidate_key="a" * 64,
            decision="IGNORED",
            atomic=True,
            matched_event_ids=[],
            reason_codes=["NO_MATCHING_SUBJECT"],
            summary=summary,
        )
        journal = EventResolutionJournal(batch_id="b" * 64, resolutions=[record])
        restored = EventResolutionJournal.model_validate_json(journal.model_dump_json(by_alias=True))
        self.assertEqual(restored.resolutions[0].summary, summary)

    def test_empty_summary_remains_invalid(self):
        with self.assertRaises(ValidationError):
            EventResolutionRecord(
                candidate_key="a" * 64,
                decision="IGNORED",
                atomic=True,
                matched_event_ids=[],
                reason_codes=["NO_MATCHING_SUBJECT"],
                summary="",
            )


if __name__ == "__main__":
    unittest.main()
