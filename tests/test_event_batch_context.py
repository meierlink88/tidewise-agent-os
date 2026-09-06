import unittest

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.session.agent import AgentSession

from agents.event_batch import CONTRACT, batch_agent


def test_batch_extraction_preserves_business_contract_in_model_message():
    original = Agent(
        model=OpenAIResponses(id="test", api_key="test"),
        instructions="Studio custom extraction instructions",
        additional_context="Business rule: missing business time is not grounds for rejection.",
    )
    batch = batch_agent(original, "batch-extract")
    message = batch.get_system_message(session=AgentSession(session_id="test"))
    assert message is not None
    assert original.additional_context in message.content
    assert CONTRACT.strip() in message.content
    assert "Studio custom extraction instructions" in message.content
    assert batch.additional_context.endswith(CONTRACT)
    assert original.additional_context == "Business rule: missing business time is not grounds for rejection."


def test_batch_extraction_without_existing_context():
    batch = batch_agent(Agent(), "batch-extract")
    assert batch.additional_context == CONTRACT


def test_other_batch_profiles_keep_their_existing_override_behavior():
    original = Agent(additional_context="Legacy single-event identity contract")
    batch = batch_agent(original, "batch-identity")
    assert original.additional_context not in batch.additional_context
    assert batch.additional_context == CONTRACT


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(test)
        for test in (
            test_batch_extraction_preserves_business_contract_in_model_message,
            test_batch_extraction_without_existing_context,
            test_other_batch_profiles_keep_their_existing_override_behavior,
        )
    )
