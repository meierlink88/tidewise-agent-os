"""Tests for code-registered AgentOS model configurations."""

import os
import unittest
from unittest.mock import MagicMock, patch

from agno.agent import Agent
from agno.models.deepseek import DeepSeek
from agno.models.openai import OpenAIResponses

from agents.event_extractor import build_event_extractor_agent
from agents.event_identity import build_event_identity_agent
from agents.event_signal_analyst import build_event_signal_analyst_agent
from agents.evidence_extractor import build_evidence_extractor_agent
from agents.investment_reasoner import build_investment_reasoner_agent
from agents.investment_report_writer import build_investment_report_writer_agent
from agents.investment_reviewer import build_investment_reviewer_agent
from agents.tidewise_assistant import tidewise_assistant
from agents.title_curator import build_title_curator_agent, ensure_title_curator_agent, load_title_curator_agent
from app.registry import registry
from app.settings import SOL_MEDIUM_DEFAULT_BASE_URL, SOL_MEDIUM_MODEL_ID, is_sol_medium_model, sol_medium_model


class ModelRegistryTest(unittest.TestCase):
    def test_filter_low_survives_registry_round_trip_without_changing_other_agents(self) -> None:
        with patch.dict(os.environ, {"RAW_EVIDENCE_FILTER_REASONING_EFFORT": "low"}):
            agent = build_title_curator_agent()
            restored = Agent.from_dict(agent.to_dict(), registry=registry)
            assert isinstance(restored.model, OpenAIResponses)
            self.assertEqual(restored.model.reasoning_effort, "low")
            db = MagicMock()
            db.get_component.return_value = {"current_version": 29}
            with (
                patch("agents.title_curator.get_postgres_db", return_value=db),
                patch("agents.title_curator.Agent.load", return_value=restored),
            ):
                self.assertEqual(ensure_title_curator_agent(registry), 29)
                loaded = load_title_curator_agent(registry).agent
            assert isinstance(loaded.model, OpenAIResponses)
            self.assertEqual(loaded.model.reasoning_effort, "low")
            self.assertTrue(is_sol_medium_model(build_evidence_extractor_agent().model))
            self.assertTrue(is_sol_medium_model(registry.get_model(SOL_MEDIUM_MODEL_ID)))

    def test_filter_rejects_unknown_reasoning_level(self) -> None:
        with patch.dict(os.environ, {"RAW_EVIDENCE_FILTER_REASONING_EFFORT": "light"}):
            with self.assertRaisesRegex(ValueError, "must be none, low, or medium"):
                build_title_curator_agent()

    def test_sol_medium_model_uses_agno_openai_responses_configuration(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-openai-key",
                "OPENAI_BASE_URL": "https://proxy.example/v1",
            },
        ):
            model = sol_medium_model()

        self.assertIsInstance(model, OpenAIResponses)
        self.assertEqual(model.id, SOL_MEDIUM_MODEL_ID)
        self.assertEqual(model.api_key, "test-openai-key")
        self.assertEqual(str(model.base_url), "https://proxy.example/v1")
        self.assertEqual(model.reasoning_effort, "medium")
        self.assertIs(model.store, False)

    def test_sol_medium_model_defaults_to_verified_proxy(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            model = sol_medium_model()

        self.assertIsNone(model.api_key)
        self.assertEqual(str(model.base_url), SOL_MEDIUM_DEFAULT_BASE_URL)

    def test_registry_keeps_deepseek_and_adds_sol_medium(self) -> None:
        deepseek = registry.get_model("deepseek-v4-flash")
        sol = registry.get_model(SOL_MEDIUM_MODEL_ID)

        self.assertIsInstance(deepseek, DeepSeek)
        self.assertIsInstance(sol, OpenAIResponses)
        assert isinstance(sol, OpenAIResponses)
        self.assertEqual(sol.reasoning_effort, "medium")

    @patch.dict(os.environ, {"RAW_EVIDENCE_FILTER_REASONING_EFFORT": "medium"})
    def test_all_agents_use_sol_medium(self) -> None:
        agents = (
            tidewise_assistant,
            build_title_curator_agent(),
            build_evidence_extractor_agent(),
            build_event_extractor_agent(),
            build_event_identity_agent(),
            build_event_signal_analyst_agent(),
            build_investment_reasoner_agent(),
            build_investment_report_writer_agent(),
            build_investment_reviewer_agent(),
        )

        self.assertEqual(len(agents), 9)
        self.assertTrue(all(is_sol_medium_model(agent.model) for agent in agents))


if __name__ == "__main__":
    unittest.main()
