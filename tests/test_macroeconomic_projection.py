"""Joined Data snapshot validation, exact graph parity and safe replay."""

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from sematica.initialization.macroeconomic.projection import (
    Snapshot,
    build_plan,
    execute_plan,
    preflight,
    verify,
)
from sematica.ontology import EDGE_TYPE_MAP, EDGE_TYPES, MacroEconomic
from sematica.projection.runtime import ProjectionError


def payload():
    return {
        "schema_version": "macroeconomic-projection-snapshot.v1",
        "source_count": 1,
        "items": [
            {
                "id": "MEC11111111-1111-4111-8111-111111111111",
                "name": "美联储政策利率路径",
                "macro_economics_domain_id": "MCD22222222-2222-4222-8222-222222222222",
                "domain_code": "MONETARY",
                "domain_name": "货币政策线",
                "domain_description": "政策利率与央行流动性",
                "tactics": [
                    {"name": "降息", "description": "下调政策利率"},
                    {"name": "前瞻指引", "description": "央行政策路径沟通"},
                ],
                "core_proposition": "政策利率变化通过利差影响人民币汇率和中国融资环境",
                "candidate_assets": ["国债期货", "黄金"],
                "created_at": "2026-09-06T01:00:00Z",
                "updated_at": "2026-09-06T02:00:00Z",
                "domain_created_at": "2026-09-05T01:00:00Z",
                "domain_updated_at": "2026-09-05T02:00:00Z",
            }
        ],
    }


def graph_row(node):
    return {
        "labels": node.labels,
        "dimension": 3,
        "props": {
            "uuid": node.uuid,
            "name": node.name,
            "summary": node.summary,
            "group_id": node.group_id,
            "labels": node.labels,
            "created_at": node.created_at,
            **node.attributes,
        },
    }


class ProjectionTests(unittest.IsolatedAsyncioTestCase):
    def test_storyline_contract_rejects_old_policy_fields(self):
        for key, value in (("policy_key", "RATE_CUT"), ("category", "MONETARY"), ("status", "ACTIVE")):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                MacroEconomic.model_validate({key: value})
        self.assertNotIn("CountryImplementsMacroEconomic", EDGE_TYPES)
        self.assertNotIn(("Country", "MacroEconomic"), EDGE_TYPE_MAP)

    def test_tactics_are_an_ordered_json_array_not_an_object(self):
        for tactics in ("{}", "[]", '[{"name":"降息"}]', '[{"name":"降息","description":"降息","code":"X"}]'):
            with self.subTest(tactics=tactics), self.assertRaises(ValidationError):
                MacroEconomic(tactics=tactics)

    async def test_legacy_namespace_fails_before_embedding_or_write(self):
        nodes = build_plan(Snapshot.model_validate(payload()))
        row = graph_row(nodes[0])
        row["props"].pop("projection_owner")
        g = SimpleNamespace(
            embedder=SimpleNamespace(create_batch=AsyncMock()),
            nodes=SimpleNamespace(entity=SimpleNamespace(save_bulk=AsyncMock())),
        )
        with patch("sematica.initialization.macroeconomic.projection.inspect_state", AsyncMock(return_value=[row])):
            with self.assertRaises(ProjectionError):
                await execute_plan(g, nodes, 3)
        g.embedder.create_batch.assert_not_awaited()
        g.nodes.entity.save_bulk.assert_not_awaited()

    async def test_new_storyline_writes_and_verifies(self):
        nodes = build_plan(Snapshot.model_validate(payload()))
        g = SimpleNamespace(
            embedder=SimpleNamespace(create_batch=AsyncMock(return_value=[[0.1, 0.2, 0.3]])),
            nodes=SimpleNamespace(entity=SimpleNamespace(save_bulk=AsyncMock())),
        )
        with patch(
            "sematica.initialization.macroeconomic.projection.inspect_state",
            AsyncMock(side_effect=[[], [], [graph_row(nodes[0])]]),
        ):
            result = await execute_plan(g, nodes, 3)
        self.assertEqual(result["nodes_written"], 1)
        g.embedder.create_batch.assert_awaited_once_with([nodes[0].name])
        g.nodes.entity.save_bulk.assert_awaited_once_with(nodes, batch_size=1)

    def test_complete_profile_and_matching_boundary(self):
        data = payload()
        node = build_plan(Snapshot.model_validate(data))[0]
        self.assertEqual(json.loads(node.attributes["tactics"]), data["items"][0]["tactics"])
        self.assertEqual(node.attributes["candidate_assets"], ["国债期货", "黄金"])
        self.assertNotIn("macro_economics_domain_id", node.attributes)
        self.assertNotIn("黄金", node.summary)
        self.assertIn("前瞻指引", node.summary)
        self.assertEqual(node.uuid, build_plan(Snapshot.model_validate(data))[0].uuid)
        verify([node], [graph_row(node)], 3)

    def test_invalid_or_incomplete_snapshot(self):
        variants = []
        for assets in ([], ["黄金", "黄金"], [" 黄金"], [2], ["x" * 101]):
            data = payload()
            data["items"][0]["candidate_assets"] = assets
            variants.append(data)
        for field in ("id", "tactics", "domain_code"):
            data = payload()
            del data["items"][0][field]
            variants.append(data)
        data = payload()
        data["source_count"] = 2
        variants.append(data)
        data = payload()
        data["items"][0]["id"] = "MECinvalid"
        variants.append(data)
        data = payload()
        data["items"][0]["tactics"] *= 2
        variants.append(data)
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                Snapshot.model_validate(data)

    def test_conflicting_domain_and_duplicate_identity(self):
        data = payload()
        other = copy.deepcopy(data["items"][0])
        data["items"].append(other)
        data["source_count"] = 2
        with self.assertRaises(ValidationError):
            Snapshot.model_validate(data)
        other["id"] = "MEC33333333-3333-4333-8333-333333333333"
        other["name"] = "另一命题"
        other["domain_description"] = "不一致"
        with self.assertRaises(ValidationError):
            Snapshot.model_validate(data)

    def test_collision_and_exact_verification(self):
        nodes = build_plan(Snapshot.model_validate(payload()))
        row = graph_row(nodes[0])
        row["labels"] = ["Entity", "Company"]
        with self.assertRaises(ProjectionError):
            preflight(nodes, [row])
        row = graph_row(nodes[0])
        row["props"]["candidate_assets"] = ["错误资产"]
        with self.assertRaises(ProjectionError):
            verify(nodes, [row], 3)
        with self.assertRaises(ProjectionError):
            preflight(nodes, [graph_row(nodes[0]), graph_row(nodes[0])])

    async def test_replay_skips_embedding_and_writes(self):
        nodes = build_plan(Snapshot.model_validate(payload()))
        g = SimpleNamespace(
            embedder=SimpleNamespace(create_batch=AsyncMock()),
            nodes=SimpleNamespace(entity=SimpleNamespace(save_bulk=AsyncMock())),
        )
        with patch(
            "sematica.initialization.macroeconomic.projection.inspect_state",
            AsyncMock(return_value=[graph_row(nodes[0])]),
        ):
            result = await execute_plan(g, nodes, 3)
        self.assertEqual(result["nodes_written"], 0)
        g.embedder.create_batch.assert_not_awaited()
        g.nodes.entity.save_bulk.assert_not_awaited()

    async def test_bad_vectors_fail_before_write(self):
        nodes = build_plan(Snapshot.model_validate(payload()))
        g = SimpleNamespace(
            embedder=SimpleNamespace(create_batch=AsyncMock(return_value=[[1.0]])),
            nodes=SimpleNamespace(entity=SimpleNamespace(save_bulk=AsyncMock())),
        )
        with patch("sematica.initialization.macroeconomic.projection.inspect_state", AsyncMock(return_value=[])):
            with self.assertRaises(ProjectionError):
                await execute_plan(g, nodes, 3)
        g.nodes.entity.save_bulk.assert_not_awaited()
