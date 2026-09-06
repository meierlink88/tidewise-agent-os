"""Native Step single-call transport/skip binding; no semantic iteration here."""

from dataclasses import replace
from types import MethodType

from agno.workflow import Step, StepInput, StepOutput

from agents.event_batch import batch_agent
from app.event_step_runtime import _check_event

_execute = Step.execute
_aexecute = Step.aexecute
_execute_stream = Step.execute_stream
_aexecute_stream = Step.aexecute_stream


def _input(value: StepInput, step_id: str | None = None) -> StepInput | None:
    envelope = value.previous_step_content
    if isinstance(envelope, dict) and "batch_inputs" in envelope:
        envelope = envelope["batch_inputs"].get(step_id)
    if not isinstance(envelope, dict) or "batch_call" not in envelope or "input" not in envelope:
        raise ValueError("batch Step requires prepared input envelope")
    if envelope["input"] is None:
        return None
    if not isinstance(envelope["input"], str):
        raise ValueError("batch Agent input must be explicit JSON text")
    message = StepOutput(content=envelope["input"])
    return replace(value, previous_step_content=message.content, previous_step_outputs={"prepared": message})


def _execute_batch(step, step_input, **kwargs):
    prepared = _input(step_input, step.step_id)
    return _execute(step, prepared, **kwargs) if prepared else StepOutput(content={"skipped": True})


async def _aexecute_batch(step, step_input, **kwargs):
    prepared = _input(step_input, step.step_id)
    return await _aexecute(step, prepared, **kwargs) if prepared else StepOutput(content={"skipped": True})


def _stream_batch(step, step_input, **kwargs):
    prepared = _input(step_input, step.step_id)
    if prepared is None:
        yield StepOutput(content={"skipped": True})
        return
    for event in _execute_stream(step, prepared, **kwargs):
        _check_event(event)
        yield event


async def _astream_batch(step, step_input, **kwargs):
    prepared = _input(step_input, step.step_id)
    if prepared is None:
        yield StepOutput(content={"skipped": True})
        return
    async for event in _aexecute_stream(step, prepared, **kwargs):
        _check_event(event)
        yield event


def bind_batch_step(step: Step) -> None:
    if step.agent is None or step.step_id is None:
        raise ValueError("batch Agent Step requires stable identity")
    step.agent = batch_agent(step.agent, step.step_id)
    step._set_active_executor()
    step.execute = MethodType(_execute_batch, step)  # type: ignore[method-assign]
    step.aexecute = MethodType(_aexecute_batch, step)  # type: ignore[method-assign]
    step.execute_stream = MethodType(_stream_batch, step)  # type: ignore[method-assign]
    step.aexecute_stream = MethodType(_astream_batch, step)  # type: ignore[method-assign]
