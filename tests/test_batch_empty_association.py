"""Narrow response normalization: missing explanation is not a batch failure."""

import json
import unittest

from pydantic import ValidationError

from capabilities.event import BatchAssociationDecision, BatchSignalDecision


class EmptyAssociationTest(unittest.TestCase):
    def parse(self, item):
        return BatchAssociationDecision.model_validate_json(json.dumps({"events": [item]})).events[0]

    def test_explicit_empty_matches_get_technical_reason(self):
        for reason in ({}, {"no_match_reason": None}, {"no_match_reason": ""}, {"no_match_reason": "  "}):
            with self.subTest(reason=reason):
                item = self.parse({"candidate_key": "event-1", "matches": [], **reason})
                self.assertEqual(item.matches, [])
                self.assertEqual(item.no_match_reason, "Model returned no matches without an explanation")

    def test_explicit_reason_is_preserved(self):
        item = self.parse({"candidate_key": "event-1", "matches": [], "no_match_reason": "No direct subject"})
        self.assertEqual(item.no_match_reason, "No direct subject")

    def test_duplicate_matches_and_unused_blank_reason_are_harmless(self):
        match = {"uuid": "known", "reason": "direct subject"}
        item = self.parse({"candidate_key": "one", "matches": [match, match], "no_match_reason": ""})
        self.assertEqual(len(item.matches), 1)
        self.assertIsNone(item.no_match_reason)

    def test_explicit_empty_signals_without_reason_are_valid(self):
        for reason in ({}, {"no_signal_reason": None}, {"no_signal_reason": " "}):
            result = BatchSignalDecision.model_validate_json(
                json.dumps({"events": [{"candidate_key": "one", "proposals": [], **reason}]})
            )
            self.assertEqual(result.events[0].proposals, [])
            self.assertEqual(result.events[0].no_signal_reason, "Model returned no signals without an explanation")

    def test_other_invalid_responses_still_fail(self):
        cases: tuple[dict[str, object], ...] = (
            {"candidate_key": "event-1"},
            {"candidate_key": "event-1", "matches": None},
            {"matches": []},
            {"candidate_key": "event-1", "matches": [{"uuid": "", "reason": "explicit"}]},
            {"candidate_key": "event-1", "matches": [], "no_match_reason": 123},
        )
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ValidationError):
                self.parse(item)
