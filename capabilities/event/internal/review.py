"""Deterministic safety gate after LLM grounding and Signal detail extraction."""

from __future__ import annotations

from datetime import datetime, timedelta

from sematica.analysis.event.contracts import (
    AnchorCandidate,
    EventAnalysisInput,
    EventClassification,
    SignalProposal,
    VariableCandidate,
)
from sematica.ingestion.episcode.event.contracts import EventCandidateDTO, event_time_anchor


class ControlledSignalReviewer:
    """Reject proposals that violate identity, temporal or scope invariants."""

    async def review(
        self,
        event: EventAnalysisInput,
        classification: EventClassification,
        proposal: SignalProposal,
        variable: VariableCandidate,
        anchor: AnchorCandidate,
    ) -> bool:
        return self.review_candidate(event.event.event, event.reference_time, proposal, variable, anchor)

    def review_candidate(
        self,
        event: EventCandidateDTO,
        reference_time: datetime,
        proposal: SignalProposal,
        variable: VariableCandidate,
        anchor: AnchorCandidate,
    ) -> bool:
        """The same gate before publication, without manufacturing a formal Event ID."""
        event_time = event_time_anchor(event.semantic.time)
        assert event_time is not None
        expected_modality = {
            "FACT": "ACTUAL",
            "PLAN": "ANTICIPATED",
            "SPEC": "ASSUMED",
        }[event.semantic.modality]
        onset = proposal.impact_onset_latest or proposal.impact_onset_earliest
        latest_end = proposal.expected_end_latest or proposal.expected_end_earliest
        if onset is None or latest_end is None:
            return False
        return all(
            (
                proposal.anchor_uuid == anchor.uuid,
                proposal.variable_uuid == variable.uuid,
                anchor.entity_type.value != "IndustryChain",
                anchor.entity_type in variable.allowed_anchor_types,
                proposal.valid_at == reference_time,
                onset >= event_time,
                onset <= event_time + timedelta(days=1095),
                proposal.invalid_at is None,
                proposal.assertion_modality == expected_modality,
                latest_end <= onset + timedelta(days=1095),
            )
        )
