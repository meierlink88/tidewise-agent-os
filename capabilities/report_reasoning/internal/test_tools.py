"""Contract and data-boundary regressions, without invoking a model."""

import copy
import tempfile
import unittest
from pathlib import Path

from capabilities.report_reasoning.tools.data import freeze, load_snapshot, query

from .live import export_live
from .storage import read, write
from .validation import check_variables, validate_report


def fixture():
    report = read(Path(__file__).with_name("test_report.json"))
    unit = report["macroeconomic_stories"][0]
    row = unit["detail"]["variable_signals"][0]
    signal = {**row, "id": row["signal_id"], "entity_id": unit["source_id"]}
    events = [
        {"id": row["event_ids"][0], "classes": ["MACRO_ECONOMIC"], "evidence_ids": row["evidence_ids"]},
        {"id": "geo-event", "classes": ["GEOPOLITICAL"], "evidence_ids": ["geo-evidence"]},
    ]
    snapshot = {
        "source_kind": "test",
        "window": report["analysis_window"],
        "observed_at": report["generated_at"],
        "entities": {unit["source_id"]: {"id": unit["source_id"], "name": "测试宏观", "type": "MacroEconomic"}},
        "events": events,
        "signals": [signal],
        "structure": [],
        "evidences": {i: {"id": i} for i in [*row["evidence_ids"], "geo-evidence"]},
    }
    return report, snapshot


class ToolsTest(unittest.TestCase):
    def test_variable_grouping_preserves_conflicting_evidence_without_voting(self):
        report, snapshot = fixture()
        report["schema_version"] = "report-publication/v6-draft"
        detail = report["macroeconomic_stories"][0]["detail"]
        row = detail["variable_signals"][0]
        group = {
            "local_key": "m1-variable",
            "variable_id": row["variable_id"],
            "variable_name": row["variable_name"],
            "scope": "same market",
            "timeframe": "current",
            "direction": "DOWN",
            "synthesis": "Qualitative judgment",
            "conflict_resolution": "No vote",
            "support_signal_ids": [row["signal_id"]],
            "counter_signal_ids": [],
            "excluded_signal_ids": [],
            "evidence_ids": row["evidence_ids"],
        }
        detail["variable_assessments"] = [group]
        self.assertTrue(validate_report(report, snapshot)["passed"])
        from capabilities.report_reasoning.tools.render import render

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write(root / "report.json", report)
            write(root / "evidence.json", {i: {"summary": "source evidence"} for i in row["evidence_ids"]})
            rendered = render(root / "report.json", root / "evidence.json", root / "html")
            html = Path(rendered["html"]).read_text()
            self.assertIn("综合方向", html)
            self.assertIn(group["synthesis"], html)
            self.assertIn(row["signal"], html)
            self.assertIn("查看综合依据与原始信号", html)
            self.assertEqual(read(root / "report.json"), report)
        conflicting = {**row, "signal_id": "other", "source_direction": "UP"}
        group["counter_signal_ids"] = ["other"]
        self.assertEqual(check_variables([row, conflicting], [group]), [])
        group["support_signal_ids"].append("other")
        self.assertTrue(check_variables([row, conflicting], [group]))

    def test_variable_groups_reject_omission_wrong_variable_and_duplicate_scope(self):
        report, _ = fixture()
        row = report["macroeconomic_stories"][0]["detail"]["variable_signals"][0]
        self.assertTrue(check_variables([row], []))
        group = {
            "local_key": "v",
            "variable_id": "wrong",
            "variable_name": row["variable_name"],
            "scope": "scope",
            "timeframe": "time",
            "support_signal_ids": [row["signal_id"]],
            "counter_signal_ids": [],
            "excluded_signal_ids": [],
            "evidence_ids": row["evidence_ids"],
        }
        self.assertTrue(check_variables([row], [group]))
        group["variable_id"] = row["variable_id"]
        self.assertTrue(check_variables([row], [group, {**group, "local_key": "v2"}]))

    def test_scopes_pagination_and_lookup(self):
        _, snapshot = fixture()
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / "input"
            freeze(snapshot, run)
            self.assertEqual(query(run, "geopolitics", "signals")["total"], 0)
            self.assertEqual(query(run, "macroeconomics", "evidences")["total"], 1)
            page = query(run, "industry", "events", limit=1)
            self.assertEqual(page["next_offset"], 1)
            second = query(run, "industry", "events", offset=1, limit=1)
            self.assertIsNone(second["next_offset"])
            self.assertNotEqual(page["items"], second["items"])
            with self.assertRaises(ValueError):
                query(run, "industry", "events", limit=0)

    def test_frozen_inputs_and_old_run_rejected(self):
        _, snapshot = fixture()
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / "input"
            freeze(snapshot, run)
            with self.assertRaises(FileExistsError):
                freeze(snapshot, run)
            snapshot["signals"] = []
            write(run / "snapshot.json", snapshot)
            with self.assertRaises(ValueError):
                load_snapshot(run)

    def test_validation_never_rewrites_or_claims_semantic_review(self):
        report, snapshot = fixture()
        before = copy.deepcopy(report)
        receipt = validate_report(report, snapshot)
        self.assertTrue(receipt["passed"], receipt)
        self.assertEqual(before, report)
        self.assertIn("not certified", receipt["semantic_review"])

    def test_wrong_root_signal_owner_text_and_event_are_rejected(self):
        for mutation in ("root", "owner", "text", "event"):
            with self.subTest(mutation=mutation):
                report, snapshot = fixture()
                unit = report["macroeconomic_stories"][0]
                if mutation == "root":
                    snapshot["entities"][unit["source_id"]]["type"] = "IndustryChain"
                elif mutation == "owner":
                    snapshot["signals"][0]["entity_id"] = "other"
                elif mutation == "text":
                    unit["detail"]["variable_signals"][0]["signal"] = "改写来源"
                else:
                    unit["reasoning_sources"]["event_ids"] = ["geo-event"]
                receipt = validate_report(report, snapshot)
                self.assertFalse(receipt["passed"])
                self.assertTrue(all("path" in issue for issue in receipt["issues"]))

    def test_summary_reference_must_resolve_in_own_unit(self):
        report, snapshot = fixture()
        report["macroeconomic_stories"][0]["summary"]["affected_refs"] = [
            {"target_type": "industry_chain", "local_key": "missing", "chain_local_key": None}
        ]
        self.assertFalse(validate_report(report, snapshot)["passed"])

    def test_schema_errors_are_structured(self):
        _, snapshot = fixture()
        self.assertFalse(validate_report({}, snapshot)["passed"])

    def test_mixed_scope_signal_is_not_silently_trimmed(self):
        _, snapshot = fixture()
        snapshot["signals"][0]["event_ids"].append("geo-event")
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / "input"
            freeze(snapshot, run)
            with self.assertRaisesRegex(ValueError, "mixed-scope"):
                query(run, "macroeconomics", "signals")

    def test_live_rejects_invalid_time_before_connecting(self):
        with self.assertRaises(ValueError):
            export_live(Path("/nonexistent"), "2026-09-01", "2026-09-02")


if __name__ == "__main__":
    unittest.main()
