"""Window, remote API and recovery contracts without models or external services."""

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from agno.run import RunContext
from agno.workflow import StepInput

from capabilities.geopolitical_research.functions import (
    geopolitical_research_complete,
    research_next_geopolitical_story,
    select_geopolitical_stories,
)
from capabilities.geopolitical_research.internal.client import ResearchClient
from capabilities.geopolitical_research.internal.execution import job_key, research_story, user_variables
from capabilities.geopolitical_research.internal.models import ResearchPlan, ResearchStory
from capabilities.geopolitical_research.internal.selection import request, select_stories
from capabilities.geopolitical_research.internal.storage import ResearchBusy, digest, lock, root, run_root
from sematica.graphiti.geopolitical_research import WINDOW_QUERY

END = datetime(2026, 9, 15, 1, 30, tzinfo=UTC)
START = END - timedelta(hours=24)
REPORT = "# 地缘冲突报告\n\n完整结论与来源：中文。\n" * 1200


def row(sid="GPR-1", eid="EVT-1", created=START):
    return {
        "story": {"uuid": "uuid-" + sid, "data_object_id": sid, "name": "美伊冲突", "core_proposition": "能源传导"},
        "event": {
            "uuid": "uuid-" + eid,
            "domain_object_id": eid,
            "created_at": created.isoformat(),
            "content": json.dumps({"id": eid, "summary": "新事件", "semantic": {"action": "行动"}}),
        },
    }


def plan(run_id="run-1", count=1):
    return ResearchPlan(
        workflow_run_id=run_id,
        start_at=START,
        cutoff_at=END,
        market="A股市场",
        stories=select_stories([row(f"GPR-{i}", f"EVT-{i}") for i in range(count)], START, END),
    )


class FakeResearch:
    def __init__(self):
        self.creates = []
        self.gets = []
        self.requests = {}
        self.statuses = ["completed"]
        self.fail_create = False
        self.wrong_identity = False
        self.report = REPORT

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            data = json.loads(req.content)
            self.creates.append(data)
            self.assert_preset(data)
            if self.fail_create:
                raise httpx.ReadTimeout("contains-secret-provider-details", request=req)
            rid = f"swarm-{len(self.creates)}"
            self.requests[rid] = data["user_vars"]
            return httpx.Response(200, json={"id": rid, "status": "pending", "preset_name": "geopolitical_war_room"})
        rid = req.url.path.rsplit("/", 1)[-1]
        self.gets.append(rid)
        status = self.statuses[0]
        if len(self.statuses) > 1:
            self.statuses.pop(0)
        return httpx.Response(
            200,
            json={
                "id": rid,
                "preset_name": "geopolitical_war_room",
                "status": status,
                "user_vars": {} if self.wrong_identity else self.requests[rid],
                "final_report": self.report,
            },
        )

    @staticmethod
    def assert_preset(data):
        assert data["preset_name"] == "geopolitical_war_room"

    def client(self):
        return ResearchClient(transport=httpx.MockTransport(self.handle))


class SelectionTests(unittest.TestCase):
    def test_deduplicates_per_story_and_keeps_shared_events(self):
        rows = [row(), row(), row(eid="EVT-2"), row(sid="GPR-2")]
        stories = select_stories(rows, START, END)
        self.assertEqual([s.story_id for s in stories], ["GPR-1", "GPR-2"])
        self.assertEqual([len(s.events) for s in stories], [2, 1])

    def test_creation_window_is_half_open_and_not_event_occurrence_time(self):
        self.assertEqual(len(select_stories([row(created=START)], START, END)), 1)
        for timestamp in [START - timedelta(microseconds=1), END]:
            with self.assertRaises(ValueError):
                select_stories([row(created=timestamp)], START, END)
        self.assertIn("e.created_at >= datetime($start)", WINDOW_QUERY)
        self.assertIn("e.created_at < datetime($end)", WINDOW_QUERY)
        self.assertIn("GeopoliticRivalry {group_id:$group}", WINDOW_QUERY)

    def test_rejects_ambiguous_or_untraceable_input(self):
        bad = row()
        bad["event"]["content"] = '{"id":"wrong"}'
        with self.assertRaises(ValueError):
            select_stories([bad], START, END)
        bad = row()
        bad["story"]["uuid"] = "another-node"
        with self.assertRaises(ValueError):
            select_stories([row(), bad], START, END)

    def test_old_schedule_cannot_restore_48_hour_reasoning(self):
        parsed = request('{"question":"旧命题","event_window_hours":48,"include_company":false}')
        self.assertIsNone(parsed.cutoff_at)
        self.assertEqual(parsed.market, "A股市场")
        self.assertEqual(request("旧的自然语言命题"), parsed)
        with self.assertRaises(ValueError):
            request({"cutoff_at": "2026-09-15T07:30:00"})

    def test_name_only_scope_crosses_midnight_and_job_identity_ignores_run_id(self):
        p = plan()
        variables = user_variables(p, p.stories[0])
        self.assertEqual(START.isoformat(), variables["event_window_start"])
        self.assertEqual(END.isoformat(), variables["event_window_end"])
        self.assertEqual(p.stories[0].name, variables["crisis"])
        self.assertNotIn("EVT-0", json.dumps(variables))
        self.assertEqual(variables["research_date"], "")
        other = p.model_copy(update={"workflow_run_id": "next-run"})
        self.assertEqual(job_key(p, p.stories[0]), job_key(other, other.stories[0]))
        shifted = p.model_copy(update={"cutoff_at": END + timedelta(minutes=1)})
        self.assertNotEqual(job_key(p, p.stories[0]), job_key(shifted, shifted.stories[0]))
        added = ResearchStory.model_validate({**p.stories[0].model_dump(), "events": [{"id": "EVT-new"}]})
        self.assertNotEqual(job_key(p, p.stories[0]), job_key(p, added))


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "GEOPOLITICAL_RESEARCH_ARTIFACT_ROOT": self.tmp.name,
                "TIDEWISE_RESEARCH_BASE_URL": "http://research.test",
                "TIDEWISE_RESEARCH_POLL_SECONDS": "0.001",
                "TIDEWISE_RESEARCH_WAIT_SECONDS": "1",
            },
        )
        self.env.start()
        self.fake = FakeResearch()
        self.factory = patch("capabilities.geopolitical_research.internal.execution.ResearchClient", self.fake.client)
        self.factory.start()

    def tearDown(self):
        self.factory.stop()
        self.env.stop()
        self.tmp.cleanup()

    async def test_complete_report_is_byte_exact_and_same_input_reuses_report(self):
        p = plan()
        self.fake.statuses = ["running", "completed"]
        result = await research_story(p, p.stories[0])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(Path(result["report_path"]).read_bytes(), REPORT.encode())
        self.assertEqual(result["report_sha256"], digest(REPORT))
        self.assertEqual(result["report_bytes"], len(REPORT.encode()))
        replay = await research_story(plan("another-run"), p.stories[0])
        self.assertTrue(replay["reused"])
        self.assertEqual(len(self.fake.creates), 1)
        self.assertEqual(len(self.fake.gets), 2)
        Path(result["report_path"]).write_text("changed")
        with self.assertRaises(ValueError):
            await research_story(p, p.stories[0])

    async def test_crlf_and_whitespace_are_preserved_on_replay(self):
        p = plan()
        self.fake.report = "  # Report\r\n正文\r\n  "
        result = await research_story(p, p.stories[0])
        self.assertEqual(Path(result["report_path"]).read_bytes(), self.fake.report.encode())
        replay = await research_story(p, p.stories[0])
        self.assertEqual(replay["status"], "completed")

    async def test_timeout_resumes_known_run_without_post(self):
        p = plan()
        self.fake.statuses = ["running"]
        with patch.dict(os.environ, {"TIDEWISE_RESEARCH_WAIT_SECONDS": "0.01"}):
            first = await research_story(p, p.stories[0])
        self.assertEqual(first["status"], "running")
        self.assertEqual(first["error_code"], "research_wait_interrupted")
        self.fake.statuses = ["completed"]
        second = await research_story(p, p.stories[0])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(len(self.fake.creates), 1)

    async def test_indeterminate_post_never_automatically_retries(self):
        p = plan()
        self.fake.fail_create = True
        first = await research_story(p, p.stories[0])
        second = await research_story(p, p.stories[0])
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(second["status"], "unknown")
        self.assertNotIn("contains-secret", json.dumps(first))
        self.assertEqual(len(self.fake.creates), 1)

    async def test_wrong_identity_or_empty_report_is_not_published(self):
        for index, mode in enumerate(["identity", "empty"]):
            p = plan(count=index + 1)
            self.fake.wrong_identity = mode == "identity"
            self.fake.report = "" if mode == "empty" else REPORT
            result = await research_story(p, p.stories[index])
            self.assertEqual(result["status"], "failed")
            self.assertIsNone(result["report_path"])
            self.assertEqual(result["error_code"], "research_contract_rejected")

    async def test_concurrent_story_dispatch_is_exclusive(self):
        p = plan()
        with lock(root() / "jobs" / job_key(p, p.stories[0]) / ".lock"):
            with self.assertRaises(ResearchBusy):
                await research_story(p, p.stories[0])
        self.assertEqual(self.fake.creates, [])

    async def test_loop_continues_after_remote_failure_and_records_partial(self):
        ctx = RunContext(run_id="run-1", session_id="session")
        source = AsyncMock(return_value=[row("GPR-0", "EVT-0"), row("GPR-1", "EVT-1")])
        with patch("capabilities.geopolitical_research.internal.selection.load_geopolitical_event_window", source):
            await select_geopolitical_stories(StepInput(input={"cutoff_at": END.isoformat()}), ctx)
            await select_geopolitical_stories(StepInput(input={"cutoff_at": END.isoformat()}), ctx)
        source.assert_awaited_once()
        self.fake.statuses = ["failed", "completed"]
        first = await research_next_geopolitical_story(StepInput(), ctx)
        self.assertFalse(geopolitical_research_complete([first]))
        second = await research_next_geopolitical_story(StepInput(), ctx)
        self.assertTrue(geopolitical_research_complete([second]))
        assert isinstance(second.content, dict)
        self.assertEqual(second.content["outcome"], "partial")
        self.assertEqual(second.content["completed"], 1)
        self.assertEqual(second.content["unresolved"], 1)
        self.assertEqual(len(self.fake.creates), 2)
        replay = await research_next_geopolitical_story(StepInput(), ctx)
        self.assertEqual(replay.content, second.content)

    async def test_zero_events_does_not_need_research_configuration(self):
        ctx = RunContext(run_id="empty", session_id="session")
        with (
            patch(
                "capabilities.geopolitical_research.internal.selection.load_geopolitical_event_window",
                AsyncMock(return_value=[]),
            ),
            patch.dict(os.environ, {"TIDEWISE_RESEARCH_BASE_URL": ""}),
        ):
            await select_geopolitical_stories(StepInput(input={"cutoff_at": END.isoformat()}), ctx)
            result = await research_next_geopolitical_story(StepInput(), ctx)
        assert isinstance(result.content, dict)
        self.assertEqual(result.content["outcome"], "no_change")
        self.assertEqual(result.content["items"], [])
        self.assertEqual(self.fake.creates, [])
        self.assertTrue((run_root("empty") / "plan.json").exists())

    async def test_corrupt_loop_checkpoint_cannot_skip_stories(self):
        ctx = RunContext(run_id="corrupt", session_id="session")
        with patch(
            "capabilities.geopolitical_research.internal.selection.load_geopolitical_event_window",
            AsyncMock(return_value=[row()]),
        ):
            await select_geopolitical_stories(StepInput(input={"cutoff_at": END.isoformat()}), ctx)
        (run_root("corrupt") / "result.json").write_text(
            json.dumps(
                {
                    "workflow_run_id": "corrupt",
                    "items": [{"story_id": "GPR-other", "status": "completed"}],
                }
            )
        )
        with self.assertRaises(ValueError):
            await research_next_geopolitical_story(StepInput(), ctx)
        self.assertEqual(self.fake.creates, [])

    async def test_crash_after_remote_report_resumes_via_saved_receipt(self):
        p = plan()
        result = await research_story(p, p.stories[0])
        # No Workflow result checkpoint yet: the same story is selected after a crash.
        replay = await research_story(p, p.stories[0])
        self.assertEqual(replay["report_sha256"], result["report_sha256"])
        self.assertEqual(len(self.fake.creates), 1)


if __name__ == "__main__":
    unittest.main()
