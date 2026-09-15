"""Readback paging/cache tests against the provider's current response shape."""

import os
import unittest
from typing import Any
from unittest.mock import patch

import readback
import test_workflow as fixtures
import workflow as w


class ReadbackTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkflowTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_capture_resumes_with_bounded_calls(self):
        self.fixture.assembled()
        self.fixture.state["publication"] = {"status": "published_unverified", "report_id": "RPT1", "replayed": True}
        calls = []

        def fake(method, base, route, token):
            result: dict[str, Any]
            calls.append(route)
            if route.endswith("/home"):
                result = {"schema_version": w.WIRE}
            elif "/evidences?" in route:
                result = {"report_id": "RPT1", "scope_token": "scope1", "items": [{"summary": "测试事实"}]}
            elif "/macroeconomic_stories/m" in route:
                result = {"local_key": "m", "reasonings": [], "evidence_scope_token": "scope1", "evidence_count": 1}
            else:
                items = [{"local_key": "m"}] if "/macroeconomic_stories?" in route else []
                result = {"items": items, "next_cursor": None}
            return 200, {"result": result}

        with patch.dict(os.environ, {"DATA_SERVICE_BEARER_TOKEN": "synthetic"}):
            first = readback.capture(self.fixture.root, self.fixture.state, 2, fake)
            self.assertFalse(first["capture_complete"])
            self.assertEqual(first["new_requests"], 2)
            final = readback.capture(self.fixture.root, self.fixture.state, 20, fake)
            self.assertTrue(final["capture_complete"])
            self.assertEqual(len(calls), len(set(calls)))
            cached = readback.capture(self.fixture.root, self.fixture.state, 20, fake)
            self.assertEqual(cached["new_requests"], 0)

    def test_readback_wrong_order_is_not_success(self):
        self.fixture.assembled()
        self.fixture.state["publication"] = {"status": "published_unverified", "report_id": "RPT1"}

        def fake(method, base, route, token):
            return 200, {
                "result": {"schema_version": w.WIRE}
                if route.endswith("/home")
                else {"items": [{"local_key": "wrong"}], "next_cursor": None}
            }

        with patch.dict(os.environ, {"DATA_SERVICE_BEARER_TOKEN": "synthetic"}):
            with self.assertRaisesRegex(ValueError, "order_or_count"):
                readback.capture(self.fixture.root, self.fixture.state, 20, fake)
