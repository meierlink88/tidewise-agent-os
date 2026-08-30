"""Vertical behavior tests for the Studio-managed five-phase Event Workflow."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

from agno.agent import Agent
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.workflow import Step, Workflow

from agents.event_extractor import (
    EVENT_EXTRACTOR_CONTRACT_VERSION,
    LoadedEventExtractorAgent,
    ensure_event_extractor_agent,
)
from agents.event_identity import (
    EVENT_IDENTITY_CONTRACT_VERSION,
    LoadedEventIdentityAgent,
    ensure_event_identity_agent,
)
from agents.event_signal_analyst import (
    EVENT_SIGNAL_ANALYST_CONTRACT_VERSION,
    LoadedEventSignalAnalystAgent,
    ensure_event_signal_analyst_agent,
)
from capabilities.event import (
    EventExtractionBatch,
    EventExtractionDraft,
    EventExtractionResult,
    EventIdentityDecision,
    EventIdentityRequest,
    EventPublicationRecord,
    EventSignalAnalysisDraft,
    EventSignalAnalysisRequest,
    EventSignalClassificationRequest,
    configure_event_workflow_runtime,
)
from capabilities.event.functions import enqueue_evidence_artifact
from capabilities.event.functions.extraction import _candidate_key
from capabilities.event.internal.models import (
    EventPublicationJournal,
    EventResolutionRecord,
    EventSignalJournal,
    EventSignalRecord,
)
from capabilities.event.internal.storage import (
    claim_event_batch,
    freeze_draft,
    freeze_resolution,
    release_event_batch_lease,
    write_publication_journal,
    write_signal_journal,
)
from capabilities.evidence import ResolvedEvidence
from sematica.analysis.event.contracts import (
    AnchorCandidate,
    CandidateSet,
    DirectSignalDraft,
    EventClassification,
    VariableCandidate,
)
from sematica.analysis.event.errors import PermanentEventAnalysisFailure
from sematica.ingestion.episcode.event.contracts import EventCandidateDTO, HistoricalEvent
from workflows.event_extraction import (
    EVENT_EXTRACTION_CONTRACT_VERSION,
    _seed_workflow,
    ensure_event_extraction_workflow,
)


class FakeEventWorkflowRuntime:
    """Stateful external-I/O seam for Data and native Graphiti operations."""

    EVENT_ID = "EVT15bec7e3-998c-5434-aa5d-29712c4c67cf"

    def __init__(self) -> None:
        self.history: list[HistoricalEvent] = []
        self.signal_candidates = CandidateSet(anchors=[], variables=[])
        self.fail_before_data_ack_checkpoint_once = False
        self.fail_signal_candidate_read_once = False
        self.permanent_signal_rejections: set[int] = set()
        self._published_by_key: dict[str, Any] = {}
        self.history_reads = 0
        self.data_publication_requests = 0
        self.data_publications = 0
        self.episode_projections = 0
        self.signal_candidate_reads = 0
        self.signal_projection_attempts = 0
        self.signal_projections = 0

    async def retrieve_history(self, candidate) -> list[HistoricalEvent]:
        del candidate
        self.history_reads += 1
        return list(self.history)

    async def publish(
        self,
        candidate,
        candidate_key: str,
        resolution,
        *,
        existing: EventPublicationRecord | None,
        checkpoint,
    ) -> EventPublicationRecord:
        published: Any = existing.published_event if existing is not None else None
        if published is None:
            checkpoint(
                EventPublicationRecord(
                    candidate_key=candidate_key,
                    decision=resolution.decision,
                    publication_started=True,
                    event_id=None,
                    event_created=False,
                    evidence_link_result="NOT_ATTEMPTED",
                    graph_projection_status="NOT_ATTEMPTED",
                    reason_codes=["PUBLICATION_STARTED", *resolution.reason_codes],
                    matched_event_ids=resolution.matched_event_ids,
                )
            )
            self.data_publication_requests += 1
            published = self._published_by_key.get(candidate_key)
            if published is None:
                self.data_publications += 1
                published = {
                    "id": self.EVENT_ID,
                    "event": candidate.event.model_dump(mode="json"),
                }
                self._published_by_key[candidate_key] = published
            if self.fail_before_data_ack_checkpoint_once:
                self.fail_before_data_ack_checkpoint_once = False
                raise ConnectionError("Data acknowledgement was lost before the local ACK checkpoint")
            checkpoint(
                EventPublicationRecord(
                    candidate_key=candidate_key,
                    decision=resolution.decision,
                    publication_started=True,
                    event_id=self.EVENT_ID,
                    event_created=True,
                    evidence_link_result="CREATED",
                    graph_projection_status="NOT_ATTEMPTED",
                    reason_codes=resolution.reason_codes,
                    matched_event_ids=resolution.matched_event_ids,
                    published_event=published,
                )
            )

        self.episode_projections += 1
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
            published_event=published,
        )

    async def retrieve_signal_candidates(self, event, classification) -> CandidateSet:
        del event, classification
        self.signal_candidate_reads += 1
        if self.fail_signal_candidate_read_once:
            self.fail_signal_candidate_read_once = False
            raise ConnectionError("Graph candidate retrieval temporarily unavailable")
        return self.signal_candidates

    async def project_signal(self, event, classification, variable, anchor, proposal) -> str:
        del event, classification, variable, anchor, proposal
        self.signal_projection_attempts += 1
        if self.signal_projection_attempts in self.permanent_signal_rejections:
            raise PermanentEventAnalysisFailure("formal Signal endpoint is permanently invalid")
        self.signal_projections += 1
        return f"signal-fact-{self.signal_projections}"

    async def close(self) -> None:
        return None


class FakeStudioResponses:
    """Three Studio Agent boundaries with deterministic reviewed outputs."""

    def __init__(
        self,
        *,
        extraction: Any,
        identity: EventIdentityDecision | list[Any],
        classification: EventClassification,
        proposals: list[DirectSignalDraft],
        signal_outputs: list[Any] | None = None,
    ) -> None:
        self.extraction = extraction
        self.identity_outputs = identity if isinstance(identity, list) else [identity]
        self.classification = classification
        self.proposals = proposals
        self.signal_outputs = signal_outputs
        self.extraction_inputs: list[EventExtractionBatch] = []
        self.identity_inputs: list[EventIdentityRequest] = []
        self.signal_inputs: list[EventSignalClassificationRequest | EventSignalAnalysisRequest] = []

    @staticmethod
    def _input(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        if "input" in kwargs:
            return kwargs["input"]
        if args:
            return args[0]
        raise AssertionError("Studio Agent did not receive a Workflow input")

    async def run_extractor(self, *args: Any, **kwargs: Any) -> RunOutput:
        self.extraction_inputs.append(EventExtractionBatch.model_validate(self._input(args, kwargs)))
        return RunOutput(
            agent_id="event-extractor",
            content=self.extraction,
            content_type="EventExtractionDraft",
            status=RunStatus.completed,
        )

    async def run_identity(self, *args: Any, **kwargs: Any) -> RunOutput:
        self.identity_inputs.append(EventIdentityRequest.model_validate(self._input(args, kwargs)))
        output_index = min(len(self.identity_inputs) - 1, len(self.identity_outputs) - 1)
        return RunOutput(
            agent_id="event-identity",
            content=self.identity_outputs[output_index],
            content_type="EventIdentityDecision",
            status=RunStatus.completed,
        )

    async def run_signal_analyst(self, *args: Any, **kwargs: Any) -> RunOutput:
        payload = self._input(args, kwargs)
        task = (
            payload.task
            if isinstance(payload, (EventSignalClassificationRequest, EventSignalAnalysisRequest))
            else None
        )
        if task is None and isinstance(payload, dict):
            task = payload.get("task")
        request: EventSignalClassificationRequest | EventSignalAnalysisRequest
        if task == "CLASSIFY":
            request = EventSignalClassificationRequest.model_validate(payload)
            proposals: list[DirectSignalDraft] = []
        else:
            request = EventSignalAnalysisRequest.model_validate(payload)
            proposals = self.proposals
        self.signal_inputs.append(request)
        if self.signal_outputs is not None:
            output_index = min(len(self.signal_inputs) - 1, len(self.signal_outputs) - 1)
            content = self.signal_outputs[output_index]
        else:
            content = EventSignalAnalysisDraft(
                classification=self.classification,
                proposals=proposals,
                no_signal_reason=(None if task == "CLASSIFY" or proposals else "事件没有直接支持的 Signal。"),
            )
        return RunOutput(
            agent_id="event-signal-analyst",
            content=content,
            content_type="EventSignalAnalysisDraft",
            status=RunStatus.completed,
        )


class EventExtractionWorkflowTest(unittest.IsolatedAsyncioTestCase):
    FIRST_EVIDENCE_ID = "EVD15bec7e3-998c-5434-aa5d-29712c4c67cf"
    SECOND_EVIDENCE_ID = "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15"
    RAW_EVIDENCE_ID = "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"
    HISTORICAL_EVENT_ID = "EVT5cb71bef-5b1d-5995-add0-7408eaa2be15"
    AGENT_VERSIONS = {
        "event-extractor": 11,
        "event-identity": 13,
        "event-signal-analyst": 17,
    }

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.evidence_root = root / "evidence"
        self.event_root = root / "event"
        self.evidence_manifest = self.evidence_root / "documents" / "published" / "manifest.json"
        self.evidence_manifest.parent.mkdir(parents=True)
        self.evidence_manifest.write_text("{}\n", encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {
                "EVIDENCE_ARTIFACT_ROOT": str(self.evidence_root),
                "EVENT_ARTIFACT_ROOT": str(self.event_root),
            },
        )
        self.environment.start()
        self.runtime = FakeEventWorkflowRuntime()
        configure_event_workflow_runtime(self.runtime)

    def tearDown(self) -> None:
        configure_event_workflow_runtime(None)
        self.environment.stop()
        self.temporary.cleanup()

    @classmethod
    def evidences(cls) -> list[ResolvedEvidence]:
        semantic = {
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
        }
        return [
            ResolvedEvidence(
                id=cls.FIRST_EVIDENCE_ID,
                raw_evidence_id=cls.RAW_EVIDENCE_ID,
                summary="示例公司宣布签署服务器订单",
                keywords=["服务器", "订单"],
                semantic=semantic,
            ),
            ResolvedEvidence(
                id=cls.SECOND_EVIDENCE_ID,
                raw_evidence_id=cls.RAW_EVIDENCE_ID,
                summary="示例公司的同一服务器订单公告",
                keywords=["服务器", "订单"],
                semantic={**semantic, "method": "媒体转述"},
            ),
        ]

    @classmethod
    def extraction_draft(cls) -> EventExtractionDraft:
        return EventExtractionDraft(
            candidates=[
                {
                    "event": {
                        "title": "示例公司签署服务器订单",
                        "summary": "示例公司于 2026 年 8 月 25 日宣布签署服务器订单。",
                        "semantic": {
                            "actors": ["示例公司"],
                            "action": "签署",
                            "objects": ["服务器订单"],
                            "stage": "ANNOUNCED",
                            "jurisdictions": ["中国"],
                            "effective_at": None,
                            "time_precision": "DAY",
                        },
                        "modality": "FACT",
                        "occurred_at": None,
                        "announced_at": "2026-08-25T00:00:00Z",
                    },
                    "evidence_ids": [cls.FIRST_EVIDENCE_ID, cls.SECOND_EVIDENCE_ID],
                }
            ],
            no_event=[],
        )

    @staticmethod
    def classification() -> EventClassification:
        return EventClassification(
            event_class="CHAIN_NODE",
            confidence="HIGH",
            anchor_type_hints=["ChainNode"],
            variable_group_hints=["SUPPLY_CAPACITY"],
            retrieval_queries=["服务器 供给"],
            rationale="事件直接发生在产业链节点。",
        )

    @staticmethod
    def candidates() -> CandidateSet:
        return CandidateSet(
            anchors=[
                AnchorCandidate(
                    uuid="anchor-server",
                    name="服务器",
                    entity_type="ChainNode",
                    business_id="chain-node-server",
                )
            ],
            variables=[
                VariableCandidate(
                    uuid="variable-supply",
                    variable_id="effective_supply",
                    name="有效供给",
                    variable_group="SUPPLY_CAPACITY",
                    allowed_anchor_types=["ChainNode"],
                    definition="可向目标市场实际交付的供给能力。",
                )
            ],
        )

    @staticmethod
    def signal_draft() -> DirectSignalDraft:
        return DirectSignalDraft(
            anchor_uuid="anchor-server",
            variable_uuid="variable-supply",
            fact="服务器订单提高有效供给需求。",
            direction="UP",
            magnitude="MEDIUM",
            impact_onset_days=0,
            impact_peak_days=30,
            expected_duration_days=90,
            mechanism="订单直接提高服务器供给需求。",
            duration_basis="订单履行周期。",
            assumptions=[],
            invalidation_conditions=["订单取消"],
            provenance_confidence="HIGH",
            mechanism_confidence="HIGH",
            temporal_confidence="MEDIUM",
        )

    @classmethod
    def historical_event(cls) -> HistoricalEvent:
        payload = cls.extraction_draft().candidates[0].event.model_dump(mode="json")
        payload["semantic"]["action"] = "披露合作意向"
        return HistoricalEvent(
            id=cls.HISTORICAL_EVENT_ID,
            event=EventCandidateDTO.model_validate(payload),
        )

    @classmethod
    def studio_agents(cls) -> tuple[Agent, Agent, Agent]:
        return (
            Agent(id="event-extractor", name="Event Extractor", instructions="test extractor"),
            Agent(id="event-identity", name="Event Identity", instructions="test identity"),
            Agent(id="event-signal-analyst", name="Event Signal Analyst", instructions="test signal analyst"),
        )

    @classmethod
    def workflow(cls) -> tuple[Workflow, Agent, Agent, Agent]:
        extractor, identity, signal_analyst = cls.studio_agents()
        workflow = _seed_workflow(
            extractor,
            identity,
            signal_analyst,
            agent_versions=cls.AGENT_VERSIONS,
        )
        workflow.db = None
        return workflow, extractor, identity, signal_analyst

    @staticmethod
    def direct_agent_ids(items: list[object]) -> list[str]:
        result: list[str] = []
        for item in items:
            if isinstance(item, Step) and item.agent is not None:
                result.append(str(item.agent.id))
            nested = getattr(item, "steps", None)
            if isinstance(nested, list):
                result.extend(EventExtractionWorkflowTest.direct_agent_ids(cast(list[object], nested)))
        return result

    @staticmethod
    def rename_every_display_name(items: list[object]) -> None:
        serial = 0

        def rename(nested_items: list[object]) -> None:
            nonlocal serial
            for item in nested_items:
                serial += 1
                item.name = f"Studio 可编辑名称 {serial}"  # type: ignore[attr-defined]
                nested = getattr(item, "steps", None)
                if isinstance(nested, list):
                    rename(cast(list[object], nested))

        rename(items)

    async def execute_workflow(
        self,
        workflow: Workflow,
        extractor: Agent,
        identity: Agent,
        signal_analyst: Agent,
        studio: FakeStudioResponses,
        *,
        run_id: str,
        enqueue: bool,
    ):
        with (
            patch(
                "capabilities.event.internal.queue.read_resolved_evidences",
                return_value=self.evidences(),
            ),
            patch.object(extractor, "arun", new=studio.run_extractor),
            patch.object(identity, "arun", new=studio.run_identity),
            patch.object(signal_analyst, "arun", new=studio.run_signal_analyst),
        ):
            if enqueue:
                enqueue_evidence_artifact(
                    str(self.evidence_manifest),
                    [item.id for item in self.evidences()],
                )
            return await workflow.arun(
                input="处理所有已发布且尚未提炼 Event 的 Evidence",
                run_id=run_id,
                session_id=f"session-{run_id}",
            )

    def test_workflow_has_five_exact_business_phases_and_three_direct_studio_agents(self) -> None:
        workflow, _, _, _ = self.workflow()
        self.assertIsInstance(workflow.steps, list)
        assert isinstance(workflow.steps, list)
        phase_steps = cast(list[Any], workflow.steps)

        self.assertEqual(
            [step.name for step in phase_steps],
            ["Extract Events", "Resolve Events", "Publish Events", "Analyze Signals", "Publish Signals"],
        )
        self.assertEqual(
            self.direct_agent_ids(cast(list[object], workflow.steps)),
            ["event-extractor", "event-identity", "event-signal-analyst"],
        )
        self.assertEqual(
            workflow.metadata,
            {
                "event_extraction_contract_version": EVENT_EXTRACTION_CONTRACT_VERSION,
                "event_agent_versions": self.AGENT_VERSIONS,
            },
        )

    async def test_stale_workflow_owner_cannot_freeze_agent_output_after_lease_takeover(self) -> None:
        workflow, extractor, _, _ = self.workflow()
        extractor_started = asyncio.Event()
        allow_extractor_to_finish = asyncio.Event()

        class ControlledClock:
            current = datetime(2026, 8, 30, tzinfo=UTC)

            @classmethod
            def now(cls, tz=None):
                del tz
                return cls.current

        async def delayed_extractor(*args: Any, **kwargs: Any) -> RunOutput:
            del args, kwargs
            extractor_started.set()
            await allow_extractor_to_finish.wait()
            return RunOutput(
                agent_id="event-extractor",
                content=self.extraction_draft(),
                content_type="EventExtractionDraft",
                status=RunStatus.completed,
            )

        replacement: EventExtractionBatch | None = None
        with (
            patch(
                "capabilities.event.internal.queue.read_resolved_evidences",
                return_value=self.evidences(),
            ),
            patch("capabilities.event.internal.storage.datetime", ControlledClock),
            patch.object(extractor, "arun", new=delayed_extractor),
        ):
            enqueue_evidence_artifact(
                str(self.evidence_manifest),
                [item.id for item in self.evidences()],
            )
            stale_run = asyncio.create_task(
                workflow.arun(
                    input="处理所有已发布且尚未提炼 Event 的 Evidence",
                    run_id="run-stale-lease-owner",
                    session_id="session-stale-lease-owner",
                )
            )
            await extractor_started.wait()
            ControlledClock.current += timedelta(seconds=601)
            claimed = claim_event_batch()
            self.assertIsInstance(claimed, EventExtractionBatch)
            replacement = cast(EventExtractionBatch, claimed)
            allow_extractor_to_finish.set()
            with self.assertRaisesRegex(RuntimeError, "Event Workflow stage EVENT_EXTRACTION failed"):
                await stale_run

            self.assertFalse((self.event_root / ".pending" / replacement.batch_id / "draft.json").exists())

        assert replacement is not None
        release_event_batch_lease(replacement)

    async def test_agent_provider_exception_releases_the_batch_lease(self) -> None:
        workflow, extractor, _, _ = self.workflow()

        async def failed_extractor(*args: Any, **kwargs: Any) -> RunOutput:
            del args, kwargs
            raise ConnectionError("Studio Agent provider is unavailable")

        with (
            patch(
                "capabilities.event.internal.queue.read_resolved_evidences",
                return_value=self.evidences(),
            ),
            patch.object(extractor, "arun", new=failed_extractor),
        ):
            enqueue_evidence_artifact(
                str(self.evidence_manifest),
                [item.id for item in self.evidences()],
            )
            with self.assertRaisesRegex(RuntimeError, "Event Workflow stage EVENT_EXTRACTION failed"):
                await workflow.arun(
                    input="处理所有已发布且尚未提炼 Event 的 Evidence",
                    run_id="run-agent-provider-failure",
                    session_id="session-agent-provider-failure",
                )

        self.assertEqual(list((self.event_root / ".pending").glob("*/lease.json")), [])

    async def test_renamed_workflow_completes_new_event_and_signal_as_v4_result(self) -> None:
        workflow, extractor, identity, signal_analyst = self.workflow()
        assert isinstance(workflow.steps, list)
        self.rename_every_display_name(cast(list[object], workflow.steps))
        self.runtime.signal_candidates = self.candidates()
        studio = FakeStudioResponses(
            extraction=self.extraction_draft(),
            identity=EventIdentityDecision(
                decision="NEW_EVENT",
                atomic=True,
                matched_event_ids=[],
                reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                summary="没有同一正式 Event。",
            ),
            classification=self.classification(),
            proposals=[self.signal_draft()],
        )

        response = await self.execute_workflow(
            workflow,
            extractor,
            identity,
            signal_analyst,
            studio,
            run_id="run-renamed-complete",
            enqueue=True,
        )

        self.assertEqual(response.status, RunStatus.completed)
        self.assertEqual(response.metadata["event_agent_versions"], self.AGENT_VERSIONS)
        result = EventExtractionResult.model_validate(response.content)
        self.assertEqual(result.schema_version, "event_extraction_result.v4")
        self.assertEqual(result.evidence_ids, [self.FIRST_EVIDENCE_ID, self.SECOND_EVIDENCE_ID])
        self.assertEqual(result.candidate_count, 1)
        self.assertEqual(result.no_event_count, 0)
        self.assertEqual(result.published_event_ids, [self.runtime.EVENT_ID])
        self.assertEqual(result.duplicate_event_count, 0)
        self.assertEqual(result.ignored_candidate_count, 0)
        self.assertEqual(result.failed_candidate_count, 0)
        self.assertEqual(result.signal_fact_uuids, ["signal-fact-1"])

        self.assertEqual(len(studio.extraction_inputs), 1)
        self.assertEqual(len(studio.identity_inputs), 1)
        self.assertEqual([request.task for request in studio.signal_inputs], ["CLASSIFY", "PROPOSE_SIGNALS"])
        proposed = cast(EventSignalAnalysisRequest, studio.signal_inputs[1])
        self.assertEqual(proposed.classification, self.classification())
        self.assertEqual(proposed.candidates, self.candidates())
        self.assertEqual(
            (
                self.runtime.history_reads,
                self.runtime.data_publications,
                self.runtime.episode_projections,
                self.runtime.signal_candidate_reads,
                self.runtime.signal_projections,
            ),
            (1, 1, 1, 1, 1),
        )

    async def test_duplicate_event_completes_without_data_graph_or_signal_writes(self) -> None:
        workflow, extractor, identity, signal_analyst = self.workflow()
        self.runtime.history = [self.historical_event()]
        studio = FakeStudioResponses(
            extraction=self.extraction_draft(),
            identity=EventIdentityDecision(
                decision="SAME_EVENT",
                atomic=True,
                matched_event_ids=[self.HISTORICAL_EVENT_ID],
                reason_codes=["SAME_REAL_WORLD_OCCURRENCE"],
                summary="候选与给定历史 Event 是同一现实事件。",
            ),
            classification=self.classification(),
            proposals=[],
        )

        response = await self.execute_workflow(
            workflow,
            extractor,
            identity,
            signal_analyst,
            studio,
            run_id="run-duplicate",
            enqueue=True,
        )

        self.assertEqual(response.status, RunStatus.completed)
        result = EventExtractionResult.model_validate(response.content)
        self.assertEqual(result.schema_version, "event_extraction_result.v4")
        self.assertEqual(result.candidate_count, 1)
        self.assertEqual(result.duplicate_event_count, 1)
        self.assertEqual(result.published_event_ids, [])
        self.assertEqual(result.signal_fact_uuids, [])
        self.assertEqual(len(studio.extraction_inputs), 1)
        self.assertEqual(len(studio.identity_inputs), 1)
        self.assertEqual(studio.identity_inputs[0].historical_candidates, [self.historical_event()])
        self.assertEqual(studio.signal_inputs, [])
        self.assertEqual(
            (
                self.runtime.history_reads,
                self.runtime.data_publications,
                self.runtime.episode_projections,
                self.runtime.signal_candidate_reads,
                self.runtime.signal_projections,
            ),
            (1, 0, 0, 0, 0),
        )

    async def test_no_signal_path_preserves_classification_in_the_frozen_proposal_request(self) -> None:
        workflow, extractor, identity, signal_analyst = self.workflow()
        self.runtime.signal_candidates = self.candidates()
        studio = FakeStudioResponses(
            extraction=self.extraction_draft(),
            identity=EventIdentityDecision(
                decision="NEW_EVENT",
                atomic=True,
                matched_event_ids=[],
                reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                summary="没有同一正式 Event。",
            ),
            classification=self.classification(),
            proposals=[],
        )

        response = await self.execute_workflow(
            workflow,
            extractor,
            identity,
            signal_analyst,
            studio,
            run_id="run-no-signal",
            enqueue=True,
        )

        self.assertEqual(response.status, RunStatus.completed)
        result = EventExtractionResult.model_validate(response.content)
        self.assertEqual(result.schema_version, "event_extraction_result.v4")
        self.assertEqual(result.published_event_ids, [self.runtime.EVENT_ID])
        self.assertEqual(result.signal_fact_uuids, [])
        self.assertEqual([request.task for request in studio.signal_inputs], ["CLASSIFY", "PROPOSE_SIGNALS"])
        proposal_request = cast(EventSignalAnalysisRequest, studio.signal_inputs[1])
        self.assertEqual(proposal_request.classification, self.classification())
        self.assertEqual(proposal_request.candidates, self.candidates())
        self.assertEqual(self.runtime.signal_candidate_reads, 1)
        self.assertEqual(self.runtime.signal_projections, 0)

    async def test_classification_output_cannot_include_a_no_signal_judgment(self) -> None:
        workflow, extractor, identity, signal_analyst = self.workflow()
        studio = FakeStudioResponses(
            extraction=self.extraction_draft(),
            identity=EventIdentityDecision(
                decision="NEW_EVENT",
                atomic=True,
                matched_event_ids=[],
                reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                summary="没有同一正式 Event。",
            ),
            classification=self.classification(),
            proposals=[],
            signal_outputs=[
                EventSignalAnalysisDraft(
                    classification=self.classification(),
                    proposals=[],
                    no_signal_reason="分类阶段越权作出了无 Signal 判断。",
                )
            ],
        )

        response = await self.execute_workflow(
            workflow,
            extractor,
            identity,
            signal_analyst,
            studio,
            run_id="run-noncompliant-classification",
            enqueue=True,
        )

        result = EventExtractionResult.model_validate(response.content)
        self.assertEqual(result.published_event_ids, [self.runtime.EVENT_ID])
        self.assertEqual(result.signal_fact_uuids, [])
        self.assertEqual([request.task for request in studio.signal_inputs], ["CLASSIFY"])
        self.assertEqual(self.runtime.signal_candidate_reads, 0)
        self.assertEqual(self.runtime.signal_projections, 0)

    async def test_signal_proposals_cannot_coexist_with_a_no_signal_reason(self) -> None:
        workflow, extractor, identity, signal_analyst = self.workflow()
        self.runtime.signal_candidates = self.candidates()
        studio = FakeStudioResponses(
            extraction=self.extraction_draft(),
            identity=EventIdentityDecision(
                decision="NEW_EVENT",
                atomic=True,
                matched_event_ids=[],
                reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                summary="没有同一正式 Event。",
            ),
            classification=self.classification(),
            proposals=[],
            signal_outputs=[
                EventSignalAnalysisDraft(
                    classification=self.classification(),
                    proposals=[],
                    no_signal_reason=None,
                ),
                EventSignalAnalysisDraft(
                    classification=self.classification(),
                    proposals=[self.signal_draft()],
                    no_signal_reason="与非空提案矛盾。",
                ),
            ],
        )

        response = await self.execute_workflow(
            workflow,
            extractor,
            identity,
            signal_analyst,
            studio,
            run_id="run-inconsistent-signal-output",
            enqueue=True,
        )

        result = EventExtractionResult.model_validate(response.content)
        self.assertEqual(result.published_event_ids, [self.runtime.EVENT_ID])
        self.assertEqual(result.signal_fact_uuids, [])
        self.assertEqual(
            [request.task for request in studio.signal_inputs],
            ["CLASSIFY", "PROPOSE_SIGNALS"],
        )
        self.assertEqual(self.runtime.signal_candidate_reads, 1)
        self.assertEqual(self.runtime.signal_projections, 0)

    async def test_malformed_identity_output_ignores_only_its_candidate(self) -> None:
        workflow, extractor, identity, signal_analyst = self.workflow()
        first = self.extraction_draft().candidates[0].model_copy(deep=True)
        first.evidence_ids = [self.FIRST_EVIDENCE_ID]
        second = first.model_copy(deep=True)
        second.evidence_ids = [self.SECOND_EVIDENCE_ID]
        second.event.title = "示例公司开始交付服务器订单"
        second.event.semantic.action = "交付"
        extraction = EventExtractionDraft(candidates=[first, second], no_event=[])
        studio = FakeStudioResponses(
            extraction=extraction,
            identity=[
                {"unexpected": "noncompliant identity output"},
                EventIdentityDecision(
                    decision="NEW_EVENT",
                    atomic=True,
                    matched_event_ids=[],
                    reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                    summary="没有同一正式 Event。",
                ),
            ],
            classification=self.classification(),
            proposals=[],
        )

        response = await self.execute_workflow(
            workflow,
            extractor,
            identity,
            signal_analyst,
            studio,
            run_id="run-one-malformed-identity",
            enqueue=True,
        )

        result = EventExtractionResult.model_validate(response.content)
        self.assertEqual(result.candidate_count, 2)
        self.assertEqual(result.ignored_candidate_count, 1)
        self.assertEqual(result.ignored_evidence_ids, [self.FIRST_EVIDENCE_ID])
        self.assertEqual(result.published_event_ids, [self.runtime.EVENT_ID])
        self.assertEqual(len(studio.identity_inputs), 2)
        self.assertEqual(self.runtime.data_publications, 1)

    async def test_malformed_extractor_children_do_not_discard_a_valid_sibling(self) -> None:
        workflow, extractor, identity, signal_analyst = self.workflow()
        valid = self.extraction_draft().candidates[0].model_copy(deep=True)
        valid.evidence_ids = [self.FIRST_EVIDENCE_ID]
        extraction = {
            "candidates": [
                valid.model_dump(mode="json"),
                {
                    "event": {"title": "broken child"},
                    "evidence_ids": [self.SECOND_EVIDENCE_ID],
                },
            ],
            "no_event": [{"evidence_id": "not-a-formal-id", "reason": "bad sibling"}],
        }
        studio = FakeStudioResponses(
            extraction=extraction,
            identity=EventIdentityDecision(
                decision="NEW_EVENT",
                atomic=True,
                matched_event_ids=[],
                reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                summary="没有同一正式 Event。",
            ),
            classification=self.classification(),
            proposals=[],
        )

        response = await self.execute_workflow(
            workflow,
            extractor,
            identity,
            signal_analyst,
            studio,
            run_id="run-malformed-extractor-child",
            enqueue=True,
        )

        result = EventExtractionResult.model_validate(response.content)
        self.assertEqual(result.candidate_count, 1)
        self.assertEqual(result.no_event_count, 1)
        self.assertEqual(result.published_event_ids, [self.runtime.EVENT_ID])
        self.assertEqual(len(studio.identity_inputs), 1)
        self.assertEqual(self.runtime.data_publications, 1)

    async def test_same_occurrence_candidates_in_one_batch_publish_only_once(self) -> None:
        workflow, extractor, identity, signal_analyst = self.workflow()
        first = self.extraction_draft().candidates[0].model_copy(deep=True)
        first.evidence_ids = [self.FIRST_EVIDENCE_ID]
        second = first.model_copy(deep=True)
        second.evidence_ids = [self.FIRST_EVIDENCE_ID, self.SECOND_EVIDENCE_ID]
        studio = FakeStudioResponses(
            extraction=EventExtractionDraft(candidates=[first, second], no_event=[]),
            identity=EventIdentityDecision(
                decision="NEW_EVENT",
                atomic=True,
                matched_event_ids=[],
                reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                summary="没有同一正式 Event。",
            ),
            classification=self.classification(),
            proposals=[],
        )

        response = await self.execute_workflow(
            workflow,
            extractor,
            identity,
            signal_analyst,
            studio,
            run_id="run-batch-duplicate",
            enqueue=True,
        )

        result = EventExtractionResult.model_validate(response.content)
        self.assertEqual(result.candidate_count, 1)
        self.assertEqual(result.evidence_ids, [self.FIRST_EVIDENCE_ID, self.SECOND_EVIDENCE_ID])
        self.assertEqual(result.published_event_ids, [self.runtime.EVENT_ID])
        self.assertEqual(len(studio.identity_inputs), 1)
        self.assertEqual(self.runtime.data_publications, 1)
        self.assertEqual(self.runtime.episode_projections, 1)

    async def test_permanent_signal_rejection_is_terminal_per_proposal_and_keeps_sibling(self) -> None:
        workflow, extractor, identity, signal_analyst = self.workflow()
        candidates = self.candidates()
        second_variable = candidates.variables[0].model_copy(
            update={
                "uuid": "variable-delivery",
                "variable_id": "delivery_capacity",
                "name": "交付能力",
            }
        )
        self.runtime.signal_candidates = candidates.model_copy(
            update={"variables": [*candidates.variables, second_variable]}
        )
        self.runtime.permanent_signal_rejections = {1}
        second_signal = self.signal_draft().model_copy(
            update={
                "variable_uuid": "variable-delivery",
                "fact": "服务器订单提高交付能力需求。",
            }
        )
        studio = FakeStudioResponses(
            extraction=self.extraction_draft(),
            identity=EventIdentityDecision(
                decision="NEW_EVENT",
                atomic=True,
                matched_event_ids=[],
                reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                summary="没有同一正式 Event。",
            ),
            classification=self.classification(),
            proposals=[self.signal_draft(), second_signal],
        )

        response = await self.execute_workflow(
            workflow,
            extractor,
            identity,
            signal_analyst,
            studio,
            run_id="run-one-permanent-signal-rejection",
            enqueue=True,
        )

        result = EventExtractionResult.model_validate(response.content)
        self.assertEqual(result.signal_fact_uuids, ["signal-fact-1"])
        self.assertEqual(self.runtime.signal_projection_attempts, 2)
        self.assertEqual(self.runtime.signal_projections, 1)

    async def test_pre_v8_publication_checkpoint_resumes_episode_without_rerunning_identity(self) -> None:
        workflow, extractor, identity, signal_analyst = self.workflow()
        draft = self.extraction_draft()
        with patch(
            "capabilities.event.internal.queue.read_resolved_evidences",
            return_value=self.evidences(),
        ):
            enqueue_evidence_artifact(
                str(self.evidence_manifest),
                [item.id for item in self.evidences()],
            )
            claimed = claim_event_batch()
        self.assertIsInstance(claimed, EventExtractionBatch)
        batch = cast(EventExtractionBatch, claimed)
        freeze_draft(batch, draft)
        key = _candidate_key(draft.candidates[0])
        write_publication_journal(
            batch,
            EventPublicationJournal(
                batch_id=batch.batch_id,
                publications=[
                    EventPublicationRecord(
                        candidate_key=key,
                        decision="NEW_EVENT",
                        publication_started=True,
                        event_id=self.runtime.EVENT_ID,
                        event_created=True,
                        evidence_link_result="CREATED",
                        graph_projection_status="NOT_ATTEMPTED",
                        reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                        matched_event_ids=[],
                        published_event={
                            "id": self.runtime.EVENT_ID,
                            "event": draft.candidates[0].event.model_dump(mode="json"),
                        },
                    )
                ],
            ),
        )
        release_event_batch_lease(batch)
        studio = FakeStudioResponses(
            extraction=draft,
            identity=EventIdentityDecision(
                decision="SAME_EVENT",
                atomic=True,
                matched_event_ids=[self.runtime.EVENT_ID],
                reason_codes=["SAME_REAL_WORLD_OCCURRENCE"],
                summary="该 Event 已经可见。",
            ),
            classification=self.classification(),
            proposals=[],
        )

        response = await self.execute_workflow(
            workflow,
            extractor,
            identity,
            signal_analyst,
            studio,
            run_id="run-pre-v8-publication-resume",
            enqueue=False,
        )

        result = EventExtractionResult.model_validate(response.content)
        self.assertEqual(result.published_event_ids, [self.runtime.EVENT_ID])
        self.assertEqual(studio.extraction_inputs, [])
        self.assertEqual(studio.identity_inputs, [])
        self.assertEqual(self.runtime.data_publication_requests, 0)
        self.assertEqual(self.runtime.episode_projections, 1)

    async def test_conflicting_publication_checkpoint_and_resolution_fail_closed(self) -> None:
        workflow, extractor, identity, signal_analyst = self.workflow()
        draft = self.extraction_draft()
        with patch(
            "capabilities.event.internal.queue.read_resolved_evidences",
            return_value=self.evidences(),
        ):
            enqueue_evidence_artifact(
                str(self.evidence_manifest),
                [item.id for item in self.evidences()],
            )
            claimed = claim_event_batch()
        self.assertIsInstance(claimed, EventExtractionBatch)
        batch = cast(EventExtractionBatch, claimed)
        freeze_draft(batch, draft)
        key = _candidate_key(draft.candidates[0])
        freeze_resolution(
            batch,
            EventResolutionRecord(
                candidate_key=key,
                decision="SAME_EVENT",
                atomic=True,
                matched_event_ids=[self.runtime.EVENT_ID],
                reason_codes=["SAME_REAL_WORLD_OCCURRENCE"],
                summary="冲突夹具把已发布 Event 错误标记成重复。",
            ),
        )
        write_publication_journal(
            batch,
            EventPublicationJournal(
                batch_id=batch.batch_id,
                publications=[
                    EventPublicationRecord(
                        candidate_key=key,
                        decision="NEW_EVENT",
                        publication_started=True,
                        event_id=self.runtime.EVENT_ID,
                        event_created=True,
                        evidence_link_result="CREATED",
                        graph_projection_status="NOT_ATTEMPTED",
                        reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                        matched_event_ids=[],
                        published_event={
                            "id": self.runtime.EVENT_ID,
                            "event": draft.candidates[0].event.model_dump(mode="json"),
                        },
                    )
                ],
            ),
        )
        release_event_batch_lease(batch)
        studio = FakeStudioResponses(
            extraction=draft,
            identity=EventIdentityDecision(
                decision="SAME_EVENT",
                atomic=True,
                matched_event_ids=[self.runtime.EVENT_ID],
                reason_codes=["SAME_REAL_WORLD_OCCURRENCE"],
                summary="该 Event 已经可见。",
            ),
            classification=self.classification(),
            proposals=[],
        )

        with self.assertRaisesRegex(RuntimeError, "Event Workflow stage EVENT_PREPARATION failed"):
            await self.execute_workflow(
                workflow,
                extractor,
                identity,
                signal_analyst,
                studio,
                run_id="run-conflicting-publication-resolution",
                enqueue=False,
            )

        self.assertEqual(studio.extraction_inputs, [])
        self.assertEqual(studio.identity_inputs, [])
        self.assertEqual(self.runtime.data_publication_requests, 0)
        self.assertEqual(self.runtime.episode_projections, 0)

    async def test_legacy_failed_publication_recovery_is_repeatable_after_later_crash(self) -> None:
        workflow, extractor, identity, signal_analyst = self.workflow()
        draft = self.extraction_draft()
        with patch(
            "capabilities.event.internal.queue.read_resolved_evidences",
            return_value=self.evidences(),
        ):
            enqueue_evidence_artifact(
                str(self.evidence_manifest),
                [item.id for item in self.evidences()],
            )
            claimed = claim_event_batch()
        self.assertIsInstance(claimed, EventExtractionBatch)
        batch = cast(EventExtractionBatch, claimed)
        freeze_draft(batch, draft)
        key = _candidate_key(draft.candidates[0])
        write_publication_journal(
            batch,
            EventPublicationJournal(
                batch_id=batch.batch_id,
                publications=[
                    EventPublicationRecord(
                        candidate_key=key,
                        decision="FAILED",
                        publication_started=True,
                        event_id=None,
                        event_created=False,
                        evidence_link_result="NOT_ATTEMPTED",
                        graph_projection_status="NOT_ATTEMPTED",
                        reason_codes=["DATA_PUBLICATION_REJECTED"],
                        matched_event_ids=[],
                    )
                ],
            ),
        )
        release_event_batch_lease(batch)
        studio = FakeStudioResponses(
            extraction=draft,
            identity=EventIdentityDecision(
                decision="NEW_EVENT",
                atomic=True,
                matched_event_ids=[],
                reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                summary="没有同一正式 Event。",
            ),
            classification=self.classification(),
            proposals=[],
        )

        with (
            patch(
                "capabilities.event.functions.extraction.complete_batch",
                side_effect=OSError("manifest persistence interrupted after legacy recovery"),
            ),
            self.assertRaisesRegex(RuntimeError, "Event Workflow stage SIGNAL_PUBLICATION failed"),
        ):
            await self.execute_workflow(
                workflow,
                extractor,
                identity,
                signal_analyst,
                studio,
                run_id="run-legacy-failed-recovery-crash",
                enqueue=False,
            )

        response = await self.execute_workflow(
            workflow,
            extractor,
            identity,
            signal_analyst,
            studio,
            run_id="run-legacy-failed-recovery-resume",
            enqueue=False,
        )

        result = EventExtractionResult.model_validate(response.content)
        self.assertEqual(result.failed_candidate_count, 1)
        self.assertEqual(result.failed_evidence_ids, [self.FIRST_EVIDENCE_ID, self.SECOND_EVIDENCE_ID])
        self.assertEqual(studio.extraction_inputs, [])
        self.assertEqual(studio.identity_inputs, [])
        self.assertEqual(self.runtime.data_publication_requests, 0)

    async def test_legacy_terminal_signal_journal_skips_signal_agent_on_resume(self) -> None:
        workflow, extractor, identity, signal_analyst = self.workflow()
        draft = self.extraction_draft()
        with patch(
            "capabilities.event.internal.queue.read_resolved_evidences",
            return_value=self.evidences(),
        ):
            enqueue_evidence_artifact(
                str(self.evidence_manifest),
                [item.id for item in self.evidences()],
            )
            claimed = claim_event_batch()
        self.assertIsInstance(claimed, EventExtractionBatch)
        batch = cast(EventExtractionBatch, claimed)
        freeze_draft(batch, draft)
        key = _candidate_key(draft.candidates[0])
        write_publication_journal(
            batch,
            EventPublicationJournal(
                batch_id=batch.batch_id,
                publications=[
                    EventPublicationRecord(
                        candidate_key=key,
                        decision="NEW_EVENT",
                        publication_started=True,
                        event_id=self.runtime.EVENT_ID,
                        event_created=True,
                        evidence_link_result="CREATED",
                        graph_projection_status="SUCCEEDED",
                        reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                        matched_event_ids=[],
                        episode_uuid=f"episode-{self.runtime.EVENT_ID}",
                        published_event={
                            "id": self.runtime.EVENT_ID,
                            "event": draft.candidates[0].event.model_dump(mode="json"),
                        },
                    )
                ],
            ),
        )
        write_signal_journal(
            batch,
            EventSignalJournal(
                batch_id=batch.batch_id,
                signals=[
                    EventSignalRecord(
                        event_id=self.runtime.EVENT_ID,
                        status="SUCCEEDED",
                        signal_fact_uuids=["legacy-signal-fact"],
                        reason_codes=["DIRECT_SIGNAL_FACTS_PROJECTED"],
                    )
                ],
            ),
        )
        release_event_batch_lease(batch)
        studio = FakeStudioResponses(
            extraction=draft,
            identity=EventIdentityDecision(
                decision="SAME_EVENT",
                atomic=True,
                matched_event_ids=[self.runtime.EVENT_ID],
                reason_codes=["SAME_REAL_WORLD_OCCURRENCE"],
                summary="该 Event 已经可见。",
            ),
            classification=self.classification(),
            proposals=[],
        )

        response = await self.execute_workflow(
            workflow,
            extractor,
            identity,
            signal_analyst,
            studio,
            run_id="run-legacy-signal-terminal-resume",
            enqueue=False,
        )

        result = EventExtractionResult.model_validate(response.content)
        self.assertEqual(result.signal_fact_uuids, ["legacy-signal-fact"])
        self.assertEqual(studio.extraction_inputs, [])
        self.assertEqual(studio.identity_inputs, [])
        self.assertEqual(studio.signal_inputs, [])
        self.assertEqual(self.runtime.signal_projections, 0)

    async def test_retry_replays_same_key_after_data_ack_is_lost_before_local_checkpoint(self) -> None:
        workflow, extractor, identity, signal_analyst = self.workflow()
        self.runtime.signal_candidates = self.candidates()
        self.runtime.fail_before_data_ack_checkpoint_once = True
        studio = FakeStudioResponses(
            extraction=self.extraction_draft(),
            identity=EventIdentityDecision(
                decision="NEW_EVENT",
                atomic=True,
                matched_event_ids=[],
                reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                summary="没有同一正式 Event。",
            ),
            classification=self.classification(),
            proposals=[self.signal_draft()],
        )

        with self.assertRaisesRegex(RuntimeError, "Event Workflow stage EVENT_PUBLICATION failed"):
            await self.execute_workflow(
                workflow,
                extractor,
                identity,
                signal_analyst,
                studio,
                run_id="run-lost-data-ack",
                enqueue=True,
            )

        self.assertEqual(len(studio.extraction_inputs), 1)
        self.assertEqual(len(studio.identity_inputs), 1)
        self.assertEqual(studio.signal_inputs, [])
        self.assertEqual(
            (
                self.runtime.history_reads,
                self.runtime.data_publication_requests,
                self.runtime.data_publications,
                self.runtime.episode_projections,
            ),
            (1, 1, 1, 0),
        )

        resumed = await self.execute_workflow(
            workflow,
            extractor,
            identity,
            signal_analyst,
            studio,
            run_id="run-resume-data-checkpoint",
            enqueue=False,
        )

        self.assertEqual(resumed.status, RunStatus.completed)
        result = EventExtractionResult.model_validate(resumed.content)
        self.assertEqual(result.schema_version, "event_extraction_result.v4")
        self.assertEqual(result.published_event_ids, [self.runtime.EVENT_ID])
        self.assertEqual(result.signal_fact_uuids, ["signal-fact-1"])
        self.assertEqual(len(studio.extraction_inputs), 1)
        self.assertEqual(len(studio.identity_inputs), 1)
        self.assertEqual([request.task for request in studio.signal_inputs], ["CLASSIFY", "PROPOSE_SIGNALS"])
        self.assertEqual(
            (
                self.runtime.history_reads,
                self.runtime.data_publication_requests,
                self.runtime.data_publications,
                self.runtime.episode_projections,
                self.runtime.signal_candidate_reads,
                self.runtime.signal_projections,
            ),
            (1, 2, 1, 1, 1, 1),
        )

    async def test_candidate_retrieval_retry_reuses_frozen_classification(self) -> None:
        workflow, extractor, identity, signal_analyst = self.workflow()
        self.runtime.signal_candidates = self.candidates()
        self.runtime.fail_signal_candidate_read_once = True
        studio = FakeStudioResponses(
            extraction=self.extraction_draft(),
            identity=EventIdentityDecision(
                decision="NEW_EVENT",
                atomic=True,
                matched_event_ids=[],
                reason_codes=["NO_SAME_OCCURRENCE_FOUND"],
                summary="没有同一正式 Event。",
            ),
            classification=self.classification(),
            proposals=[],
        )

        with self.assertRaisesRegex(RuntimeError, "Event Workflow stage SIGNAL_PREPARATION failed"):
            await self.execute_workflow(
                workflow,
                extractor,
                identity,
                signal_analyst,
                studio,
                run_id="run-candidate-retrieval-failure",
                enqueue=True,
            )
        self.assertEqual([request.task for request in studio.signal_inputs], ["CLASSIFY"])

        resumed = await self.execute_workflow(
            workflow,
            extractor,
            identity,
            signal_analyst,
            studio,
            run_id="run-candidate-retrieval-resume",
            enqueue=False,
        )

        self.assertEqual(resumed.status, RunStatus.completed)
        self.assertEqual([request.task for request in studio.signal_inputs], ["CLASSIFY", "PROPOSE_SIGNALS"])
        self.assertEqual(len(studio.extraction_inputs), 1)
        self.assertEqual(len(studio.identity_inputs), 1)
        self.assertEqual(self.runtime.signal_candidate_reads, 2)
        self.assertEqual(
            (self.runtime.data_publications, self.runtime.episode_projections, self.runtime.signal_projections),
            (1, 1, 0),
        )

    def test_workflow_publication_pins_exact_agent_versions_without_saving_agents(self) -> None:
        extractor, identity, signal_analyst = self.studio_agents()
        database = MagicMock()
        database.get_component.return_value = None
        database.upsert_config.return_value = {"version": 23}
        loaded = (
            LoadedEventExtractorAgent(extractor, 11, "a" * 64),
            LoadedEventIdentityAgent(identity, 13, "b" * 64),
            LoadedEventSignalAnalystAgent(signal_analyst, 17, "c" * 64),
        )

        with (
            patch("workflows.event_extraction.get_postgres_db", return_value=database),
            patch("workflows.event_extraction.load_event_extractor_agent", return_value=loaded[0]),
            patch("workflows.event_extraction.load_event_identity_agent", return_value=loaded[1]),
            patch("workflows.event_extraction.load_event_signal_analyst_agent", return_value=loaded[2]),
            patch.object(Agent, "save", autospec=True) as agent_save,
        ):
            version = ensure_event_extraction_workflow(MagicMock())

        self.assertEqual(version, 23)
        agent_save.assert_not_called()
        component_metadata = database.upsert_component.call_args.kwargs["metadata"]
        self.assertEqual(
            component_metadata,
            {
                "event_extraction_contract_version": EVENT_EXTRACTION_CONTRACT_VERSION,
                "event_agent_versions": self.AGENT_VERSIONS,
            },
        )
        publication = database.upsert_config.call_args.kwargs
        self.assertEqual(publication["stage"], "published")
        self.assertEqual(publication["config"]["metadata"], component_metadata)
        self.assertEqual(
            publication["links"],
            [
                {
                    "link_kind": "step_agent",
                    "link_key": "event-extract-agent",
                    "child_component_id": "event-extractor",
                    "child_version": 11,
                    "position": 0,
                },
                {
                    "link_kind": "step_agent",
                    "link_key": "event-identity-agent",
                    "child_component_id": "event-identity",
                    "child_version": 13,
                    "position": 1,
                },
                {
                    "link_kind": "step_agent",
                    "link_key": "event-signal-agent",
                    "child_component_id": "event-signal-analyst",
                    "child_version": 17,
                    "position": 1,
                },
            ],
        )

    def test_agent_contract_migrations_reconfigure_code_fields_without_replacing_studio_instructions(self) -> None:
        cases = (
            (
                "agents.event_extractor",
                "event-extractor",
                "event_extractor_contract_version",
                EVENT_EXTRACTOR_CONTRACT_VERSION,
                EventExtractionDraft,
                ensure_event_extractor_agent,
            ),
            (
                "agents.event_identity",
                "event-identity",
                "event_identity_contract_version",
                EVENT_IDENTITY_CONTRACT_VERSION,
                EventIdentityDecision,
                ensure_event_identity_agent,
            ),
            (
                "agents.event_signal_analyst",
                "event-signal-analyst",
                "event_signal_analyst_contract_version",
                EVENT_SIGNAL_ANALYST_CONTRACT_VERSION,
                EventSignalAnalysisDraft,
                ensure_event_signal_analyst_agent,
            ),
        )
        for module, agent_id, contract_key, contract_version, output_schema, ensure in cases:
            with self.subTest(agent_id=agent_id):
                database = MagicMock()
                database.get_component.return_value = {"current_version": 41}
                instructions = f"Studio customized prompt for {agent_id}"
                current = Agent(
                    id=agent_id,
                    instructions=instructions,
                    additional_context="stale runtime contract",
                    metadata={contract_key: 0},
                )
                with (
                    patch(f"{module}.get_postgres_db", return_value=database),
                    patch(f"{module}.Agent.load", return_value=current),
                    patch.object(current, "save", autospec=True, return_value=42) as save,
                ):
                    version = ensure(MagicMock())

                self.assertEqual(version, 42)
                self.assertEqual(current.instructions, instructions)
                self.assertEqual(current.output_schema, output_schema)
                self.assertEqual(current.tools, [])
                self.assertIsNotNone(current.metadata)
                assert current.metadata is not None
                self.assertEqual(current.metadata[contract_key], contract_version)
                self.assertNotEqual(current.additional_context, "stale runtime contract")
                agent_name = {
                    "event-extractor": "Event Extractor",
                    "event-identity": "Event Identity",
                    "event-signal-analyst": "Event Signal Analyst",
                }[agent_id]
                save.assert_called_once_with(
                    db=database,
                    stage="published",
                    notes=f"{agent_name} runtime contract migration {contract_version}",
                )

    def test_current_workflow_rejects_incomplete_agent_version_metadata_or_links(self) -> None:
        complete_versions = dict(self.AGENT_VERSIONS)
        complete_links = [
            {
                "link_kind": "step_agent",
                "child_component_id": agent_id,
                "child_version": version,
            }
            for agent_id, version in complete_versions.items()
        ]
        cases = (
            (
                {"event-extractor": 11, "event-identity": 13},
                complete_links,
                "version metadata is incomplete",
            ),
            (
                complete_versions,
                complete_links[:-1],
                "does not pin all exact Agent versions",
            ),
        )
        for metadata_versions, links, error in cases:
            with self.subTest(error=error):
                database = MagicMock()
                database.get_component.return_value = {"current_version": 29}
                database.get_config.return_value = {
                    "config": {
                        "id": "event-extraction",
                        "metadata": {
                            "event_extraction_contract_version": EVENT_EXTRACTION_CONTRACT_VERSION,
                            "event_agent_versions": metadata_versions,
                        },
                    }
                }
                database.get_links.return_value = links
                with (
                    patch("workflows.event_extraction.get_postgres_db", return_value=database),
                    patch.object(Workflow, "load", autospec=True) as workflow_load,
                    self.assertRaisesRegex(ValueError, error),
                ):
                    ensure_event_extraction_workflow(MagicMock())

                workflow_load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
