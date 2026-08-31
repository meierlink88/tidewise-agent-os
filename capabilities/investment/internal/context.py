"""Build the governed investment context from raw Graphiti records."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from capabilities.investment.internal.models import (
    AcceptedImpactClaim,
    AnalysisAnchorSnapshot,
    ChainNodeSnapshot,
    Confidence,
    Direction,
    EventSnapshot,
    FactSnapshot,
    Horizon,
    ImpactLayer,
    IndustryChainSnapshot,
    InvestmentAnalysisContext,
    InvestmentAnalysisRequest,
    LayerAnalysisContext,
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
MAX_LAYER_ANCHORS = 100
MAX_LAYER_FACTS = 1200
MAX_LAYER_QUERIES = 25
MAX_LAYER_QUERY_LENGTH = 500
LAYER_EVENTS_PER_QUERY = 20
LAYER_SIGNALS_PER_QUERY = 4
LAYER_PARENTS_PER_QUERY = 2
MAX_LAYER_SIGNAL_FRAGMENTS = MAX_LAYER_QUERIES * LAYER_SIGNALS_PER_QUERY
MAX_LAYER_PARENT_FRAGMENTS = MAX_LAYER_QUERIES * LAYER_PARENTS_PER_QUERY

LAYER_LABELS: dict[ImpactLayer, set[str]] = {
    ImpactLayer.GEOPOLITICAL: {"GeopoliticRivalry"},
    ImpactLayer.MACRO_ECONOMIC: {"MacroEconomic"},
    ImpactLayer.INDUSTRY: {"IndustryChain", "ChainNode"},
}


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
        anchors = self._parse_direct_anchors(mention_records, selected_facts, episode_to_event)
        scoped_event_ids = set(event_ids)
        signal_counts = Counter(
            event_id
            for fact in selected_facts
            if fact.is_active_signal(request.decision_at)
            for event_id in fact.source_event_ids
            if event_id in scoped_event_ids
        )
        issues = [
            f"EVENT_WITHOUT_SIGNAL_FACT:{event.event_id}" for event in events if signal_counts[event.event_id] == 0
        ]
        if not any(
            item.is_active_signal(request.decision_at) and bool(scoped_event_ids.intersection(item.source_event_ids))
            for item in selected_facts
        ):
            issues.append("NO_ELIGIBLE_SIGNAL_ROOT")
        return InvestmentAnalysisContext(
            request=request,
            events=events,
            facts=selected_facts,
            anchors=anchors,
            chains=[],
            native_retrieved_fact_ids=native_ids,
            validation_issues=issues,
        )

    async def build_layer_context(
        self,
        base: InvestmentAnalysisContext,
        layer: ImpactLayer,
        parent_claims: list[AcceptedImpactClaim],
        *,
        supplemental_queries: list[str] | None = None,
        retrieval_round: int = 1,
    ) -> LayerAnalysisContext:
        """Retrieve only the ontology and Facts needed by one reasoning layer."""

        labels = LAYER_LABELS[layer]
        queries = self._layer_queries(base, parent_claims, supplemental_queries or [])
        candidate_records = await self._reader.search_anchor_nodes(queries, labels, limit=MAX_LAYER_ANCHORS)
        direct = [item for item in base.anchors if item.entity_type in labels]
        candidates = self._parse_search_anchors(candidate_records)
        anchors_by_uuid = {item.uuid: item for item in [*direct, *candidates]}
        anchors = list(anchors_by_uuid.values())[:MAX_LAYER_ANCHORS]
        episode_to_event = {item.episode_uuid: item.event_id for item in base.events}
        related_records = await self._reader.load_anchor_facts(
            [item.uuid for item in anchors],
            base.request.decision_at,
            base.request.decision_at + timedelta(days=base.request.forward_horizon_days),
            limit=MAX_LAYER_FACTS + 1,
        )
        if len(related_records) > MAX_LAYER_FACTS:
            raise ValueError(f"{layer.value} layer fact scope exceeds deterministic limit {MAX_LAYER_FACTS}")
        # Layer expansion may load historical ontology Facts as mechanisms, but its
        # Signal roots remain strictly frozen to the Event window prepared above.
        related = [
            fact
            for fact in self._parse_facts(base.request, related_records, episode_to_event)
            if fact.kind == "ORDINARY"
        ]
        facts_by_id = {item.uuid: item for item in [*base.facts, *related]}
        facts = list(facts_by_id.values())[:MAX_LAYER_FACTS]
        anchor_uuids = {item.uuid for item in anchors}
        anchor_ids = {item.business_id for item in anchors}
        scoped_event_ids = {item.event_id for item in base.events}
        direct_signal_ids = [
            fact.uuid
            for fact in base.facts
            if fact.is_active_signal(base.request.decision_at)
            and bool(scoped_event_ids.intersection(fact.source_event_ids))
            and (
                fact.source_uuid in anchor_uuids
                or fact.target_uuid in anchor_uuids
                or fact.source_business_id in anchor_ids
                or fact.target_business_id in anchor_ids
            )
        ]
        return LayerAnalysisContext(
            layer=layer,
            decision_at=base.request.decision_at,
            question=base.request.question,
            events=base.events,
            anchors=anchors,
            facts=facts,
            parent_claims=parent_claims,
            direct_signal_fact_ids=list(dict.fromkeys(direct_signal_ids)),
            retrieval_round=retrieval_round,
        )

    async def expand_industry_context(
        self,
        base: InvestmentAnalysisContext,
        layer_context: LayerAnalysisContext,
        industry_claims: list[AcceptedImpactClaim],
    ) -> InvestmentAnalysisContext:
        """Load canonical chain membership and topology only after industry candidates exist."""

        facts_by_id = {item.uuid: item for item in [*base.facts, *layer_context.facts]}
        facts = list(facts_by_id.values())
        anchor_node_ids: set[str] = set()
        direct_chain_ids: set[str] = set()
        eligible_signals = [
            fact for fact in facts if fact.uuid in base.eligible_signal_fact_ids and fact.anchor_type == "ChainNode"
        ]
        signal_node_ids = {
            endpoint
            for fact in eligible_signals
            for endpoint in (fact.source_business_id, fact.target_business_id)
            if endpoint is not None
        }
        # IndustryChain is an aggregate view. Only Signal-backed ChainNodes and
        # accepted node claims may select chains for topology expansion.
        for anchor in base.anchors:
            if anchor.entity_type == "ChainNode" and anchor.business_id in signal_node_ids:
                anchor_node_ids.add(anchor.business_id)
        for claim in industry_claims:
            if claim.anchor_type == "ChainNode":
                anchor_node_ids.add(claim.anchor_id)
        chains = await self._load_chains(
            base.request,
            anchor_node_ids,
            direct_chain_ids,
            facts,
            eligible_signal_fact_ids=base.eligible_signal_fact_ids,
            industry_claims=industry_claims,
        )
        selected_anchor_ids = {item.anchor_id for item in industry_claims} | anchor_node_ids | direct_chain_ids
        selected_layer_anchors = [item for item in layer_context.anchors if item.business_id in selected_anchor_ids]
        anchors_by_uuid = {item.uuid: item for item in [*base.anchors, *selected_layer_anchors]}
        chain_ids = {item.business_id for item in chains}
        node_ids = {node.business_id for chain in chains for node in chain.nodes}
        cited_fact_ids = {
            fact_id
            for claim in industry_claims
            for fact_id in [*claim.source_fact_ids, *claim.mechanism_fact_ids, *claim.root_signal_fact_ids]
        }
        base_fact_ids = {item.uuid for item in base.facts}
        facts = [
            item
            for item in facts
            if item.uuid in base_fact_ids
            or item.uuid in cited_fact_ids
            or item.source_business_id in chain_ids | node_ids
            or item.target_business_id in chain_ids | node_ids
        ]
        return base.model_copy(
            update={
                "facts": facts[:MAX_FACTS],
                "anchors": list(anchors_by_uuid.values()),
                "chains": chains,
            }
        )

    @staticmethod
    def _layer_queries(
        base: InvestmentAnalysisContext,
        parent_claims: list[AcceptedImpactClaim],
        supplemental_queries: list[str],
    ) -> list[str]:
        event_fragments = [f"{event.title[:8]} {event.summary[:4]}".strip() for event in base.events]
        signal_fragments = [
            f"{fact.source_name[:12]} {fact.target_name[:12]}" for fact in base.facts if fact.kind == "SIGNAL"
        ][:MAX_LAYER_SIGNAL_FRAGMENTS]
        parent_fragments = [f"{claim.anchor_name[:16]} {claim.summary[:24]}" for claim in parent_claims][
            :MAX_LAYER_PARENT_FRAGMENTS
        ]
        supplements = [item.strip()[:120] for item in supplemental_queries if item.strip()][:4]
        batch_count = max(
            1,
            (len(event_fragments) + LAYER_EVENTS_PER_QUERY - 1) // LAYER_EVENTS_PER_QUERY,
            (len(signal_fragments) + LAYER_SIGNALS_PER_QUERY - 1) // LAYER_SIGNALS_PER_QUERY,
            (len(parent_fragments) + LAYER_PARENTS_PER_QUERY - 1) // LAYER_PARENTS_PER_QUERY,
            len(supplements),
        )
        queries: list[str] = []
        for index in range(min(batch_count, MAX_LAYER_QUERIES)):
            fragments = [base.request.question[:80]]
            if index < len(supplements):
                fragments.append(supplements[index])
            fragments.extend(signal_fragments[index * LAYER_SIGNALS_PER_QUERY : (index + 1) * LAYER_SIGNALS_PER_QUERY])
            fragments.extend(parent_fragments[index * LAYER_PARENTS_PER_QUERY : (index + 1) * LAYER_PARENTS_PER_QUERY])
            fragments.extend(event_fragments[index * LAYER_EVENTS_PER_QUERY : (index + 1) * LAYER_EVENTS_PER_QUERY])
            query = "\n".join(item for item in fragments if item).strip()[:MAX_LAYER_QUERY_LENGTH]
            if query:
                queries.append(query)
        return list(dict.fromkeys(queries))[:MAX_LAYER_QUERIES]

    @staticmethod
    def _entity_type(labels: list[str]) -> str | None:
        for entity_type in ("GeopoliticRivalry", "MacroEconomic", "IndustryChain", "ChainNode"):
            if entity_type in labels:
                return entity_type
        return None

    @classmethod
    def _parse_direct_anchors(
        cls,
        mention_records: list[dict[str, Any]],
        facts: list[FactSnapshot],
        episode_to_event: dict[str, str],
    ) -> list[AnalysisAnchorSnapshot]:
        anchors: dict[str, AnalysisAnchorSnapshot] = {}
        for record in mention_records:
            entity_type = cls._entity_type(record.get("labels") or [])
            business_id = record.get("business_id")
            if entity_type is None or not business_id:
                continue
            anchors[record["uuid"]] = AnalysisAnchorSnapshot(
                uuid=record["uuid"],
                business_id=business_id,
                name=record.get("name") or business_id,
                entity_type=entity_type,
                summary=record.get("summary") or "",
                source_event_ids=[episode_to_event[record["episode_uuid"]]]
                if record.get("episode_uuid") in episode_to_event
                else [],
            )
        for fact in facts:
            for uuid, business_id, name, labels in (
                (fact.source_uuid, fact.source_business_id, fact.source_name, fact.source_labels),
                (fact.target_uuid, fact.target_business_id, fact.target_name, fact.target_labels),
            ):
                entity_type = cls._entity_type(labels)
                if entity_type is None or not business_id:
                    continue
                existing = anchors.get(uuid)
                source_event_ids = list(
                    dict.fromkeys([*(existing.source_event_ids if existing else []), *fact.source_event_ids])
                )
                anchors[uuid] = AnalysisAnchorSnapshot(
                    uuid=uuid,
                    business_id=business_id,
                    name=name,
                    entity_type=entity_type,
                    summary=existing.summary if existing else "",
                    source_event_ids=source_event_ids,
                )
        return list(anchors.values())

    @classmethod
    def _parse_search_anchors(cls, records: list[dict[str, Any]]) -> list[AnalysisAnchorSnapshot]:
        result: list[AnalysisAnchorSnapshot] = []
        for record in records:
            entity_type = cls._entity_type(record.get("labels") or [])
            business_id = record.get("business_id")
            if entity_type is None or not business_id:
                continue
            result.append(
                AnalysisAnchorSnapshot(
                    uuid=record["uuid"],
                    business_id=business_id,
                    name=record.get("name") or business_id,
                    entity_type=entity_type,
                    summary=record.get("summary") or "",
                )
            )
        return result

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
        result: list[FactSnapshot] = []
        for record in records:
            valid_at = _native_datetime(record["valid_at"])
            invalid_at = _native_datetime(record["invalid_at"])
            if invalid_at is not None and invalid_at <= request.decision_at:
                continue
            if valid_at is not None and valid_at > request.decision_at:
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
                    event_class=record.get("event_class"),
                    anchor_type=record.get("anchor_type"),
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
        *,
        eligible_signal_fact_ids: set[str],
        industry_claims: list[AcceptedImpactClaim] | None = None,
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
        active_signals = [item for item in facts if item.uuid in eligible_signal_fact_ids]
        claims = industry_claims or []
        result: list[IndustryChainSnapshot] = []
        for candidate in candidates:
            chain_id = candidate["business_id"]
            node_ids = {item.business_id for item in nodes_by_chain[chain_id]}
            roots = [
                fact
                for fact in active_signals
                if fact.target_business_id in node_ids or fact.source_business_id in node_ids
            ]
            root_nodes = [
                endpoint
                for fact in roots
                for endpoint in (fact.source_business_id, fact.target_business_id)
                if endpoint in node_ids
            ]
            claim_roots = [
                claim
                for claim in claims
                if claim.anchor_type == "ChainNode"
                and claim.anchor_id in node_ids
                and set(claim.root_signal_fact_ids) <= eligible_signal_fact_ids
            ]
            root_nodes.extend(claim.anchor_id for claim in claim_roots if claim.anchor_id in node_ids)
            result.append(
                IndustryChainSnapshot(
                    uuid=candidate["uuid"],
                    business_id=chain_id,
                    name=candidate["name"],
                    anchor_match_count=max(1, len(candidate["matched_node_ids"])),
                    matched_node_ids=candidate["matched_node_ids"],
                    signal_root_fact_ids=list(
                        dict.fromkeys(
                            [fact.uuid for fact in roots]
                            + [fact_id for claim in claim_roots for fact_id in claim.root_signal_fact_ids]
                        )
                    ),
                    signal_root_node_ids=list(dict.fromkeys(root_nodes)),
                    nodes=nodes_by_chain[chain_id],
                    edges=edges_by_chain[chain_id],
                )
            )
        return result
