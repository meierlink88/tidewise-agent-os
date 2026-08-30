"""Public Data Service adapter behavior for Event publication."""

import json
import unittest
from dataclasses import dataclass

import httpx

from sematica.ingestion.episcode.event.adapters import DataEventClient, PublicationRejected
from sematica.ingestion.episcode.event.contracts import EventCandidateDTO


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
                        "jurisdictions": ["中国"],
                        "effective_at": None,
                        "time_precision": "DAY",
                    },
                    "modality": "FACT",
                    "occurred_at": None,
                    "announced_at": "2026-08-25T00:00:00Z",
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
                    "jurisdictions": ["中国"],
                    "effective_at": None,
                    "time_precision": "DAY",
                },
                "modality": "FACT",
                "occurred_at": None,
                "announced_at": "2026-08-25T00:00:00Z",
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


if __name__ == "__main__":
    unittest.main()
