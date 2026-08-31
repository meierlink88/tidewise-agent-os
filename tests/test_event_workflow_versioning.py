"""Published Event Workflow version-pin rehydration tests."""

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from agno.agent import Agent
from agno.registry import Registry
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.workflow import Workflow

from agents.event_extractor import LoadedEventExtractorAgent
from agents.event_identity import LoadedEventIdentityAgent
from agents.event_signal_analyst import LoadedEventSignalAnalystAgent
from capabilities.event import (
    EventExtractionDraft,
    EventIdentityDecision,
    EventPublicationRecord,
    EventSignalAnalysisDraft,
    configure_event_workflow_runtime,
)
from capabilities.event.functions import (
    analyze_signals,
    enqueue_evidence_artifact,
    event_extraction_complete,
    event_extraction_required,
    event_resolution_complete,
    extract_events,
    freeze_event_extraction,
    has_pending_event_resolution,
    has_pending_signal_analysis,
    persist_event_resolution,
    persist_signal_task,
    prepare_event_extraction,
    prepare_event_resolution,
    prepare_signal_task,
    publish_events,
    publish_signals,
    resolve_events,
    signal_analysis_complete,
)
from capabilities.evidence import ResolvedEvidence
from sematica.analysis.event.contracts import CandidateSet, EventClassification
from workflows.event_extraction import ensure_event_extraction_workflow


class _PinnedVersionRuntime:
    """Record the exact Agent bindings requested by the rehydrated Function Steps."""

    EVENT_ID = "EVT15bec7e3-998c-5434-aa5d-29712c4c67cf"

    def __init__(self, evidence_id: str) -> None:
        self.evidence_id = evidence_id
        self.agent_requests: list[tuple[str, int]] = []

    @staticmethod
    def _classification() -> EventClassification:
        return EventClassification(
            event_class="CHAIN_NODE",
            confidence="HIGH",
            anchor_type_hints=["ChainNode"],
            variable_group_hints=["SUPPLY_CAPACITY"],
            retrieval_queries=["服务器 供给"],
            rationale="事件直接发生在产业链节点。",
        )

    async def invoke_agent(self, agent_id: str, version: int, request: Any, run_context: Any) -> RunOutput:
        del run_context
        self.agent_requests.append((agent_id, version))
        if agent_id == "event-extractor":
            content: Any = EventExtractionDraft.model_validate(
                {
                    "candidates": [
                        {
                            "event": {
                                "title": "示例公司签署服务器订单",
                                "summary": "示例公司宣布签署服务器订单。",
                                "semantic": {
                                    "actors": ["示例公司"],
                                    "action": "签署",
                                    "objects": ["服务器订单"],
                                    "stage": "ANNOUNCED",
                                    "modality": "FACT",
                                    "time": {
                                        "occurred_at": None,
                                        "announced_at": "2026-08-25T00:00:00Z",
                                        "effective_at": None,
                                        "precision": "DAY",
                                    },
                                    "jurisdictions": ["中国"],
                                    "reason": None,
                                    "method": "签署正式采购合同",
                                    "metrics": [],
                                },
                            },
                            "evidence_ids": [self.evidence_id],
                        }
                    ],
                    "no_event": [],
                }
            )
        elif agent_id == "event-identity":
            content = EventIdentityDecision(
                decision="NEW_EVENT",
                atomic=True,
                matched_event_ids=[],
                reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                summary="没有同一正式 Event。",
            )
        elif agent_id == "event-signal-analyst":
            task = getattr(request, "task", None)
            content = EventSignalAnalysisDraft(
                classification=self._classification(),
                proposals=[],
                no_signal_reason=None if task == "CLASSIFY" else "没有可投影的直接 Signal。",
            )
        else:  # pragma: no cover - the assertion below owns the allowed identities
            raise AssertionError(f"unexpected Event Agent {agent_id}")
        return RunOutput(agent_id=agent_id, content=content, status=RunStatus.completed)

    async def retrieve_history(self, candidate: Any) -> list[Any]:
        del candidate
        return []

    async def publish(
        self,
        candidate: Any,
        candidate_key: str,
        resolution: Any,
        *,
        existing: EventPublicationRecord | None,
        checkpoint: Any,
    ) -> EventPublicationRecord:
        del existing, checkpoint
        return EventPublicationRecord(
            candidate_key=candidate_key,
            decision=resolution.decision,
            publication_started=True,
            event_id=self.EVENT_ID,
            event_created=True,
            evidence_link_result="CREATED",
            graph_projection_status="SUCCEEDED",
            reason_codes=resolution.reason_codes,
            matched_event_ids=resolution.matched_event_ids,
            episode_uuid=f"episode-{self.EVENT_ID}",
            published_event={"id": self.EVENT_ID, "event": candidate.event.model_dump(mode="json")},
        )

    async def retrieve_signal_candidates(self, event: Any, classification: Any) -> CandidateSet:
        del event, classification
        return CandidateSet(anchors=[], variables=[])

    async def project_signal(self, *args: Any, **kwargs: Any) -> str:
        del args, kwargs
        raise AssertionError("an empty candidate set must not project a Signal")

    async def close(self) -> None:
        return None


class EventWorkflowVersioningTest(unittest.IsolatedAsyncioTestCase):
    def test_same_contract_keeps_workflow_version_when_agent_pins_are_current(self) -> None:
        versions = {
            "event-extractor": 11,
            "event-identity": 2,
            "event-signal-analyst": 8,
        }
        agents = {
            agent_id: Agent(id=agent_id, name=agent_id, instructions=f"published {agent_id}") for agent_id in versions
        }
        loaded_agents = (
            LoadedEventExtractorAgent(agents["event-extractor"], 11, "a" * 64),
            LoadedEventIdentityAgent(agents["event-identity"], 2, "b" * 64),
            LoadedEventSignalAnalystAgent(agents["event-signal-analyst"], 8, "c" * 64),
        )
        database = MagicMock()
        database.get_component.return_value = {"current_version": 30}
        database.get_config.return_value = {
            "config": {
                "metadata": {
                    "event_extraction_contract_version": 13,
                    "event_extraction_publication_policy": "code_managed_exact_agent_links.v1",
                    "event_agent_versions": versions,
                },
            },
            "version": 30,
        }
        database.get_links.return_value = [
            {
                "link_kind": "step_agent",
                "link_key": step_id,
                "child_component_id": agent_id,
                "child_version": versions[agent_id],
                "position": position,
            }
            for step_id, agent_id, position in (
                ("event-extract", "event-extractor", 0),
                ("event-resolve", "event-identity", 1),
                ("event-signal-analyze", "event-signal-analyst", 3),
            )
        ]
        rehydrated = MagicMock()
        rehydrated.steps = [MagicMock()]

        with (
            patch("workflows.event_extraction.get_postgres_db", return_value=database),
            patch("workflows.event_extraction.load_event_extractor_agent", return_value=loaded_agents[0]),
            patch("workflows.event_extraction.load_event_identity_agent", return_value=loaded_agents[1]),
            patch("workflows.event_extraction.load_event_signal_analyst_agent", return_value=loaded_agents[2]),
            patch("workflows.event_extraction.Workflow.load", return_value=rehydrated) as workflow_load,
        ):
            self.assertEqual(ensure_event_extraction_workflow(MagicMock()), 30)

        workflow_load.assert_called_once()
        database.upsert_config.assert_not_called()

    def test_same_contract_republishes_workflow_when_pinned_agent_version_is_stale(self) -> None:
        stale_versions = {
            "event-extractor": 10,
            "event-identity": 2,
            "event-signal-analyst": 8,
        }
        current_versions = {**stale_versions, "event-extractor": 11}
        agents = {
            agent_id: Agent(id=agent_id, name=agent_id, instructions=f"published {agent_id}")
            for agent_id in current_versions
        }
        loaded_agents = (
            LoadedEventExtractorAgent(agents["event-extractor"], 11, "a" * 64),
            LoadedEventIdentityAgent(agents["event-identity"], 2, "b" * 64),
            LoadedEventSignalAnalystAgent(agents["event-signal-analyst"], 8, "c" * 64),
        )
        database = MagicMock()
        database.get_component.return_value = {"current_version": 29}
        database.get_config.return_value = {
            "config": {
                "id": "event-extraction",
                "name": "Event Extraction",
                "description": "Studio-managed description",
                "metadata": {
                    "event_extraction_contract_version": 13,
                    "event_extraction_publication_policy": "code_managed_exact_agent_links.v1",
                    "event_agent_versions": stale_versions,
                },
            },
            "version": 29,
        }
        database.get_links.return_value = [
            {
                "link_kind": "step_agent",
                "link_key": step_id,
                "child_component_id": agent_id,
                "child_version": stale_versions[agent_id],
                "position": position,
            }
            for step_id, agent_id, position in (
                ("event-extract", "event-extractor", 0),
                ("event-resolve", "event-identity", 1),
                ("event-signal-analyze", "event-signal-analyst", 3),
            )
        ]
        database.upsert_config.return_value = {"version": 30}

        with (
            patch("workflows.event_extraction.get_postgres_db", return_value=database),
            patch("workflows.event_extraction.load_event_extractor_agent", return_value=loaded_agents[0]),
            patch("workflows.event_extraction.load_event_identity_agent", return_value=loaded_agents[1]),
            patch("workflows.event_extraction.load_event_signal_analyst_agent", return_value=loaded_agents[2]),
            patch("workflows.event_extraction.Workflow.load") as workflow_load,
        ):
            self.assertEqual(ensure_event_extraction_workflow(MagicMock()), 30)

        workflow_load.assert_not_called()
        publication = database.upsert_config.call_args.kwargs
        self.assertEqual(publication["config"]["name"], "Event Extraction")
        self.assertEqual(publication["config"]["description"], "Studio-managed description")
        self.assertEqual(publication["config"]["metadata"]["event_agent_versions"], current_versions)
        self.assertEqual(
            {
                link["child_component_id"]: link["child_version"]
                for link in publication["links"]
                if link["link_kind"] == "step_agent"
            },
            current_versions,
        )

    def test_application_registry_rehydrates_flat_and_historical_event_workflows(self) -> None:
        from app.registry import registry

        function_names = {
            "extract_events",
            "resolve_events",
            "publish_events",
            "analyze_signals",
            "publish_signals",
            "event_extraction_complete",
            "prepare_event_extraction",
            "event_extraction_required",
            "freeze_event_extraction",
            "has_pending_event_resolution",
            "prepare_event_resolution",
            "persist_event_resolution",
            "event_resolution_complete",
            "has_pending_signal_analysis",
            "prepare_signal_task",
            "persist_signal_task",
            "signal_analysis_complete",
        }

        self.assertEqual(
            {name for name in function_names if registry.get_function(name) is not None},
            function_names,
        )

    async def test_published_workflow_rehydrates_without_agents_and_executes_only_pinned_versions(self) -> None:
        versions = {
            "event-extractor": 11,
            "event-identity": 13,
            "event-signal-analyst": 17,
        }
        agents = {
            agent_id: Agent(id=agent_id, name=agent_id, instructions=f"published {agent_id}") for agent_id in versions
        }
        loaded_agents = (
            LoadedEventExtractorAgent(agents["event-extractor"], 11, "a" * 64),
            LoadedEventIdentityAgent(agents["event-identity"], 13, "b" * 64),
            LoadedEventSignalAnalystAgent(agents["event-signal-analyst"], 17, "c" * 64),
        )
        database = MagicMock()
        database.get_component.return_value = None
        database.upsert_config.return_value = {"version": 29}

        with (
            patch("workflows.event_extraction.get_postgres_db", return_value=database),
            patch("workflows.event_extraction.load_event_extractor_agent", return_value=loaded_agents[0]),
            patch("workflows.event_extraction.load_event_identity_agent", return_value=loaded_agents[1]),
            patch("workflows.event_extraction.load_event_signal_analyst_agent", return_value=loaded_agents[2]),
        ):
            self.assertEqual(ensure_event_extraction_workflow(MagicMock()), 29)

        publication = database.upsert_config.call_args.kwargs
        self.assertEqual(
            {
                link["child_component_id"]: link["child_version"]
                for link in publication["links"]
                if link["link_kind"] == "step_agent"
            },
            versions,
        )
        database.get_config.return_value = {
            "config": publication["config"],
            "version": 29,
        }
        database.get_links.return_value = publication["links"]
        function_registry = Registry(
            functions=[
                extract_events,
                resolve_events,
                analyze_signals,
                event_extraction_complete,
                # Historical published Workflow versions must remain rehydratable.
                prepare_event_extraction,
                event_extraction_required,
                freeze_event_extraction,
                has_pending_event_resolution,
                prepare_event_resolution,
                persist_event_resolution,
                event_resolution_complete,
                publish_events,
                has_pending_signal_analysis,
                prepare_signal_task,
                persist_signal_task,
                signal_analysis_complete,
                publish_signals,
            ]
        )
        with patch("agno.agent.agent.get_agent_by_id") as implicit_agent_load:
            rehydrated = Workflow.load(
                "event-extraction",
                db=database,
                registry=function_registry,
                version=29,
                strict=True,
                published_only=True,
            )

        self.assertIsNotNone(rehydrated)
        implicit_agent_load.assert_not_called()
        assert rehydrated is not None
        assert rehydrated.metadata is not None
        self.assertEqual(rehydrated.metadata["event_agent_versions"], versions)

        evidence_id = "EVD15bec7e3-998c-5434-aa5d-29712c4c67cf"
        raw_evidence_id = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
        evidence = ResolvedEvidence.model_validate(
            {
                "id": evidence_id,
                "raw_evidence_id": raw_evidence_id,
                "summary": "示例公司宣布签署服务器订单",
                "keywords": ["服务器", "订单"],
                "semantic": {
                    "actors": ["示例公司"],
                    "action": "签署",
                    "objects": ["服务器订单"],
                    "stage": "ANNOUNCED",
                    "modality": "FACT",
                    "time": {
                        "raw": "2026-08-25",
                        "start_at": "2026-08-24T16:00:00Z",
                        "end_at": "2026-08-25T15:59:59.999999Z",
                        "precision": "DAY",
                    },
                    "jurisdictions": ["中国"],
                    "reason": None,
                    "method": "公告宣布",
                    "metrics": [],
                    "attribution": {"reported_by": None, "claimed_by": "示例公司"},
                },
            }
        )
        runtime = _PinnedVersionRuntime(evidence_id)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "evidence" / "documents" / "published" / "manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}\n", encoding="utf-8")
            with (
                patch.dict(
                    os.environ,
                    {
                        "EVIDENCE_ARTIFACT_ROOT": str(root / "evidence"),
                        "EVENT_ARTIFACT_ROOT": str(root / "event"),
                    },
                ),
                patch(
                    "capabilities.event.internal.queue.read_resolved_evidences",
                    return_value=[evidence],
                ),
            ):
                enqueue_evidence_artifact(str(manifest), [evidence_id])
                configure_event_workflow_runtime(runtime)
                rehydrated.db = None
                try:
                    response = await rehydrated.arun(
                        input="处理所有已发布且尚未提炼 Event 的 Evidence",
                        run_id="run-pinned-workflow-version",
                        session_id="session-pinned-workflow-version",
                    )
                finally:
                    configure_event_workflow_runtime(None)

        self.assertEqual(response.status, RunStatus.completed)
        self.assertEqual(
            runtime.agent_requests,
            [
                ("event-extractor", 11),
                ("event-identity", 13),
                ("event-signal-analyst", 17),
                ("event-signal-analyst", 17),
            ],
        )
        self.assertIsNotNone(response.metadata)
        assert response.metadata is not None
        self.assertEqual(response.metadata["event_agent_versions"], versions)
        self.assertEqual(response.metadata["event_agent_execution_versions"], versions)


if __name__ == "__main__":
    unittest.main()
