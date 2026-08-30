"""Deterministic safety gate after LLM grounding and Signal detail extraction."""

from __future__ import annotations

from datetime import timedelta

from sematica.analysis.event.contracts import (
    AnchorCandidate,
    EventAnalysisInput,
    EventClass,
    EventClassification,
    SignalDirection,
    SignalProposal,
    VariableCandidate,
)


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
        event_time = (
            event.event.event.semantic.effective_at or event.event.event.occurred_at or event.event.event.announced_at
        )
        assert event_time is not None
        expected_modality = {
            "FACT": "ACTUAL",
            "PLAN": "ANTICIPATED",
            "SPEC": "ASSUMED",
        }[event.event.event.modality]
        latest_end = proposal.expected_end_latest or proposal.expected_end_earliest
        if latest_end is None:
            return False
        return all(
            (
                classification.event_class != EventClass.COMPANY,
                proposal.anchor_uuid == anchor.uuid,
                proposal.variable_uuid == variable.uuid,
                anchor.entity_type in variable.allowed_anchor_types,
                proposal.direction != SignalDirection.UNKNOWN,
                proposal.valid_at >= event_time,
                proposal.valid_at <= event_time + timedelta(days=1095),
                proposal.invalid_at is None,
                proposal.assertion_modality == expected_modality,
                latest_end <= proposal.valid_at + timedelta(days=1095),
            )
        )
