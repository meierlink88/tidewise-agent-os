"""Single-batch Event operations and deterministic direct-Step execution gates."""

import json
from typing import Any

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput
from pydantic import BaseModel

from capabilities.event.functions import storyline as operations
from capabilities.event.functions.extraction import _batch, _event_run_state


def event_semantic_input_text(content: Any) -> str:
    """Encode business data as user content, never an Agno Message dictionary."""
    if isinstance(content, BaseModel):
        payload = content.model_dump(mode="json")
    elif isinstance(content, dict):
        if not isinstance(content.get("event"), dict) or not content["event"]:
            raise ValueError("Event Agent input requires an Event object")
        if not isinstance(content.get("classification"), dict) or not content["classification"]:
            raise ValueError("Event Agent input requires frozen classification")
        candidates = content.get("candidates")
        if isinstance(candidates, list):
            valid = bool(candidates) and all(isinstance(row, dict) and row.get("uuid") for row in candidates)
        elif isinstance(candidates, dict):
            valid = all(
                isinstance(candidates.get(key), list)
                and candidates[key]
                and all(isinstance(row, dict) and row.get("uuid") for row in candidates[key])
                for key in ("anchors", "variables")
            )
        else:
            valid = False
        if not valid:
            raise ValueError("Event Agent input requires nonempty identified candidates")
        payload = content
    else:
        raise ValueError("Event Agent input must be a structured business object")
    if not payload:
        raise ValueError("Event Agent input cannot be empty")
    try:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        raise ValueError("Event Agent input is not valid JSON data") from None


async def prepare_linear_candidate(step_input: StepInput, run_context: RunContext) -> StepOutput:
    active = operations.has_storyline_candidates(step_input, run_context)
    _event_run_state(run_context)["linear_candidate_active"] = active
    if not active:
        return StepOutput(content={"candidates_done": True})
    return await operations.prepare_storyline_candidate(step_input, run_context)


def _active(context: RunContext) -> bool:
    return _event_run_state(context).get("linear_candidate_active", False)


def _publishable(step_input: StepInput, context: RunContext) -> bool:
    return _active(context) and operations.is_publishable_storyline(step_input, context)


async def prepare_linear_catalog(step_input: StepInput, run_context: RunContext) -> StepOutput:
    if not _publishable(step_input, run_context):
        return StepOutput(content={"skipped": "event_not_publishable"})
    return await operations.prepare_storyline_catalog(step_input, run_context)


async def prepare_linear_signals(step_input: StepInput, run_context: RunContext) -> StepOutput:
    if not _publishable(step_input, run_context):
        return StepOutput(content={"skipped": "event_not_publishable"})
    return await operations.prepare_storyline_signals(step_input, run_context)


async def complete_linear_candidate(step_input: StepInput, run_context: RunContext) -> StepOutput:
    if not _active(run_context):
        return StepOutput(content={"candidates_done": True})
    if _publishable(step_input, run_context):
        await operations.publish_storyline_candidate(step_input, run_context)
    return operations.finish_storyline_candidate(step_input, run_context)


async def prepare_event_semantic_call(agent_id: str, step_input: StepInput, context: RunContext) -> StepOutput | None:
    """Return authoritative input, or skip without ever asking an Agent to decide."""
    if agent_id == "event-extractor":
        batch = _batch(context)
        return StepOutput(content=batch) if batch.needs_analysis else None
    if not _active(context):
        return None
    if agent_id == "event-identity":
        if operations.needs_storyline_identity(step_input, context):
            return await operations.prepare_storyline_candidate(step_input, context)
        return None
    if not _publishable(step_input, context):
        return None
    if agent_id == "event-association":
        if operations.has_association_pages(step_input, context):
            return operations.prepare_association_page(step_input, context)
        return None
    if agent_id == "event-signal-analyst":
        if operations.has_storyline_signal_pages(step_input, context):
            return operations.prepare_storyline_signal_page(step_input, context)
        return None
    raise ValueError("unsupported Event semantic Agent")


async def settle_event_semantic_call(agent_id: str, output: StepOutput, context: RunContext) -> bool:
    """Validate and checkpoint before another technical page; never invoke an Agent."""
    predecessor = StepInput(previous_step_outputs={"semantic": output})
    if agent_id == "event-extractor":
        operations.freeze_storyline_draft(predecessor, context)
        return False
    if agent_id == "event-identity":
        operations.freeze_storyline_identity(predecessor, context)
        return False
    if agent_id == "event-association":
        await operations.freeze_association_page(predecessor, context)
        return operations.has_association_pages(predecessor, context)
    if agent_id == "event-signal-analyst":
        operations.freeze_storyline_signal_page(predecessor, context)
        return operations.has_storyline_signal_pages(predecessor, context)
    raise ValueError("unsupported Event semantic Agent")


LINEAR_EVENT_FUNCTIONS = [
    prepare_linear_candidate,
    prepare_linear_catalog,
    prepare_linear_signals,
    complete_linear_candidate,
]
