"""Public Data Service adapter behavior for Event publication."""

import json
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx

from sematica.ingestion.episcode.event.adapters import (
    DataEventClient,
    EventRecallUnavailable,
    GraphitiEventHistory,
    PublicationRejected,
)
from sematica.ingestion.episcode.event.contracts import EventCandidateDTO
from sematica.ingestion.episcode.event.provenance import EVENT_SOURCE_DESCRIPTION


@dataclass(frozen=True)
class _Submission:
    submission_id: str
    event: EventCandidateDTO
    evidence_ids: list[str]


class DataEventClientPublicationTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def submission() -> _Submission:
        return _Submission(
            submission_id="evt-workflow-candidate-key",
            event=EventCandidateDTO.model_validate(
                {
                    "title": "示例公司签署服务器订单",
                    "summary": "示例公司宣布签署服务器订单。",
                    "semantic": {
                        "actors": ["示例公司"],
                        "action": "签署",
                        "objects": ["服务器订单"],
                        "stage": "ANNOUNCED",
                        "modality": "FACT",
                        "time": {
                            "occurred_at": None,
                            "announced_at": "2026-08-25T00:00:00Z",
                            "effective_at": None,
                            "precision": "DAY",
                        },
                        "jurisdictions": ["中国"],
                        "reason": "扩建算力基础设施",
                        "method": "签署正式采购协议",
                        "metrics": [
                            {
                                "name": "订单金额",
                                "value": "10",
                                "unit": "亿元",
                                "change": None,
                                "period": None,
                            }
                        ],
                    },
                }
            ),
            evidence_ids=[
                "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15",
                "EVD15bec7e3-998c-5434-aa5d-29712c4c67cf",
            ],
        )

    async def test_only_payload_contract_rejections_are_permanent(self) -> None:
        expected_payload = {
            "publication_key": "evt-workflow-candidate-key:create",
            "event": {
                "title": "示例公司签署服务器订单",
                "summary": "示例公司宣布签署服务器订单。",
                "semantic": {
                    "actors": ["示例公司"],
                    "action": "签署",
                    "objects": ["服务器订单"],
                    "stage": "ANNOUNCED",
                    "modality": "FACT",
                    "time": {
                        "occurred_at": None,
                        "announced_at": "2026-08-25T00:00:00Z",
                        "effective_at": None,
                        "precision": "DAY",
                    },
                    "jurisdictions": ["中国"],
                    "reason": "扩建算力基础设施",
                    "method": "签署正式采购协议",
                    "metrics": [
                        {
                            "name": "订单金额",
                            "value": "10",
                            "unit": "亿元",
                            "change": None,
                            "period": None,
                        }
                    ],
                },
            },
            "evidence_ids": [
                "EVD15bec7e3-998c-5434-aa5d-29712c4c67cf",
                "EVD5cb71bef-5b1d-5995-add0-7408eaa2be15",
            ],
        }
        cases = (
            (422, PublicationRejected),
            (429, httpx.HTTPStatusError),
            (401, httpx.HTTPStatusError),
        )

        for status_code, expected_error in cases:
            with self.subTest(status_code=status_code):
                requests: list[dict[str, object]] = []

                async def respond(request: httpx.Request) -> httpx.Response:
                    requests.append(json.loads(request.content))
                    return httpx.Response(status_code, request=request)

                client = DataEventClient(
                    "http://data:9011",
                    "test-service-token",
                    transport=httpx.MockTransport(respond),
                )
                with self.assertRaises(expected_error):
                    await client.publish(self.submission())

                self.assertEqual(requests, [expected_payload])

    async def test_successful_publication_round_trips_complete_event_semantics(self) -> None:
        submission = self.submission()
        response_event = {
            "id": "EVT5cb71bef-5b1d-5995-add0-7408eaa2be15",
            "status": "ACTIVE",
            **submission.event.model_dump(mode="json"),
        }

        async def respond(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            return httpx.Response(
                201,
                request=request,
                json={
                    "request_id": "request-1",
                    "result": {
                        "event": response_event,
                        "evidence_link_ids": ["EVL1"],
                        "receipt_id": "receipt-1",
                        "payload_hash": "payload-hash-1",
                        "replayed": False,
                    },
                },
            )

        client = DataEventClient(
            "http://data:9011",
            "test-service-token",
            transport=httpx.MockTransport(respond),
        )

        published = await client.publish(submission)

        self.assertEqual(published.id, response_event["id"])
        self.assertEqual(published.event.model_dump(mode="json"), submission.event.model_dump(mode="json"))
        self.assertEqual(published.event.semantic.reason, "扩建算力基础设施")
        self.assertEqual(published.event.semantic.method, "签署正式采购协议")
        self.assertEqual([metric.name for metric in published.event.semantic.metrics], ["订单金额"])


class GraphitiEventHistoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_retrieves_event_history_without_a_data_history_client(self) -> None:
        candidate = DataEventClientPublicationTest.submission().event
        event_payload = {
            "id": "EVT5cb71bef-5b1d-5995-add0-7408eaa2be15",
            "status": "ACTIVE",
            **candidate.model_dump(mode="json"),
        }
        search_interface = MagicMock()
        search_interface.episode_fulltext_search = AsyncMock(
            return_value=[
                SimpleNamespace(
                    content=json.dumps(event_payload, ensure_ascii=False),
                    source_description=EVENT_SOURCE_DESCRIPTION,
                )
            ]
        )
        driver = MagicMock(search_interface=search_interface)
        driver.execute_query = AsyncMock(return_value=([], None, None))
        graphiti = MagicMock(driver=driver)

        result = await GraphitiEventHistory(graphiti).retrieve(candidate)

        self.assertEqual([event.id for event in result], [event_payload["id"]])
        self.assertEqual(result[0].event.semantic.reason, "扩建算力基础设施")

    async def test_fails_closed_when_all_graphiti_history_queries_fail(self) -> None:
        candidate = DataEventClientPublicationTest.submission().event
        search_interface = MagicMock()
        search_interface.episode_fulltext_search = AsyncMock(side_effect=RuntimeError("fulltext unavailable"))
        driver = MagicMock(search_interface=search_interface)
        driver.execute_query = AsyncMock(side_effect=RuntimeError("anchor query unavailable"))
        graphiti = MagicMock(driver=driver)

        with self.assertRaisesRegex(EventRecallUnavailable, "Graphiti Event recall failed"):
            await GraphitiEventHistory(graphiti).retrieve(candidate)

    async def test_fails_closed_when_either_graphiti_history_query_fails(self) -> None:
        candidate = DataEventClientPublicationTest.submission().event
        cases = (
            (RuntimeError("fulltext unavailable"), ([], None, None)),
            ([], RuntimeError("anchor query unavailable")),
        )
        for fulltext_result, anchor_result in cases:
            with self.subTest(fulltext_result=fulltext_result, anchor_result=anchor_result):
                search_interface = MagicMock()
                if isinstance(fulltext_result, Exception):
                    search_interface.episode_fulltext_search = AsyncMock(side_effect=fulltext_result)
                else:
                    search_interface.episode_fulltext_search = AsyncMock(return_value=fulltext_result)
                driver = MagicMock(search_interface=search_interface)
                if isinstance(anchor_result, Exception):
                    driver.execute_query = AsyncMock(side_effect=anchor_result)
                else:
                    driver.execute_query = AsyncMock(return_value=anchor_result)
                graphiti = MagicMock(driver=driver)

                with self.assertRaisesRegex(EventRecallUnavailable, "Graphiti Event recall failed"):
                    await GraphitiEventHistory(graphiti).retrieve(candidate)

    async def test_fails_closed_when_graphiti_matches_are_all_malformed(self) -> None:
        candidate = DataEventClientPublicationTest.submission().event
        search_interface = MagicMock()
        search_interface.episode_fulltext_search = AsyncMock(
            return_value=[
                SimpleNamespace(
                    content='{"id":"not-a-formal-event"}',
                    source_description=EVENT_SOURCE_DESCRIPTION,
                )
            ]
        )
        driver = MagicMock(search_interface=search_interface)
        driver.execute_query = AsyncMock(return_value=([], None, None))
        graphiti = MagicMock(driver=driver)

        with self.assertRaisesRegex(EventRecallUnavailable, "only malformed Event content"):
            await GraphitiEventHistory(graphiti).retrieve(candidate)

    async def test_isolates_one_malformed_graphiti_match_when_valid_history_exists(self) -> None:
        candidate = DataEventClientPublicationTest.submission().event
        event_payload = {
            "id": "EVT5cb71bef-5b1d-5995-add0-7408eaa2be15",
            "status": "ACTIVE",
            **candidate.model_dump(mode="json"),
        }
        search_interface = MagicMock()
        search_interface.episode_fulltext_search = AsyncMock(
            return_value=[
                SimpleNamespace(content="not-json", source_description=EVENT_SOURCE_DESCRIPTION),
                SimpleNamespace(
                    content=json.dumps(event_payload, ensure_ascii=False),
                    source_description=EVENT_SOURCE_DESCRIPTION,
                ),
            ]
        )
        driver = MagicMock(search_interface=search_interface)
        driver.execute_query = AsyncMock(return_value=([], None, None))
        graphiti = MagicMock(driver=driver)

        result = await GraphitiEventHistory(graphiti).retrieve(candidate)

        self.assertEqual([event.id for event in result], [event_payload["id"]])


if __name__ == "__main__":
    unittest.main()
