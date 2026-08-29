"""Contract tests for the Schedule-driven investment reasoning Workflow."""

import os
import unittest
from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from agno.agent import Agent
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.workflow import Step, StepInput, StepOutput, Workflow

from agents.investment_planner import (
    INVESTMENT_PLANNER_CONTRACT_VERSION,
    build_investment_planner_agent,
    ensure_investment_planner_agent,
)
from agents.investment_reasoner import (
    INVESTMENT_REASONER_CONTRACT_VERSION,
    ensure_investment_reasoner_agent,
)
from agents.investment_reviewer import (
    INVESTMENT_REVIEWER_CONTRACT_VERSION,
    ensure_investment_reviewer_agent,
)
from app.registry import TidewiseRegistry
from capabilities.investment import (
    AcceptedTransmission,
    AnalysisDraft,
    ChainNodeSnapshot,
    Confidence,
    Direction,
    EventSnapshot,
    FactSnapshot,
    Horizon,
    IndustryChainSnapshot,
    InvestmentAnalysisContext,
    InvestmentAnalysisPlan,
    InvestmentAnalysisRequest,
    InvestmentAnalysisResult,
    InvestmentAssessment,
    InvestmentDraftState,
    InvestmentTransmissionState,
    NodeTrendView,
    PreparedInvestmentContext,
    TopologyEdgeSnapshot,
    TransmissionBatch,
    TransmissionProposal,
    Trend,
    configure_investment_workflow_runtime,
)
from capabilities.investment.functions import reason_signal_transmissions, review_and_finalize
from capabilities.investment.internal.context import InvestmentContextBuilder
from capabilities.investment.internal.engine import InvestmentReasoningEngine
from sematica.graphiti.investment import GraphitiInvestmentReader
from workflows.investment_reasoning import (
    INVESTMENT_REASONING_CONTRACT_VERSION,
    _seed_workflow,
    ensure_investment_reasoning_workflow,
)


class InvestmentReasoningGateTest(unittest.TestCase):
    def _context(self, fact: FactSnapshot) -> InvestmentAnalysisContext:
        return InvestmentAnalysisContext(
            request=InvestmentAnalysisRequest(
                question="分析最近48小时事件对相关产业链节点投资价值的影响",
                decision_at=datetime(2026, 8, 29, 0, 0, tzinfo=UTC),
            ),
            events=[
                EventSnapshot(
                    episode_uuid="episode-1",
                    event_id="event-1",
                    title="测试事件",
                    summary="测试事件摘要",
                    modality="FACT",
                    occurred_at=datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
                )
            ],
            facts=[fact],
            chains=[
                IndustryChainSnapshot(
                    uuid="chain-uuid",
                    business_id="chain-1",
                    name="测试产业链",
                    anchor_match_count=1,
                    matched_node_ids=["node-a"],
                    signal_root_fact_ids=[fact.uuid] if fact.kind == "SIGNAL" else [],
                    signal_root_node_ids=["node-a"] if fact.kind == "SIGNAL" else [],
                    nodes=[
                        ChainNodeSnapshot(uuid="node-a-uuid", business_id="node-a", name="上游"),
                        ChainNodeSnapshot(uuid="node-b-uuid", business_id="node-b", name="下游"),
                    ],
                    edges=[
                        TopologyEdgeSnapshot(
                            uuid="edge-uuid",
                            business_id="edge-1",
                            name="ChainNodeInputTo",
                            source_node_id="node-a",
                            source_name="上游",
                            target_node_id="node-b",
                            target_name="下游",
                            fact="上游向下游提供投入品",
                        )
                    ],
                )
            ],
        )

    def _proposal(self, fact_id: str) -> TransmissionBatch:
        return TransmissionBatch(
            proposals=[
                TransmissionProposal(
                    chain_id="chain-1",
                    topology_edge_id="edge-1",
                    source_node_id="node-a",
                    target_node_id="node-b",
                    flow="ALONG_EDGE",
                    target_variable="下游供给",
                    direction=Direction.DOWN,
                    horizon=Horizon.SHORT,
                    confidence=Confidence.MEDIUM,
                    mechanism="上游收缩沿真实投入关系传导至下游。",
                    source_fact_ids=[fact_id],
                )
            ]
        )

    def test_ordinary_fact_cannot_start_directional_transmission(self) -> None:
        fact = FactSnapshot(
            uuid="ordinary-1",
            kind="ORDINARY",
            name="SUPPLIES",
            fact="上游向下游供货",
            source_uuid="node-a-uuid",
            source_name="上游",
            source_business_id="node-a",
            source_labels=["Entity", "ChainNode"],
            target_uuid="node-b-uuid",
            target_name="下游",
            target_business_id="node-b",
            target_labels=["Entity", "ChainNode"],
            source_event_ids=["event-1"],
        )

        accepted = InvestmentReasoningEngine.validate_round(
            self._context(fact), [], self._proposal(fact.uuid), round_number=1
        )

        self.assertEqual(accepted, [])

    def test_active_signal_fact_can_start_and_carries_root_lineage(self) -> None:
        fact = FactSnapshot(
            uuid="signal-1",
            kind="SIGNAL",
            name="SIGNAL_ON",
            fact="有效产能下降作用于上游节点",
            source_uuid="variable-uuid",
            source_name="有效产能",
            source_business_id="variable-1",
            source_labels=["Entity", "Variable"],
            target_uuid="node-a-uuid",
            target_name="上游",
            target_business_id="node-a",
            target_labels=["Entity", "ChainNode"],
            source_event_ids=["event-1"],
            direction=Direction.DOWN,
            horizons=[Horizon.SHORT],
            confidence=Confidence.HIGH,
            valid_at=datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
        )

        accepted = InvestmentReasoningEngine.validate_round(
            self._context(fact), [], self._proposal(fact.uuid), round_number=1
        )

        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].root_signal_fact_ids, [fact.uuid])
        self.assertEqual(accepted[0].confidence, Confidence.MEDIUM)

    def test_first_hop_cannot_escape_signal_horizon(self) -> None:
        fact = self._active_signal()
        proposal = self._proposal(fact.uuid).proposals[0].model_copy(update={"horizon": Horizon.LONG})

        accepted = InvestmentReasoningEngine.validate_round(
            self._context(fact), [], TransmissionBatch(proposals=[proposal]), round_number=1
        )

        self.assertEqual(accepted, [])

    def test_topology_flow_must_match_the_real_edge_direction(self) -> None:
        fact = self._active_signal()
        proposal = self._proposal(fact.uuid).proposals[0]
        wrong_flow = proposal.model_copy(update={"flow": "AGAINST_EDGE"})

        accepted = InvestmentReasoningEngine.validate_round(
            self._context(fact), [], TransmissionBatch(proposals=[wrong_flow]), round_number=1
        )

        self.assertEqual(accepted, [])

    def test_later_hop_requires_previous_same_chain_same_horizon_parent(self) -> None:
        fact = self._active_signal()
        context = self._context(fact)
        first = InvestmentReasoningEngine.validate_round(context, [], self._proposal(fact.uuid), round_number=1)[0]
        reverse = TransmissionProposal(
            chain_id="chain-1",
            topology_edge_id="edge-1",
            source_node_id="node-b",
            target_node_id="node-a",
            flow="AGAINST_EDGE",
            target_variable="上游需求",
            direction=Direction.DOWN,
            horizon=Horizon.SHORT,
            confidence=Confidence.HIGH,
            mechanism="下游减产反向压低上游需求。",
            parent_transmission_ids=[first.transmission_id],
        )

        second = InvestmentReasoningEngine.validate_round(
            context, [first], TransmissionBatch(proposals=[reverse]), round_number=2
        )
        jump = InvestmentReasoningEngine.validate_round(
            context, [first], TransmissionBatch(proposals=[reverse]), round_number=3
        )
        wrong_chain_parent = first.model_copy(update={"chain_id": "other-chain"})
        cross_chain = InvestmentReasoningEngine.validate_round(
            context, [wrong_chain_parent], TransmissionBatch(proposals=[reverse]), round_number=2
        )

        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].confidence, Confidence.LOW)
        self.assertEqual(jump, [])
        self.assertEqual(cross_chain, [])

    def test_three_hop_fixture_keeps_lineage_and_rejects_a_fabricated_edge(self) -> None:
        fact = self._active_signal()
        nodes = [
            ChainNodeSnapshot(uuid=f"node-{name}-uuid", business_id=f"node-{name}", name=name.upper())
            for name in ("a", "b", "c", "d")
        ]
        edges = [
            TopologyEdgeSnapshot(
                uuid=f"edge-{index}-uuid",
                business_id=f"edge-{index}",
                name="ChainNodeInputTo",
                source_node_id=f"node-{source}",
                source_name=source.upper(),
                target_node_id=f"node-{target}",
                target_name=target.upper(),
                fact=f"{source.upper()} 向 {target.upper()} 提供投入品",
            )
            for index, (source, target) in enumerate((("a", "b"), ("b", "c"), ("c", "d")), 1)
        ]
        chain = IndustryChainSnapshot(
            uuid="chain-uuid",
            business_id="chain-1",
            name="三跳测试链",
            anchor_match_count=1,
            matched_node_ids=["node-a"],
            signal_root_fact_ids=[fact.uuid],
            signal_root_node_ids=["node-a"],
            nodes=nodes,
            edges=edges,
        )
        context = self._context(fact).model_copy(update={"chains": [chain]})
        accepted: list[AcceptedTransmission] = []
        parent_id = None
        for hop, (source, target) in enumerate((("a", "b"), ("b", "c"), ("c", "d")), 1):
            proposal = TransmissionProposal(
                chain_id="chain-1",
                topology_edge_id=f"edge-{hop}",
                source_node_id=f"node-{source}",
                target_node_id=f"node-{target}",
                flow="ALONG_EDGE",
                target_variable="需求传导",
                direction=Direction.DOWN,
                horizon=Horizon.SHORT,
                confidence=Confidence.HIGH,
                mechanism=f"第 {hop} 跳沿真实拓扑传导。",
                source_fact_ids=[fact.uuid] if hop == 1 else [],
                parent_transmission_ids=[parent_id] if parent_id else [],
            )
            new_items = InvestmentReasoningEngine.validate_round(
                context, accepted, TransmissionBatch(proposals=[proposal]), round_number=hop
            )
            self.assertEqual(len(new_items), 1)
            accepted.extend(new_items)
            parent_id = new_items[0].transmission_id

        fabricated = accepted[-1].model_copy(
            update={"topology_edge_id": "edge-does-not-exist", "transmission_id": "ignored"}
        )
        fabricated_proposal = TransmissionProposal.model_validate(
            fabricated.model_dump(exclude={"transmission_id", "hop", "root_signal_fact_ids"})
        )
        rejected = InvestmentReasoningEngine.validate_round(
            context, accepted[:-1], TransmissionBatch(proposals=[fabricated_proposal]), round_number=3
        )

        self.assertEqual([item.hop for item in accepted], [1, 2, 3])
        self.assertTrue(all(item.root_signal_fact_ids == [fact.uuid] for item in accepted))
        self.assertEqual(rejected, [])

    def test_uncited_directional_node_claim_is_normalized_to_insufficient(self) -> None:
        fact = self._active_signal()
        context = self._context(fact)
        node = NodeTrendView(
            chain_id="chain-1",
            node_id="node-a",
            node_name="上游",
            short=Trend.WARMING,
            medium=Trend.INSUFFICIENT_EVIDENCE,
            long=Trend.INSUFFICIENT_EVIDENCE,
            confidence=Confidence.HIGH,
            investment_assessment=InvestmentAssessment.OPPORTUNITY_CANDIDATE,
            rationale="供给收缩。",
        )

        normalized = InvestmentReasoningEngine._normalize_node(context, [], "chain-1", "node-a", node)

        self.assertEqual(normalized.short, Trend.INSUFFICIENT_EVIDENCE)
        self.assertEqual(normalized.investment_assessment, InvestmentAssessment.INSUFFICIENT_EVIDENCE)

    @staticmethod
    def _active_signal() -> FactSnapshot:
        return FactSnapshot(
            uuid="signal-1",
            kind="SIGNAL",
            name="SIGNAL_ON",
            fact="有效产能下降作用于上游节点",
            source_uuid="variable-uuid",
            source_name="有效产能",
            source_business_id="variable-1",
            source_labels=["Entity", "Variable"],
            target_uuid="node-a-uuid",
            target_name="上游",
            target_business_id="node-a",
            target_labels=["Entity", "ChainNode"],
            source_event_ids=["event-1"],
            direction=Direction.DOWN,
            horizons=[Horizon.SHORT],
            confidence=Confidence.HIGH,
            valid_at=datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
        )


class InvestmentWorkflowShapeTest(unittest.TestCase):
    def test_workflow_has_five_fixed_business_stages(self) -> None:
        workflow = _seed_workflow(cast(Agent, object()), cast(Agent, object()), cast(Agent, object()))
        steps = cast(list[Step], workflow.steps)
        self.assertEqual(
            [step.name for step in steps],
            [
                "plan-investment-analysis",
                "prepare-investment-context",
                "reason-signal-transmissions",
                "synthesize-investment-conclusion",
                "review-and-finalize",
            ],
        )


class InvestmentComponentLifecycleTest(unittest.TestCase):
    def test_each_agent_migrates_an_old_contract_to_the_current_version(self) -> None:
        cases = [
            (
                "agents.investment_planner",
                ensure_investment_planner_agent,
                "investment_planner_contract_version",
                INVESTMENT_PLANNER_CONTRACT_VERSION,
                "Investment Planner",
            ),
            (
                "agents.investment_reasoner",
                ensure_investment_reasoner_agent,
                "investment_reasoner_contract_version",
                INVESTMENT_REASONER_CONTRACT_VERSION,
                "Investment Reasoner",
            ),
            (
                "agents.investment_reviewer",
                ensure_investment_reviewer_agent,
                "investment_reviewer_contract_version",
                INVESTMENT_REVIEWER_CONTRACT_VERSION,
                "Investment Reviewer",
            ),
        ]
        for module, ensure, metadata_key, contract_version, name in cases:
            with self.subTest(agent=name):
                db = MagicMock()
                db.get_component.return_value = {"current_version": 3}
                current = MagicMock()
                current.metadata = {metadata_key: 0}
                current.save.return_value = 4
                with (
                    patch(f"{module}.get_postgres_db", return_value=db),
                    patch(f"{module}.Agent.load", return_value=current),
                ):
                    version = ensure(MagicMock())

                self.assertEqual(version, 4)
                self.assertEqual(current.metadata[metadata_key], contract_version)
                current.save.assert_called_once_with(
                    db=db,
                    stage="published",
                    notes=f"{name} contract migration {contract_version}",
                )

    def test_registry_resolves_all_three_investment_agents(self) -> None:
        registry = TidewiseRegistry(name="Investment Registry Test")
        planner = Agent(id="investment-planner")
        reasoner = Agent(id="investment-reasoner")
        reviewer = Agent(id="investment-reviewer")
        with (
            patch("app.registry.load_investment_planner_agent", return_value=planner),
            patch("app.registry.load_investment_reasoner_agent", return_value=reasoner),
            patch("app.registry.load_investment_reviewer_agent", return_value=reviewer),
        ):
            self.assertIs(registry.get_agent("investment-planner"), planner)
            self.assertIs(registry.get_agent("investment-reasoner"), reasoner)
            self.assertIs(registry.get_agent("investment-reviewer"), reviewer)

    def test_workflow_migration_preserves_all_three_agent_bindings(self) -> None:
        db = MagicMock()
        db.get_component.return_value = {"current_version": 7}
        db.get_config.return_value = {
            "config": {
                "id": "investment-reasoning",
                "name": "Investment Reasoning",
                "metadata": {"investment_reasoning_contract_version": 0},
            }
        }
        planner = Agent(id="investment-planner", db=None)
        reasoner = Agent(id="investment-reasoner", db=None)
        reviewer = Agent(id="investment-reviewer", db=None)
        with (
            patch("workflows.investment_reasoning.get_postgres_db", return_value=db),
            patch("workflows.investment_reasoning.load_investment_planner_agent", return_value=planner),
            patch("workflows.investment_reasoning.load_investment_reasoner_agent", return_value=reasoner),
            patch("workflows.investment_reasoning.load_investment_reviewer_agent", return_value=reviewer),
            patch.object(Workflow, "save", autospec=True, return_value=8) as saved,
        ):
            version = ensure_investment_reasoning_workflow(MagicMock())

        self.assertEqual(version, 8)
        migrated = cast(Workflow, saved.call_args.args[0])
        self.assertEqual(
            migrated.metadata,
            {"investment_reasoning_contract_version": INVESTMENT_REASONING_CONTRACT_VERSION},
        )
        self.assertEqual(
            migrated.dependencies,
            {
                "planner_agent_id": "investment-planner",
                "reasoner_agent_id": "investment-reasoner",
                "reviewer_agent_id": "investment-reviewer",
            },
        )
        planner_step = cast(list[Step], migrated.steps)[0]
        self.assertIs(planner_step.agent, planner)


class _NoSignalRuntime:
    def __init__(self) -> None:
        self.propagate_calls = 0

    async def propagate(self, *args, **kwargs):
        self.propagate_calls += 1
        raise AssertionError("no-signal context must not invoke the Reasoner")

    async def review(self, *args, **kwargs):
        raise AssertionError("an abstaining draft must not invoke the Reviewer")


class _CompleteNoSignalRuntime(_NoSignalRuntime):
    def __init__(self, context: InvestmentAnalysisContext) -> None:
        super().__init__()
        self.context = context

    async def prepare(self, request):
        return self.context.model_copy(update={"request": request})

    async def synthesize(self, context, transmissions):
        del transmissions
        return AnalysisDraft(
            one_sentence_conclusion="没有有效 Signal Fact，当前不形成方向结论。",
            chains=[InvestmentReasoningEngine.insufficient_chain(context.chains[0])],
            limitations=["NO_ELIGIBLE_SIGNAL_ROOT"],
        )


class InvestmentWorkflowExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_message_runs_all_five_stages_to_a_typed_result(self) -> None:
        gate = InvestmentReasoningGateTest()
        ordinary = FactSnapshot(
            uuid="ordinary-1",
            kind="ORDINARY",
            name="MENTIONS",
            fact="事件与上游节点相关",
            source_uuid="event-entity",
            source_name="事件主题",
            target_uuid="node-a-uuid",
            target_name="上游",
            target_business_id="node-a",
            target_labels=["Entity", "ChainNode"],
        )
        runtime = _CompleteNoSignalRuntime(gate._context(ordinary))
        planner = build_investment_planner_agent()
        planner.db = None
        workflow = _seed_workflow(planner, Agent(id="reasoner"), Agent(id="reviewer"))
        workflow.db = None
        planner_run = AsyncMock(
            return_value=RunOutput(
                agent_id="investment-planner",
                content=InvestmentAnalysisPlan(
                    question="分析最近48小时事件对产业链的影响",
                    event_window_hours=48,
                ),
                content_type="InvestmentAnalysisPlan",
            )
        )
        configure_investment_workflow_runtime(runtime)
        try:
            with patch.object(planner, "arun", new=planner_run):
                response = await workflow.arun(
                    input="获取最近48小时Event并分析产业链节点趋势",
                    run_id="investment-boundary-run",
                    session_id="investment-boundary-session",
                )
        finally:
            configure_investment_workflow_runtime(None)

        self.assertEqual(response.status, RunStatus.completed)
        result = InvestmentAnalysisResult.model_validate(response.content)
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(result.stage_metrics["transmission_rounds"], 0)
        self.assertEqual(planner_run.call_count, 1)

    async def test_no_signal_context_skips_model_propagation(self) -> None:
        gate = InvestmentReasoningGateTest()
        ordinary = FactSnapshot(
            uuid="ordinary-1",
            kind="ORDINARY",
            name="MENTIONS",
            fact="事件与上游节点相关",
            source_uuid="event-entity",
            source_name="事件主题",
            target_uuid="node-a-uuid",
            target_name="上游",
            target_business_id="node-a",
            target_labels=["Entity", "ChainNode"],
        )
        context = gate._context(ordinary)
        runtime = _NoSignalRuntime()
        configure_investment_workflow_runtime(runtime)
        try:
            output = await reason_signal_transmissions(
                StepInput(
                    previous_step_outputs={
                        "prepare-investment-context": StepOutput(
                            content=PreparedInvestmentContext(
                                context=context,
                                context_fingerprint=InvestmentReasoningEngine.context_fingerprint(context),
                            )
                        )
                    }
                )
            )
        finally:
            configure_investment_workflow_runtime(None)

        state = cast(InvestmentTransmissionState, output.content)
        self.assertEqual(runtime.propagate_calls, 0)
        self.assertEqual(state.rounds_executed, 0)
        self.assertEqual(state.transmissions, [])

    async def test_all_insufficient_draft_is_a_successful_abstention(self) -> None:
        gate = InvestmentReasoningGateTest()
        ordinary = FactSnapshot(
            uuid="ordinary-1",
            kind="ORDINARY",
            name="MENTIONS",
            fact="事件与上游节点相关",
            source_uuid="event-entity",
            source_name="事件主题",
            target_uuid="node-a-uuid",
            target_name="上游",
            target_business_id="node-a",
            target_labels=["Entity", "ChainNode"],
        )
        context = gate._context(ordinary)
        runtime = _NoSignalRuntime()
        configure_investment_workflow_runtime(runtime)
        state = InvestmentDraftState(
            context=context,
            context_fingerprint=InvestmentReasoningEngine.context_fingerprint(context),
            transmissions=[],
            rounds_executed=0,
            draft=AnalysisDraft(
                one_sentence_conclusion="没有有效 Signal Fact，当前不形成方向结论。",
                chains=[InvestmentReasoningEngine.insufficient_chain(context.chains[0])],
                limitations=["NO_ELIGIBLE_SIGNAL_ROOT"],
            ),
        )
        try:
            output = await review_and_finalize(
                StepInput(previous_step_outputs={"synthesize-investment-conclusion": StepOutput(content=state)})
            )
        finally:
            configure_investment_workflow_runtime(None)

        result = cast(Any, output.content)
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertTrue(result.review.accepted)

    async def test_json_step_boundary_returns_typed_workflow_result(self) -> None:
        gate = InvestmentReasoningGateTest()
        context = gate._context(
            FactSnapshot(
                uuid="ordinary-1",
                kind="ORDINARY",
                name="MENTIONS",
                fact="事件与上游节点相关",
                source_uuid="event-entity",
                source_name="事件主题",
                target_uuid="node-a-uuid",
                target_name="上游",
                target_business_id="node-a",
                target_labels=["Entity", "ChainNode"],
            )
        )
        state = InvestmentDraftState(
            context=context,
            context_fingerprint=InvestmentReasoningEngine.context_fingerprint(context),
            transmissions=[],
            rounds_executed=0,
            draft=AnalysisDraft(
                one_sentence_conclusion="证据不足。",
                chains=[InvestmentReasoningEngine.insufficient_chain(context.chains[0])],
            ),
        )
        configure_investment_workflow_runtime(_NoSignalRuntime())
        try:
            output = await review_and_finalize(
                StepInput(
                    previous_step_outputs={
                        "synthesize-investment-conclusion": StepOutput(content=state.model_dump_json())
                    }
                )
            )
        finally:
            configure_investment_workflow_runtime(None)

        self.assertIsInstance(output.content, InvestmentAnalysisResult)


class _SearchOnlyGraphiti:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, **kwargs):
        self.queries.append(query)
        return [SimpleNamespace(uuid="fact-1")]


class GraphitiInvestmentRetrievalTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_search_batches_question_and_events_instead_of_one_large_query(self) -> None:
        graphiti = _SearchOnlyGraphiti()
        reader = GraphitiInvestmentReader(cast(Any, graphiti))
        events = [
            EventSnapshot(
                episode_uuid=f"episode-{index}",
                event_id=f"event-{index}",
                title=f"事件{index}",
                summary="摘要" * 20,
                modality="FACT",
                occurred_at=datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
            )
            for index in range(25)
        ]

        queries = InvestmentContextBuilder.build_native_queries("分析命题", events)
        ids = await reader.search_fact_ids(queries, {"fact-1"})

        self.assertEqual(ids, ["fact-1"])
        self.assertEqual(len(graphiti.queries), 3)
        self.assertTrue(all(len(query) <= 2000 for query in graphiti.queries))
        event_queries = "\n".join(graphiti.queries[1:])
        self.assertTrue(all(event.title in event_queries for event in events))

    async def test_native_search_controls_ordinary_context_but_never_drops_signal_roots(self) -> None:
        gate = InvestmentReasoningGateTest()
        signal = gate._active_signal()
        ordinary_selected = signal.model_copy(update={"uuid": "ordinary-selected", "kind": "ORDINARY"})
        ordinary_ignored = signal.model_copy(update={"uuid": "ordinary-ignored", "kind": "ORDINARY"})

        selected = InvestmentContextBuilder.select_retrieved_facts(
            [signal, ordinary_selected, ordinary_ignored], [ordinary_selected.uuid]
        )

        self.assertEqual([item.uuid for item in selected], [signal.uuid, ordinary_selected.uuid])

    async def test_five_hundred_long_events_have_a_strict_twenty_six_query_budget(self) -> None:
        events = [
            EventSnapshot(
                episode_uuid=f"episode-{index}",
                event_id=f"event-{index}",
                title=f"唯一事件{index:03d}",
                summary="很长的事件摘要" * 200,
                modality="FACT",
                occurred_at=datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
            )
            for index in range(500)
        ]

        queries = InvestmentContextBuilder.build_native_queries("分析命题", events)

        self.assertEqual(len(queries), 26)
        self.assertTrue(all(len(query) <= 500 for query in queries[1:]))
        combined = "\n".join(queries[1:])
        self.assertTrue(all(event.title in combined for event in events))


class InvestmentLifespanTest(unittest.IsolatedAsyncioTestCase):
    async def test_startup_failure_closes_already_created_event_runtime(self) -> None:
        with patch.dict(os.environ, {"RUNTIME_ENV": "dev"}):
            import app.main as main

        event_runtime = SimpleNamespace(close=AsyncMock())
        ensure_names = [
            "ensure_collector_agent",
            "ensure_title_curator_agent",
            "ensure_evidence_extractor_agent",
            "ensure_event_extractor_agent",
            "ensure_investment_planner_agent",
            "ensure_investment_reasoner_agent",
            "ensure_investment_reviewer_agent",
            "ensure_raw_collection_workflow",
            "ensure_evidence_extraction_workflow",
            "ensure_event_extraction_workflow",
            "ensure_investment_reasoning_workflow",
        ]
        with ExitStack() as stack:
            for name in ensure_names:
                stack.enter_context(patch.object(main, name))
            stack.enter_context(patch.object(main.registry, "get_model", return_value=object()))
            stack.enter_context(patch.object(main, "create_local_event_workflow_runtime", return_value=event_runtime))
            stack.enter_context(patch.object(main, "load_investment_reasoner_agent", return_value=Agent(id="reasoner")))
            stack.enter_context(patch.object(main, "load_investment_reviewer_agent", return_value=Agent(id="reviewer")))
            stack.enter_context(
                patch.object(
                    main,
                    "create_local_investment_workflow_runtime",
                    side_effect=RuntimeError("investment runtime failed"),
                )
            )
            with self.assertRaisesRegex(RuntimeError, "investment runtime failed"):
                async with main.lifespan(None):
                    pass

        event_runtime.close.assert_awaited_once()

    async def test_one_close_failure_does_not_block_the_other_runtime(self) -> None:
        with patch.dict(os.environ, {"RUNTIME_ENV": "dev"}):
            import app.main as main

        event_runtime = SimpleNamespace(close=AsyncMock())
        investment_runtime = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close failed")))
        ensure_names = [
            "ensure_collector_agent",
            "ensure_title_curator_agent",
            "ensure_evidence_extractor_agent",
            "ensure_event_extractor_agent",
            "ensure_investment_planner_agent",
            "ensure_investment_reasoner_agent",
            "ensure_investment_reviewer_agent",
            "ensure_raw_collection_workflow",
            "ensure_evidence_extraction_workflow",
            "ensure_event_extraction_workflow",
            "ensure_investment_reasoning_workflow",
        ]
        with ExitStack() as stack:
            for name in ensure_names:
                stack.enter_context(patch.object(main, name))
            stack.enter_context(patch.object(main.registry, "get_model", return_value=object()))
            stack.enter_context(patch.object(main, "create_local_event_workflow_runtime", return_value=event_runtime))
            stack.enter_context(patch.object(main, "load_investment_reasoner_agent", return_value=Agent(id="reasoner")))
            stack.enter_context(patch.object(main, "load_investment_reviewer_agent", return_value=Agent(id="reviewer")))
            stack.enter_context(
                patch.object(main, "create_local_investment_workflow_runtime", return_value=investment_runtime)
            )
            stack.enter_context(patch.object(main, "validate_schedules"))
            with self.assertRaisesRegex(ExceptionGroup, "AgentOS runtime shutdown failed"):
                async with main.lifespan(None):
                    pass

        investment_runtime.close.assert_awaited_once()
        event_runtime.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
