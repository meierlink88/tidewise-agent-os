"""Tests for the strict Reasoning Server Event Candidate client."""

import json
import os
import unittest
from unittest.mock import patch

from capabilities.event.internal.client import post_event_candidate


class _Response:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class EventCandidateClientTest(unittest.TestCase):
    def test_posts_one_candidate_with_bearer_auth_and_three_second_timeout(self) -> None:
        acceptance = {
            "submission_id": "evt-submission-1",
            "status": "ACCEPTED",
            "status_url": "/api/reason/v1/event-candidates/evt-submission-1",
            "replayed": False,
        }
        payload = {"event": {"title": "示例事件"}, "evidence_ids": ["EVD-example"]}
        with (
            patch.dict(
                os.environ,
                {
                    "REASON_SERVICE_BASE_URL": "http://reason.test:8890/",
                    "REASON_SERVICE_TOKEN": "secret-token",
                },
            ),
            patch(
                "capabilities.event.internal.client.urlopen",
                return_value=_Response(202, acceptance),
            ) as opened,
        ):
            result = post_event_candidate(payload)

        self.assertEqual(result, acceptance)
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "http://reason.test:8890/api/reason/v1/event-candidates")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["Authorization"], "Bearer secret-token")
        self.assertEqual(opened.call_args.kwargs["timeout"], 3)

    def test_requires_service_token_before_network_access(self) -> None:
        with (
            patch.dict(os.environ, {"REASON_SERVICE_TOKEN": ""}),
            patch("capabilities.event.internal.client.urlopen") as opened,
            self.assertRaisesRegex(ValueError, "REASON_SERVICE_TOKEN"),
        ):
            post_event_candidate({})
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
