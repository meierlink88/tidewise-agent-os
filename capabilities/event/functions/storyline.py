"""Visible Workflow operations. No Agent invocation, hidden semantic loop or tools."""

import json
from collections.abc import Callable
from typing import Any

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput

from capabilities.event.functions.extraction import (
    _batch,
    _candidate_key,
    _direct_predecessor,
    _event_run_state,
    _model_from_content,
    _validate_partition,
    _validated_resolution,
)
from capabilities.event.internal.models import (
    EventExtractionBusy,
    EventExtractionDraft,
    EventExtractionIdle,
    EventExtractionResult,
    EventIdentityRequest,
)
from capabilities.event.internal.review import ControlledSignalReviewer
from capabilities.event.internal.runtime import event_workflow_runtime
from capabilities.event.internal.storage import (
    claim_event_batch,
    complete_batch,
    freeze_draft,
    load_draft,
    load_storyline_journal,
    release_event_batch_lease,
    renew_event_batch_lease,
    write_storyline_journal,
)
from capabilities.event.internal.storyline_execution import release_on_failure
from capabilities.event.internal.storyline_models import (
    AssociationDecision,
    AssociationProfile,
    IdentityClassificationDecision,
    SignalDecision,
    StorylineAgentVersions,
    StorylineCandidateState,
    StorylineJournal,
)
from sematica.analysis.event.contracts import AnchorCandidate, CandidateSet, EventAnalysisInput
from sematica.ingestion.episcode.event.contracts import HistoricalEvent, event_time_anchor

PAGE_SIZE = 64
PAGE_CHAR_BUDGET = 48_000
MAX_PAGES = 1000
CLASS_LABEL = {
    "GEOPOLITICAL": "GeopoliticRivalry",
    "MACRO_ECONOMIC": "MacroEconomic",
    "INDUSTRY_CHAIN": "IndustryChain",
    "COMPANY": "Company",
}


def _journal(ctx: RunContext) -> StorylineJournal:
    journal = load_storyline_journal(_batch(ctx).batch_id)
    if journal is None:
        raise ValueError("missing frozen storyline journal")
    return journal


def _current(ctx: RunContext, journal: StorylineJournal) -> StorylineCandidateState:
    return journal.candidates[_event_run_state(ctx)["storyline_candidate_key"]]


def _save(ctx: RunContext, journal: StorylineJournal) -> None:
    write_storyline_journal(_batch(ctx), journal)
    renew_event_batch_lease(_batch(ctx))


def _output(step_input: StepInput, model):
    predecessor = _direct_predecessor(step_input)
    if not predecessor.success:
        raise ValueError("semantic Agent failed; publication is forbidden")
    return _model_from_content(model, predecessor.content)


def _pages(profiles: list[AssociationProfile]) -> list[list[AssociationProfile]]:
    """Cover the whole frozen catalog, never silently truncate at a retrieval limit."""
    if len({p.uuid for p in profiles}) != len(profiles):
        raise ValueError("catalog UUID collision")
    pages: list[list[AssociationProfile]] = []
    size = 0
    for profile in sorted(profiles, key=lambda p: p.uuid):
        length = len(profile.model_dump_json())
        if length > PAGE_CHAR_BUDGET:
            raise ValueError("one profile exceeds the context budget")
        if not pages or len(pages[-1]) == PAGE_SIZE or size + length > PAGE_CHAR_BUDGET:
            pages.append([])
            size = 0
        pages[-1].append(profile)
        size += length
    if len(pages) > MAX_PAGES:
        raise ValueError("complete catalog exceeds Workflow safety cap")
    return pages


def _catalog_profiles(records: list[dict[str, Any]], allowed_types: set[str]) -> list[AssociationProfile]:
    profiles = [AssociationProfile.model_validate(record) for record in records]
    if any(profile.entity_type not in allowed_types for profile in profiles):
        raise ValueError("catalog returned an entity outside the Event class whitelist")
    return profiles


def _selected(state: StorylineCandidateState) -> list[tuple[AssociationProfile, str]]:
    selected = {}
    for page, result in zip(state.association_pages or [], state.association_results, strict=False):
        candidates = {p.uuid: p for p in page}
        for match in result.matches:
            selected[match.uuid] = (candidates[match.uuid], match.reason)
    return [selected[key] for key in sorted(selected)]


async def _extend_node_catalog(state: StorylineCandidateState) -> None:
    if state.association_pages is None or state.node_catalog_loaded:
        return
    if len(state.association_results) != len(state.association_pages):
        return
    chain_ids = [p.uuid for p, _ in _selected(state) if p.entity_type == "IndustryChain"]
    records = (
        await event_workflow_runtime().storyline_profiles(["ChainNode"], chain_uuids=chain_ids) if chain_ids else []
    )
    state.association_pages.extend(_pages(_catalog_profiles(records, {"ChainNode"})))
    if len(state.association_pages) > MAX_PAGES:
        raise ValueError("combined chain and node catalog exceeds safety cap")
    state.node_catalog_loaded = True


@release_on_failure
async def prepare_storyline_batch(step_input: StepInput, run_context: RunContext) -> StepOutput:
    batch = claim_event_batch()
    if batch is None or isinstance(batch, EventExtractionBusy):
        return StepOutput(content=batch or EventExtractionIdle(), stop=True)
    try:
        pins = StorylineAgentVersions.model_validate((run_context.metadata or {}).get("event_agent_versions"))
        journal = load_storyline_journal(batch.batch_id)
        if journal is None:
            if not batch.needs_analysis:
                raise ValueError("legacy pending Event batch requires explicit operator reconciliation")
            journal = StorylineJournal(agent_versions=pins)
            write_storyline_journal(batch, journal)
        elif journal.agent_versions != pins:
            raise ValueError("resume requires the original exact Agent versions")
        _event_run_state(run_context)["batch"] = batch.model_dump(mode="json")
        renew_event_batch_lease(batch)
        return StepOutput(content=batch)
    except Exception:
        release_event_batch_lease(batch)
        raise


@release_on_failure
def freeze_storyline_draft(step_input: StepInput, run_context: RunContext) -> StepOutput:
    batch = _batch(run_context)
    draft = _output(step_input, EventExtractionDraft)
    expected = {e.id for e in batch.evidences}
    supplied = [eid for c in draft.candidates for eid in c.evidence_ids] + [e.evidence_id for e in draft.no_event]
    if set(supplied) != expected or len(supplied) != len(set(supplied)):
        raise ValueError("Extractor must partition supplied Evidence exactly once")
    frozen = freeze_draft(batch, _validate_partition(batch, draft))
    renew_event_batch_lease(batch)
    return StepOutput(content=frozen)


@release_on_failure
def has_storyline_candidates(step_input: StepInput, run_context: RunContext) -> bool:
    journal = _journal(run_context)
    return any(
        _candidate_key(c) not in journal.candidates or not journal.candidates[_candidate_key(c)].done
        for c in load_draft(_batch(run_context).batch_id).candidates
    )


@release_on_failure
async def prepare_storyline_candidate(step_input: StepInput, run_context: RunContext) -> StepOutput:
    journal = _journal(run_context)
    candidate = next(
        c
        for c in load_draft(_batch(run_context).batch_id).candidates
        if _candidate_key(c) not in journal.candidates or not journal.candidates[_candidate_key(c)].done
    )
    key = _candidate_key(candidate)
    if key not in journal.candidates:
        history = await event_workflow_runtime().retrieve_history(candidate)
        journal.candidates[key] = StorylineCandidateState(
            identity_request=EventIdentityRequest(candidate_key=key, candidate=candidate, historical_candidates=history)
        )
    _event_run_state(run_context)["storyline_candidate_key"] = key
    _save(run_context, journal)
    return StepOutput(content=journal.candidates[key].identity_request)


@release_on_failure
def needs_storyline_identity(step_input: StepInput, run_context: RunContext) -> bool:
    return _current(run_context, _journal(run_context)).resolution is None


@release_on_failure
def freeze_storyline_identity(step_input: StepInput, run_context: RunContext) -> StepOutput:
    journal = _journal(run_context)
    state = _current(run_context, journal)
    decision = _output(step_input, IdentityClassificationDecision)
    if not set(decision.matched_event_ids) <= {h.id for h in state.identity_request.historical_candidates}:
        raise ValueError("identity outside frozen historical candidates")
    state.resolution = _validated_resolution(state.identity_request, decision.model_dump(exclude={"classification"}))
    state.classification = decision.classification
    _save(run_context, journal)
    return StepOutput(content=state.resolution)


@release_on_failure
def is_publishable_storyline(step_input: StepInput, run_context: RunContext) -> bool:
    state = _current(run_context, _journal(run_context))
    if state.resolution is None:
        raise ValueError("identity must complete before routing")
    return state.resolution.decision in {"NEW_EVENT", "RELATED_BUT_DISTINCT"}


@release_on_failure
async def prepare_storyline_catalog(step_input: StepInput, run_context: RunContext) -> StepOutput:
    journal = _journal(run_context)
    state = _current(run_context, journal)
    if state.classification is None:
        raise ValueError("missing primary classification")
    if state.association_pages is None:
        label = CLASS_LABEL[state.classification.event_class]
        semantic = state.identity_request.candidate.event.semantic
        records = await event_workflow_runtime().storyline_profiles(
            [label], terms=[*semantic.actors, *semantic.objects] if label == "Company" else None
        )
        state.association_pages = _pages(_catalog_profiles(records, {label}))
        state.node_catalog_loaded = label != "IndustryChain"
    await _extend_node_catalog(state)
    _save(run_context, journal)
    return StepOutput(content={"catalog_pages": len(state.association_pages)})


@release_on_failure
def has_association_pages(step_input: StepInput, run_context: RunContext) -> bool:
    state = _current(run_context, _journal(run_context))
    return len(state.association_results) < len(state.association_pages or [])


@release_on_failure
def prepare_association_page(step_input: StepInput, run_context: RunContext) -> StepOutput:
    state = _current(run_context, _journal(run_context))
    if state.classification is None:
        raise ValueError("missing frozen classification")
    page = (state.association_pages or [])[len(state.association_results)]
    return StepOutput(
        content={
            "event": state.identity_request.candidate.event.model_dump(mode="json"),
            "classification": state.classification.model_dump(mode="json"),
            "candidates": [p.model_dump(mode="json") for p in page],
        }
    )


@release_on_failure
async def freeze_association_page(step_input: StepInput, run_context: RunContext) -> StepOutput:
    journal = _journal(run_context)
    state = _current(run_context, journal)
    result = _output(step_input, AssociationDecision)
    if state.association_pages is None:
        raise ValueError("missing frozen association pages")
    page = (state.association_pages or [])[len(state.association_results)]
    if not {m.uuid for m in result.matches} <= {p.uuid for p in page}:
        raise ValueError("association UUID outside the frozen page")
    state.association_results.append(result)
    # Persist the semantic decision before the next external catalog read can fail.
    _save(run_context, journal)
    await _extend_node_catalog(state)
    _save(run_context, journal)
    return StepOutput(
        content={"association_done": len(state.association_results) == len(state.association_pages or [])}
    )


def _loop_flag(outputs: list[StepOutput], flag: str) -> bool:
    for output in reversed(outputs):
        if isinstance(output.content, dict) and output.content.get(flag) is True:
            return True
        if output.steps and _loop_flag(output.steps, flag):
            return True
    return False


def association_pages_complete(iteration_outputs: list[StepOutput]) -> bool:
    return _loop_flag(iteration_outputs, "association_done")


@release_on_failure
async def prepare_storyline_signals(step_input: StepInput, run_context: RunContext) -> StepOutput:
    journal = _journal(run_context)
    state = _current(run_context, journal)
    if not state.node_catalog_loaded or len(state.association_results) != len(state.association_pages or []):
        raise ValueError("association catalog coverage is incomplete")
    if state.signal_pages is None:
        # Cross-layer candidates require explicit names/aliases, never candidate-assets or topology propagation.
        semantic = state.identity_request.candidate.event.semantic
        exact = await event_workflow_runtime().storyline_profiles(
            ["GeopoliticRivalry", "MacroEconomic", "ChainNode", "Company"], terms=[*semantic.actors, *semantic.objects]
        )
        profiles = {p.uuid: p for p, _ in _selected(state) if p.entity_type != "IndustryChain"}
        for row in exact:
            p = AssociationProfile.model_validate(row)
            profiles.setdefault(p.uuid, p)
        anchors = [
            AnchorCandidate(
                uuid=p.uuid,
                business_id=p.business_id,
                name=p.name,
                entity_type=p.entity_type,
                summary=json.dumps(p.profile, ensure_ascii=False),
                retrieval_sources=["MENTION"],
            )
            for p in sorted(profiles.values(), key=lambda p: p.uuid)
        ]
        variables = await event_workflow_runtime().storyline_variables()
        if len({v.uuid for v in variables}) != len(variables) or len({v.variable_id for v in variables}) != len(
            variables
        ):
            raise ValueError("duplicate Variable identity")
        state.signal_pages = []
        # Both dimensions are fully covered. Pagination is execution, not semantic selection.
        for offset in range(0, len(anchors), 30):
            chunk = anchors[offset : offset + 30]
            relevant = [v for v in variables if any(a.entity_type in v.allowed_anchor_types for a in chunk)]
            for index in range(0, len(relevant), 64):
                state.signal_pages.append(CandidateSet(anchors=chunk, variables=relevant[index : index + 64]))
        if len(state.signal_pages) > MAX_PAGES:
            raise ValueError("Signal candidate coverage exceeds safety cap")
    _save(run_context, journal)
    return StepOutput(content={"signal_pages": len(state.signal_pages)})


@release_on_failure
def has_storyline_signal_pages(step_input: StepInput, run_context: RunContext) -> bool:
    state = _current(run_context, _journal(run_context))
    return len(state.signal_results) < len(state.signal_pages or [])


@release_on_failure
def prepare_storyline_signal_page(step_input: StepInput, run_context: RunContext) -> StepOutput:
    state = _current(run_context, _journal(run_context))
    if state.classification is None:
        raise ValueError("missing frozen classification")
    return StepOutput(
        content={
            "event": state.identity_request.candidate.event.model_dump(mode="json"),
            "classification": state.classification.model_dump(mode="json"),
            "candidates": (state.signal_pages or [])[len(state.signal_results)].model_dump(mode="json"),
        }
    )


@release_on_failure
def freeze_storyline_signal_page(step_input: StepInput, run_context: RunContext) -> StepOutput:
    journal = _journal(run_context)
    state = _current(run_context, journal)
    result = _output(step_input, SignalDecision)
    page = (state.signal_pages or [])[len(state.signal_results)]
    anchors = {a.uuid: a for a in page.anchors}
    variables = {v.uuid: v for v in page.variables}
    event = state.identity_request.candidate.event
    pairs = {(p.anchor_uuid, p.variable_uuid) for p in state.proposals}
    for draft in result.proposals:
        pair = draft.anchor_uuid, draft.variable_uuid
        if draft.anchor_uuid not in anchors or draft.variable_uuid not in variables or pair in pairs:
            raise ValueError("Signal endpoints outside page or duplicate pair")
        proposal = draft.proposal(
            event_time=event_time_anchor(event.semantic.time),
            reference_time=_batch(run_context).created_at,
            assertion_modality={"FACT": "ACTUAL", "PLAN": "ANTICIPATED", "SPEC": "ASSUMED"}[event.semantic.modality],
        )
        if not ControlledSignalReviewer().review_candidate(
            event, _batch(run_context).created_at, proposal, variables[draft.variable_uuid], anchors[draft.anchor_uuid]
        ):
            raise ValueError("Signal violated deterministic identity, type or temporal constraints")
        pairs.add(pair)
        state.proposals.append(proposal)
    state.signal_results.append(result)
    _save(run_context, journal)
    return StepOutput(content={"signal_pages_done": len(state.signal_results) == len(state.signal_pages or [])})


def storyline_signal_pages_complete(iteration_outputs: list[StepOutput]) -> bool:
    return _loop_flag(iteration_outputs, "signal_pages_done")


@release_on_failure
async def publish_storyline_candidate(step_input: StepInput, run_context: RunContext) -> StepOutput:
    journal = _journal(run_context)
    state = _current(run_context, journal)
    if state.resolution is None or state.classification is None:
        raise ValueError("missing frozen identity and classification")
    if state.signal_pages is None or len(state.signal_results) != len(state.signal_pages):
        raise ValueError("all semantic decisions must be frozen before publication")
    if not is_publishable_storyline(step_input, run_context):
        raise ValueError("duplicate or ignored Event cannot be published")

    def checkpoint(record):
        state.publication = record
        _save(run_context, journal)

    if state.publication is None or state.publication.graph_projection_status != "SUCCEEDED":
        state.publication = await event_workflow_runtime().publish(
            state.identity_request.candidate,
            state.identity_request.candidate_key,
            state.resolution,
            existing=state.publication,
            checkpoint=checkpoint,
            associations=[
                {"uuid": p.uuid, "business_id": p.business_id, "entity_type": p.entity_type, "reason": reason}
                for p, reason in _selected(state)
            ],
        )
        _save(run_context, journal)
    publication = state.publication
    if (
        publication.published_event is None
        or not publication.episode_uuid
        or publication.graph_projection_status != "SUCCEEDED"
    ):
        raise ValueError("formal Data and graph acknowledgements required before Signal projection")
    analysis = EventAnalysisInput(
        event=HistoricalEvent.model_validate(publication.published_event.model_dump(mode="json")),
        episode_uuid=publication.episode_uuid,
        reference_time=_batch(run_context).created_at,
    )
    anchors = {a.uuid: a for page in state.signal_pages for a in page.anchors}
    variables = {v.uuid: v for page in state.signal_pages for v in page.variables}
    for proposal in state.proposals:
        key = f"{proposal.anchor_uuid}:{proposal.variable_uuid}"
        if key not in state.signal_receipts:
            state.signal_receipts[key] = await event_workflow_runtime().project_signal(
                analysis,
                state.classification,
                variables[proposal.variable_uuid],
                anchors[proposal.anchor_uuid],
                proposal,
            )
            _save(run_context, journal)
    return StepOutput(content=publication)


@release_on_failure
def finish_storyline_candidate(step_input: StepInput, run_context: RunContext) -> StepOutput:
    journal = _journal(run_context)
    state = _current(run_context, journal)
    if state.resolution is None:
        raise ValueError("cannot complete unresolved Event")
    if is_publishable_storyline(step_input, run_context) and (
        state.publication is None
        or state.publication.graph_projection_status != "SUCCEEDED"
        or len(state.signal_receipts) != len(state.proposals)
    ):
        raise ValueError("cannot complete partial publication")
    state.done = True
    _save(run_context, journal)
    return StepOutput(content={"candidates_done": not has_storyline_candidates(step_input, run_context)})


def storyline_candidates_complete(iteration_outputs: list[StepOutput]) -> bool:
    return _loop_flag(iteration_outputs, "candidates_done")


@release_on_failure
def complete_storyline_batch(step_input: StepInput, run_context: RunContext) -> StepOutput:
    if has_storyline_candidates(step_input, run_context):
        raise ValueError("candidate safety cap reached before completion")
    batch = _batch(run_context)
    draft = load_draft(batch.batch_id)
    states = list(_journal(run_context).candidates.values())
    ignored = [s for s in states if s.resolution and s.resolution.decision == "IGNORED"]
    result = EventExtractionResult(
        batch_id=batch.batch_id,
        evidence_ids=[e.id for e in batch.evidences],
        candidate_count=len(draft.candidates),
        no_event_count=len(draft.no_event),
        published_event_ids=[s.publication.event_id for s in states if s.publication],
        duplicate_event_count=sum(s.resolution is not None and s.resolution.decision == "SAME_EVENT" for s in states),
        ignored_candidate_count=len(ignored),
        ignored_evidence_ids=sorted({eid for s in ignored for eid in s.identity_request.candidate.evidence_ids}),
        failed_candidate_count=0,
        failed_evidence_ids=[],
        signal_fact_uuids=sorted({uuid for s in states for uuid in s.signal_receipts.values()}),
    )
    complete_batch(batch, result)
    return StepOutput(content=result)


STORYLINE_FUNCTIONS: list[Callable[..., Any]] = [
    prepare_storyline_batch,
    freeze_storyline_draft,
    has_storyline_candidates,
    prepare_storyline_candidate,
    needs_storyline_identity,
    freeze_storyline_identity,
    is_publishable_storyline,
    prepare_storyline_catalog,
    has_association_pages,
    prepare_association_page,
    freeze_association_page,
    association_pages_complete,
    prepare_storyline_signals,
    has_storyline_signal_pages,
    prepare_storyline_signal_page,
    freeze_storyline_signal_page,
    storyline_signal_pages_complete,
    publish_storyline_candidate,
    finish_storyline_candidate,
    storyline_candidates_complete,
    complete_storyline_batch,
]
