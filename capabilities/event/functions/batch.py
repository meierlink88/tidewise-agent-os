"""Batch preparation/checkpoints for native Parallel and direct Agent Steps. No LLM calls."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput
from pydantic import ValidationError

from capabilities.event.functions import storyline as legacy
from capabilities.event.functions.extraction import (
    _batch,
    _candidate_key,
    _compile_candidate_time,
    _direct_predecessor,
    _event_run_state,
    _model_from_content,
    _validate_partition,
    _validated_resolution,
)
from capabilities.event.internal.batch_models import (
    BatchAssociationDecision,
    BatchIdentityDecision,
    BatchSignalDecision,
    ClassifiedEventDraft,
)
from capabilities.event.internal.models import (
    EventCandidateSubmission,
    EventExtractionBusy,
    EventExtractionDraft,
    EventExtractionIdle,
    EventIdentityRequest,
    EventResolutionRecord,
)
from capabilities.event.internal.runtime import event_workflow_runtime
from capabilities.event.internal.storage import (
    claim_event_batch,
    freeze_batch_checkpoint,
    freeze_draft,
    load_batch_checkpoint,
    load_storyline_journal,
    release_event_batch_lease,
    renew_event_batch_lease,
    write_storyline_journal,
)
from capabilities.event.internal.storyline_models import (
    AssociationDecision,
    AssociationProfile,
    SignalDecision,
    StorylineAgentVersions,
    StorylineCandidateState,
    StorylineEventClassification,
    StorylineJournal,
)
from sematica.analysis.event.contracts import AnchorCandidate, CandidateSet, VariableCandidate
from sematica.ingestion.episcode.event.contracts import event_time_anchor

GROUPS = {"geo": "GEOPOLITICAL", "macro": "MACRO_ECONOMIC", "chain": "INDUSTRY_CHAIN", "company": "COMPANY"}
MAX_INPUT_CHARACTERS = 1_000_000


def _read(ctx: RunContext, key: str) -> dict | None:
    return load_batch_checkpoint(_batch(ctx).batch_id, key)


def _required(ctx: RunContext, key: str) -> dict:
    value = _read(ctx, key)
    if value is None:
        raise ValueError(f"missing batch checkpoint: {key}")
    return value


def _freeze(ctx: RunContext, key: str, value: dict) -> dict:
    return freeze_batch_checkpoint(_batch(ctx), key, value)


def _coverage(items, expected: list[str]) -> dict:
    result = {item.candidate_key: item for item in items}
    if len(result) != len(items) or set(result) != set(expected):
        raise ValueError("batch response must cover every supplied Event exactly once")
    return result


def _call(ctx: RunContext, key: str, payload: dict) -> StepOutput:
    frozen = _read(ctx, key + "-input") or _freeze(ctx, key + "-input", payload)
    text = json.dumps(frozen, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_INPUT_CHARACTERS:
        raise ValueError("batch context exceeds input budget; no silent paging or truncation")
    if _read(ctx, key + "-result") is None and frozen.get("events") == []:
        _freeze(ctx, key + "-result", {"events": []})
    if _read(ctx, key + "-result") is None and frozen.get("candidates") == []:
        _freeze(
            ctx,
            key + "-result",
            {
                "events": [
                    {
                        "candidate_key": e["candidate_key"],
                        "matches": [],
                        "no_match_reason": "No authoritative candidates",
                    }
                    for e in frozen["events"]
                ]
            },
        )
    if _read(ctx, key + "-result") is None and frozen.get("anchors") == []:
        _freeze(
            ctx,
            key + "-result",
            {
                "events": [
                    {
                        "candidate_key": e["candidate_key"],
                        "proposals": [],
                        "no_signal_reason": "No eligible Signal anchors",
                    }
                    for e in frozen["events"]
                ]
            },
        )
    renew_event_batch_lease(_batch(ctx))
    return StepOutput(content={"batch_call": key, "input": None if _read(ctx, key + "-result") else text})


def _response(step_input: StepInput, ctx: RunContext, key: str, schema):
    saved = _read(ctx, key + "-result")
    if saved is not None:
        return schema.model_validate(saved)
    output = _direct_predecessor(step_input)
    if not output.success:
        raise ValueError(f"batch Agent failed: {key}")
    return _model_from_content(schema, output.content)


async def claim_parallel_batch(step_input: StepInput, run_context: RunContext) -> StepOutput:
    batch = claim_event_batch()
    if batch is None or isinstance(batch, EventExtractionBusy):
        return StepOutput(content=batch or EventExtractionIdle(), stop=True)
    _event_run_state(run_context)["batch"] = batch.model_dump(mode="json")
    try:
        pins = StorylineAgentVersions.model_validate((run_context.metadata or {}).get("event_agent_versions"))
        marker = _read(run_context, "version")
        if marker is None and (load_storyline_journal(batch.batch_id) is not None or not batch.needs_analysis):
            raise ValueError("legacy pending batch requires explicit operator reconciliation before v16")
        _freeze(run_context, "version", {"contract": 16, "agent_versions": pins.as_mapping()})
        return _call(run_context, "extract", {"evidences": [e.model_dump(mode="json") for e in batch.evidences]})
    except Exception:
        release_event_batch_lease(batch)
        raise


async def prepare_batch_identity(step_input: StepInput, run_context: RunContext) -> StepOutput:
    ctx = run_context
    raw = _response(step_input, ctx, "extract", ClassifiedEventDraft)
    batch = _batch(ctx)
    expected = {e.id for e in batch.evidences}
    ids = {eid for c in raw.candidates for eid in c.evidence_ids}
    if not ids <= expected:
        raise ValueError("Extractor referenced Evidence outside the batch")
    if _read(ctx, "extract-result") is None:
        _freeze(
            ctx,
            "extract-rejections",
            {
                "items": [
                    {"evidence_id": e.evidence_id, "reason": "NO_EVENT_EVIDENCE_OUTSIDE_BATCH"}
                    for e in raw.no_event
                    if e.evidence_id not in expected
                ]
            },
        )
    raw = raw.model_copy(update={"no_event": [e for e in raw.no_event if e.evidence_id in expected]})
    _freeze(ctx, "extract-result", raw.model_dump(mode="json"))
    normalized = _validate_partition(
        batch,
        EventExtractionDraft(
            candidates=[
                EventCandidateSubmission.model_validate(c.model_dump(exclude={"classification"}))
                for c in raw.candidates
            ],
            no_event=raw.no_event,
        ),
    )
    draft = freeze_draft(batch, normalized)
    if _read(ctx, "identity-input") is None:
        events = []
        evidence_by_id = {e.id: e for e in batch.evidences}
        classifications = [
            c.classification for c in raw.candidates if _compile_candidate_time(c, evidence_by_id) is not None
        ]
        for candidate, classification in zip(draft.candidates, classifications, strict=True):
            history = await event_workflow_runtime().retrieve_history(candidate)
            events.append(
                {
                    "candidate_key": _candidate_key(candidate),
                    "candidate": candidate.model_dump(mode="json"),
                    "historical_candidates": [h.model_dump(mode="json") for h in history],
                    "classification": classification.model_dump(mode="json"),
                }
            )
        _freeze(ctx, "identity-input", {"events": events})
    return _call(ctx, "identity", _required(ctx, "identity-input"))


def freeze_batch_identity(step_input: StepInput, run_context: RunContext) -> StepOutput:
    ctx = run_context
    request = _required(ctx, "identity-input")
    response = _response(step_input, ctx, "identity", BatchIdentityDecision)
    by_key = _coverage(response.events, [e["candidate_key"] for e in request["events"]])
    journal = StorylineJournal(
        agent_versions=StorylineAgentVersions.model_validate((ctx.metadata or {})["event_agent_versions"]),
        input_transport_version=2,
    )
    ordered = [e["candidate_key"] for e in request["events"]]
    for entry in request["events"]:
        key = entry["candidate_key"]
        item = by_key[key]
        identity = EventIdentityRequest.model_validate({k: v for k, v in entry.items() if k != "classification"})
        if item.duplicate_of is not None:
            target = journal.candidates.get(item.duplicate_of)
            valid_duplicate = (
                item.duplicate_of in ordered[: ordered.index(key)]
                and by_key[item.duplicate_of].duplicate_of is None
                and target is not None
                and target.resolution is not None
                and target.resolution.decision != "IGNORED"
                and set(item.decision.matched_event_ids) <= {h.id for h in identity.historical_candidates}
            )
            resolution = EventResolutionRecord(
                candidate_key=key,
                decision="IGNORED",
                atomic=True,
                matched_event_ids=[],
                reason_codes=["BATCH_DUPLICATE" if valid_duplicate else "NONCOMPLIANT_IDENTITY_OUTPUT"],
                summary=f"Same batch Event: {item.duplicate_of}"
                if valid_duplicate
                else "Invalid batch duplicate reference; candidate skipped.",
            )
        else:
            resolution = _validated_resolution(identity, item.decision.model_dump())
        journal.candidates[key] = StorylineCandidateState(
            identity_request=identity,
            resolution=resolution,
            classification=StorylineEventClassification.model_validate(entry["classification"]),
        )
    for item in response.events:
        saved_resolution = journal.candidates[item.candidate_key].resolution
        if item.duplicate_of and saved_resolution and saved_resolution.reason_codes == ["BATCH_DUPLICATE"]:
            target = journal.candidates[item.duplicate_of]
            source = journal.candidates[item.candidate_key]
            target.identity_request = target.identity_request.model_copy(
                update={
                    "candidate": target.identity_request.candidate.model_copy(
                        update={
                            "evidence_ids": sorted(
                                set(
                                    target.identity_request.candidate.evidence_ids
                                    + source.identity_request.candidate.evidence_ids
                                )
                            )
                        }
                    )
                }
            )
    _freeze(ctx, "identity-result", response.model_dump(mode="json"))
    if load_storyline_journal(_batch(ctx).batch_id) is None:
        write_storyline_journal(_batch(ctx), journal)
    return StepOutput(content={"identity_complete": True})


def _events(ctx: RunContext, group: str) -> list[dict]:
    journal = legacy._journal(ctx)
    return [
        {
            "candidate_key": key,
            "event": s.identity_request.candidate.event.model_dump(mode="json"),
            "classification": s.classification.model_dump(mode="json"),
        }
        for key, s in journal.candidates.items()
        if s.classification
        and s.classification.event_class == GROUPS[group]
        and s.resolution
        and s.resolution.decision in {"NEW_EVENT", "RELATED_BUT_DISTINCT"}
    ]


async def _prepare_match(ctx: RunContext, group: str) -> StepOutput:
    key = "match-" + group
    saved = _read(ctx, key + "-input")
    if saved is not None:
        return _call(ctx, key, saved)
    events = _events(ctx, group)
    profiles: dict[str, dict] = {}
    if group == "company":
        semaphore = asyncio.Semaphore(4)

        async def retrieve(e):
            async with semaphore:
                semantic = e["event"]["semantic"]
                rows = await event_workflow_runtime().company_profiles([*semantic["actors"], *semantic["objects"]])
                return e, rows

        for e, rows in await asyncio.gather(*(retrieve(e) for e in events)):
            e["allowed_uuids"] = [p["uuid"] for p in rows]
            profiles.update({p["uuid"]: p for p in rows})
    elif events:
        rows = await event_workflow_runtime().storyline_profiles([legacy.CLASS_LABEL[GROUPS[group]]])
        profiles = {p["uuid"]: p for p in rows}
    return _call(ctx, key, {"events": events, "candidates": list(profiles.values())})


def _freeze_match(step_input: StepInput, ctx: RunContext, key: str) -> StepOutput:
    request = _required(ctx, key + "-input")
    response = _response(step_input, ctx, key, BatchAssociationDecision)
    by_key = _coverage(response.events, [e["candidate_key"] for e in request["events"]])
    ids = {p["uuid"] for p in request["candidates"]}
    rejected: list[dict[str, str]] = []
    for e in request["events"]:
        allowed = set(e.get("allowed_uuids", ids))
        item = by_key[e["candidate_key"]]
        rejected.extend(
            {"candidate_key": item.candidate_key, "uuid": m.uuid, "reason": "MATCH_OUTSIDE_EVENT_CANDIDATES"}
            for m in item.matches
            if m.uuid not in allowed & ids
        )
        matches = [m for m in item.matches if m.uuid in allowed & ids]
        by_key[item.candidate_key] = item.model_copy(
            update={
                "matches": matches,
                "no_match_reason": None
                if matches
                else item.no_match_reason or "No valid matches after removing out-of-scope references",
            }
        )
    response = BatchAssociationDecision(events=list(by_key.values()))
    if _read(ctx, key + "-result") is None:
        _freeze(ctx, key + "-rejections", {"items": rejected})
    _freeze(ctx, key + "-result", response.model_dump(mode="json"))
    return StepOutput(content={"matched": key})


async def prepare_geo_matches(step_input: StepInput, run_context: RunContext) -> StepOutput:
    return await _prepare_match(run_context, "geo")


def freeze_geo_matches(step_input: StepInput, run_context: RunContext) -> StepOutput:
    return _freeze_match(step_input, run_context, "match-geo")


async def prepare_macro_matches(step_input: StepInput, run_context: RunContext) -> StepOutput:
    return await _prepare_match(run_context, "macro")


def freeze_macro_matches(step_input: StepInput, run_context: RunContext) -> StepOutput:
    return _freeze_match(step_input, run_context, "match-macro")


async def prepare_chain_matches(step_input: StepInput, run_context: RunContext) -> StepOutput:
    return await _prepare_match(run_context, "chain")


async def prepare_node_matches(step_input: StepInput, run_context: RunContext) -> StepOutput:
    ctx = run_context
    _freeze_match(step_input, ctx, "match-chain")
    saved = _read(ctx, "match-node-input")
    if saved is not None:
        return _call(ctx, "match-node", saved)
    events = _required(ctx, "match-chain-input")["events"]
    result = _required(ctx, "match-chain-result")["events"]
    by_chain = {}
    for chain_id in sorted({m["uuid"] for e in result for m in e["matches"]}):
        by_chain[chain_id] = await event_workflow_runtime().storyline_profiles(["ChainNode"], chain_uuids=[chain_id])
    profiles = {p["uuid"]: p for rows in by_chain.values() for p in rows}
    by_key = {item["candidate_key"]: item for item in result}
    for event in events:
        matches = by_key[event["candidate_key"]]
        event["allowed_uuids"] = sorted({p["uuid"] for m in matches["matches"] for p in by_chain[m["uuid"]]})
    return _call(ctx, "match-node", {"events": events, "candidates": list(profiles.values())})


def freeze_node_matches(step_input: StepInput, run_context: RunContext) -> StepOutput:
    return _freeze_match(step_input, run_context, "match-node")


async def prepare_company_matches(step_input: StepInput, run_context: RunContext) -> StepOutput:
    return await _prepare_match(run_context, "company")


def freeze_company_matches(step_input: StepInput, run_context: RunContext) -> StepOutput:
    return _freeze_match(step_input, run_context, "match-company")


def freeze_batch_associations(step_input: StepInput, run_context: RunContext) -> StepOutput:
    ctx = run_context
    if _read(ctx, "associations-ready"):
        return StepOutput(content={"associations_ready": True})
    journal = legacy._journal(ctx)
    selected: dict[str, dict] = {key: {} for key in journal.candidates}
    reasons: dict[str, list[str]] = {key: [] for key in journal.candidates}
    for group in ("geo", "macro", "chain", "node", "company"):
        request = _required(ctx, f"match-{group}-input")
        result = _required(ctx, f"match-{group}-result")
        profiles = {p["uuid"]: p for p in request["candidates"]}
        for e in result["events"]:
            for match in e["matches"]:
                selected[e["candidate_key"]][match["uuid"]] = (profiles[match["uuid"]], match)
            if e["no_match_reason"]:
                reasons[e["candidate_key"]].append(e["no_match_reason"])
    for key, state in journal.candidates.items():
        pairs = list(selected[key].values())
        state.association_pages = [[AssociationProfile.model_validate(p) for p, _ in pairs]]
        state.association_results = [
            AssociationDecision(
                matches=[m for _, m in pairs],
                no_match_reason=None if pairs else "; ".join(reasons[key]) or "Not publishable after identity",
            )
        ]
        state.node_catalog_loaded = True
        if not pairs:
            if state.resolution and state.resolution.decision in {"NEW_EVENT", "RELATED_BUT_DISTINCT"}:
                state.resolution = state.resolution.model_copy(
                    update={
                        "decision": "IGNORED",
                        "reason_codes": ["NO_MATCHING_SUBJECT"],
                        "summary": state.association_results[0].no_match_reason,
                    }
                )
            state.done = True
            state.signal_pages = []
    write_storyline_journal(_batch(ctx), journal)
    _freeze(ctx, "associations-ready", {"complete": True})
    return StepOutput(content={"associations_ready": True})


async def prepare_batch_signals(step_input: StepInput, run_context: RunContext) -> StepOutput:
    ctx = run_context
    _required(ctx, "associations-ready")
    remaining = [g for g in GROUPS if _read(ctx, f"signal-{g}-complete") is None]
    if not remaining:
        return StepOutput(content={"batch_call": "signal-done", "input": None})
    group = remaining[0]
    _event_run_state(ctx)["signal_group"] = group
    key = f"signal-{group}"
    saved = _read(ctx, key + "-input")
    if saved is not None:
        return _call(ctx, key, saved)
    journal = legacy._journal(ctx)
    variables = await event_workflow_runtime().storyline_variables()
    events, anchors = [], {}
    for e in _events(ctx, group):
        state = journal.candidates[e["candidate_key"]]
        allowed = []
        profiles = {p.uuid: p for p, _ in legacy._selected(state)}
        semantic = e["event"]["semantic"]
        explicit = await event_workflow_runtime().storyline_profiles(
            ["GeopoliticRivalry", "MacroEconomic", "ChainNode", "Company"],
            terms=[*semantic["actors"], *semantic["objects"]],
        )
        profiles.update({p["uuid"]: AssociationProfile.model_validate(p) for p in explicit})
        for p in profiles.values():
            if p.entity_type == "IndustryChain":
                continue
            anchor = AnchorCandidate(
                uuid=p.uuid,
                name=p.name,
                business_id=p.business_id,
                entity_type=p.entity_type,
                summary=json.dumps(p.profile, ensure_ascii=False),
                retrieval_sources=["MENTION"],
            )
            if any(anchor.entity_type in v.allowed_anchor_types for v in variables):
                anchors[p.uuid] = anchor.model_dump(mode="json")
                allowed.append(p.uuid)
        e["allowed_uuids"] = allowed
        events.append(e)
    return _call(
        ctx,
        key,
        {
            "events": events,
            "anchors": list(anchors.values()),
            "variables": [v.model_dump(mode="json") for v in variables],
        },
    )


def freeze_batch_signals(step_input: StepInput, run_context: RunContext) -> StepOutput:
    ctx = run_context
    group = _event_run_state(ctx).get("signal_group")
    if group is None:
        return StepOutput(content={"signals_complete": True})
    key = f"signal-{group}"
    request = _required(ctx, key + "-input")
    response = _response(step_input, ctx, key, BatchSignalDecision)
    by_key = _coverage(response.events, [e["candidate_key"] for e in request["events"]])
    anchors = {a["uuid"]: AnchorCandidate.model_validate(a) for a in request["anchors"]}
    variables = {v["uuid"]: VariableCandidate.model_validate(v) for v in request["variables"]}
    journal = legacy._journal(ctx)
    rejected = []
    for e in request["events"]:
        item = by_key[e["candidate_key"]]
        event = journal.candidates[e["candidate_key"]].identity_request.candidate.event
        accepted, pairs = [], set()
        for draft in item.proposals:
            pair = draft.anchor_uuid, draft.variable_uuid
            reason = None
            if (
                draft.anchor_uuid not in set(e["allowed_uuids"])
                or draft.anchor_uuid not in anchors
                or draft.variable_uuid not in variables
            ):
                reason = "SIGNAL_OUTSIDE_EVENT_CANDIDATES"
            elif pair in pairs:
                reason = "DUPLICATE_SIGNAL_PAIR"
            else:
                try:
                    draft.proposal(
                        event_time=event_time_anchor(event.semantic.time),
                        reference_time=_batch(ctx).created_at,
                        assertion_modality={"FACT": "ACTUAL", "PLAN": "ANTICIPATED", "SPEC": "ASSUMED"}[
                            event.semantic.modality
                        ],
                    )
                except (ValidationError, OverflowError):
                    reason = "SIGNAL_CONSTRAINT_REJECTED"
            if reason:
                rejected.append(
                    {
                        "candidate_key": item.candidate_key,
                        "anchor_uuid": draft.anchor_uuid,
                        "variable_uuid": draft.variable_uuid,
                        "reason": reason,
                    }
                )
            else:
                pairs.add(pair)
                accepted.append(draft)
        by_key[item.candidate_key] = item.model_copy(
            update={
                "proposals": accepted,
                "no_signal_reason": None
                if accepted
                else item.no_signal_reason or "No valid signals after deterministic review",
            }
        )
    response = BatchSignalDecision(events=list(by_key.values()))
    if _read(ctx, key + "-result") is None:
        _freeze(ctx, key + "-rejections", {"items": rejected})
    _freeze(ctx, key + "-result", response.model_dump(mode="json"))
    # Sequential stage: reuse proven pair/type/time checks and compilation, not publication.
    for e in request["events"]:
        journal = legacy._journal(ctx)
        state = journal.candidates[e["candidate_key"]]
        if state.signal_results:
            continue
        proposals = by_key[e["candidate_key"]].proposals
        used_anchors = {p.anchor_uuid for p in proposals}
        used_variables = {p.variable_uuid for p in proposals}
        state.signal_pages = [
            CandidateSet(
                anchors=[AnchorCandidate.model_validate(a) for a in request["anchors"] if a["uuid"] in used_anchors],
                variables=[
                    VariableCandidate.model_validate(v) for v in request["variables"] if v["uuid"] in used_variables
                ],
            )
        ]
        write_storyline_journal(_batch(ctx), journal)
        _event_run_state(ctx)["storyline_candidate_key"] = e["candidate_key"]
        decision = SignalDecision.model_validate(by_key[e["candidate_key"]].model_dump(exclude={"candidate_key"}))
        legacy.freeze_storyline_signal_page(
            StepInput(previous_step_outputs={"signal": StepOutput(content=decision)}), ctx
        )
    _freeze(ctx, key + "-complete", {"complete": True})
    return StepOutput(content={"signals_complete": all(_read(ctx, f"signal-{g}-complete") is not None for g in GROUPS)})


def batch_signals_complete(iteration_outputs: list[StepOutput]) -> bool:
    return legacy._loop_flag(iteration_outputs, "signals_complete")


def freeze_publication_package(step_input: StepInput, run_context: RunContext) -> StepOutput:
    for group in GROUPS:
        _required(run_context, f"signal-{group}-complete")
    journal = legacy._journal(run_context)
    package = {
        "events": [
            {
                "candidate_key": key,
                "candidate": s.identity_request.candidate.model_dump(mode="json"),
                "resolution": s.resolution.model_dump(mode="json") if s.resolution else None,
                "associations": s.association_results[0].model_dump(mode="json"),
                "signals": [p.model_dump(mode="json") for p in s.proposals],
            }
            for key, s in journal.candidates.items()
        ]
    }
    _freeze(run_context, "publication-package", package)
    return StepOutput(content={"publication_ready": True})


async def publish_next_batch_event(step_input: StepInput, run_context: RunContext) -> StepOutput:
    _required(run_context, "publication-package")
    journal = legacy._journal(run_context)
    key = next((key for key, s in journal.candidates.items() if not s.done), None)
    if key is None:
        return StepOutput(content={"candidates_done": True})
    _event_run_state(run_context)["storyline_candidate_key"] = key
    await legacy.publish_storyline_candidate(step_input, run_context)
    return legacy.finish_storyline_candidate(step_input, run_context)


async def prepare_parallel_matches(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Commit identity and prepare one explicit input per native Parallel branch."""
    freeze_batch_identity(step_input, run_context)
    inputs = {}
    for group in GROUPS:
        prepared = await _prepare_match(run_context, group)
        inputs["batch-" + group] = prepared.content
    return StepOutput(content={"batch_inputs": inputs})


def collect_parallel_matches(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Join native outputs by stable Step ID; save good branches before reporting failures."""
    outputs: dict[str, StepOutput] = {}

    def visit(output: StepOutput) -> None:
        if output.step_id:
            outputs[output.step_id] = output
        for child in output.steps or []:
            visit(child)

    for output in (step_input.previous_step_outputs or {}).values():
        visit(output)
    failures = []
    for group in ("geo", "macro", "node", "company"):
        key = "match-" + group
        if _read(run_context, key + "-result") is not None:
            continue
        branch_output = outputs.get("batch-" + group)
        if branch_output is None or not branch_output.success:
            failures.append(key)
            continue
        try:
            _freeze_match(StepInput(previous_step_outputs={"result": branch_output}), run_context, key)
        except ValueError:
            failures.append(key)
    if failures:
        raise ValueError("missing batch checkpoint after Parallel: " + ", ".join(failures))
    return freeze_batch_associations(step_input, run_context)


async def publish_prepared_batch_event(step_input: StepInput, run_context: RunContext) -> StepOutput:
    """Freeze the package once, then reuse the existing receipt-aware publisher."""
    if _read(run_context, "publication-package") is None:
        freeze_publication_package(step_input, run_context)
    return await publish_next_batch_event(step_input, run_context)


BATCH_FUNCTIONS: list[Callable[..., Any]] = [
    prepare_parallel_matches,
    collect_parallel_matches,
    publish_prepared_batch_event,
    claim_parallel_batch,
    prepare_batch_identity,
    freeze_batch_identity,
    prepare_geo_matches,
    freeze_geo_matches,
    prepare_macro_matches,
    freeze_macro_matches,
    prepare_chain_matches,
    prepare_node_matches,
    freeze_node_matches,
    prepare_company_matches,
    freeze_company_matches,
    freeze_batch_associations,
    prepare_batch_signals,
    freeze_batch_signals,
    batch_signals_complete,
    freeze_publication_package,
    publish_next_batch_event,
]
