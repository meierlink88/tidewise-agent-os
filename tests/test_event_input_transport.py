"""Exercise real Agno message construction, without model/network mocks."""

import json
import unittest

from agno.agent import Agent, _messages
from agno.models.openai import OpenAIChat
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.session import AgentSession
from agno.workflow import Step, StepInput, StepOutput

from app.event_step_runtime import _input
from capabilities.event.internal.models import EventIdentityRequest
from tests import test_event_extraction as fixtures


class EventInputTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_business_payload_survives_real_message_construction(self):
        for agent_id, candidates in (
            ("event-association", [{"uuid": "ANCHOR_SENTINEL", "name": "产业节点"}]),
            (
                "event-signal-analyst",
                {"anchors": [{"uuid": "ANCHOR_SENTINEL"}], "variables": [{"uuid": "VARIABLE_SENTINEL"}]},
            ),
        ):
            with self.subTest(agent_id=agent_id):
                payload = {
                    "event": {"title": "事件哨兵", "amount": 123, "optional": None},
                    "classification": {"event_class": "INDUSTRY_CHAIN"},
                    "candidates": candidates,
                }
                agent = Agent(id=agent_id, model=OpenAIChat(id="unused", api_key="unused"))
                step = Step(name="renamed", agent=agent)
                step._executor_type = "agent"
                supplied = _input(StepInput(input="not the event"), StepOutput(content=payload))
                message = step._prepare_message(supplied.previous_step_content, supplied.previous_step_outputs)
                messages = await _messages.aget_run_messages(
                    agent,
                    run_response=RunOutput(),
                    run_context=RunContext(run_id="probe", session_id="probe"),
                    input=message,
                    session=AgentSession(session_id="probe"),
                )
                user = [m for m in messages.messages if m.role == "user"]
                self.assertEqual(len(user), 1, "Event payload was dropped before model invocation")
                self.assertEqual(json.loads(user[0].content), payload)

    async def test_typed_identity_input_and_nested_predecessor_are_preserved(self):
        content = EventIdentityRequest(
            candidate_key="a" * 64,
            candidate=fixtures.EventExtractionWorkflowTest.extraction_draft().candidates[0],
            historical_candidates=[],
        )
        prepared = StepOutput(content=content, steps=[StepOutput(content={"stale": "must not replace payload"})])
        supplied = _input(StepInput(), prepared)
        agent = Agent(id="event-identity", model=OpenAIChat(id="unused", api_key="unused"))
        step = Step(name="renamed", agent=agent)
        step._executor_type = "agent"
        message = step._prepare_message(supplied.previous_step_content, supplied.previous_step_outputs)
        messages = _messages.get_run_messages(
            agent,
            run_response=RunOutput(),
            run_context=RunContext(run_id="probe", session_id="probe"),
            input=message,
            session=AgentSession(session_id="probe"),
        )
        user = [m for m in messages.messages if m.role == "user"]
        self.assertEqual(json.loads(user[0].content), content.model_dump(mode="json"))
        self.assertIs(prepared.content, content)
        self.assertIsNotNone(prepared.steps)

    def test_missing_and_unserializable_inputs_fail_before_agent(self):
        valid = {
            "event": {"title": "test"},
            "classification": {"event_class": "COMPANY"},
            "candidates": [{"uuid": "candidate"}],
        }
        for invalid in (
            None,
            {},
            "",
            {**valid, "event": None},
            {**valid, "classification": None},
            {**valid, "candidates": []},
            {**valid, "bad": object()},
            {**valid, "bad": float("nan")},
        ):
            with self.subTest(value=type(invalid).__name__), self.assertRaises(ValueError):
                _input(StepInput(), StepOutput(content=invalid))
