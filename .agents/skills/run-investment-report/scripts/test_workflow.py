"""Offline behavioral checks: synthetic inputs and injected HTTP, never live services."""

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import workflow as w


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "run"
        self.method = self.base / "method.md"
        self.method.write_text("method")
        self.scope = {
            "environment": "synthetic",
            "start": "2020-01-01T00:00:00Z",
            "end": "2020-01-02T00:00:00Z",
            "timezone": "Asia/Shanghai",
            "market": "中国A股、港股及全球相关资产",
            "research_base_url": "http://research.invalid",
            "data_base_url": "http://data.invalid",
            "source_identity": {"hostname": "fake"},
            "method_files": [str(self.method)],
        }
        self.snapshot = {
            "selection_time_field": "created_at",
            "source_kind": "synthetic",
            "window": {"start": self.scope["start"], "end": self.scope["end"]},
            "events": [
                {"id": "EVT1", "created_at": self.scope["start"], "classes": ["GEOPOLITICAL"]},
                {"id": "EVT2", "created_at": self.scope["end"], "classes": ["COMPANY"]},
            ],
            "signals": [],
            "entities": {"GPR1": {"id": "GPR1", "name": "测试故事线", "type": "GeopoliticRivalry"}},
            "structure": [],
            "evidences": {},
        }
        w.write(self.base / "scope.json", self.scope)
        w.write(self.base / "snapshot.json", self.snapshot)
        self.state = w.initialize(self.root, self.base / "scope.json", self.base / "snapshot.json")

    def tearDown(self):
        self.temp.cleanup()

    def selection(self, research=True):
        s = {
            "snapshot_sha256": self.state["snapshot"]["sha256"],
            "event_reviews": [{"event_id": "EVT1", "story_ids": ["GPR1"] if research else [], "reason": "测试判断"}],
            "stories": [
                {
                    "story_id": "GPR1",
                    "name": "测试故事线",
                    "event_ids": ["EVT1"],
                    "decision": "research",
                    "reason": "本期变化",
                }
            ]
            if research
            else [],
        }
        w.write(self.base / "selection.json", s)
        w.select(self.root, self.state, self.base / "selection.json")

    def report(self, lane):
        report: dict[str, Any] = {
            "schema_version": w.WIRE,
            "report_type": {"code": "investment_reasoning", "label": "投研推理报告"},
            "generated_at": "2020-01-02T00:00:00Z",
            "timezone": self.scope["timezone"],
            "analysis_window": {"start": self.scope["start"], "end": self.scope["end"]},
            "company_analyses": [],
            "observations": [],
            "limitations": [],
            **{k: [] for k in w.KINDS},
        }
        if lane == "macroeconomics":
            report["macroeconomic_stories"] = [
                {
                    "local_key": "m",
                    "source_id": "MEC1",
                    "title": "宏观",
                    "summary": {
                        "conclusion": "原始结论",
                        "affected_refs": [{"reasoning_local_key": "r", "local_key": "a"}],
                    },
                    "detail": {
                        "reasonings": [
                            {
                                "local_key": "r",
                                "assessment": {"conclusion": "推理"},
                                "reasoning_summary": {"logic": "原始机制"},
                                "affected_assets": [{"local_key": "a"}],
                            }
                        ]
                    },
                }
            ]
        return report

    def pack(self, lane, report=None):
        f = self.base / (lane + ".json")
        w.write(f, report or self.report(lane))
        review = self.base / (lane + "-review.json")
        w.write(
            review, {"status": "passed", "artifact_sha256": w.sha(f.read_bytes()), "findings": [], "reviewer": "test"}
        )
        w.lane_pack(self.root, self.state, lane, f, review)

    def assembled(self):
        self.selection(False)
        for lane in w.LANES:
            self.pack(lane)
        return w.assemble(self.root, self.state)

    def test_half_open_window(self):
        self.assertEqual([e["id"] for e in w.artifact(self.root, self.state["snapshot"])["events"]], ["EVT1"])

    def test_incomplete_review_and_story_identity_rejected(self):
        bad = {"snapshot_sha256": self.state["snapshot"]["sha256"], "event_reviews": [], "stories": []}
        w.write(self.base / "bad.json", bad)
        with self.assertRaisesRegex(ValueError, "coverage"):
            w.select(self.root, self.state, self.base / "bad.json")
        bad["event_reviews"] = [{"event_id": "EVT1", "story_ids": ["GPRmissing"], "reason": "test"}]
        w.write(self.base / "bad.json", bad)
        with self.assertRaisesRegex(ValueError, "unknown_story"):
            w.select(self.root, self.state, self.base / "bad.json")

    def test_post_unknown_is_not_retried(self):
        self.selection()
        calls = []

        def fake(*args):
            calls.append(args)
            raise ValueError("timeout")

        with self.assertRaisesRegex(ValueError, "dispatch_unknown"):
            w.research(self.root, self.state, "GPR1", transport=fake)
        with self.assertRaisesRegex(ValueError, "do_not_repeat"):
            w.research(self.root, self.state, "GPR1", transport=fake)
        self.assertEqual(len(calls), 1)
        self.assertEqual(w.read(self.root / "state.json")["research"]["GPR1"]["status"], "unknown")

    def test_research_identity_and_exact_body(self):
        self.selection()
        bodies = []

        def submit(*args):
            bodies.append(w.parse(args[4]))
            return 200, {"id": "run1", "preset_name": w.PRESET}

        w.research(self.root, self.state, "GPR1", transport=submit)
        self.assertEqual(bodies[0]["user_vars"]["crisis"], "测试故事线")
        self.assertEqual(bodies[0]["user_vars"]["market"], "中国A股市场")
        self.assertEqual(w.artifact(self.root, self.state["scope"])["market"], self.scope["market"])
        self.assertEqual(bodies[0]["user_vars"]["story_id"], "GPR1")
        self.assertEqual(bodies[0]["user_vars"]["event_window_start"], self.scope["start"])
        self.assertEqual(bodies[0]["user_vars"]["event_window_end"], self.scope["end"])
        self.assertNotIn("events", bodies[0]["user_vars"])
        detail = {
            "id": "run1",
            "preset_name": w.PRESET,
            "user_vars": bodies[0]["user_vars"],
            "status": "completed",
            "final_report": "# 原始\n\n",
        }
        wrong = {**detail, "id": "other"}
        with self.assertRaisesRegex(ValueError, "identity"):
            w.research(self.root, self.state, "GPR1", True, lambda *a: (200, wrong))
        w.research(self.root, self.state, "GPR1", True, lambda *a: (200, detail))
        self.assertEqual((self.root / "research/GPR1/report.md").read_bytes(), detail["final_report"].encode())
        with self.assertRaisesRegex(ValueError, "changed"):
            w.research(self.root, self.state, "GPR1", True, lambda *a: (200, {**detail, "final_report": "new"}))

    def test_stage_barriers_and_legacy_rejection(self):
        self.selection()
        with self.assertRaisesRegex(ValueError, "all_research"):
            self.pack("geopolitics")
        with self.assertRaisesRegex(ValueError, "three_lane"):
            w.assemble(self.root, self.state)

    def test_no_candidates_valid_but_macro_cannot_start_before_geo_pack(self):
        self.selection(False)
        with self.assertRaisesRegex(ValueError, "geopolitical_package_barrier"):
            self.pack("macroeconomics")
        legacy = self.report("geopolitics")
        legacy["schema_version"] = "report-publication/v5"
        with self.assertRaisesRegex(ValueError, "formal_v6"):
            self.pack("geopolitics", legacy)

    def test_merge_preserves_lane_content(self):
        request = self.assembled()
        self.assertEqual(
            request["report"]["macroeconomic_stories"], self.report("macroeconomics")["macroeconomic_stories"]
        )
        self.assertEqual(request["report"]["geopolitical_stories"], [])

    def test_bad_refs_and_duplicate_assets(self):
        unit = self.report("macroeconomics")["macroeconomic_stories"][0]
        unit["summary"]["affected_refs"][0]["local_key"] = "wrong"
        with self.assertRaisesRegex(ValueError, "dangling"):
            w.unit_check(unit)
        unit["detail"]["reasonings"][0]["affected_assets"] *= 2
        with self.assertRaisesRegex(ValueError, "duplicate_asset"):
            w.unit_check(unit)

    def test_frozen_source_and_method_tamper(self):
        self.method.write_text("changed")
        with self.assertRaisesRegex(ValueError, "method_changed"):
            with w.locked(self.root):
                pass

    def test_metadata_difference_rejected(self):
        self.selection(False)
        self.pack("geopolitics")
        self.pack("macroeconomics")
        report = self.report("industry")
        report["generated_at"] = "2020-01-03T00:00:00Z"
        self.pack("industry", report)
        with self.assertRaisesRegex(ValueError, "metadata"):
            w.assemble(self.root, self.state)

    def test_publication_no_server_hash_required_and_same_package_replay(self):
        self.assembled()
        validation = {"passed": True, "request_sha256": self.state["candidate"]["sha256"]}
        w.write(self.root / "valid.json", validation)
        self.state["validation"] = {"path": "valid.json", "sha256": w.sha((self.root / "valid.json").read_bytes())}
        preflight = self.base / "preflight.json"
        w.write(
            preflight,
            {
                "status": "passed",
                **validation,
                "environment": self.scope["environment"],
                "data_base_url": self.scope["data_base_url"],
                "deployed_contract_verified": True,
                "evidence_verified": True,
            },
        )
        bodies = []

        def fake(*args):
            bodies.append(args[4])
            return (201 if len(bodies) == 1 else 200), {
                "result": {"report_id": "RPT1", "published_at": "2020-01-03T00:00:00Z", "replayed": len(bodies) > 1}
            }

        with patch.dict(os.environ, {"DATA_SERVICE_BEARER_TOKEN": "synthetic-not-a-secret"}):
            w.publish(self.root, self.state, preflight, fake)
            w.publish(self.root, self.state, preflight, fake)
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(self.state["publication"]["status"], "published_unverified")
        self.assertTrue(self.state["publication"]["replayed"])

    def test_changed_candidate_invalidates_validation(self):
        self.assembled()
        (self.root / "candidate.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "hash_changed"):
            w.artifact(self.root, self.state["candidate"])

    def test_lane_notes_are_preserved_without_cross_lane_synthesis(self):
        self.selection(False)
        for lane in w.LANES:
            report = self.report(lane)
            report["limitations"] = [lane + "原文限制", "完全相同限制"]
            self.pack(lane, report)
        request = w.assemble(self.root, self.state)
        self.assertEqual(
            request["report"]["limitations"],
            ["geopolitics原文限制", "完全相同限制", "macroeconomics原文限制", "industry原文限制"],
        )

    def test_nested_company_products_rejected(self):
        self.selection(False)
        self.pack("geopolitics")
        report = self.report("macroeconomics")
        report["macroeconomic_stories"][0]["detail"]["companies"] = [{"source_id": "COM1"}]
        with self.assertRaisesRegex(ValueError, "company_product"):
            self.pack("macroeconomics", report)

    def test_assemble_recovers_after_candidate_saved_before_state(self):
        self.selection(False)
        for lane in w.LANES:
            self.pack(lane)
        with patch.object(w, "persist", side_effect=OSError("simulated disk interruption")):
            with self.assertRaises(OSError):
                w.assemble(self.root, self.state)
        disk_state = w.read(self.root / "state.json")
        result = w.assemble(self.root, disk_state)
        self.assertEqual(w.read(self.root / "candidate.json"), result)
        self.assertIn("candidate", w.read(self.root / "state.json"))

    def test_unknown_binding_checks_actual_run_context(self):
        self.selection()

        def timeout(*args):
            raise ValueError("unknown")

        with self.assertRaises(ValueError):
            w.research(self.root, self.state, "GPR1", transport=timeout)
        receipt = self.state["research"]["GPR1"]
        proof = self.base / "proof.json"
        w.write(
            proof,
            {
                "run_id": "run1",
                "story_id": "GPR1",
                "source_reference": "synthetic server creation log",
                "request_sha256": w.sha(w.encoded({"preset_name": w.PRESET, "user_vars": receipt["user_vars"]})),
            },
        )
        detail = {"id": "run1", "preset_name": w.PRESET, "user_vars": receipt["user_vars"]}
        with self.assertRaisesRegex(ValueError, "identity"):
            w.research_bind(self.root, self.state, "GPR1", "run1", proof, lambda *a: (200, {**detail, "user_vars": {}}))
        w.research_bind(self.root, self.state, "GPR1", "run1", proof, lambda *a: (200, detail))
        self.assertEqual(receipt["run_id"], "run1")

    def test_internal_fields_cannot_be_renamed_v6_and_packed(self):
        unit = self.report("macroeconomics")["macroeconomic_stories"][0]
        unit["detail"]["variable_assessments"] = []
        with self.assertRaisesRegex(ValueError, "legacy_or_internal"):
            w.unit_check(unit)

    def test_duplicate_json_keys_and_nonfinite_rejected(self):
        for raw in ('{"x":1,"x":2}', '{"x":NaN}'):
            with self.assertRaises(ValueError):
                w.parse(raw)


if __name__ == "__main__":
    unittest.main()
