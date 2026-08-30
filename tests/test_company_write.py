"""Tests for direct, episode-free Company graph writes."""

from __future__ import annotations

import inspect
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

from graphiti_core import Graphiti

from capabilities.company import (
    CandidateSetAudit,
    CompanyInferenceDecision,
    Confidence,
    DecisionStatus,
    ProjectionRunManifest,
    ResolvedTarget,
    StageDecision,
)
from sematica.ontology import CompanyOperatesInIndustry, CompanyParticipatesInChainNode
from sematica.ontology.entities.company import COMPANY_PROJECTION_OWNER
from sematica.projection import company as company_projection
from sematica.projection.authoritative_writer import edge_uuid, node_uuid
from sematica.projection.company import (
    CanonicalGraphTarget,
    CompanyFacts,
    DataCompanyDTO,
    _write_changed_company_facts,
    build_inferred_edges,
    build_plan,
    company_subject,
    diff_company_projection,
    execute_company_projection,
    preflight_company_namespace,
    preflight_company_relation_namespace,
    verify_company_projection,
)
from sematica.projection.runtime import ProjectionError

COM = "COM00000000-0000-4000-8000-000000000001"
IND = "IND00000000-0000-4000-8000-000000000001"
CND = "CND00000000-0000-4000-8000-000000000001"
STALE_COM = "COM00000000-0000-4000-8000-000000000002"
NOW = datetime(2026, 8, 30, 8, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64


def _company() -> DataCompanyDTO:
    return DataCompanyDTO.model_validate(
        {
            "id": COM,
            "code": "TEST",
            "name": "测试半导体股份有限公司",
            "name_en": None,
            "legal_name": "测试半导体股份有限公司",
            "aliases": [],
            "registration_country_id": None,
            "operating_area": None,
            "headquarters_city": None,
            "founding_date": None,
            "ipo_date": None,
            "legal_form": None,
            "ownership_type": None,
            "strategic_positioning": None,
            "description": None,
            "status": "active",
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-02T00:00:00Z",
            "industry_links": [],
        }
    )


def _decision() -> CompanyInferenceDecision:
    industry = StageDecision(
        status=DecisionStatus.MAPPED,
        accepted_targets=[
            ResolvedTarget(
                target_id=IND,
                confidence=Confidence.HIGH,
                rationale="公司主营半导体制造",
                supporting_company_fields=["name"],
            )
        ],
        rejected_targets=[],
    )
    chain_node = StageDecision(
        status=DecisionStatus.MAPPED,
        accepted_targets=[
            ResolvedTarget(
                target_id=CND,
                confidence=Confidence.MEDIUM,
                rationale="公司业务对应晶圆制造环节",
                supporting_company_fields=["name"],
                source_industry_ids=[IND],
                industry_chain_ids=["ICH00000000-0000-4000-8000-000000000001"],
            )
        ],
        rejected_targets=[],
    )
    return CompanyInferenceDecision(
        decision_id=SHA_A,
        company_id=COM,
        input_index=0,
        status=DecisionStatus.MAPPED,
        industry=industry,
        chain_node=chain_node,
        candidates=CandidateSetAudit(
            root_industry_candidate_ids=[IND],
            selected_root_industry_ids=[IND],
            industry_candidate_ids=[IND],
            chain_node_candidate_ids=[CND],
        ),
        source_company_fingerprint=company_subject(_company(), 0).fingerprint(),
        snapshot_id="c" * 64,
        target_catalog_fingerprint="d" * 64,
        ontology_version="reasoning-ontology/v5",
        policy_version="company-projection-policy/v1",
        model_id="deepseek-v4-flash",
        prompt_contract_version="company-target-selection/v1",
        decided_at=NOW,
    )


def _targets() -> dict[str, CanonicalGraphTarget]:
    return {
        IND: CanonicalGraphTarget(data_object_id=IND, uuid=node_uuid(IND), name="半导体", label="Industry"),
        CND: CanonicalGraphTarget(data_object_id=CND, uuid=node_uuid(CND), name="晶圆制造", label="ChainNode"),
    }


class CompanyInferredEdgeTest(unittest.TestCase):
    def test_direct_builder_creates_only_explicit_existing_targets(self) -> None:
        edges = build_inferred_edges(
            CompanyFacts(
                schema_version="company-projection-snapshot.v1",
                snapshot_id="c" * 64,
                companies=(_company(),),
            ),
            [_decision()],
            _targets(),
        )

        self.assertEqual([edge.name for edge in edges], ["CompanyOperatesInIndustry", "CompanyParticipatesInChainNode"])
        self.assertEqual(edges[0].uuid, edge_uuid("CompanyOperatesInIndustry", COM, IND))
        self.assertEqual(edges[1].uuid, edge_uuid("CompanyParticipatesInChainNode", COM, CND))
        self.assertEqual(edges[0].target_node_uuid, node_uuid(IND))
        self.assertEqual(edges[1].target_node_uuid, node_uuid(CND))
        CompanyOperatesInIndustry.model_validate(edges[0].attributes)
        CompanyParticipatesInChainNode.model_validate(edges[1].attributes)

    def test_unknown_target_fails_closed_instead_of_creating_a_node(self) -> None:
        with self.assertRaisesRegex(ValueError, "not in the canonical preflight catalog"):
            build_inferred_edges(
                CompanyFacts(
                    schema_version="company-projection-snapshot.v1",
                    snapshot_id="c" * 64,
                    companies=(_company(),),
                ),
                [_decision()],
                {IND: _targets()[IND]},
            )

    def test_decision_must_match_the_exact_company_input_fingerprint(self) -> None:
        corrupted = _decision().model_copy(update={"source_company_fingerprint": SHA_B})

        with self.assertRaisesRegex(ValueError, "Company input mismatch"):
            build_inferred_edges(
                CompanyFacts(
                    schema_version="company-projection-snapshot.v1",
                    snapshot_id="c" * 64,
                    companies=(_company(),),
                ),
                [corrupted],
                _targets(),
            )

    def test_company_projection_has_no_episode_write_path(self) -> None:
        source = inspect.getsource(company_projection)

        self.assertNotIn("add_episode", source)
        self.assertNotIn("add_episode_bulk", source)

        repository = Path(__file__).resolve().parents[1]
        company_sources = [
            *sorted((repository / "capabilities/company").rglob("*.py")),
            repository / "sematica/graphiti/company.py",
            repository / "sematica/projection/company.py",
            repository / "sematica/projection/company_cli.py",
        ]
        for path in company_sources:
            with self.subTest(path=path):
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("add_episode(", source)
                self.assertNotIn("add_episode_bulk(", source)


class CompanyNamespacePreflightTest(unittest.IsolatedAsyncioTestCase):
    async def test_foreign_owner_on_a_canonical_company_fails_closed(self) -> None:
        plan = build_plan(
            CompanyFacts(
                schema_version="company-projection-snapshot.v1",
                snapshot_id="c" * 64,
                companies=(_company(),),
            )
        )
        record = SimpleNamespace(
            data=lambda: {
                "uuid": node_uuid(COM),
                "data_object_id": COM,
                "labels": ["Entity", "Company"],
                "group_id": "neo4j",
                "projection_owner": "another-projection",
            }
        )
        graphiti = cast(
            Graphiti,
            SimpleNamespace(
                driver=SimpleNamespace(execute_query=AsyncMock(return_value=SimpleNamespace(records=[record])))
            ),
        )

        with self.assertRaisesRegex(ProjectionError, "outside the owned projection namespace"):
            await preflight_company_namespace(graphiti, plan)

    async def test_reverse_owned_relation_fails_closed(self) -> None:
        edge = build_inferred_edges(
            CompanyFacts(
                schema_version="company-projection-snapshot.v1",
                snapshot_id="c" * 64,
                companies=(_company(),),
            ),
            [_decision()],
            _targets(),
        )[0]
        record = {
            "uuid": edge.uuid,
            "relationship_type": "RELATES_TO",
            "name": edge.name,
            "group_id": "neo4j",
            "projection_owner": COMPANY_PROJECTION_OWNER,
            "source_uuid": node_uuid(IND),
            "source_id": IND,
            "source_labels": ["Entity", "Industry"],
            "source_group_id": "neo4j",
            "source_projection_owner": None,
            "target_uuid": node_uuid(COM),
            "target_id": COM,
            "target_labels": ["Entity", "Company"],
            "target_group_id": "neo4j",
        }
        graphiti = cast(
            Graphiti,
            SimpleNamespace(
                driver=SimpleNamespace(
                    execute_query=AsyncMock(
                        return_value=SimpleNamespace(records=[SimpleNamespace(data=lambda: record)])
                    )
                )
            ),
        )

        with self.assertRaisesRegex(ProjectionError, "Company relation namespace preflight failed"):
            await preflight_company_relation_namespace(graphiti, [edge])

    async def test_expected_relation_uuid_with_wrong_target_fails_closed(self) -> None:
        edge = build_inferred_edges(
            CompanyFacts(
                schema_version="company-projection-snapshot.v1",
                snapshot_id="c" * 64,
                companies=(_company(),),
            ),
            [_decision()],
            _targets(),
        )[0]
        record = {
            "uuid": edge.uuid,
            "relationship_type": "RELATES_TO",
            "name": edge.name,
            "group_id": "neo4j",
            "projection_owner": COMPANY_PROJECTION_OWNER,
            "source_uuid": node_uuid(COM),
            "source_id": COM,
            "source_labels": ["Entity", "Company"],
            "source_group_id": "neo4j",
            "source_projection_owner": COMPANY_PROJECTION_OWNER,
            "target_uuid": node_uuid(CND),
            "target_id": CND,
            "target_labels": ["Entity", "ChainNode"],
            "target_group_id": "neo4j",
        }
        graphiti = cast(
            Graphiti,
            SimpleNamespace(
                driver=SimpleNamespace(
                    execute_query=AsyncMock(
                        return_value=SimpleNamespace(records=[SimpleNamespace(data=lambda: record)])
                    )
                )
            ),
        )

        with self.assertRaisesRegex(ProjectionError, "Company relation namespace preflight failed"):
            await preflight_company_relation_namespace(graphiti, [edge])


class CompanyProjectionDiffTest(unittest.TestCase):
    def test_identical_graph_state_requires_no_embedding_or_upsert(self) -> None:
        facts = CompanyFacts(
            schema_version="company-projection-snapshot.v1",
            snapshot_id="c" * 64,
            companies=(_company(),),
        )
        plan = build_plan(facts)
        inferred = build_inferred_edges(facts, [_decision()], _targets())
        node = plan.nodes[0]
        state = {
            "nodes": [
                {
                    "uuid": node.uuid,
                    "data_object_id": COM,
                    "labels": ["Entity", "Company"],
                    "name": node.name,
                    "summary": node.summary,
                    "source_record_fingerprint": node.attributes["source_record_fingerprint"],
                    "properties": dict(node.attributes),
                    "embedding_dimension": 1024,
                }
            ],
            "edges": [
                {
                    "uuid": edge.uuid,
                    "name": edge.name,
                    "source_id": COM,
                    "target_id": IND if edge.name == "CompanyOperatesInIndustry" else CND,
                    "source_labels": ["Entity", "Company"],
                    "target_labels": [
                        "Entity",
                        "Industry" if edge.name == "CompanyOperatesInIndustry" else "ChainNode",
                    ],
                    "target_uuid": edge.target_node_uuid,
                    "fact": edge.fact,
                    "projection_fingerprint": edge.attributes["projection_fingerprint"],
                    "properties": dict(edge.attributes),
                    "embedding_dimension": 1024,
                }
                for edge in inferred
            ],
        }

        changed_nodes, changed_edges = diff_company_projection(
            plan,
            inferred,
            state,
            embedding_dimension=1024,
        )

        self.assertEqual(changed_nodes, [])
        self.assertEqual(changed_edges, [])

    def test_changed_decision_is_the_only_edge_rewritten(self) -> None:
        facts = CompanyFacts(
            schema_version="company-projection-snapshot.v1",
            snapshot_id="c" * 64,
            companies=(_company(),),
        )
        plan = build_plan(facts)
        inferred = build_inferred_edges(facts, [_decision()], _targets())
        node = plan.nodes[0]
        state = {
            "nodes": [
                {
                    "uuid": node.uuid,
                    "data_object_id": COM,
                    "labels": ["Entity", "Company"],
                    "name": node.name,
                    "summary": node.summary,
                    "source_record_fingerprint": node.attributes["source_record_fingerprint"],
                    "properties": dict(node.attributes),
                    "embedding_dimension": 1024,
                }
            ],
            "edges": [
                {
                    "uuid": inferred[0].uuid,
                    "name": inferred[0].name,
                    "source_id": COM,
                    "target_id": IND,
                    "source_labels": ["Entity", "Company"],
                    "target_labels": ["Entity", "Industry"],
                    "target_uuid": inferred[0].target_node_uuid,
                    "fact": inferred[0].fact,
                    "projection_fingerprint": "f" * 64,
                    "properties": dict(inferred[0].attributes),
                    "embedding_dimension": 1024,
                },
                {
                    "uuid": inferred[1].uuid,
                    "name": inferred[1].name,
                    "source_id": COM,
                    "target_id": CND,
                    "source_labels": ["Entity", "Company"],
                    "target_labels": ["Entity", "ChainNode"],
                    "target_uuid": inferred[1].target_node_uuid,
                    "fact": inferred[1].fact,
                    "projection_fingerprint": inferred[1].attributes["projection_fingerprint"],
                    "properties": dict(inferred[1].attributes),
                    "embedding_dimension": 1024,
                },
            ],
        }

        changed_nodes, changed_edges = diff_company_projection(
            plan,
            inferred,
            state,
            embedding_dimension=1024,
        )

        self.assertEqual(changed_nodes, [])
        self.assertEqual([edge.name for edge in changed_edges], ["CompanyOperatesInIndustry"])

    def test_verifier_rejects_wrongly_typed_existing_target(self) -> None:
        facts = CompanyFacts(
            schema_version="company-projection-snapshot.v1",
            snapshot_id="c" * 64,
            companies=(_company(),),
        )
        plan = build_plan(facts)
        inferred = build_inferred_edges(facts, [_decision()], _targets())
        node = plan.nodes[0]
        state = {
            "nodes": [
                {
                    "uuid": node.uuid,
                    "data_object_id": COM,
                    "labels": ["Entity", "Company"],
                    "name": node.name,
                    "summary": node.summary,
                    "source_record_fingerprint": node.attributes["source_record_fingerprint"],
                    "properties": dict(node.attributes),
                    "embedding_dimension": 1024,
                }
            ],
            "edges": [
                {
                    "uuid": edge.uuid,
                    "name": edge.name,
                    "source_id": COM,
                    "target_id": IND if edge.name == "CompanyOperatesInIndustry" else CND,
                    "source_labels": ["Entity", "Company"],
                    "target_labels": ["Entity", "Concept"],
                    "target_uuid": edge.target_node_uuid,
                    "fact": edge.fact,
                    "projection_fingerprint": edge.attributes["projection_fingerprint"],
                    "properties": dict(edge.attributes),
                    "embedding_dimension": 1024,
                }
                for edge in inferred
            ],
        }

        with self.assertRaisesRegex(ProjectionError, "wrongly typed target"):
            verify_company_projection(plan, inferred, state, embedding_dimension=1024)

    def test_verifier_rejects_swapped_relation_types_even_when_other_facts_match(self) -> None:
        facts = CompanyFacts(
            schema_version="company-projection-snapshot.v1",
            snapshot_id="c" * 64,
            companies=(_company(),),
        )
        plan = build_plan(facts)
        inferred = build_inferred_edges(facts, [_decision()], _targets())
        node = plan.nodes[0]
        state = {
            "nodes": [
                {
                    "uuid": node.uuid,
                    "data_object_id": COM,
                    "labels": ["Entity", "Company"],
                    "name": node.name,
                    "summary": node.summary,
                    "source_record_fingerprint": node.attributes["source_record_fingerprint"],
                    "properties": dict(node.attributes),
                    "embedding_dimension": 1024,
                }
            ],
            "edges": [
                {
                    "uuid": edge.uuid,
                    "name": inferred[1 - index].name,
                    "source_id": COM,
                    "target_id": IND if index == 0 else CND,
                    "source_labels": ["Entity", "Company"],
                    "target_labels": ["Entity", "Industry" if index == 0 else "ChainNode"],
                    "target_uuid": edge.target_node_uuid,
                    "fact": edge.fact,
                    "projection_fingerprint": edge.attributes["projection_fingerprint"],
                    "properties": dict(edge.attributes),
                    "embedding_dimension": 1024,
                }
                for index, edge in enumerate(inferred)
            ],
        }

        with self.assertRaisesRegex(ProjectionError, "relation type differs"):
            verify_company_projection(plan, inferred, state, embedding_dimension=1024)


class CompanyProjectionExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_changed_facts_are_written_in_bounded_chunks_and_embeddings_are_released(self) -> None:
        facts = CompanyFacts(
            schema_version="company-projection-snapshot.v1",
            snapshot_id="c" * 64,
            companies=(_company(),),
        )
        node = build_plan(facts).nodes[0]
        edge = build_inferred_edges(facts, [_decision()], _targets())[0]
        nodes = [node for _ in range(205)]
        edges = [edge for _ in range(201)]
        calls: list[tuple[int, int]] = []

        async def fake_write(_graphiti, *, nodes, edges, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append((len(nodes), len(edges)))
            for item in nodes:
                item.name_embedding = [0.1]
            for item in edges:
                item.fact_embedding = [0.1]
            return len(nodes), len(edges), {"nodes": 0, "relationships": 0}

        with patch("sematica.projection.company.write_projection", side_effect=fake_write):
            await _write_changed_company_facts(SimpleNamespace(), nodes, edges, progress=None)  # type: ignore[arg-type]

        self.assertEqual(calls, [(100, 0), (100, 0), (5, 0), (0, 100), (0, 100), (0, 1)])
        self.assertIsNone(node.name_embedding)
        self.assertIsNone(edge.fact_embedding)

    async def test_catalog_change_stops_replace_before_any_sweep_query(self) -> None:
        facts = CompanyFacts(
            schema_version="company-projection-snapshot.v1",
            snapshot_id="c" * 64,
            companies=(_company(),),
        )
        plan = build_plan(facts)
        manifest = ProjectionRunManifest(
            snapshot_id=facts.snapshot_id,
            company_snapshot_fingerprint="b" * 64,
            target_catalog_fingerprint="d" * 64,
            company_ids=[COM],
            ontology_version="reasoning-ontology/v5",
            policy_version="company-projection-policy/v2",
            model_id="deepseek-v4-flash",
            prompt_contract_version="company-target-selection/v2",
            created_at=NOW,
        )
        driver = SimpleNamespace(execute_query=AsyncMock())
        graphiti = cast(Graphiti, SimpleNamespace(driver=driver))

        with (
            patch(
                "sematica.projection.company._validate_current_target_catalog",
                new=AsyncMock(side_effect=[None, ProjectionError("catalog changed")]),
            ),
            patch(
                "sematica.projection.company.preflight_company_namespace",
                new=AsyncMock(),
            ),
            patch(
                "sematica.projection.company.preflight_company_relation_namespace",
                new=AsyncMock(),
            ),
            patch(
                "sematica.projection.company.preflight_canonical_targets",
                new=AsyncMock(return_value=_targets()),
            ),
            patch(
                "sematica.projection.company.inspect_company_projection_state",
                new=AsyncMock(return_value={"nodes": [], "edges": []}),
            ),
            patch("sematica.projection.company._write_changed_company_facts", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(ProjectionError, "catalog changed"):
                await execute_company_projection(
                    graphiti,
                    facts,
                    plan,
                    [_decision()],
                    manifest,
                    embedding_dimension=1024,
                    replace=True,
                )

        driver.execute_query.assert_not_awaited()

    async def test_replace_deletes_only_explicit_owned_stale_uuids(self) -> None:
        facts = CompanyFacts(
            schema_version="company-projection-snapshot.v1",
            snapshot_id="c" * 64,
            companies=(_company(),),
        )
        plan = build_plan(facts)
        manifest = ProjectionRunManifest(
            snapshot_id=facts.snapshot_id,
            company_snapshot_fingerprint="b" * 64,
            target_catalog_fingerprint="d" * 64,
            company_ids=[COM],
            ontology_version="reasoning-ontology/v5",
            policy_version="company-projection-policy/v2",
            model_id="deepseek-v4-flash",
            prompt_contract_version="company-target-selection/v2",
            created_at=NOW,
        )
        stale_edge_uuid = "00000000-0000-4000-8000-000000000003"
        before = {
            "nodes": [{"uuid": node_uuid(STALE_COM)}],
            "edges": [{"uuid": stale_edge_uuid}],
        }
        driver = SimpleNamespace(execute_query=AsyncMock(return_value=SimpleNamespace(records=[])))
        graphiti = cast(Graphiti, SimpleNamespace(driver=driver))

        with (
            patch("sematica.projection.company.preflight_company_namespace", new=AsyncMock()),
            patch("sematica.projection.company.preflight_company_relation_namespace", new=AsyncMock()),
            patch("sematica.projection.company._validate_current_target_catalog", new=AsyncMock()),
            patch("sematica.projection.company.preflight_canonical_targets", new=AsyncMock(return_value=_targets())),
            patch(
                "sematica.projection.company.inspect_company_projection_state",
                new=AsyncMock(side_effect=[before, {"nodes": [], "edges": []}]),
            ),
            patch("sematica.projection.company._write_changed_company_facts", new=AsyncMock()),
            patch("sematica.projection.company.verify_company_projection", return_value={"verified": True}),
        ):
            result = await execute_company_projection(
                graphiti,
                facts,
                plan,
                [_decision()],
                manifest,
                embedding_dimension=1024,
                replace=True,
            )

        self.assertEqual(result["removed_after_complete_write"], {"nodes": 1, "relationships": 1})
        self.assertEqual(driver.execute_query.await_count, 2)
        edge_call, node_call = driver.execute_query.await_args_list
        self.assertIn("edge.projection_owner = $projection_owner", edge_call.args[0])
        self.assertEqual(edge_call.kwargs["projection_owner"], COMPANY_PROJECTION_OWNER)
        self.assertEqual(edge_call.kwargs["stale_edge_uuids"], [stale_edge_uuid])
        self.assertNotIn("expected_edge_uuids", edge_call.kwargs)
        self.assertIn("company.uuid IN $stale_node_uuids", node_call.args[0])
        self.assertNotIn("DETACH DELETE", node_call.args[0])
        self.assertEqual(node_call.kwargs["projection_owner"], COMPANY_PROJECTION_OWNER)
        self.assertEqual(node_call.kwargs["stale_node_uuids"], [node_uuid(STALE_COM)])
        self.assertNotIn("expected_node_uuids", node_call.kwargs)


if __name__ == "__main__":
    unittest.main()
