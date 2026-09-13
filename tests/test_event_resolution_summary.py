"""Event model narratives survive parsing and durable journal round trips."""

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from pydantic import ValidationError

from capabilities.event.internal.batch_models import BatchAssociationDecision
from capabilities.event.internal.models import EventResolutionJournal, EventResolutionRecord
from capabilities.event.internal.review import ControlledSignalReviewer
from capabilities.event.internal.storyline_models import AssociationDecision, AssociationMatch
from sematica.analysis.event.contracts import (
    DirectSignalDraft,
    EventClassification,
    SignalFactAttributes,
    SignalProposal,
)
from sematica.ingestion.episcode.event.contracts import EventCandidateDTO, EventSemanticDTO
from sematica.ontology.enums import AnalysisAnchorType


class ResolutionSummaryTests(unittest.TestCase):
    def test_long_association_reason_survives_journal_roundtrip(self):
        reasons = ["a" * 562, "b" * 528]
        batch = BatchAssociationDecision.model_validate(
            {"events": [{"candidate_key": "a" * 64, "matches": [], "no_match_reason": reason} for reason in reasons]}
        )
        summary = "; ".join(item.no_match_reason for item in batch.events)
        decision = AssociationDecision(matches=[], no_match_reason=summary)
        record = EventResolutionRecord(
            candidate_key="a" * 64,
            decision="IGNORED",
            atomic=True,
            matched_event_ids=[],
            reason_codes=["NO_MATCHING_SUBJECT"],
            summary=decision.no_match_reason,
        )
        journal = EventResolutionJournal(batch_id="b" * 64, resolutions=[record])
        restored = EventResolutionJournal.model_validate_json(journal.model_dump_json(by_alias=True))
        self.assertEqual(restored.resolutions[0].summary, summary)
        self.assertEqual(len(summary), 1092)

    def test_narratives_have_no_length_limits_in_output_or_storage_schema(self):
        for model, fields in (
            (AssociationDecision, ["no_match_reason"]),
            (AssociationMatch, ["reason"]),
            (EventCandidateDTO, ["title", "summary"]),
            (EventSemanticDTO, ["action", "reason", "method"]),
            (EventClassification, ["rationale"]),
            (DirectSignalDraft, ["fact", "mechanism", "duration_basis"]),
            (SignalProposal, ["fact", "mechanism", "duration_basis"]),
            (SignalFactAttributes, ["mechanism", "duration_basis"]),
            (EventResolutionRecord, ["summary"]),
        ):
            for field in fields:
                with self.subTest(model=model.__name__, field=field):
                    prop = model.model_json_schema()["properties"][field]
                    variants = prop.get("anyOf", [prop])
                    for variant in variants:
                        self.assertNotIn("maxLength", variant)
                        self.assertNotIn("minLength", variant)

    def test_empty_narrative_is_preserved_but_wrong_type_is_rejected(self):
        self.assertEqual(AssociationDecision(matches=[], no_match_reason="").no_match_reason, "")
        with self.assertRaises(ValidationError):
            AssociationDecision(matches=[], no_match_reason=123)

    def test_event_content_roundtrip_preserves_long_blank_and_repeated_text(self):
        event = EventCandidateDTO.model_validate(
            {
                "title": "标题" * 1000,
                "summary": "",
                "semantic": {
                    "actors": ["", "actor", "actor"],
                    "action": "",
                    "objects": [],
                    "stage": "OCCURRED",
                    "modality": "FACT",
                    "time": {
                        "occurred_at": "2026-09-13T00:00:00Z",
                        "announced_at": None,
                        "effective_at": None,
                        "precision": "DAY",
                    },
                    "jurisdictions": [],
                    "reason": "理由" * 1000,
                    "method": "",
                    "metrics": [{"name": "", "value": None, "unit": "单位" * 100, "change": None, "period": None}],
                },
            }
        )
        restored = EventCandidateDTO.model_validate_json(event.model_dump_json())
        self.assertEqual(restored, event)

    def test_signal_text_survives_compilation_and_fact_storage(self):
        draft = DirectSignalDraft(
            anchor_uuid="anchor",
            variable_uuid="variable",
            fact="事实" * 2000,
            mechanism="",
            duration_basis="依据" * 2000,
            direction="UP",
            magnitude="LOW",
            impact_onset_days=2000,
            impact_peak_days=2001,
            expected_duration_days=2000,
            assumptions=[""] * 20,
            invalidation_conditions=[],
            provenance_confidence="LOW",
            mechanism_confidence="LOW",
            temporal_confidence="LOW",
        )
        now = datetime(2026, 9, 13, tzinfo=UTC)
        proposal = draft.proposal(event_time=now, reference_time=now, assertion_modality="ACTUAL")
        event = SimpleNamespace(
            semantic=SimpleNamespace(
                time=SimpleNamespace(occurred_at=now),
                modality="FACT",
            )
        )
        anchor = SimpleNamespace(uuid="anchor", entity_type=AnalysisAnchorType("Company"))
        variable = SimpleNamespace(uuid="variable", allowed_anchor_types=[anchor.entity_type])
        reviewer = ControlledSignalReviewer()
        self.assertTrue(reviewer.review_candidate(event, now, proposal, variable, anchor))
        variable.uuid = "outside-frozen-candidates"
        self.assertFalse(reviewer.review_candidate(event, now, proposal, variable, anchor))
        fields = proposal.model_dump(include=set(SignalFactAttributes.model_fields))
        attributes = SignalFactAttributes(
            **fields,
            source_event_ids=["event"],
            event_class="COMPANY",
            variable_id="test_variable",
            anchor_type="Company",
            anchor_business_id="company",
            methodology_version="v1",
        )
        restored = SignalFactAttributes.model_validate_json(attributes.model_dump_json())
        self.assertEqual(restored.duration_basis, draft.duration_basis)
        self.assertEqual(restored.invalidation_conditions, [])

    def test_duplicate_formal_association_ids_remain_invalid(self):
        with self.assertRaises(ValidationError):
            AssociationDecision(matches=[AssociationMatch(uuid="same", reason="")] * 2)


if __name__ == "__main__":
    unittest.main()
