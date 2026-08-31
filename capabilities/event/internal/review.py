"""Deterministic safety gate after LLM grounding and Signal detail extraction."""

from __future__ import annotations

from datetime import timedelta

from sematica.analysis.event.contracts import (
    AnchorCandidate,
    EventAnalysisInput,
    EventClassification,
    SignalDirection,
    SignalProposal,
    VariableCandidate,
)
from sematica.ingestion.episcode.event.contracts import event_time_anchor


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
        event_time = event_time_anchor(event.event.event.semantic.time)
        assert event_time is not None
        expected_modality = {
            "FACT": "ACTUAL",
            "PLAN": "ANTICIPATED",
            "SPEC": "ASSUMED",
        }[event.event.event.semantic.modality]
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
                proposal.direction != SignalDirection.UNKNOWN,
                proposal.valid_at == event.reference_time,
                onset >= event_time,
                onset <= event_time + timedelta(days=1095),
                proposal.invalid_at is None,
                proposal.assertion_modality == expected_modality,
                latest_end <= onset + timedelta(days=1095),
            )
        )
