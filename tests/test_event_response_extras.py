"""Agno LLM parsing ignores extra fields, not invalid contract content."""

import json
import unittest
from copy import deepcopy

from agno.utils.string import parse_response_model_str
from pydantic import ValidationError

from capabilities.event import (
    BatchAssociationDecision,
    BatchIdentityDecision,
    BatchSignalDecision,
    ClassifiedEventDraft,
)
from capabilities.event.internal.storyline_models import SignalDecision
from tests import test_event_extraction as fixtures


class EventResponseExtrasTest(unittest.TestCase):
    def cases(self):
        extract = fixtures.EventExtractionWorkflowTest.extraction_draft().model_dump(mode="json")
        for candidate in extract["candidates"]:
            candidate["classification"] = fixtures.EventExtractionWorkflowTest.classification().model_dump(mode="json")
            candidate["classification"]["event_class"] = "INDUSTRY_CHAIN"
        return (
            (ClassifiedEventDraft, extract),
            (
                BatchIdentityDecision,
                {
                    "events": [
                        {
                            "candidate_key": "one",
                            "decision": {
                                "decision": "NEW_EVENT",
                                "atomic": True,
                                "matched_event_ids": [],
                                "reason_codes": ["NEW"],
                                "summary": "new",
                            },
                        }
                    ]
                },
            ),
            (
                BatchAssociationDecision,
                {"events": [{"candidate_key": "one", "matches": [{"uuid": "anchor-one", "reason": "direct"}]}]},
            ),
            (
                BatchSignalDecision,
                {
                    "events": [
                        {
                            "candidate_key": "one",
                            "proposals": [fixtures.EventExtractionWorkflowTest.signal_draft().model_dump(mode="json")],
                        },
                        {"candidate_key": "two", "proposals": [], "no_signal_reason": "No direct evidence"},
                    ]
                },
            ),
        )

    def test_agno_and_dict_parsers_ignore_extras_at_every_object_depth(self):
        def add_extras(value):
            if isinstance(value, dict):
                return {**{k: add_extras(v) for k, v in value.items()}, "unsolicited_detail": "ignore"}
            if isinstance(value, list):
                return [add_extras(v) for v in value]
            return value

        for schema, clean in self.cases():
            with self.subTest(schema=schema.__name__):
                expected = schema.model_validate(clean).model_dump(mode="json")
                dirty = add_extras(clean)
                parsed = parse_response_model_str(json.dumps(dirty), schema)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed.model_dump(mode="json"), expected)
                self.assertEqual(schema.model_validate(dirty).model_dump(mode="json"), expected)

    def test_real_failure_shape_reason_codes_is_not_persisted(self):
        result = BatchSignalDecision.model_validate_json(
            json.dumps(
                {
                    "events": [
                        {
                            "candidate_key": "one",
                            "proposals": [],
                            "no_signal_reason": "No direct evidence",
                            "reason_codes": ["NO_DIRECT_VARIABLE_EFFECT", "NO_PROFILE_EVIDENCE"],
                        }
                    ]
                }
            )
        )
        self.assertNotIn("reason_codes", result.model_dump()["events"][0])

    def test_invalid_required_fields_still_fail_even_with_extras(self):
        for schema, clean in self.cases():
            dirty = deepcopy(clean)
            dirty.pop(next(iter(dirty)))
            dirty["unsolicited_detail"] = clean
            with self.subTest(schema=schema.__name__), self.assertRaises(ValidationError):
                schema.model_validate(dirty)
        signal = fixtures.EventExtractionWorkflowTest.signal_draft().model_dump(mode="json")
        signal.update(direction="SIDEWAYS", unsolicited_detail="extra")
        with self.assertRaises(ValidationError):
            BatchSignalDecision.model_validate({"events": [{"candidate_key": "one", "proposals": [signal]}]})
        with self.assertRaises(ValidationError):
            BatchSignalDecision.model_validate(
                {"events": [{"candidate_key": "one", "proposals": None, "reason_codes": ["NO_EVIDENCE"]}]}
            )

    def test_shared_contract_remains_strict(self):
        with self.assertRaises(ValidationError):
            SignalDecision.model_validate({"proposals": [], "no_signal_reason": "No evidence", "reason_codes": []})
