"""Model binding and prompt-preserving migration for the collection Reviewer."""

import unittest
from unittest.mock import MagicMock, patch

from agno.models.deepseek import DeepSeek

from agents.title_curator import build_title_curator_agent, ensure_title_curator_agent


class ReviewerDeepSeekTests(unittest.TestCase):
    def test_review_uses_non_thinking_json_mode(self):
        agent = build_title_curator_agent()
        assert isinstance(agent.model, DeepSeek)
        self.assertFalse(agent.model.use_thinking)
        self.assertIsNone(agent.model.reasoning_effort)
        self.assertTrue(agent.use_json_mode)
        self.assertFalse(agent.structured_outputs)
        self.assertEqual(agent.model.timeout, 120)
        params = agent.model.get_request_params()
        self.assertEqual(params["extra_body"]["thinking"], {"type": "disabled"})

    def test_model_migration_preserves_published_prompt_and_is_idempotent(self):
        agent = build_title_curator_agent()
        assert agent.metadata is not None
        agent.metadata["title_curator_contract_version"] = 13
        agent.instructions = "Operator-maintained review instructions"
        db = MagicMock()
        db.get_component.return_value = {"current_version": 7}
        with (
            patch("agents.title_curator.get_postgres_db", return_value=db),
            patch("agents.title_curator.Agent.load", return_value=agent),
            patch.object(type(agent), "save", return_value=8) as save,
        ):
            self.assertEqual(ensure_title_curator_agent(MagicMock()), 8)
            self.assertEqual(agent.instructions, "Operator-maintained review instructions")
            assert agent.metadata is not None
            self.assertEqual(agent.metadata["title_curator_contract_version"], 14)
            save.assert_called_once()
            db.get_component.return_value = {"current_version": 8}
            self.assertEqual(ensure_title_curator_agent(MagicMock()), 8)
            save.assert_called_once()
