"""Contract tests for the Data-owned Company projection boundary."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from pydantic import ValidationError

from sematica.ingestion.episcode.event.stages.episode import event_entity_types
from sematica.ontology import (
    EDGE_TYPE_MAP,
    EDGE_TYPES,
    ENTITY_TYPES,
    ONTOLOGY_VERSION,
    CompanyBelongsToIndustry,
    CompanyOperatesInIndustry,
    CompanyParticipatesInChainNode,
)
from sematica.projection.authoritative_writer import edge_uuid, node_uuid
from sematica.projection.company import (
    COMPANY_PROJECTION_SCHEMA_VERSION,
    CompanyFacts,
    CompanyPageEnvelope,
    build_plan,
    load_facts,
    preflight_canonical_targets,
)
from sematica.projection.runtime import ProjectionError, RuntimeConfig

COMPANY_ID = "COM11111111-1111-4111-8111-111111111111"
OTHER_COMPANY_ID = "COM22222222-2222-4222-8222-222222222222"
LINK_ID = "CIL33333333-3333-4333-8333-333333333333"
INDUSTRY_ID = "IND44444444-4444-4444-8444-444444444444"
CHAIN_NODE_ID = "CND55555555-5555-4555-8555-555555555555"
SNAPSHOT_ID = "a" * 64
CREATED_AT = "2026-08-01T00:00:00Z"
UPDATED_AT = "2026-08-02T00:00:00Z"


def company_payload(*, company_id: str = COMPANY_ID, code: str = "000001.SZ") -> dict[str, object]:
    return {
        "id": company_id,
        "code": code,
        "name": "示例股份",
        "name_en": "Example Corp.",
        "legal_name": "示例股份有限公司",
        "aliases": ["示例公司"],
        "registration_country_id": "COU66666666-6666-4666-8666-666666666666",
        "operating_area": "中国",
        "headquarters_city": "深圳",
        "founding_date": "2000-01-01",
        "ipo_date": "2010-01-01",
        "legal_form": "股份有限公司",
        "ownership_type": "DISPERSED",
        "strategic_positioning": "核心供应商",
        "description": "生产工业设备。",
        "status": "active",
        "created_at": CREATED_AT,
        "updated_at": UPDATED_AT,
        "industry_links": [
            {
                "id": LINK_ID,
                "company_id": company_id,
                "industry_id": INDUSTRY_ID,
                "created_at": CREATED_AT,
            }
        ],
    }


def envelope(
    items: list[dict[str, object]],
    *,
    snapshot_id: str = SNAPSHOT_ID,
    next_cursor: str | None = None,
) -> dict[str, object]:
    return {
        "request_id": "request-1",
        "result": {
            "schema_version": COMPANY_PROJECTION_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "items": items,
            "next_cursor": next_cursor,
        },
    }


def runtime_config() -> RuntimeConfig:
    return RuntimeConfig.model_validate(
        {
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": "password",
            "NEO4J_BOLT_PORT": 7687,
            "NEO4J_HTTP_PORT": 7474,
            "GRAPHITI_EMBEDDING_API_KEY": "embedding-key",
            "GRAPHITI_EMBEDDING_BASE_URL": "https://embedding.example/v1",
            "GRAPHITI_EMBEDDING_MODEL": "embedding-model",
            "GRAPHITI_EMBEDDING_DIM": 1024,
            "GRAPHITI_LLM_API_KEY": "llm-key",
            "GRAPHITI_LLM_BASE_URL": "https://llm.example/v1",
            "GRAPHITI_LLM_MODEL": "llm-model",
            "TIDEWISE_DATA_BASE_URL": "https://data.example",
            "TIDEWISE_DATA_SERVICE_TOKEN": "service-token",
        }
    )


class CompanyDTOContractTest(unittest.TestCase):
    def test_accepts_the_complete_versioned_snapshot_contract(self) -> None:
        parsed = CompanyPageEnvelope.model_validate(envelope([company_payload()]))

        self.assertEqual(parsed.result.schema_version, "company-projection-snapshot.v1")
        self.assertEqual(parsed.result.snapshot_id, SNAPSHOT_ID)
        self.assertEqual(parsed.result.items[0].industry_links[0].id, LINK_ID)

    def test_rejects_unknown_fields_malformed_ids_dates_timestamps_and_status(self) -> None:
        mutations = []

        unknown = envelope([company_payload()])
        unknown["result"]["items"][0]["unknown"] = True  # type: ignore[index]
        mutations.append(unknown)

        for path, value in (
            (("id",), "COMAAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
            (("registration_country_id",), "country-cn"),
            (("founding_date",), "2026-02-30"),
            (("founding_date",), "2000-01-01T00:00:00Z"),
            (("created_at",), "2026-08-01T08:00:00+08:00"),
            (("created_at",), 1785542400),
            (("status",), "ACTIVE"),
            (("industry_links", 0, "id"), "link-1"),
            (("industry_links", 0, "industry_id"), "industry-1"),
            (("industry_links", 0, "created_at"), "2026-08-01T08:00:00+08:00"),
        ):
            payload = envelope([company_payload()])
            target: object = payload["result"]["items"][0]  # type: ignore[index]
            for part in path[:-1]:
                target = target[part]  # type: ignore[index]
            target[path[-1]] = value  # type: ignore[index]
            mutations.append(payload)

        aliases = envelope([company_payload()])
        aliases["result"]["items"][0]["aliases"] = ["重复", "重复"]  # type: ignore[index]
        mutations.append(aliases)

        reversed_dates = envelope([company_payload()])
        reversed_dates["result"]["items"][0]["founding_date"] = "2020-01-01"  # type: ignore[index]
        reversed_dates["result"]["items"][0]["ipo_date"] = "2010-01-01"  # type: ignore[index]
        mutations.append(reversed_dates)

        reversed_timestamps = envelope([company_payload()])
        reversed_timestamps["result"]["items"][0]["created_at"] = UPDATED_AT  # type: ignore[index]
        reversed_timestamps["result"]["items"][0]["updated_at"] = CREATED_AT  # type: ignore[index]
        mutations.append(reversed_timestamps)

        mutations.append(envelope([company_payload()], next_cursor="x" * 513))

        for payload in mutations:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    CompanyPageEnvelope.model_validate(payload)

    def test_rejects_a_link_whose_company_endpoint_does_not_match_its_container(self) -> None:
        payload = envelope([company_payload()])
        payload["result"]["items"][0]["industry_links"][0]["company_id"] = OTHER_COMPANY_ID  # type: ignore[index]

        with self.assertRaises(ValidationError):
            CompanyPageEnvelope.model_validate(payload)


class CompanyDataClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_loads_every_page_with_auth_and_freezes_one_snapshot(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.params.get("cursor") is None:
                return httpx.Response(200, json=envelope([company_payload()], next_cursor="opaque-page-2"))
            second = company_payload(company_id=OTHER_COMPANY_ID, code="000002.SZ")
            second["industry_links"] = []
            return httpx.Response(
                200,
                json=envelope(
                    [second],
                    next_cursor=None,
                ),
            )

        facts = await load_facts(runtime_config(), transport=httpx.MockTransport(handler))

        self.assertEqual(facts.schema_version, COMPANY_PROJECTION_SCHEMA_VERSION)
        self.assertEqual(facts.snapshot_id, SNAPSHOT_ID)
        self.assertEqual([item.id for item in facts.companies], [COMPANY_ID, OTHER_COMPANY_ID])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].headers["authorization"], "Bearer service-token")
        self.assertEqual(requests[0].url.params["page_size"], "100")
        self.assertEqual(requests[1].url.params["cursor"], "opaque-page-2")

    async def test_rejects_snapshot_drift_repeated_cursors_duplicates_and_partial_contracts(self) -> None:
        cases: list[tuple[str, list[dict[str, object]]]] = [
            (
                "snapshot",
                [
                    envelope([company_payload()], next_cursor="page-2"),
                    envelope([], snapshot_id="b" * 64),
                ],
            ),
            (
                "cursor",
                [
                    envelope([company_payload()], next_cursor="page-2"),
                    envelope(
                        [
                            {
                                **company_payload(company_id=OTHER_COMPANY_ID, code="000002.SZ"),
                                "industry_links": [],
                            }
                        ],
                        next_cursor="page-2",
                    ),
                ],
            ),
            (
                "Company ID",
                [
                    envelope([company_payload()], next_cursor="page-2"),
                    envelope([company_payload()]),
                ],
            ),
        ]

        duplicate_link = company_payload(company_id=OTHER_COMPANY_ID, code="000002.SZ")
        duplicate_link["industry_links"][0]["company_id"] = OTHER_COMPANY_ID  # type: ignore[index]
        cases.append(
            (
                "CompanyIndustryLink ID",
                [envelope([company_payload(), duplicate_link])],
            )
        )

        for expected, pages in cases:
            index = 0

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal index
                del request
                page = pages[index]
                index += 1
                return httpx.Response(200, json=page)

            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ProjectionError, expected):
                    await load_facts(runtime_config(), transport=httpx.MockTransport(handler))

    async def test_maps_snapshot_conflict_without_exposing_the_service_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                409,
                json={"error": {"code": "COMPANY_PROJECTION_SNAPSHOT_CHANGED", "message": "changed"}},
            )

        with self.assertRaisesRegex(ProjectionError, "snapshot changed") as caught:
            await load_facts(runtime_config(), transport=httpx.MockTransport(handler))

        self.assertNotIn("service-token", str(caught.exception))


class CompanyOntologyAndPlanTest(unittest.TestCase):
    def test_registers_company_without_enabling_event_extraction(self) -> None:
        self.assertEqual(ONTOLOGY_VERSION, "reasoning-ontology/v5")
        self.assertIs(ENTITY_TYPES["Company"].__name__, "Company")
        self.assertEqual(
            EDGE_TYPE_MAP[("Company", "Industry")],
            ["CompanyBelongsToIndustry", "CompanyOperatesInIndustry"],
        )
        self.assertEqual(
            EDGE_TYPE_MAP[("Company", "ChainNode")],
            ["CompanyParticipatesInChainNode"],
        )
        self.assertEqual(
            set(EDGE_TYPES).intersection(
                {
                    "CompanyBelongsToIndustry",
                    "CompanyOperatesInIndustry",
                    "CompanyParticipatesInChainNode",
                }
            ),
            {
                "CompanyBelongsToIndustry",
                "CompanyOperatesInIndustry",
                "CompanyParticipatesInChainNode",
            },
        )
        self.assertNotIn("Company", event_entity_types())

    def test_keeps_formal_and_inferred_relation_provenance_distinct(self) -> None:
        formal = CompanyBelongsToIndustry(
            data_object_id=LINK_ID,
            source_company_id=COMPANY_ID,
            target_data_object_id=INDUSTRY_ID,
            projection_fingerprint="a" * 64,
            source_record_created_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        inferred = CompanyOperatesInIndustry(
            derivation_type="MODEL_INFERRED",
            decision_id="d" * 64,
            source_company_id=COMPANY_ID,
            target_data_object_id=INDUSTRY_ID,
            projection_fingerprint="e" * 64,
            confidence="HIGH",
            rationale="企业名称与主营范围直接对应该行业。",
            source_company_fingerprint="b" * 64,
            target_catalog_fingerprint="c" * 64,
            model_id="deepseek-v4",
            prompt_contract_version="company-relation-inference.v1",
            ontology_version="reasoning-ontology/v5",
            policy_version="company-projection-policy/v1",
            supporting_company_fields=["name"],
            source_industry_ids=[],
            decided_at=datetime(2026, 8, 30, tzinfo=UTC),
        )
        chain = CompanyParticipatesInChainNode(
            derivation_type="MODEL_INFERRED",
            decision_id="f" * 64,
            source_company_id=COMPANY_ID,
            target_data_object_id=CHAIN_NODE_ID,
            projection_fingerprint="1" * 64,
            confidence="MEDIUM",
            rationale="候选节点对应公司的主要生产环节。",
            source_company_fingerprint="b" * 64,
            target_catalog_fingerprint="c" * 64,
            model_id="deepseek-v4",
            prompt_contract_version="company-relation-inference.v1",
            ontology_version="reasoning-ontology/v5",
            policy_version="company-projection-policy/v1",
            supporting_company_fields=["name"],
            source_industry_ids=[INDUSTRY_ID],
            industry_chain_ids=["ICH77777777-7777-4777-8777-777777777777"],
            decided_at=datetime(2026, 8, 30, tzinfo=UTC),
        )

        self.assertEqual(formal.data_object_id, LINK_ID)
        self.assertFalse(hasattr(formal, "derivation_type"))
        self.assertEqual(inferred.derivation_type, "MODEL_INFERRED")
        self.assertEqual(chain.source_industry_ids, [INDUSTRY_ID])

        invalid_payload = inferred.model_dump()
        invalid_payload["confidence"] = "LOW"
        with self.assertRaises(ValidationError):
            CompanyOperatesInIndustry.model_validate(invalid_payload)
        with self.assertRaises(ValidationError):
            CompanyParticipatesInChainNode.model_validate({**chain.model_dump(), "source_industry_ids": []})
        with self.assertRaises(ValidationError):
            CompanyOperatesInIndustry.model_validate({**inferred.model_dump(), "target_data_object_id": CHAIN_NODE_ID})
        with self.assertRaises(ValidationError):
            CompanyParticipatesInChainNode.model_validate({**chain.model_dump(), "target_data_object_id": INDUSTRY_ID})

    def test_builds_deterministic_company_nodes_and_only_formal_industry_edges(self) -> None:
        facts = CompanyFacts(
            schema_version=COMPANY_PROJECTION_SCHEMA_VERSION,
            snapshot_id=SNAPSHOT_ID,
            companies=(CompanyPageEnvelope.model_validate(envelope([company_payload()])).result.items[0],),
        )

        plan = build_plan(facts)

        self.assertEqual(plan.company_count, 1)
        self.assertEqual(plan.formal_industry_relation_count, 1)
        self.assertEqual(plan.formal_industry_ids, frozenset({INDUSTRY_ID}))
        self.assertEqual(plan.nodes[0].uuid, node_uuid(COMPANY_ID))
        self.assertEqual(plan.nodes[0].labels, ["Company"])
        self.assertEqual(plan.nodes[0].attributes["data_object_id"], COMPANY_ID)
        self.assertEqual(
            plan.formal_industry_edges[0].uuid,
            edge_uuid("CompanyBelongsToIndustry", COMPANY_ID, INDUSTRY_ID),
        )
        self.assertEqual(plan.formal_industry_edges[0].source_node_uuid, node_uuid(COMPANY_ID))
        self.assertEqual(plan.formal_industry_edges[0].target_node_uuid, node_uuid(INDUSTRY_ID))
        self.assertEqual(plan.formal_industry_edges[0].attributes["data_object_id"], LINK_ID)
        self.assertEqual(plan.formal_industry_edges[0].attributes["source_record_created_at"], CREATED_AT)

    def test_build_plan_rejects_duplicate_company_and_formal_link_identity(self) -> None:
        item = CompanyPageEnvelope.model_validate(envelope([company_payload()])).result.items[0]
        duplicate = item.model_copy(update={"code": "000002.SZ"})

        with self.assertRaisesRegex(ProjectionError, "duplicate Company ID"):
            build_plan(
                CompanyFacts(
                    schema_version=COMPANY_PROJECTION_SCHEMA_VERSION,
                    snapshot_id=SNAPSHOT_ID,
                    companies=(item, duplicate),
                )
            )


class _Record:
    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def data(self) -> dict[str, object]:
        return self._data


class CompanyTargetPreflightTest(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_only_existing_exclusive_labels_and_deterministic_uuids(self) -> None:
        records = [
            _Record(
                {
                    "data_object_id": INDUSTRY_ID,
                    "uuid": node_uuid(INDUSTRY_ID),
                    "name": "工业设备",
                    "labels": ["Entity", "Industry"],
                }
            ),
            _Record(
                {
                    "data_object_id": CHAIN_NODE_ID,
                    "uuid": node_uuid(CHAIN_NODE_ID),
                    "name": "设备制造",
                    "labels": ["Entity", "ChainNode"],
                }
            ),
        ]
        graphiti = SimpleNamespace(
            driver=SimpleNamespace(execute_query=AsyncMock(return_value=SimpleNamespace(records=records)))
        )

        targets = await preflight_canonical_targets(
            graphiti,  # type: ignore[arg-type]
            {INDUSTRY_ID: "Industry", CHAIN_NODE_ID: "ChainNode"},
        )

        self.assertEqual(targets[INDUSTRY_ID].name, "工业设备")
        self.assertEqual(targets[CHAIN_NODE_ID].label, "ChainNode")

    async def test_rejects_missing_duplicate_wrong_label_and_noncanonical_targets(self) -> None:
        bad_records = [
            _Record(
                {
                    "data_object_id": INDUSTRY_ID,
                    "uuid": "wrong-uuid",
                    "name": "工业设备",
                    "labels": ["Entity", "Industry", "Contextual"],
                }
            )
        ]
        graphiti = SimpleNamespace(
            driver=SimpleNamespace(execute_query=AsyncMock(return_value=SimpleNamespace(records=bad_records)))
        )

        with self.assertRaisesRegex(ProjectionError, "canonical target preflight failed"):
            await preflight_canonical_targets(
                graphiti,  # type: ignore[arg-type]
                {INDUSTRY_ID: "Industry", CHAIN_NODE_ID: "ChainNode"},
            )

    async def test_malformed_graph_record_fails_as_a_projection_contract_error(self) -> None:
        graphiti = SimpleNamespace(
            driver=SimpleNamespace(
                execute_query=AsyncMock(
                    return_value=SimpleNamespace(
                        records=[
                            _Record(
                                {
                                    "data_object_id": INDUSTRY_ID,
                                    "uuid": node_uuid(INDUSTRY_ID),
                                    "name": "工业设备",
                                    "labels": None,
                                }
                            )
                        ]
                    )
                )
            )
        )

        with self.assertRaisesRegex(ProjectionError, "canonical target preflight failed"):
            await preflight_canonical_targets(
                graphiti,  # type: ignore[arg-type]
                {INDUSTRY_ID: "Industry"},
            )


if __name__ == "__main__":
    unittest.main()
