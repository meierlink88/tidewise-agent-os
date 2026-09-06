"""Tests for code-registered AgentOS model configurations."""

import os
import unittest
from unittest.mock import MagicMock, patch

from agno.agent import Agent
from agno.models.deepseek import DeepSeek
from agno.models.openai import OpenAIResponses

from agents.event_association import (
    LoadedEventAssociationAgent,
    build_event_association_agent,
    ensure_event_association_agent,
)
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
from app.settings import (
    SOL_LOW_DEFAULT_BASE_URL,
    SOL_LOW_MODEL_ID,
    event_model,
    is_event_model,
    is_sol_low_model,
    sol_low_model,
)


class ModelRegistryTest(unittest.TestCase):
    def test_association_model_migration_preserves_prompt_and_is_idempotent(self) -> None:
        for old_model, expected_version in ((sol_low_model(), 42), (event_model(), 41)):
            with self.subTest(model=old_model.id):
                agent = build_event_association_agent()
                agent.model = old_model
                agent.instructions = "Studio custom association instructions"
                loaded = LoadedEventAssociationAgent(agent, 41, "test-digest")
                with (
                    patch("agents.event_association.get_postgres_db", return_value=MagicMock()),
                    patch("agents.event_association.load_event_association_agent", return_value=loaded),
                    patch.object(Agent, "save", autospec=True, return_value=42) as save,
                ):
                    self.assertEqual(ensure_event_association_agent(registry), expected_version)
                if expected_version == 42:
                    repaired = save.call_args.args[0]
                    self.assertTrue(is_event_model(repaired.model))
                    self.assertEqual(repaired.instructions, agent.instructions)
                    self.assertIsNotNone(repaired.skills)
                else:
                    save.assert_not_called()

    def test_filter_low_survives_registry_round_trip_without_changing_other_agents(self) -> None:
        with patch.dict(os.environ, {"RAW_EVIDENCE_FILTER_REASONING_EFFORT": "low"}):
            agent = build_title_curator_agent()
            restored = Agent.from_dict(agent.to_dict(), registry=registry)
            assert isinstance(restored.model, OpenAIResponses)
            self.assertEqual(restored.model.reasoning_effort, "low")
            self.assertTrue(restored.structured_outputs)
            self.assertFalse(restored.use_json_mode)
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
            self.assertTrue(loaded.model.strict_output)
            self.assertTrue(loaded.structured_outputs)
            self.assertFalse(loaded.use_json_mode)
            self.assertTrue(is_sol_low_model(build_evidence_extractor_agent().model))
            self.assertTrue(is_sol_low_model(registry.get_model(SOL_LOW_MODEL_ID)))

    def test_legacy_filter_override_cannot_restore_other_efforts(self) -> None:
        for effort in ("none", "medium", "light"):
            with patch.dict(os.environ, {"RAW_EVIDENCE_FILTER_REASONING_EFFORT": effort}):
                model = build_title_curator_agent().model
                assert isinstance(model, OpenAIResponses)
                self.assertEqual(model.reasoning_effort, "low")

    def test_registry_exposes_one_gpt_but_restores_legacy_names(self) -> None:
        self.assertEqual(len([m for m in registry.models if m.id == SOL_LOW_MODEL_ID]), 1)
        for name in ("OpenAIResponses", "RawEvidenceFilter-low", "RawEvidenceFilter-none"):
            original = Agent(model=OpenAIResponses(id=SOL_LOW_MODEL_ID, name=name))
            restored = Agent.from_dict(original.to_dict(), registry=registry)
            assert isinstance(restored.model, OpenAIResponses)
            self.assertEqual(restored.model.reasoning_effort, "low")
            canonical = registry.get_model(SOL_LOW_MODEL_ID)
            assert isinstance(canonical, OpenAIResponses)
            self.assertEqual(restored.model.base_url, canonical.base_url)

    def test_reviewer_effort_migration_preserves_studio_prompt(self) -> None:
        current = build_title_curator_agent()
        current.instructions = "Studio custom instructions"
        assert current.metadata is not None
        current.metadata["raw_evidence_filter_reasoning_effort"] = "medium"
        db = MagicMock()
        db.get_component.return_value = {"current_version": 29}
        with (
            patch("agents.title_curator.get_postgres_db", return_value=db),
            patch("agents.title_curator.Agent.load", return_value=current),
            patch.object(current, "save", return_value=30),
        ):
            self.assertEqual(ensure_title_curator_agent(registry), 30)
        self.assertEqual(current.instructions, "Studio custom instructions")
        assert isinstance(current.model, OpenAIResponses)
        self.assertEqual(current.model.reasoning_effort, "low")

    def test_sol_low_model_uses_agno_openai_responses_configuration(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-openai-key",
                "OPENAI_BASE_URL": "https://proxy.example/v1",
            },
        ):
            model = sol_low_model()

        self.assertIsInstance(model, OpenAIResponses)
        self.assertEqual(model.id, SOL_LOW_MODEL_ID)
        self.assertEqual(model.api_key, "test-openai-key")
        self.assertEqual(str(model.base_url), "https://proxy.example/v1")
        self.assertEqual(model.reasoning_effort, "low")
        self.assertIs(model.store, False)

    def test_sol_low_model_defaults_to_verified_proxy(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            model = sol_low_model()

        self.assertIsNone(model.api_key)
        self.assertEqual(str(model.base_url), SOL_LOW_DEFAULT_BASE_URL)

    def test_registry_keeps_deepseek_and_adds_sol_low(self) -> None:
        deepseek = registry.get_model("deepseek-v4-flash")
        sol = registry.get_model(SOL_LOW_MODEL_ID)

        self.assertIsInstance(deepseek, DeepSeek)
        self.assertIsInstance(sol, OpenAIResponses)
        assert isinstance(sol, OpenAIResponses)
        self.assertEqual(sol.reasoning_effort, "low")

    @patch.dict(os.environ, {"RAW_EVIDENCE_FILTER_REASONING_EFFORT": "medium"})
    def test_only_event_agents_switch_to_deepseek(self) -> None:
        other_agents = (
            tidewise_assistant,
            build_title_curator_agent(),
            build_evidence_extractor_agent(),
            build_investment_reasoner_agent(),
            build_investment_report_writer_agent(),
            build_investment_reviewer_agent(),
        )

        event_agents = (
            build_event_association_agent(),
            build_event_extractor_agent(),
            build_event_identity_agent(),
            build_event_signal_analyst_agent(),
        )
        for agent in other_agents:
            with self.subTest(agent=agent.name):
                self.assertTrue(is_sol_low_model(agent.model))
        for agent in event_agents:
            with self.subTest(agent=agent.name):
                self.assertTrue(is_event_model(agent.model))


if __name__ == "__main__":
    unittest.main()
