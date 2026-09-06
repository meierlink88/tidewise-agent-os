"""Event model migration and SDK retry boundary."""

import unittest
from unittest.mock import patch

import httpx
from agno.agent import Agent
from agno.models.deepseek import DeepSeek
from openai import APITimeoutError

from agents.event_association import build_event_association_agent
from agents.event_batch import batch_agent
from agents.event_extractor import build_event_extractor_agent
from agents.event_identity import build_event_identity_agent
from agents.event_signal_analyst import build_event_signal_analyst_agent
from app.registry import registry
from app.settings import default_model, event_model, is_event_model


class EventModelsTest(unittest.IsolatedAsyncioTestCase):
    def test_all_event_agents_disable_thinking_independent_of_shared_default(self):
        with patch.dict("os.environ", {"DEEPSEEK_USE_THINKING": "true"}):
            for build in (
                build_event_extractor_agent,
                build_event_identity_agent,
                build_event_association_agent,
                build_event_signal_analyst_agent,
            ):
                with self.subTest(agent=build.__name__):
                    model = build().model
                    self.assertIsInstance(model, DeepSeek)
                    self.assertTrue(is_event_model(model))
                    self.assertFalse(model.use_thinking)
                    self.assertIsNone(model.reasoning_effort)
            self.assertTrue(default_model().use_thinking)

    def test_batch_profile_survives_registry_model_rehydration(self):
        model = Agent.from_dict(Agent(model=event_model()).to_dict(), registry=registry).model
        with patch.dict("os.environ", {"DEEPSEEK_USE_THINKING": "false"}):
            self.assertTrue(is_event_model(model))
        runtime_model = batch_agent(Agent(model=model), "batch-extract").model
        self.assertEqual(runtime_model.max_tokens, 32768)
        self.assertEqual(runtime_model.max_retries, 0)
        self.assertEqual(runtime_model.timeout, 180)
        params = runtime_model.get_request_params(response_format={"type": "json_object"})
        self.assertEqual(params["extra_body"]["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", params)
        self.assertEqual(params["response_format"], {"type": "json_object"})

    def test_old_thinking_profile_stays_loadable_but_requires_migration(self):
        self.assertFalse(is_event_model(default_model()))
        for effort in ("low", "high", "max"):
            model = event_model()
            model.reasoning_effort = effort
            self.assertFalse(is_event_model(model))
        old = event_model()
        old.name = "EventDeepSeek-low"
        old.use_thinking = True
        old.reasoning_effort = "low"
        restored = Agent.from_dict(Agent(model=old).to_dict(), registry=registry).model
        self.assertTrue(restored.use_thinking)
        self.assertEqual(restored.reasoning_effort, "low")
        self.assertFalse(is_event_model(restored))

    async def test_transport_timeout_is_not_retried_by_sdk(self):
        attempts = []

        async def timeout(request):
            attempts.append(request)
            raise httpx.ReadTimeout("offline simulated timeout", request=request)

        model = batch_agent(Agent(model=event_model()), "batch-extract").model
        model.api_key = "test"
        model.base_url = "https://invalid.example/v1"
        model.http_client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        client = model.get_async_client()
        try:
            with self.assertRaises(APITimeoutError):
                await client.chat.completions.create(model=model.id, messages=[{"role": "user", "content": "test"}])
            self.assertEqual(len(attempts), 1)
            self.assertEqual(client.max_retries, 0)
            self.assertEqual(client.timeout, 180)
        finally:
            await client.close()
