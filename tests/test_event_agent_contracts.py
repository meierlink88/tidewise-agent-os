"""Code-owned runtime contract tests for Studio-managed Event Agents."""

import unittest
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

from agno.agent import Agent

from agents.event_extractor import (
    EVENT_EXTRACTOR_CONTRACT_VERSION,
    build_event_extractor_agent,
    ensure_event_extractor_agent,
)
from agents.event_identity import (
    EVENT_IDENTITY_CONTRACT_VERSION,
    build_event_identity_agent,
    ensure_event_identity_agent,
)
from agents.event_signal_analyst import (
    EVENT_SIGNAL_ANALYST_CONTRACT_VERSION,
    build_event_signal_analyst_agent,
    ensure_event_signal_analyst_agent,
)
from capabilities.event import EventExtractionDraft, EventIdentityDecision, EventSignalAnalysisDraft


class EventAgentContractTest(unittest.TestCase):
    CASES: tuple[
        tuple[
            str,
            str,
            str,
            int,
            type[Any],
            Callable[[], Agent],
            Callable[[Any], int],
        ],
        ...,
    ] = (
        (
            "agents.event_extractor",
            "event-extractor",
            "event_extractor_contract_version",
            EVENT_EXTRACTOR_CONTRACT_VERSION,
            EventExtractionDraft,
            build_event_extractor_agent,
            ensure_event_extractor_agent,
        ),
        (
            "agents.event_identity",
            "event-identity",
            "event_identity_contract_version",
            EVENT_IDENTITY_CONTRACT_VERSION,
            EventIdentityDecision,
            build_event_identity_agent,
            ensure_event_identity_agent,
        ),
        (
            "agents.event_signal_analyst",
            "event-signal-analyst",
            "event_signal_analyst_contract_version",
            EVENT_SIGNAL_ANALYST_CONTRACT_VERSION,
            EventSignalAnalysisDraft,
            build_event_signal_analyst_agent,
            ensure_event_signal_analyst_agent,
        ),
    )

    def test_prompt_only_studio_versions_keep_their_published_version(self) -> None:
        for module, agent_id, _, _, _, build, ensure in self.CASES:
            with self.subTest(agent_id=agent_id):
                database = MagicMock()
                database.get_component.return_value = {"current_version": 41}
                current = build()
                current.instructions = f"Studio maintained instructions for {agent_id}"
                with (
                    patch(f"{module}.get_postgres_db", return_value=database),
                    patch(f"{module}.Agent.load", return_value=current),
                    patch.object(current, "save", autospec=True) as save,
                ):
                    self.assertEqual(ensure(MagicMock()), 41)

                self.assertEqual(current.instructions, f"Studio maintained instructions for {agent_id}")
                save.assert_not_called()

    def test_code_owned_contract_drift_is_repaired_without_replacing_the_studio_prompt(self) -> None:
        for module, agent_id, contract_key, contract_version, output_schema, build, ensure in self.CASES:
            with self.subTest(agent_id=agent_id):
                database = MagicMock()
                database.get_component.return_value = {"current_version": 41}
                instructions = f"Studio customized prompt for {agent_id}"
                current = build()
                current.instructions = instructions
                current.metadata = {contract_key: contract_version}
                current.tools = [lambda: None]
                current.knowledge = MagicMock()
                current.additional_context = "Studio drifted a code-owned runtime field"
                current.output_schema = None
                with (
                    patch(f"{module}.get_postgres_db", return_value=database),
                    patch(f"{module}.Agent.load", return_value=current),
                    patch.object(current, "save", autospec=True, return_value=42) as save,
                ):
                    self.assertEqual(ensure(MagicMock()), 42)

                self.assertEqual(current.instructions, instructions)
                self.assertEqual(current.tools, [])
                self.assertIsNone(current.knowledge)
                self.assertIs(current.output_schema, output_schema)
                save.assert_called_once()
                self.assertIn("runtime contract repair", save.call_args.kwargs["notes"])


if __name__ == "__main__":
    unittest.main()
