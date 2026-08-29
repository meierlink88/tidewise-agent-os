"""Graphiti context and LLM adapters for the investment reasoning Pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from graphiti_core import Graphiti
from graphiti_core.prompts.models import Message
from pydantic import ValidationError

from sematica.reasoning.investment.contracts import (
    AcceptedTransmission,
    AnalysisDraft,
    ChainNodeSnapshot,
    ChainTrendView,
    Confidence,
    Direction,
    EventSnapshot,
    FactSnapshot,
    Horizon,
    IndustryChainSnapshot,
    InvestmentAnalysisContext,
    InvestmentAnalysisRequest,
    InvestmentAssessment,
    NodeAnalysisBatch,
    NodeTrendView,
    RecordedReasoningPayload,
    ReviewResult,
    TopologyEdgeSnapshot,
    TransmissionBatch,
    Trend,
)

GROUP_ID = "neo4j"
logger = logging.getLogger(__name__)
TOPOLOGY_NAMES = (
    "ChainNodeInputTo",
    "ChainNodeIsComponentOf",
    "ChainNodeDependsOn",
)


def _native_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _horizons(values: list[str] | None) -> list[Horizon]:
    result: list[Horizon] = []
    for value in values or []:
        try:
            result.append(Horizon(value))
        except ValueError:
            continue
    return result


class GraphitiInvestmentContextAssembler:
    """Freeze one read-only Graphiti retrieval into an executor-neutral context."""

    def __init__(self, graphiti: Graphiti, *, group_id: str = GROUP_ID) -> None:
        self._graphiti = graphiti
        self._group_id = group_id

    async def build(self, request: InvestmentAnalysisRequest) -> InvestmentAnalysisContext:
        events = await self._load_events(request)
        if not events:
            raise ValueError("no Event Episodes fall inside the requested event window")
        event_ids = [item.event_id for item in events]
        episode_ids = [item.episode_uuid for item in events]
        episode_to_event_id = {item.episode_uuid: item.event_id for item in events}
        mentions = await self._load_mentions(episode_ids)
        facts = await self._load_facts(
            request,
            event_ids,
            episode_ids,
            episode_to_event_id,
        )

        search_query = "\n".join([request.question] + [f"{item.title}：{item.summary}" for item in events])
        native_edges = await self._graphiti.search(
            search_query,
            group_ids=[self._group_id],
            num_results=50,
        )
        fact_ids = {item.uuid for item in facts}
        native_ids = [item.uuid for item in native_edges if item.uuid in fact_ids]

        anchor_node_ids = {
            item["business_id"] for item in mentions if "ChainNode" in item["labels"] and item["business_id"]
        }
        for fact in facts:
            if "ChainNode" in fact.target_labels and fact.target_business_id:
                anchor_node_ids.add(fact.target_business_id)
            if "ChainNode" in fact.source_labels and fact.source_business_id:
                anchor_node_ids.add(fact.source_business_id)
        chains = await self._load_chains(request, anchor_node_ids)
        issues = self._validation_issues(events, facts)
        return InvestmentAnalysisContext(
            request=request,
            events=events,
            facts=facts,
            chains=chains,
            native_retrieved_fact_ids=list(dict.fromkeys(native_ids)),
            validation_issues=issues,
        )

    async def _load_events(self, request: InvestmentAnalysisRequest) -> list[EventSnapshot]:
        records, _, _ = await self._graphiti.driver.execute_query(
            """
            MATCH (event:Episodic {group_id: $group_id})
            WHERE event.episode_kind = 'EVENT'
            RETURN event.uuid AS episode_uuid,
                   event.domain_object_id AS event_id,
                   event.name AS name,
                   event.content AS content,
                   event.valid_at AS valid_at
            ORDER BY event.valid_at, event.uuid
            """,
            group_id=self._group_id,
            routing_="r",
        )
        start = request.decision_at - timedelta(hours=request.event_window_hours)
        result: list[EventSnapshot] = []
        for record in records:
            try:
                content = json.loads(record["content"])
            except (TypeError, json.JSONDecodeError):
                continue
            occurred_at = _native_datetime(content.get("occurred_at") or record["valid_at"])
            if occurred_at is None or not (start <= occurred_at <= request.decision_at):
                continue
            modality = str(content.get("modality") or "FACT").upper()
            if modality not in {"FACT", "PLAN", "SPEC"}:
                modality = "FACT"
            semantic = content.get("semantic") or {}
            result.append(
                EventSnapshot(
                    episode_uuid=record["episode_uuid"],
                    event_id=record["event_id"],
                    title=content.get("title") or record["name"],
                    summary=content.get("summary") or "",
                    modality=modality,
                    occurred_at=occurred_at,
                    effective_at=_native_datetime(semantic.get("effective_at")),
                )
            )
        return result

    async def _load_mentions(self, episode_ids: list[str]) -> list[dict[str, Any]]:
        records, _, _ = await self._graphiti.driver.execute_query(
            """
            MATCH (event:Episodic)-[:MENTIONS]->(entity:Entity)
            WHERE event.uuid IN $episode_ids
            RETURN event.uuid AS episode_uuid,
                   entity.uuid AS uuid,
                   entity.data_object_id AS business_id,
                   entity.name AS name,
                   labels(entity) AS labels
            """,
            episode_ids=episode_ids,
            routing_="r",
        )
        return [dict(record) for record in records]

    async def _load_facts(
        self,
        request: InvestmentAnalysisRequest,
        event_ids: list[str],
        episode_ids: list[str],
        episode_to_event_id: dict[str, str],
    ) -> list[FactSnapshot]:
        records, _, _ = await self._graphiti.driver.execute_query(
            """
            MATCH (source:Entity)-[fact:RELATES_TO]->(target:Entity)
            WHERE fact.group_id = $group_id
              AND (
                any(episode IN coalesce(fact.episodes, []) WHERE episode IN $episode_ids)
                OR any(event_id IN coalesce(fact.source_event_ids, []) WHERE event_id IN $event_ids)
              )
            RETURN source.uuid AS source_uuid,
                   source.name AS source_name,
                   source.data_object_id AS source_business_id,
                   labels(source) AS source_labels,
                   target.uuid AS target_uuid,
                   target.name AS target_name,
                   target.data_object_id AS target_business_id,
                   labels(target) AS target_labels,
                   fact.uuid AS uuid,
                   fact.name AS name,
                   fact.fact AS text,
                   fact.episodes AS source_episode_ids,
                   fact.source_event_ids AS source_event_ids,
                   fact.variable_id AS variable_id,
                   source.variable_role AS variable_role,
                   source.variable_group AS variable_group,
                   source.definition AS variable_definition,
                   source.measurement_basis AS variable_measurement_basis,
                   fact.direction AS direction,
                   fact.magnitude AS magnitude,
                   fact.horizon_tags AS horizon_tags,
                   fact.valid_at AS valid_at,
                   fact.invalid_at AS invalid_at,
                   fact.expected_end_latest AS expected_end_latest,
                   fact.assertion_modality AS assertion_modality,
                   fact.mechanism AS mechanism
            ORDER BY fact.valid_at, fact.uuid
            """,
            group_id=self._group_id,
            event_ids=event_ids,
            episode_ids=episode_ids,
            routing_="r",
        )
        latest_considered = request.decision_at + timedelta(days=request.forward_horizon_days)
        result: list[FactSnapshot] = []
        for record in records:
            valid_at = _native_datetime(record["valid_at"])
            invalid_at = _native_datetime(record["invalid_at"])
            if invalid_at is not None and invalid_at <= request.decision_at:
                continue
            if valid_at is not None and valid_at > latest_considered:
                continue
            is_signal = record["name"] == "SIGNAL_ON"
            direction = None
            if record["direction"]:
                try:
                    direction = Direction(record["direction"])
                except ValueError:
                    direction = Direction.UNKNOWN
            source_event_ids = list(
                dict.fromkeys(
                    (record["source_event_ids"] or [])
                    + [
                        episode_to_event_id[episode_id]
                        for episode_id in (record["source_episode_ids"] or [])
                        if episode_id in episode_to_event_id
                    ]
                )
            )
            result.append(
                FactSnapshot(
                    uuid=record["uuid"],
                    kind="SIGNAL" if is_signal else "ORDINARY",
                    name=record["name"],
                    fact=record["text"],
                    source_uuid=record["source_uuid"],
                    source_name=record["source_name"],
                    source_business_id=record["source_business_id"],
                    source_labels=record["source_labels"],
                    target_uuid=record["target_uuid"],
                    target_name=record["target_name"],
                    target_business_id=record["target_business_id"],
                    target_labels=record["target_labels"],
                    source_event_ids=source_event_ids,
                    variable_id=record["variable_id"],
                    variable_role=record["variable_role"],
                    variable_group=record["variable_group"],
                    variable_definition=record["variable_definition"],
                    variable_measurement_basis=record["variable_measurement_basis"],
                    direction=direction,
                    magnitude=record["magnitude"],
                    horizons=_horizons(record["horizon_tags"]),
                    valid_at=valid_at,
                    invalid_at=invalid_at,
                    expected_end_at=_native_datetime(record["expected_end_latest"]),
                    assertion_modality=record["assertion_modality"],
                    mechanism=record["mechanism"],
                )
            )
        return result

    async def _load_chains(
        self,
        request: InvestmentAnalysisRequest,
        anchor_node_ids: set[str],
    ) -> list[IndustryChainSnapshot]:
        if not anchor_node_ids:
            raise ValueError("Event facts do not resolve to any canonical ChainNode anchor")
        candidate_records, _, _ = await self._graphiti.driver.execute_query(
            """
            MATCH (node:ChainNode)-[membership:RELATES_TO]->(chain:IndustryChain)
            WHERE membership.name = 'ChainNodeBelongsToIndustryChain'
              AND node.data_object_id IN $anchor_node_ids
            RETURN chain.uuid AS uuid,
                   chain.data_object_id AS business_id,
                   chain.name AS name,
                   collect(DISTINCT node.data_object_id) AS matched_node_ids
            """,
            anchor_node_ids=sorted(anchor_node_ids),
            routing_="r",
        )
        candidates = sorted(
            (record for record in candidate_records if len(record["matched_node_ids"]) >= request.min_anchor_matches),
            key=lambda item: (-len(item["matched_node_ids"]), item["name"], item["business_id"]),
        )[: request.max_chains]
        if not candidates:
            raise ValueError("no IndustryChain meets min_anchor_matches")
        chain_ids = [item["business_id"] for item in candidates]
        node_records, _, _ = await self._graphiti.driver.execute_query(
            """
            MATCH (node:ChainNode)-[membership:RELATES_TO]->(chain:IndustryChain)
            WHERE membership.name = 'ChainNodeBelongsToIndustryChain'
              AND chain.data_object_id IN $chain_ids
            RETURN chain.data_object_id AS chain_id,
                   node.uuid AS uuid,
                   node.data_object_id AS business_id,
                   node.name AS name,
                   membership.contextual_stage AS stage,
                   membership.position AS position
            ORDER BY chain_id, position, name
            """,
            chain_ids=chain_ids,
            routing_="r",
        )
        edge_records, _, _ = await self._graphiti.driver.execute_query(
            """
            MATCH (source:ChainNode)-[edge:RELATES_TO]->(target:ChainNode)
            WHERE edge.name IN $topology_names
              AND edge.industry_chain_id IN $chain_ids
            RETURN edge.industry_chain_id AS chain_id,
                   edge.uuid AS uuid,
                   edge.data_object_id AS business_id,
                   edge.name AS name,
                   edge.fact AS fact,
                   source.data_object_id AS source_node_id,
                   source.name AS source_name,
                   target.data_object_id AS target_node_id,
                   target.name AS target_name
            ORDER BY chain_id, name, source_name, target_name
            """,
            topology_names=list(TOPOLOGY_NAMES),
            chain_ids=chain_ids,
            routing_="r",
        )
        nodes_by_chain: dict[str, list[ChainNodeSnapshot]] = {item: [] for item in chain_ids}
        for record in node_records:
            nodes_by_chain[record["chain_id"]].append(
                ChainNodeSnapshot(
                    uuid=record["uuid"],
                    business_id=record["business_id"],
                    name=record["name"],
                    stage=record["stage"],
                    position=record["position"],
                )
            )
        edges_by_chain: dict[str, list[TopologyEdgeSnapshot]] = {item: [] for item in chain_ids}
        for record in edge_records:
            edges_by_chain[record["chain_id"]].append(
                TopologyEdgeSnapshot(
                    uuid=record["uuid"],
                    business_id=record["business_id"] or record["uuid"],
                    name=record["name"],
                    source_node_id=record["source_node_id"],
                    source_name=record["source_name"],
                    target_node_id=record["target_node_id"],
                    target_name=record["target_name"],
                    fact=record["fact"],
                )
            )
        return [
            IndustryChainSnapshot(
                uuid=item["uuid"],
                business_id=item["business_id"],
                name=item["name"],
                anchor_match_count=len(item["matched_node_ids"]),
                matched_node_ids=item["matched_node_ids"],
                nodes=nodes_by_chain[item["business_id"]],
                edges=edges_by_chain[item["business_id"]],
            )
            for item in candidates
        ]

    @staticmethod
    def _validation_issues(events: list[EventSnapshot], facts: list[FactSnapshot]) -> list[str]:
        signal_counts = Counter(
            event_id for fact in facts if fact.kind == "SIGNAL" for event_id in fact.source_event_ids
        )
        return [f"EVENT_WITHOUT_SIGNAL_FACT:{event.event_id}" for event in events if signal_counts[event.event_id] == 0]


class GraphitiLLMInvestmentReasoner:
    """Use the configured Graphiti LLM client behind the fixed Pipeline stages."""

    name = "deepseek-graphiti-llm-client"

    def __init__(self, graphiti: Graphiti) -> None:
        self._llm = graphiti.llm_client
        self._llm_slots = asyncio.Semaphore(4)
        self.execution_issues: list[str] = []

    async def propagate(
        self,
        context: InvestmentAnalysisContext,
        accepted: list[AcceptedTransmission],
        *,
        round_number: int,
    ) -> TransmissionBatch:
        calls = []
        for chain in context.chains:
            chain_accepted = [item for item in accepted if item.chain_id == chain.business_id]
            node_ids = {item.business_id for item in chain.nodes}
            anchor_ids = node_ids | {chain.business_id}
            relevant_facts = [
                fact
                for fact in context.facts
                if fact.source_business_id in anchor_ids or fact.target_business_id in anchor_ids
            ]
            if round_number == 1 and not relevant_facts:
                continue
            if round_number > 1 and not chain_accepted:
                continue
            payload = {
                "question": context.request.question,
                "decision_at": context.request.decision_at.isoformat(),
                "events": [
                    {
                        "event_id": item.event_id,
                        "title": item.title,
                        "summary": item.summary,
                        "modality": item.modality,
                    }
                    for item in context.events
                    if item.event_id in {event_id for fact in relevant_facts for event_id in fact.source_event_ids}
                ],
                "chain": self._chain_prompt_payload(chain),
                "facts_on_chain_nodes": [self._fact_prompt_payload(item) for item in relevant_facts],
                "accepted_transmissions": [item.model_dump(mode="json") for item in chain_accepted],
            }
            prompt = f"""
你是产业链事件传导分析执行器。现在执行第 {round_number} 轮，只分析当前一条产业链。
规则：
1. 只能使用 JSON 中已有的 chain_id、topology_edge_id、node_id、fact_id 和 transmission_id，禁止发明实体或边。
2. 第1轮必须有 source_fact_ids；后续轮必须有 parent_transmission_ids。
3. 方向依据边的经济含义而不是箭头外观；不足则不输出。
4. 本阶段只产生变量传导，不做投资结论，不分析公司。
5. 不重复已接受的目标节点+变量+周期+方向。

冻结上下文：
{json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}
"""
            calls.append(
                self._safe_call(
                    prompt,
                    TransmissionBatch,
                    f"investment.propagate.{round_number}.{chain.business_id}",
                    max_tokens=16384,
                    fallback=TransmissionBatch(stopped_reason="LLM_CHAIN_PROPAGATION_FAILED"),
                )
            )
        batches: list[TransmissionBatch] = list(await asyncio.gather(*calls)) if calls else []
        return TransmissionBatch(
            proposals=[proposal for batch in batches for proposal in batch.proposals],
            stopped_reason="; ".join(item.stopped_reason for item in batches if item.stopped_reason) or None,
        )

    async def aggregate(
        self,
        context: InvestmentAnalysisContext,
        transmissions: list[AcceptedTransmission],
    ) -> AnalysisDraft:
        chains: list[ChainTrendView] = []
        calls = []
        for chain in context.chains:
            chain_transmissions = [item for item in transmissions if item.chain_id == chain.business_id]
            node_ids = {item.business_id for item in chain.nodes}
            anchor_ids = node_ids | {chain.business_id}
            chain_facts = [
                fact
                for fact in context.facts
                if fact.source_business_id in anchor_ids or fact.target_business_id in anchor_ids
            ]
            payload = {
                "question": context.request.question,
                "decision_at": context.request.decision_at.isoformat(),
                "events": [
                    {"event_id": item.event_id, "title": item.title, "summary": item.summary}
                    for item in context.events
                    if item.event_id in {event_id for fact in chain_facts for event_id in fact.source_event_ids}
                ],
                "chain": self._chain_prompt_payload(chain),
                "facts_on_chain_nodes": [self._fact_prompt_payload(item) for item in chain_facts],
                "accepted_transmissions": [item.model_dump(mode="json") for item in chain_transmissions],
            }
            prompt = f"""
你是中国股票投研的产业链节点分析执行器。请分析当前一条产业链的每个真实节点。
规则：
1. chain_id、node_id和node_name必须原样返回，不可发明 ID；覆盖所有已给节点。
2. 趋势只能是 WARMING/COOLING/DIVERGENT/NO_MATERIAL_CHANGE/INSUFFICIENT_EVIDENCE。
3. “投资价值升温”必须同时考虑需求、供给/产能、价格/利润、技术/替代、交易拥挤中的至少两类证据；不足时降级为 NO_CLEAR_EDGE 或 INSUFFICIENT_EVIDENCE。
4. 短期=数日至6周，中期=1至4个季度，长期=1年以上。未被当前事件覆盖的长周期不可武断外推。
5. 区分事实、传导和假设；不分析公司或个股。
6. Event 只是 Fact 的来源上下文，节点方向必须由已给 Fact 或已接受 Transmission 支撑。
7. Signal Fact 的 UP/DOWN 是变量自身方向，不是投资方向。必须结合 variable_definition 解释；如有效产能 DOWN 或出口管制暴露度 UP 通常是降温/风险。
8. 直接 Signal Fact 与传导假设冲突时优先保留直接事实，必要时标记 DIVERGENT，不得将其改写为单向升温。

冻结上下文：
{json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}
"""
            calls.append(
                (
                    chain,
                    chain_facts,
                    chain_transmissions,
                    self._safe_call(
                        prompt,
                        NodeAnalysisBatch,
                        f"investment.aggregate.{chain.business_id}",
                        max_tokens=16384,
                        fallback=NodeAnalysisBatch(),
                    ),
                )
            )
        resolved_batches = await asyncio.gather(*(item[3] for item in calls))
        for (chain, chain_facts, chain_transmissions, _), batch in zip(calls, resolved_batches, strict=True):
            canonical_by_id = {item.business_id: item for item in chain.nodes}
            canonical_by_name = {item.name: item for item in chain.nodes}
            node_views: list[NodeTrendView] = []
            seen_node_ids: set[str] = set()
            for view in batch.nodes:
                node = canonical_by_id.get(view.node_id) or canonical_by_name.get(view.node_name)
                if node is None or node.business_id in seen_node_ids:
                    continue
                seen_node_ids.add(node.business_id)
                node_facts = [
                    fact
                    for fact in chain_facts
                    if fact.source_business_id == node.business_id or fact.target_business_id == node.business_id
                ]
                node_transmissions = [
                    item
                    for item in chain_transmissions
                    if item.source_node_id == node.business_id or item.target_node_id == node.business_id
                ]
                identity_changed = (
                    view.chain_id != chain.business_id
                    or view.node_id != node.business_id
                    or view.node_name != node.name
                )
                valid_fact_ids = {item.uuid for item in node_facts}
                valid_transmission_ids = {item.transmission_id for item in node_transmissions}
                filtered_fact_ids = [item for item in view.supporting_fact_ids if item in valid_fact_ids]
                filtered_transmission_ids = [
                    item for item in view.supporting_transmission_ids if item in valid_transmission_ids
                ]
                references_changed = (
                    filtered_fact_ids != view.supporting_fact_ids
                    or filtered_transmission_ids != view.supporting_transmission_ids
                )
                risks = list(view.risks)
                if identity_changed:
                    risks.append("CANONICAL_ID_NORMALIZED")
                if references_changed:
                    risks.append("UNKNOWN_SUPPORT_REFERENCE_REMOVED")
                view = view.model_copy(
                    update={
                        "chain_id": chain.business_id,
                        "node_id": node.business_id,
                        "node_name": node.name,
                        "supporting_fact_ids": filtered_fact_ids,
                        "supporting_transmission_ids": filtered_transmission_ids,
                        "risks": list(dict.fromkeys(risks))[:10],
                    }
                )
                node_views.append(view)
            for node in chain.nodes:
                if node.business_id in seen_node_ids:
                    continue
                node_views.append(
                    NodeTrendView(
                        chain_id=chain.business_id,
                        node_id=node.business_id,
                        node_name=node.name,
                        short=Trend.INSUFFICIENT_EVIDENCE,
                        medium=Trend.INSUFFICIENT_EVIDENCE,
                        long=Trend.INSUFFICIENT_EVIDENCE,
                        confidence=Confidence.LOW,
                        investment_assessment=(InvestmentAssessment.INSUFFICIENT_EVIDENCE),
                        rationale="LLM 未返回该真实产业链节点的结构化分析。",
                        risks=["LLM_NODE_ANALYSIS_MISSING"],
                    )
                )
            chains.append(self._reduce_chain(chain, node_views))
        conclusion = "；".join(item.summary for item in chains)
        limitations = list(
            dict.fromkeys(
                context.validation_issues
                + [
                    "缺少价格、利润率、库存和交易拥挤等投资门禁数据",
                    "长期结论不得超出当前Event和Signal Fact的可验证影响周期",
                ]
            )
        )
        return AnalysisDraft(
            one_sentence_conclusion=conclusion[:2000],
            chains=chains,
            limitations=limitations[:20],
        )

    async def review(
        self,
        context: InvestmentAnalysisContext,
        transmissions: list[AcceptedTransmission],
        draft: AnalysisDraft,
    ) -> ReviewResult:
        prompt = f"""
你是投研结论审核器。检查结论是否：覆盖所有真实节点；没有发明图谱 ID；没有把传导假设写成事实；没有在证据不足时给出明确机会；没有与 Signal Fact 方向相反。
只有无实质问题时 accepted=true。issue_codes 用稳定的英文大写代码。
真实节点可以没有 Transmission；不得因为它未出现在传导摘要中就判定 ID 为伪造。

上下文指纹：{context.request.decision_at.isoformat()}
验证问题：{json.dumps(context.validation_issues, ensure_ascii=False)}
真实产业链节点：{json.dumps([{"chain_id": chain.business_id, "node_ids": [node.business_id for node in chain.nodes]} for chain in context.chains], ensure_ascii=False, separators=(",", ":"))}
传导摘要：{json.dumps([{"id": item.transmission_id, "chain_id": item.chain_id, "target_node_id": item.target_node_id, "variable": item.target_variable, "direction": item.direction, "horizon": item.horizon, "confidence": item.confidence} for item in transmissions], ensure_ascii=False, separators=(",", ":"))}
草案摘要：{json.dumps({"conclusion": draft.one_sentence_conclusion, "chains": [{"chain_id": chain.chain_id, "short": chain.short, "medium": chain.medium, "long": chain.long, "nodes": [{"node_id": node.node_id, "short": node.short, "medium": node.medium, "long": node.long, "assessment": node.investment_assessment} for node in chain.nodes]} for chain in draft.chains], "limitations": draft.limitations}, ensure_ascii=False, separators=(",", ":"))}
"""
        return await self._safe_call(
            prompt,
            ReviewResult,
            "investment.review",
            max_tokens=16384,
            fallback=ReviewResult(
                accepted=False,
                confidence=Confidence.LOW,
                issue_codes=["LLM_REVIEW_FAILED"],
                review_summary="LLM 审核阶段未能返回有效结构化结果。",
            ),
        )

    @staticmethod
    def _chain_prompt_payload(chain: IndustryChainSnapshot) -> dict[str, Any]:
        return {
            "chain_id": chain.business_id,
            "chain_name": chain.name,
            "matched_node_ids": chain.matched_node_ids,
            "nodes": [
                {
                    "node_id": item.business_id,
                    "node_name": item.name,
                    "stage": item.stage,
                    "position": item.position,
                }
                for item in chain.nodes
            ],
            "topology_edges": [
                {
                    "topology_edge_id": item.business_id,
                    "relation": item.name,
                    "source_node_id": item.source_node_id,
                    "source_name": item.source_name,
                    "target_node_id": item.target_node_id,
                    "target_name": item.target_name,
                    "fact": item.fact,
                }
                for item in chain.edges
            ],
        }

    @staticmethod
    def _fact_prompt_payload(fact: FactSnapshot) -> dict[str, Any]:
        snapshot = fact.model_dump(mode="json")
        return {
            key: value
            for key, value in {
                "fact_id": snapshot["uuid"],
                "kind": snapshot["kind"],
                "relation": snapshot["name"],
                "fact": snapshot["fact"],
                "source_name": snapshot["source_name"],
                "source_business_id": snapshot["source_business_id"],
                "target_name": snapshot["target_name"],
                "target_business_id": snapshot["target_business_id"],
                "source_event_ids": snapshot["source_event_ids"],
                "variable_id": snapshot["variable_id"],
                "variable_role": snapshot["variable_role"],
                "variable_group": snapshot["variable_group"],
                "variable_definition": snapshot["variable_definition"],
                "variable_measurement_basis": snapshot["variable_measurement_basis"],
                "direction": snapshot["direction"],
                "magnitude": snapshot["magnitude"],
                "horizons": snapshot["horizons"],
                "valid_at": snapshot["valid_at"],
                "invalid_at": snapshot["invalid_at"],
                "expected_end_at": snapshot["expected_end_at"],
                "assertion_modality": snapshot["assertion_modality"],
                "mechanism": snapshot["mechanism"],
            }.items()
            if value is not None and value != []
        }

    async def _safe_call(
        self,
        prompt: str,
        model,
        prompt_name: str,
        *,
        max_tokens: int,
        fallback,
    ):
        logger.info("investment_llm_stage_started prompt_name=%s", prompt_name)
        try:
            async with self._llm_slots:
                result = await self._call(
                    prompt,
                    model,
                    prompt_name,
                    max_tokens=max_tokens,
                )
                logger.info("investment_llm_stage_completed prompt_name=%s", prompt_name)
                return result
        except Exception as exc:
            self.execution_issues.append(f"{prompt_name}:{type(exc).__name__}")
            logger.warning(
                "investment_llm_stage_degraded prompt_name=%s error_type=%s",
                prompt_name,
                type(exc).__name__,
            )
            return fallback

    async def _call(self, prompt: str, model, prompt_name: str, *, max_tokens: int):
        async with asyncio.timeout(360):
            correction = ""
            for attempt in range(1, 4):
                response = await self._llm.generate_response(
                    [
                        Message(
                            role="system",
                            content=("你必须严格依据给定数据输出一个 JSON object，不得补造数据，不得输出 Markdown。"),
                        ),
                        Message(
                            role="user",
                            content=(prompt + "\n\n精简JSON合同：\n" + self._compact_contract(model) + correction),
                        ),
                    ],
                    response_model=None,
                    max_tokens=max_tokens,
                    group_id=GROUP_ID,
                    prompt_name=f"{prompt_name}.compact.{attempt}",
                )
                try:
                    return model.model_validate(response)
                except ValidationError as exc:
                    fields = sorted({".".join(str(part) for part in item["loc"]) for item in exc.errors()})
                    correction = (
                        f"\n上次输出未通过本地合同校验，请重新输出完整 JSON。错误字段：{','.join(fields[:20])}。"
                    )
            raise ValueError(f"LLM response violates {model.__name__} after 3 attempts")

    @staticmethod
    def _compact_contract(model) -> str:
        if model is TransmissionBatch:
            return """
{"proposals":[{"chain_id":"已有ID","topology_edge_id":"已有ID","source_node_id":"已有ID","target_node_id":"已有ID","flow":"ALONG_EDGE|AGAINST_EDGE","target_variable":"变量名","direction":"UP|DOWN|MIXED|STABLE|UNKNOWN","horizon":"SHORT|MEDIUM|LONG","confidence":"LOW|MEDIUM|HIGH","mechanism":"理由","source_fact_ids":["已有ID"],"parent_transmission_ids":["已有ID"],"assumptions":["假设"]}],"stopped_reason":null}
第1轮 source_fact_ids 非空且 parent_transmission_ids=[]；后续轮反之。无合法传导时 proposals=[]。
""".strip()
        if model is AnalysisDraft:
            return """
{"one_sentence_conclusion":"一句话","chains":[{"chain_id":"已有ID","chain_name":"已有名称","short":"WARMING|COOLING|DIVERGENT|NO_MATERIAL_CHANGE|INSUFFICIENT_EVIDENCE","medium":"同上","long":"同上","confidence":"LOW|MEDIUM|HIGH","summary":"总结","nodes":[{"chain_id":"已有ID","node_id":"已有ID","node_name":"已有名称","short":"趋势枚举","medium":"趋势枚举","long":"趋势枚举","confidence":"LOW|MEDIUM|HIGH","investment_assessment":"OPPORTUNITY_CANDIDATE|RISK_POINT|MIXED|NO_CLEAR_EDGE|INSUFFICIENT_EVIDENCE","rationale":"理由","supporting_fact_ids":["已有ID"],"supporting_transmission_ids":["已有ID"],"risks":["风险"]}]}],"limitations":["局限"]}
必须只输出当前产业链，并覆盖其所有节点。
""".strip()
        if model is NodeTrendView:
            return """
{"chain_id":"原样已有ID","node_id":"原样已有ID","node_name":"原样已有名称","short":"WARMING|COOLING|DIVERGENT|NO_MATERIAL_CHANGE|INSUFFICIENT_EVIDENCE","medium":"同上","long":"同上","confidence":"LOW|MEDIUM|HIGH","investment_assessment":"OPPORTUNITY_CANDIDATE|RISK_POINT|MIXED|NO_CLEAR_EDGE|INSUFFICIENT_EVIDENCE","rationale":"理由","supporting_fact_ids":["已有ID"],"supporting_transmission_ids":["已有ID"],"risks":["风险"]}
""".strip()
        if model is NodeAnalysisBatch:
            return """
{"nodes":[{"chain_id":"原样已有ID","node_id":"原样已有ID","node_name":"原样已有名称","short":"WARMING|COOLING|DIVERGENT|NO_MATERIAL_CHANGE|INSUFFICIENT_EVIDENCE","medium":"同上","long":"同上","confidence":"LOW|MEDIUM|HIGH","investment_assessment":"OPPORTUNITY_CANDIDATE|RISK_POINT|MIXED|NO_CLEAR_EDGE|INSUFFICIENT_EVIDENCE","rationale":"理由","supporting_fact_ids":["已有ID"],"supporting_transmission_ids":["已有ID"],"risks":["风险"]}]}
必须覆盖当前产业链的所有已给节点。
""".strip()
        if model is ReviewResult:
            return """
{"accepted":true,"confidence":"LOW|MEDIUM|HIGH","issue_codes":["英文大写代码"],"review_summary":"审核结论"}
""".strip()
        raise TypeError(f"no compact contract for {model.__name__}")

    @classmethod
    def _reduce_chain(
        cls,
        chain: IndustryChainSnapshot,
        nodes: list[NodeTrendView],
    ) -> ChainTrendView:
        short = cls._reduce_trend([item.short for item in nodes])
        medium = cls._reduce_trend([item.medium for item in nodes])
        long = cls._reduce_trend([item.long for item in nodes])
        warming = [item.node_name for item in nodes if Trend.WARMING in {item.short, item.medium}]
        cooling = [item.node_name for item in nodes if Trend.COOLING in {item.short, item.medium}]
        divergent = [item.node_name for item in nodes if Trend.DIVERGENT in {item.short, item.medium}]
        parts = []
        if warming:
            parts.append(f"{'、'.join(warming)}升温")
        if cooling:
            parts.append(f"{'、'.join(cooling)}降温")
        if divergent:
            parts.append(f"{'、'.join(divergent)}分化")
        if not parts:
            parts.append("当前证据不足以形成明确方向")
        confidence = Confidence.LOW if all(item.confidence == Confidence.LOW for item in nodes) else Confidence.MEDIUM
        return ChainTrendView(
            chain_id=chain.business_id,
            chain_name=chain.name,
            short=short,
            medium=medium,
            long=long,
            confidence=confidence,
            summary=f"{chain.name}：{'；'.join(parts)}。",
            nodes=nodes,
        )

    @staticmethod
    def _reduce_trend(values: list[Trend]) -> Trend:
        material = {item for item in values if item != Trend.INSUFFICIENT_EVIDENCE}
        if Trend.DIVERGENT in material or {
            Trend.WARMING,
            Trend.COOLING,
        }.issubset(material):
            return Trend.DIVERGENT
        if Trend.WARMING in material:
            return Trend.WARMING
        if Trend.COOLING in material:
            return Trend.COOLING
        if Trend.NO_MATERIAL_CHANGE in material:
            return Trend.NO_MATERIAL_CHANGE
        return Trend.INSUFFICIENT_EVIDENCE

    @staticmethod
    def _compact_context(context: InvestmentAnalysisContext) -> dict[str, Any]:
        return {
            "question": context.request.question,
            "decision_at": context.request.decision_at.isoformat(),
            "events": [item.model_dump(mode="json") for item in context.events],
            "facts": [item.model_dump(mode="json") for item in context.facts],
            "chains": [item.model_dump(mode="json") for item in context.chains],
            "validation_issues": context.validation_issues,
        }


class RecordedInvestmentReasoner:
    """Replay structured Codex stage decisions through the identical Pipeline."""

    name = "codex-recorded-reasoner"

    def __init__(self, payload: RecordedReasoningPayload) -> None:
        self._payload = payload
        self.name = payload.executor_name
        self.execution_issues = payload.execution_issues

    async def propagate(
        self,
        context: InvestmentAnalysisContext,
        accepted: list[AcceptedTransmission],
        *,
        round_number: int,
    ) -> TransmissionBatch:
        del context, accepted
        return self._payload.rounds.get(round_number, TransmissionBatch())

    async def aggregate(
        self,
        context: InvestmentAnalysisContext,
        transmissions: list[AcceptedTransmission],
    ) -> AnalysisDraft:
        del context, transmissions
        return self._payload.draft

    async def review(
        self,
        context: InvestmentAnalysisContext,
        transmissions: list[AcceptedTransmission],
        draft: AnalysisDraft,
    ) -> ReviewResult:
        del context, transmissions, draft
        return self._payload.review
