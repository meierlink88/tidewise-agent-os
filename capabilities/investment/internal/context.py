"""Build the governed investment context from raw Graphiti records."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from capabilities.investment.internal.models import (
    ChainNodeSnapshot,
    Confidence,
    Direction,
    EventSnapshot,
    FactSnapshot,
    Horizon,
    IndustryChainSnapshot,
    InvestmentAnalysisContext,
    InvestmentAnalysisRequest,
    TopologyEdgeSnapshot,
)
from sematica.graphiti.investment import GraphitiInvestmentReader

MAX_EVENTS = 500
MAX_FACTS = 2000
MAX_CHAIN_CANDIDATES = 100
MAX_NODES_PER_CHAIN = 200
MAX_EDGES_PER_CHAIN = 500
EVENTS_PER_NATIVE_QUERY = 20
MAX_NATIVE_EVENT_FRAGMENT_LENGTH = 24


def _native_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _horizons(values: list[str] | None) -> list[Horizon]:
    result: list[Horizon] = []
    for value in values or []:
        try:
            result.append(Horizon(value))
        except ValueError:
            continue
    return result


def _confidence(*values: str | None) -> Confidence | None:
    rank = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
    normalized: list[Confidence] = []
    for value in values:
        try:
            normalized.append(Confidence(str(value).upper()))
        except ValueError:
            continue
    return min(normalized, key=rank.__getitem__) if normalized else None


class InvestmentContextBuilder:
    """Own investment selection and validation policy above Graphiti reads."""

    def __init__(self, reader: GraphitiInvestmentReader) -> None:
        self._reader = reader

    async def build(self, request: InvestmentAnalysisRequest) -> InvestmentAnalysisContext:
        events = await self._load_events(request)
        if not events:
            raise ValueError("no Event Episodes fall inside the requested event window")
        event_ids = [item.event_id for item in events]
        episode_ids = [item.episode_uuid for item in events]
        episode_to_event = {item.episode_uuid: item.event_id for item in events}
        mention_records, fact_records = await asyncio.gather(
            self._reader.load_mentions(episode_ids),
            self._reader.load_facts(
                event_ids,
                episode_ids,
                request.decision_at,
                request.decision_at + timedelta(days=request.forward_horizon_days),
                limit=MAX_FACTS + 1,
            ),
        )
        if len(fact_records) > MAX_FACTS:
            raise ValueError(f"investment fact scope exceeds deterministic limit {MAX_FACTS}")
        facts = self._parse_facts(request, fact_records, episode_to_event)
        queries = self.build_native_queries(request.question, events)
        native_ids = await self._reader.search_fact_ids(queries, {item.uuid for item in facts})
        selected_facts = self.select_retrieved_facts(facts, native_ids)
        anchor_node_ids = {
            item["business_id"] for item in mention_records if item["business_id"] and "ChainNode" in item["labels"]
        }
        direct_chain_ids: set[str] = set()
        for fact in selected_facts:
            if fact.source_business_id and "ChainNode" in fact.source_labels:
                anchor_node_ids.add(fact.source_business_id)
            if fact.target_business_id and "ChainNode" in fact.target_labels:
                anchor_node_ids.add(fact.target_business_id)
            if fact.source_business_id and "IndustryChain" in fact.source_labels:
                direct_chain_ids.add(fact.source_business_id)
            if fact.target_business_id and "IndustryChain" in fact.target_labels:
                direct_chain_ids.add(fact.target_business_id)
        chains = await self._load_chains(request, anchor_node_ids, direct_chain_ids, selected_facts)
        signal_counts = Counter(
            event_id for fact in selected_facts if fact.kind == "SIGNAL" for event_id in fact.source_event_ids
        )
        issues = [
            f"EVENT_WITHOUT_SIGNAL_FACT:{event.event_id}" for event in events if signal_counts[event.event_id] == 0
        ]
        if not any(item.is_active_signal(request.decision_at) for item in selected_facts):
            issues.append("NO_ELIGIBLE_SIGNAL_ROOT")
        return InvestmentAnalysisContext(
            request=request,
            events=events,
            facts=selected_facts,
            chains=chains,
            native_retrieved_fact_ids=native_ids,
            validation_issues=issues,
        )

    @staticmethod
    def select_retrieved_facts(facts: list[FactSnapshot], native_ids: list[str]) -> list[FactSnapshot]:
        """Keep complete Signal roots but gate ordinary context through Graphiti search."""

        native_set = set(native_ids)
        return [item for item in facts if item.kind == "SIGNAL" or item.uuid in native_set]

    @staticmethod
    def build_native_queries(question: str, events: list[EventSnapshot]) -> list[str]:
        """Represent every Event within a deterministic Graphiti search budget."""

        queries = [question[:2000]]
        fragments = [f"{event.title[:16]} {event.summary[:8]}"[:MAX_NATIVE_EVENT_FRAGMENT_LENGTH] for event in events]
        for offset in range(0, len(fragments), EVENTS_PER_NATIVE_QUERY):
            queries.append("\n".join(fragments[offset : offset + EVENTS_PER_NATIVE_QUERY]))
        return queries

    async def _load_events(self, request: InvestmentAnalysisRequest) -> list[EventSnapshot]:
        start = request.decision_at - timedelta(hours=request.event_window_hours)
        records = await self._reader.load_events(start, request.decision_at, limit=MAX_EVENTS + 1)
        result: list[EventSnapshot] = []
        for record in records:
            try:
                content = json.loads(record["content"])
            except (TypeError, json.JSONDecodeError):
                continue
            occurred_at = _native_datetime(content.get("occurred_at") or record["valid_at"])
            if occurred_at is None or not start <= occurred_at <= request.decision_at:
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
        if len(result) > MAX_EVENTS:
            raise ValueError(f"investment event scope exceeds deterministic limit {MAX_EVENTS}")
        return result

    @staticmethod
    def _parse_facts(
        request: InvestmentAnalysisRequest,
        records: list[dict[str, Any]],
        episode_to_event: dict[str, str],
    ) -> list[FactSnapshot]:
        latest_considered = request.decision_at + timedelta(days=request.forward_horizon_days)
        result: list[FactSnapshot] = []
        for record in records:
            valid_at = _native_datetime(record["valid_at"])
            invalid_at = _native_datetime(record["invalid_at"])
            if invalid_at is not None and invalid_at <= request.decision_at:
                continue
            if valid_at is not None and valid_at > latest_considered:
                continue
            try:
                direction = Direction(str(record["direction"]).upper()) if record["direction"] else None
            except ValueError:
                direction = Direction.UNKNOWN
            source_event_ids = list(
                dict.fromkeys(
                    (record["source_event_ids"] or [])
                    + [
                        episode_to_event[item]
                        for item in (record["source_episode_ids"] or [])
                        if item in episode_to_event
                    ]
                )
            )
            result.append(
                FactSnapshot(
                    uuid=record["uuid"],
                    kind="SIGNAL" if record["name"] == "SIGNAL_ON" else "ORDINARY",
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
                    confidence=_confidence(
                        record["mechanism_confidence"],
                        record["provenance_confidence"],
                        record["temporal_confidence"],
                    ),
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
        direct_chain_ids: set[str],
        facts: list[FactSnapshot],
    ) -> list[IndustryChainSnapshot]:
        if not anchor_node_ids and not direct_chain_ids:
            return []
        records = await self._reader.load_chain_candidates(
            anchor_node_ids,
            direct_chain_ids,
            limit=MAX_CHAIN_CANDIDATES + 1,
        )
        if len(records) > MAX_CHAIN_CANDIDATES:
            raise ValueError(f"investment chain candidate scope exceeds limit {MAX_CHAIN_CANDIDATES}")
        candidates = sorted(
            (
                {**record, "matched_node_ids": [item for item in record["matched_node_ids"] if item]}
                for record in records
                if record["business_id"] in direct_chain_ids
                or len([item for item in record["matched_node_ids"] if item]) >= request.min_anchor_matches
            ),
            key=lambda item: (-len(item["matched_node_ids"]), item["name"], item["business_id"]),
        )[: request.max_chains]
        if not candidates:
            return []
        chain_ids = [item["business_id"] for item in candidates]
        node_records, edge_records = await asyncio.gather(
            self._reader.load_chain_nodes(chain_ids, limit=len(chain_ids) * MAX_NODES_PER_CHAIN + 1),
            self._reader.load_topology_edges(chain_ids, limit=len(chain_ids) * MAX_EDGES_PER_CHAIN + 1),
        )
        if len(node_records) > len(chain_ids) * MAX_NODES_PER_CHAIN:
            raise ValueError("investment topology node scope exceeds deterministic limit")
        if len(edge_records) > len(chain_ids) * MAX_EDGES_PER_CHAIN:
            raise ValueError("investment topology edge scope exceeds deterministic limit")
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
        if any(len(items) > MAX_NODES_PER_CHAIN for items in nodes_by_chain.values()):
            raise ValueError(f"investment chain exceeds {MAX_NODES_PER_CHAIN} nodes")
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
                    fact=record["fact"] or "",
                )
            )
        if any(len(items) > MAX_EDGES_PER_CHAIN for items in edges_by_chain.values()):
            raise ValueError(f"investment chain exceeds {MAX_EDGES_PER_CHAIN} topology edges")
        active_signals = [item for item in facts if item.is_active_signal(request.decision_at)]
        result: list[IndustryChainSnapshot] = []
        for candidate in candidates:
            chain_id = candidate["business_id"]
            node_ids = {item.business_id for item in nodes_by_chain[chain_id]}
            roots = [
                fact
                for fact in active_signals
                if fact.target_business_id == chain_id
                or fact.source_business_id == chain_id
                or fact.target_business_id in node_ids
                or fact.source_business_id in node_ids
            ]
            root_nodes = [
                endpoint
                for fact in roots
                for endpoint in (fact.source_business_id, fact.target_business_id)
                if endpoint in node_ids
            ]
            result.append(
                IndustryChainSnapshot(
                    uuid=candidate["uuid"],
                    business_id=chain_id,
                    name=candidate["name"],
                    anchor_match_count=max(1, len(candidate["matched_node_ids"])),
                    matched_node_ids=candidate["matched_node_ids"],
                    signal_root_fact_ids=list(dict.fromkeys(fact.uuid for fact in roots)),
                    signal_root_node_ids=list(dict.fromkeys(root_nodes)),
                    nodes=nodes_by_chain[chain_id],
                    edges=edges_by_chain[chain_id],
                )
            )
        return result
