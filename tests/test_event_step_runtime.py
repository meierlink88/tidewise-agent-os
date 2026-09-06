"""Native direct-Step gates behave consistently in all execution modes."""

import unittest
from unittest.mock import AsyncMock, patch

from agno.agent import Agent
from agno.exceptions import RunCancelledException
from agno.run.agent import RunCancelledEvent
from agno.workflow import Step, StepInput, StepOutput

from app import event_step_runtime as runtime

PAYLOAD = {
    "event": {"title": "test"},
    "classification": {"event_class": "COMPANY"},
    "candidates": [{"uuid": "test-candidate"}],
}


class EventStepRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.step = Step(name="renamed", agent=Agent(id="event-association"))
        self.input = StepInput(input="untrusted user request")

    def test_sync_and_sync_stream_skip_without_invocation(self):
        with (
            patch.object(runtime, "prepare_event_semantic_call", new=AsyncMock(return_value=None)),
            patch.object(runtime, "_execute") as execute,
            patch.object(runtime, "_execute_stream") as stream,
        ):
            output = runtime._execute_event(self.step, self.input, run_context=object())
            streamed = list(runtime._execute_event_stream(self.step, self.input, run_context=object()))
        self.assertFalse(output.stop)
        self.assertFalse(streamed[-1].stop)
        execute.assert_not_called()
        stream.assert_not_called()

    def test_sync_pages_validate_between_calls_and_preserve_outputs(self):
        order = []

        async def prepare(*args):
            order.append("prepare")
            return StepOutput(content=PAYLOAD)

        def execute(step, supplied, **kwargs):
            order.append("agent")
            self.assertIn("candidates", supplied.previous_step_content)
            return StepOutput(content={"matches": []})

        async def settle(*args):
            order.append("validate")
            return len(order) == 3

        with (
            patch.object(runtime, "prepare_event_semantic_call", new=prepare),
            patch.object(runtime, "settle_event_semantic_call", new=settle),
            patch.object(runtime, "_execute", new=execute),
        ):
            output = runtime._execute_event(self.step, self.input, run_context=object())
        self.assertEqual(order, ["prepare", "agent", "validate"] * 2)
        self.assertEqual(len(output.steps), 2)


class AsyncEventStepRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_async_and_stream_skip_without_invocation(self):
        step = Step(name="renamed", agent=Agent(id="event-association"))
        with (
            patch.object(runtime, "prepare_event_semantic_call", new=AsyncMock(return_value=None)),
            patch.object(runtime, "_aexecute") as execute,
            patch.object(runtime, "_aexecute_stream") as stream,
        ):
            output = await runtime._aexecute_event(step, StepInput(), run_context=object())
            outputs = [o async for o in runtime._aexecute_event_stream(step, StepInput(), run_context=object())]
        self.assertFalse(output.stop)
        self.assertFalse(outputs[-1].stop)
        execute.assert_not_called()
        stream.assert_not_called()

    async def test_stream_cancellation_never_validates_or_retries(self):
        step = Step(name="renamed", agent=Agent(id="event-association"))

        async def stream(*args, **kwargs):
            yield RunCancelledEvent(reason="cancelled")

        with (
            patch.object(
                runtime, "prepare_event_semantic_call", new=AsyncMock(return_value=StepOutput(content=PAYLOAD))
            ),
            patch.object(runtime, "settle_event_semantic_call", new=AsyncMock()) as settle,
            patch.object(runtime, "_aexecute_stream", new=stream),
        ):
            with self.assertRaises(RunCancelledException):
                async for _ in runtime._aexecute_event_stream(step, StepInput(), run_context=object()):
                    pass
        settle.assert_not_called()
