"""LLM semantic decisions survive deterministic preparation unchanged."""

import unittest

from capabilities.event import ClassifiedEventDraft
from capabilities.event.functions.extraction import _compile_candidate_time, _validated_resolution
from capabilities.event.internal.models import EventIdentityRequest
from tests import test_event_extraction as fixtures


class LLMAuthorityTest(unittest.TestCase):
    def test_redundant_no_event_does_not_override_candidate(self):
        candidate = fixtures.EventExtractionWorkflowTest.extraction_draft().candidates[0].model_dump(mode="json")
        candidate["classification"] = {
            **fixtures.EventExtractionWorkflowTest.classification().model_dump(mode="json"),
            "event_class": "INDUSTRY_CHAIN",
        }
        result = ClassifiedEventDraft.model_validate(
            {
                "candidates": [candidate],
                "no_event": [{"evidence_id": eid, "reason": "上述处理重复"} for eid in candidate["evidence_ids"]],
            }
        )
        self.assertEqual(result.no_event, [])
        self.assertEqual(len(result.candidates), 1)

    def test_business_time_is_not_rewritten_against_source_text(self):
        candidate = fixtures.EventExtractionWorkflowTest.extraction_draft().candidates[0]
        result = _compile_candidate_time(candidate, {})
        self.assertIsNotNone(result)
        self.assertEqual(result.event, candidate.event)

    def test_identity_algorithm_does_not_override_llm_new_event(self):
        candidate = fixtures.EventExtractionWorkflowTest.extraction_draft().candidates[0]
        historical = fixtures.EventExtractionWorkflowTest.historical_event().model_copy(
            update={"event": candidate.event}
        )
        request = EventIdentityRequest(candidate_key="a" * 64, candidate=candidate, historical_candidates=[historical])
        result = _validated_resolution(
            request,
            {
                "decision": "NEW_EVENT",
                "atomic": False,
                "matched_event_ids": [],
                "reason_codes": ["LLM_NEW"],
                "summary": "LLM considers a distinct occurrence",
            },
        )
        self.assertEqual(result.decision, "NEW_EVENT")
        self.assertFalse(result.atomic)
        self.assertEqual(result.reason_codes, ["LLM_NEW"])
