"""Tests for the read-only canonical Company target catalog adapter."""

from __future__ import annotations

import unittest

from sematica.graphiti.company import load_company_target_catalog
from sematica.projection.authoritative_writer import edge_uuid, node_uuid
from sematica.projection.runtime import ProjectionError

IND = "IND00000000-0000-4000-8000-000000000001"
ICH = "ICH00000000-0000-4000-8000-000000000001"
CND = "CND00000000-0000-4000-8000-000000000001"
IND_CHILD = "IND00000000-0000-4000-8000-000000000002"


class _Record:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value

    def data(self) -> dict[str, object]:
        return self.value


class _Result:
    def __init__(self, values: list[dict[str, object]]) -> None:
        self.records = [_Record(value) for value in values]


class _Driver:
    def __init__(self, pages: list[list[dict[str, object]]]) -> None:
        self.pages = list(pages)
        self.queries: list[str] = []

    async def execute_query(self, query: str, **kwargs):  # type: ignore[no-untyped-def]
        self.queries.append(query)
        return _Result(self.pages.pop(0))


class _Graphiti:
    def __init__(self, pages: list[list[dict[str, object]]]) -> None:
        self.driver = _Driver(pages)


def _pages(*, industry_labels: list[str] | None = None) -> list[list[dict[str, object]]]:
    return [
        [
            {
                "data_object_id": IND,
                "uuid": node_uuid(IND),
                "labels": industry_labels or ["Entity", "Industry"],
                "name": "半导体",
                "definition": "半导体研发与制造",
                "parent_edges": [],
            }
        ],
        [
            {
                "data_object_id": ICH,
                "uuid": node_uuid(ICH),
                "labels": ["Entity", "IndustryChain"],
                "name": "集成电路产业链",
            }
        ],
        [
            {
                "data_object_id": CND,
                "uuid": node_uuid(CND),
                "labels": ["Entity", "ChainNode"],
                "name": "晶圆制造",
                "definition": "晶圆制造环节",
            }
        ],
        [
            {
                "uuid": edge_uuid("IndustryChainMappedToIndustry", ICH, IND),
                "industry_chain_id": ICH,
                "industry_id": IND,
            }
        ],
        [
            {
                "uuid": edge_uuid("ChainNodeBelongsToIndustryChain", CND, ICH),
                "industry_chain_id": ICH,
                "chain_node_id": CND,
            }
        ],
    ]


class CompanyTargetCatalogReaderTest(unittest.IsolatedAsyncioTestCase):
    async def test_reads_only_existing_canonical_targets_and_topology(self) -> None:
        graphiti = _Graphiti(_pages())

        catalog = await load_company_target_catalog(graphiti)  # type: ignore[arg-type]

        self.assertEqual([item.industry_id for item in catalog.industries], [IND])
        self.assertEqual([item.industry_chain_id for item in catalog.industry_chains], [ICH])
        self.assertEqual([item.chain_node_id for item in catalog.chain_nodes], [CND])
        self.assertEqual(catalog.industry_chain_mappings[0].industry_id, IND)
        self.assertEqual(catalog.chain_memberships[0].chain_node_id, CND)
        self.assertTrue(
            all("CREATE" not in query.upper() and "MERGE" not in query.upper() for query in graphiti.driver.queries)
        )

    async def test_wrong_or_extra_target_label_fails_closed(self) -> None:
        graphiti = _Graphiti(_pages(industry_labels=["Entity", "Industry", "Organization"]))

        with self.assertRaisesRegex(ProjectionError, "canonical Industry"):
            await load_company_target_catalog(graphiti)  # type: ignore[arg-type]

    async def test_non_deterministic_target_uuid_fails_closed(self) -> None:
        pages = _pages()
        pages[2][0]["uuid"] = "00000000-0000-4000-8000-000000000000"

        with self.assertRaisesRegex(ProjectionError, "deterministic UUID"):
            await load_company_target_catalog(_Graphiti(pages))  # type: ignore[arg-type]

    async def test_non_deterministic_topology_edge_uuid_fails_closed(self) -> None:
        pages = _pages()
        pages[3][0]["uuid"] = "00000000-0000-4000-8000-000000000000"

        with self.assertRaisesRegex(ProjectionError, "mapping UUID"):
            await load_company_target_catalog(_Graphiti(pages))  # type: ignore[arg-type]

    async def test_non_deterministic_industry_parent_edge_uuid_fails_closed(self) -> None:
        pages = _pages()
        pages[0].append(
            {
                "data_object_id": IND_CHILD,
                "uuid": node_uuid(IND_CHILD),
                "labels": ["Entity", "Industry"],
                "name": "集成电路制造",
                "definition": "芯片与晶圆制造",
                "parent_edges": [{"uuid": "00000000-0000-4000-8000-000000000000", "parent_id": IND}],
            }
        )

        with self.assertRaisesRegex(ProjectionError, "parent edge UUID"):
            await load_company_target_catalog(_Graphiti(pages))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
