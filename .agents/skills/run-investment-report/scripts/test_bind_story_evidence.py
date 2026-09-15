import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bind_story_evidence import bind, main


class EvidenceBindingTest(unittest.TestCase):
    def setUp(self):
        self.evidence = "EVD11111111-1111-4111-8111-111111111111"
        self.window = {"start": "2026-09-14T00:00:00Z", "end": "2026-09-15T00:00:00Z"}
        event = {
            "id": "EVT1",
            "created_at": "2026-09-14T01:00:00Z",
            "evidence_ids": [self.evidence],
            "publication_complete": True,
            "missing_evidence_ids": [],
        }
        self.snapshot = {"events": [event], "evidences": {self.evidence: {}}}
        self.report = {
            "analysis_window": self.window,
            "geopolitical_stories": [
                {"source_id": sid, "summary": {"evidence_ids": []}, "detail": {"untouched": "original"}}
                for sid in ["GPR1", "GPR2"]
            ],
        }
        self.pages = [
            {
                "schema_version": "story-events/v1",
                "query": dict(self.window, story_id=sid, selection_time_field="created_at"),
                "storyline": {"data_object_id": sid},
                "events": [copy.deepcopy(event)],
                "total": 1,
                "next_after_event_id": None,
            }
            for sid in ["GPR1", "GPR2"]
        ]

    def test_output_and_audit_cannot_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "output.json")
            args = [
                "bind",
                "--report",
                "unused",
                "--pages",
                "unused",
                "--snapshot",
                "unused",
                "--output",
                target,
                "--audit",
                target,
            ]
            with patch("sys.argv", args), self.assertRaisesRegex(ValueError, "must_differ"):
                main()
            self.assertFalse(Path(target).exists())

    def test_shared_event_retains_each_story_scope_without_mutating_input(self):
        out, audit = bind(self.report, self.pages, self.snapshot)
        self.assertEqual(len(audit), 2)
        for unit in out["geopolitical_stories"]:
            self.assertEqual(unit["summary"]["evidence_ids"], [self.evidence])
            self.assertEqual(unit["detail"], {"untouched": "original"})
        self.assertEqual(self.report["geopolitical_stories"][0]["summary"]["evidence_ids"], [])

    def test_rejects_missing_pages_scope_drift_and_broken_edges(self):
        for mutate in [
            lambda p: p.pop(),
            lambda p: p[0].update(next_after_event_id="EVT1"),
            lambda p: p[0]["query"].update(start="2026-09-13T00:00:00Z"),
            lambda p: p[0]["events"][0].update(evidence_ids=[]),
            lambda p: p[0]["events"][0].update(publication_complete=False),
            lambda p: p[0].update(total=2),
        ]:
            pages = copy.deepcopy(self.pages)
            mutate(pages)
            with self.assertRaises(ValueError):
                bind(self.report, pages, self.snapshot)


if __name__ == "__main__":
    unittest.main()
