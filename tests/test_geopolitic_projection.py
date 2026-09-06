"""Joined Data snapshot validation, exact graph parity and safe replay."""

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from sematica.initialization.geopolitic.projection import (
    Snapshot,
    build_plan,
    execute_plan,
    preflight,
    verify,
)
from sematica.projection.runtime import ProjectionError


def payload():
    return {
        "schema_version": "geopolitic-projection-snapshot.v1",
        "source_count": 1,
        "items": [
            {
                "id": "GPR11111111-1111-4111-8111-111111111111",
                "name": "中美技术出口管制",
                "category": "中美竞争",
                "geopolitic_domain_id": "GPD22222222-2222-4222-8222-222222222222",
                "domain_code": "TECHNOLOGY_STANDARDS",
                "domain_name": "科技/标准线",
                "domain_description": "技术与标准竞争",
                "tactics": [
                    {"name": "技术出口管制", "description": "芯片出口许可"},
                    {"name": "科技外交", "description": "科研合作协议"},
                ],
                "core_proposition": "先进技术获取限制",
                "core_actors": "中国、美国",
                "main_transmission": "设备供应到产能",
                "candidate_assets": ["半导体设备板块", "黄金"],
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
    def test_complete_profile_and_matching_boundary(self):
        data = payload()
        node = build_plan(Snapshot.model_validate(data))[0]
        self.assertEqual(json.loads(node.attributes["tactics"]), data["items"][0]["tactics"])
        self.assertEqual(node.attributes["candidate_assets"], ["半导体设备板块", "黄金"])
        self.assertNotIn("黄金", node.summary)
        self.assertNotIn("设备供应到产能", node.summary)
        self.assertIn("科技外交", node.summary)
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
        data["items"][0]["id"] = "GPRinvalid"
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
        other["id"] = "GPR33333333-3333-4333-8333-333333333333"
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
            "sematica.initialization.geopolitic.projection.inspect_state", AsyncMock(return_value=[graph_row(nodes[0])])
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
        with patch("sematica.initialization.geopolitic.projection.inspect_state", AsyncMock(return_value=[])):
            with self.assertRaises(ProjectionError):
                await execute_plan(g, nodes, 3)
        g.nodes.entity.save_bulk.assert_not_awaited()
