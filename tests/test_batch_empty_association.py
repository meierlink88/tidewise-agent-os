"""Narrow response normalization: missing explanation is not a batch failure."""

import json
import unittest

from pydantic import ValidationError

from capabilities.event import BatchAssociationDecision


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

    def test_other_invalid_responses_still_fail(self):
        for item in (
            {"candidate_key": "event-1"},
            {"candidate_key": "event-1", "matches": None},
            {"matches": []},
            {"candidate_key": "event-1", "matches": [{"uuid": "", "reason": "explicit"}]},
            {"candidate_key": "event-1", "matches": [], "no_match_reason": 123},
        ):
            with self.subTest(item=item), self.assertRaises(ValidationError):
                self.parse(item)
