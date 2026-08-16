"""Tests for the strict Data Service Evidence publication client."""

import json
import os
import unittest
from unittest.mock import patch

from capabilities.evidence.internal.client import get_evidence_categories, post_publication


class _Response:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


class EvidencePublicationClientTest(unittest.TestCase):
    def test_gets_category_catalog_from_strict_success_envelope(self) -> None:
        payload = {
            "request_id": "data-test",
            "result": {
                "categories": [
                    {
                        "id": "EVC15bec7e3-998c-5434-aa5d-29712c4c67cf",
                        "code": "EVENT_BRIEF",
                        "name": "事件快讯",
                        "description": "事件发生后的简要事实更新。",
                    }
                ]
            },
        }
        with (
            patch.dict(
                os.environ,
                {
                    "DATA_SERVICE_BASE_URL": "http://data.test:9011/",
                    "DATA_SERVICE_TOKEN": "test-token",
                },
            ),
            patch("capabilities.evidence.internal.client.urlopen", return_value=_Response(200, payload)) as mocked,
        ):
            result = get_evidence_categories()

        self.assertEqual(result, payload["result"])
        request = mocked.call_args.args[0]
        self.assertEqual(request.full_url, "http://data.test:9011/api/data/v1/evidence-categories")
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.headers["Authorization"], "Bearer test-token")

    def test_rejects_category_catalog_without_strict_envelope(self) -> None:
        with (
            patch.dict(os.environ, {"DATA_SERVICE_TOKEN": "test-token"}),
            patch(
                "capabilities.evidence.internal.client.urlopen",
                return_value=_Response(200, {"request_id": "data-test", "result": []}),
            ),
            self.assertRaisesRegex(ValueError, "invalid success envelope"),
        ):
            get_evidence_categories()

    def test_returns_result_from_success_envelope(self) -> None:
        payload = {
            "request_id": "data-test",
            "result": {"id": "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"},
        }
        with (
            patch.dict(os.environ, {"DATA_SERVICE_TOKEN": "test-token"}),
            patch("capabilities.evidence.internal.client.urlopen", return_value=_Response(201, payload)),
        ):
            result = post_publication("raw-evidence-publications", {"raw_evidence": {}})

        self.assertEqual(result, payload["result"])

    def test_rejects_success_without_strict_envelope(self) -> None:
        for payload in (
            {"id": "RAW15bec7e3-998c-5434-aa5d-29712c4c67cf"},
            {"request_id": "data-test"},
            {"request_id": "", "result": {}},
        ):
            with self.subTest(payload=payload):
                with (
                    patch.dict(os.environ, {"DATA_SERVICE_TOKEN": "test-token"}),
                    patch("capabilities.evidence.internal.client.urlopen", return_value=_Response(201, payload)),
                    self.assertRaisesRegex(ValueError, "invalid success envelope"),
                ):
                    post_publication("raw-evidence-publications", {"raw_evidence": {}})


if __name__ == "__main__":
    unittest.main()
