"""Deep Event Analysis interface hiding classification, grounding and projection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from sematica.analysis.event.contracts import (
    AnchorCandidate,
    CandidateSet,
    EventAnalysisInput,
    EventAnalysisOutcome,
    EventClass,
    EventClassification,
    SignalProposal,
    VariableCandidate,
)


class EventClassifier(Protocol):
    async def classify(self, event: EventAnalysisInput) -> EventClassification: ...


class CandidateRetriever(Protocol):
    async def retrieve(self, event: EventAnalysisInput, classification: EventClassification) -> CandidateSet: ...


class SignalExtractor(Protocol):
    async def extract(
        self,
        event: EventAnalysisInput,
        classification: EventClassification,
        candidates: CandidateSet,
    ) -> list[SignalProposal]: ...


class SignalReviewer(Protocol):
    async def review(
        self,
        event: EventAnalysisInput,
        classification: EventClassification,
        proposal: SignalProposal,
        variable: VariableCandidate,
        anchor: AnchorCandidate,
    ) -> bool: ...


class SignalProjector(Protocol):
    async def project(
        self,
        event: EventAnalysisInput,
        classification: EventClassification,
        variable: VariableCandidate,
        anchor: AnchorCandidate,
        proposal: SignalProposal,
    ) -> str: ...


StageCallback = Callable[[str], Awaitable[None] | None]


class EventAnalysisPipeline:
    """Classify one Event and persist only reviewed Signals on existing identities."""

    def __init__(
        self,
        classifier: EventClassifier,
        retriever: CandidateRetriever,
        extractor: SignalExtractor,
        reviewer: SignalReviewer,
        projector: SignalProjector,
    ) -> None:
        self._classifier = classifier
        self._retriever = retriever
        self._extractor = extractor
        self._reviewer = reviewer
        self._projector = projector

    async def analyze(
        self,
        event: EventAnalysisInput,
        *,
        on_stage: StageCallback | None = None,
    ) -> EventAnalysisOutcome:
        await self._stage(on_stage, "CLASSIFYING")
        classification = await self._classifier.classify(event)
        if classification.event_class == EventClass.COMPANY:
            return EventAnalysisOutcome(
                status="NO_SUPPORTED_ANCHOR",
                classification=classification,
                signal_fact_uuids=[],
                reason_codes=["COMPANY_ANALYSIS_OUT_OF_SCOPE"],
            )

        await self._stage(on_stage, "GROUNDING")
        candidates = await self._retriever.retrieve(event, classification)
        if not candidates.anchors:
            return EventAnalysisOutcome(
                status="NO_SUPPORTED_ANCHOR",
                classification=classification,
                signal_fact_uuids=[],
                reason_codes=["NO_ELIGIBLE_EXISTING_ANCHOR"],
            )
        if not candidates.variables:
            return EventAnalysisOutcome(
                status="NO_SIGNAL",
                classification=classification,
                signal_fact_uuids=[],
                reason_codes=["NO_ELIGIBLE_FUNDAMENTAL_VARIABLE"],
            )

        await self._stage(on_stage, "EXTRACTING")
        proposals = await self._extractor.extract(event, classification, candidates)
        if not proposals:
            return EventAnalysisOutcome(
                status="NO_SIGNAL",
                classification=classification,
                signal_fact_uuids=[],
                reason_codes=["EVENT_SUPPORTS_NO_DIRECT_SIGNAL"],
            )

        await self._stage(on_stage, "VALIDATING")
        anchors = {item.uuid: item for item in candidates.anchors}
        variables = {item.uuid: item for item in candidates.variables}
        validated: list[tuple[SignalProposal, VariableCandidate, AnchorCandidate]] = []
        errors: list[str] = []
        seen_pairs: set[tuple[str, str]] = set()
        for proposal in proposals:
            anchor = anchors.get(proposal.anchor_uuid)
            variable = variables.get(proposal.variable_uuid)
            if anchor is None:
                errors.append("UNKNOWN_ANCHOR_UUID")
                continue
            if variable is None:
                errors.append("UNKNOWN_VARIABLE_UUID")
                continue
            if anchor.entity_type not in variable.allowed_anchor_types:
                errors.append("VARIABLE_ANCHOR_TYPE_NOT_ALLOWED")
                continue
            pair = (variable.uuid, anchor.uuid)
            if pair in seen_pairs:
                errors.append("DUPLICATE_EVENT_VARIABLE_ANCHOR_SIGNAL")
                continue
            seen_pairs.add(pair)
            if not await self._reviewer.review(event, classification, proposal, variable, anchor):
                errors.append("SIGNAL_REVIEW_REJECTED")
                continue
            validated.append((proposal, variable, anchor))

        if errors:
            return EventAnalysisOutcome(
                status="NO_SIGNAL",
                classification=classification,
                signal_fact_uuids=[],
                reason_codes=sorted(set(errors)),
            )

        await self._stage(on_stage, "PROJECTING")
        fact_uuids = [
            await self._projector.project(event, classification, variable, anchor, proposal)
            for proposal, variable, anchor in validated
        ]
        return EventAnalysisOutcome(
            status="SUCCEEDED",
            classification=classification,
            signal_fact_uuids=fact_uuids,
            reason_codes=["DIRECT_SIGNAL_FACTS_PROJECTED"],
        )

    @staticmethod
    async def _stage(callback: StageCallback | None, stage: str) -> None:
        if callback is None:
            return
        result = callback(stage)
        if result is not None:
            await result
