"""Execution-only gates for native Event Agent Steps in Agno 3.0.1.

Studio still stores ordinary direct Agent Steps. Functions own every decision and
checkpoint; this adapter invokes only that Step's pinned Agent, including technical
catalog pages. No business loop or Condition is added to the editable topology.
"""

import asyncio
from dataclasses import replace
from types import MethodType
from typing import Any

from agno.exceptions import RunCancelledException
from agno.run.agent import RunCancelledEvent, RunErrorEvent
from agno.workflow import Step, StepInput, StepOutput

from capabilities.event.functions.linear import prepare_event_semantic_call, settle_event_semantic_call

_execute = Step.execute
_aexecute = Step.aexecute
_execute_stream = Step.execute_stream
_aexecute_stream = Step.aexecute_stream


def _agent_id(step: Step) -> str:
    if step.agent is None or not step.agent.id:
        raise ValueError("Event execution gate requires a pinned Agent")
    return step.agent.id


def _input(original: StepInput, prepared: StepOutput) -> StepInput:
    return replace(original, previous_step_content=prepared.content, previous_step_outputs={"prepared": prepared})


def _result(outputs: list[StepOutput]) -> StepOutput:
    if not outputs:
        return StepOutput(content={"skipped": "code_owned_event_gate"})
    return StepOutput(content=outputs[-1].content, steps=outputs)


def _execute_event(step: Step, step_input: StepInput, **kwargs: Any) -> StepOutput:
    context = kwargs["run_context"]
    outputs = []
    while prepared := asyncio.run(prepare_event_semantic_call(_agent_id(step), step_input, context)):
        output = _execute(step, _input(step_input, prepared), **kwargs)
        more = asyncio.run(settle_event_semantic_call(_agent_id(step), output, context))
        outputs.append(output)
        if not more:
            break
    return _result(outputs)


async def _aexecute_event(step: Step, step_input: StepInput, **kwargs: Any) -> StepOutput:
    context = kwargs["run_context"]
    outputs = []
    while prepared := await prepare_event_semantic_call(_agent_id(step), step_input, context):
        output = await _aexecute(step, _input(step_input, prepared), **kwargs)
        more = await settle_event_semantic_call(_agent_id(step), output, context)
        outputs.append(output)
        if not more:
            break
    return _result(outputs)


def _check_event(event: Any) -> None:
    if isinstance(event, RunCancelledEvent):
        raise RunCancelledException(event.reason or "Cancelled")
    if isinstance(event, RunErrorEvent):
        raise RuntimeError("Event semantic Agent failed")


def _execute_event_stream(step: Step, step_input: StepInput, **kwargs: Any) -> Any:
    context = kwargs["run_context"]
    outputs = []
    while prepared := asyncio.run(prepare_event_semantic_call(_agent_id(step), step_input, context)):
        output = None
        for event in _execute_stream(step, _input(step_input, prepared), **kwargs):
            _check_event(event)
            if isinstance(event, StepOutput):
                output = event
            else:
                yield event
        if output is None:
            raise RuntimeError("Event Agent stream returned no StepOutput")
        more = asyncio.run(settle_event_semantic_call(_agent_id(step), output, context))
        outputs.append(output)
        if not more:
            break
    yield _result(outputs)


async def _aexecute_event_stream(step: Step, step_input: StepInput, **kwargs: Any) -> Any:
    context = kwargs["run_context"]
    outputs = []
    while prepared := await prepare_event_semantic_call(_agent_id(step), step_input, context):
        output = None
        async for event in _aexecute_stream(step, _input(step_input, prepared), **kwargs):
            _check_event(event)
            if isinstance(event, StepOutput):
                output = event
            else:
                yield event
        if output is None:
            raise RuntimeError("Event Agent stream returned no StepOutput")
        more = await settle_event_semantic_call(_agent_id(step), output, context)
        outputs.append(output)
        if not more:
            break
    yield _result(outputs)


def bind_event_step(step: Step) -> None:
    step.execute = MethodType(_execute_event, step)  # type: ignore[method-assign]
    step.aexecute = MethodType(_aexecute_event, step)  # type: ignore[method-assign]
    step.execute_stream = MethodType(_execute_event_stream, step)  # type: ignore[method-assign]
    step.aexecute_stream = MethodType(_aexecute_event_stream, step)  # type: ignore[method-assign]
