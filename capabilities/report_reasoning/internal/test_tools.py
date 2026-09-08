"""Contract and data-boundary regressions, without invoking a model."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from capabilities.report_reasoning.tools.data import freeze, load_snapshot, query

from .live import export_live
from .snapshot import normalize_export
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
    def test_concept_units_require_real_mapping_and_preserve_multichain_details(self):
        report, snapshot = fixture()
        unit = copy.deepcopy(report["macroeconomic_stories"][0])
        concept_id = "CON00000000-0000-0000-0000-000000000001"
        unit.update(source_id=concept_id, title="测试概念", judgment_origin="inferred")
        unit["reasoning_sources"]["signal_ids"] = []
        unit["detail"]["variable_signals"] = []
        snapshot["entities"][concept_id] = {"id": concept_id, "name": "测试概念", "type": "Concept"}
        evidence = unit["summary"]["evidence_ids"]
        assessment = {
            "conclusion": "融资成本下降有利于投入",
            "direction": "warming",
            "conclusion_basis": "reasoning_hypothesis",
            "validation_status": "pending_validation",
            "confidence": "low",
            "forecast_window": {"kind": "relative", "description": "短期", "start_at": None, "end_at": None},
            "scope": "测试范围",
            "conditions": ["融资可得"],
            "follow_up": ["核对投入"],
            "transmission_logic": "融资成本下降 → 投入可能增长",
            "evidence_ids": evidence,
        }
        objections = {
            "summary": "投入未证实",
            "counterevidence": [],
            "buffers": [],
            "counterevidence_status": "none_identified",
            "evidence_gaps": ["实际投入"],
            "scope_limits": ["测试"],
        }
        for i in (1, 2):
            cid, nid = f"ICH00000000-0000-0000-0000-{i:012d}", f"CND00000000-0000-0000-0000-{i:012d}"
            common = {
                "judgment_origin": "inferred",
                "reasoning_sources": copy.deepcopy(unit["reasoning_sources"]),
                "variable_signals": [],
                "assessment": copy.deepcopy(assessment),
            }
            node = {
                **copy.deepcopy(common),
                "local_key": f"n{i}",
                "node_local_key": f"n{i}",
                "source_id": nid,
                "name": f"节点{i}",
                "objections": copy.deepcopy(objections),
            }
            chain = {
                **common,
                "local_key": f"c{i}",
                "source_id": cid,
                "name": f"链{i}",
                "reasoning_summary": {
                    "logic": assessment["transmission_logic"],
                    "support": {"text": "融资成本证据", "basis": "inference", "evidence_ids": evidence},
                    "objections": copy.deepcopy(objections),
                },
                "graph": {
                    "scope": "assessed_nodes_only",
                    "nodes": [{"local_key": f"n{i}", "source_id": nid, "name": f"节点{i}"}],
                    "edges": [],
                },
                "affected_nodes": [node],
                "empty_state": None,
            }
            unit["detail"]["industry_chains"].append(chain)
            snapshot["entities"].update(
                {
                    cid: {"id": cid, "name": f"链{i}", "type": "IndustryChain"},
                    nid: {"id": nid, "name": f"节点{i}", "type": "ChainNode"},
                }
            )
            snapshot["structure"].extend(
                [
                    {"id": f"mapping-{i}", "source": cid, "target": concept_id, "type": "IndustryChainMappedToConcept"},
                    {"id": f"member-{i}", "source": nid, "target": cid, "type": "ChainNodeBelongsToIndustryChain"},
                ]
            )
        report["macroeconomic_stories"] = []
        report["concept_analyses"] = [unit]
        self.assertTrue(validate_report(report, snapshot)["passed"], validate_report(report, snapshot))
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / "input"
            freeze(snapshot, run)
            self.assertEqual(query(run, "industry", "entities", identity=concept_id)["items"][0]["type"], "Concept")
            self.assertEqual(query(run, "industry", "structure", identity=concept_id)["total"], 2)
        standalone = copy.deepcopy(report)
        fallback = standalone["concept_analyses"].pop()
        chain = fallback["detail"]["industry_chains"][0]
        fallback.update(source_id=chain["source_id"], title=chain["name"])
        fallback["detail"]["industry_chains"] = [chain]
        standalone["industry_chain_analyses"] = [fallback]
        self.assertFalse(validate_report(standalone, snapshot)["passed"])
        unmapped = copy.deepcopy(snapshot)
        unmapped["structure"] = [
            r
            for r in unmapped["structure"]
            if not (r["type"] == "IndustryChainMappedToConcept" and r["source"] == fallback["source_id"])
        ]
        self.assertTrue(validate_report(standalone, unmapped)["passed"])
        fallback["title"] = "伪造的概念名"
        self.assertFalse(validate_report(standalone, unmapped)["passed"])
        multi = copy.deepcopy(report)
        second = copy.deepcopy(unit)
        second_id = "CON00000000-0000-0000-0000-000000000002"
        second.update(source_id=second_id, title="第二概念")
        multi["concept_analyses"].append(second)
        graph = copy.deepcopy(snapshot)
        graph["entities"][second_id] = {"id": second_id, "name": "第二概念", "type": "Concept"}
        graph["structure"].extend(
            [{**r, "target": second_id} for r in snapshot["structure"] if r["type"] == "IndustryChainMappedToConcept"]
        )
        self.assertTrue(validate_report(multi, graph)["passed"])
        for mutation in ("mapping", "name", "duplicate", "empty"):
            broken = copy.deepcopy(report)
            graph = copy.deepcopy(snapshot)
            if mutation == "mapping":
                graph["structure"] = [r for r in graph["structure"] if r["type"] != "IndustryChainMappedToConcept"]
            elif mutation == "name":
                broken["concept_analyses"][0]["title"] = "自造概念"
            elif mutation == "duplicate":
                broken["concept_analyses"][0]["detail"]["industry_chains"] *= 2
            else:
                broken["concept_analyses"][0]["detail"]["industry_chains"] = []
            self.assertFalse(validate_report(broken, graph)["passed"], mutation)

    def test_export_keeps_authoritative_concepts_and_rejects_broken_mapping(self):
        raw: dict[str, Any] = {
            "retrieved_at": "2026-09-08T00:00:00Z",
            "events": [
                {
                    "data": {
                        "content": json.dumps({"id": "event", "title": "事件", "summary": "事件", "semantic": {}}),
                        "valid_at": "2026-09-08T00:00:00Z",
                    }
                }
            ],
            "journals": [
                {
                    "input": {"evidences": [{"id": "evidence"}]},
                    "journal": {
                        "candidates": {
                            "one": {
                                "publication": {"event_id": "event"},
                                "classification": {"event_class": "INDUSTRY_CHAIN"},
                                "identity_request": {"candidate": {"evidence_ids": ["evidence"]}},
                            }
                        }
                    },
                }
            ],
            "entities": [
                {
                    "uuid": "chain",
                    "id": "ICH00000000-0000-0000-0000-000000000001",
                    "labels": ["Entity", "IndustryChain"],
                    "name": "链",
                },
                {
                    "uuid": "concept",
                    "id": "CON00000000-0000-0000-0000-000000000001",
                    "labels": ["Entity", "Concept"],
                    "name": "概念",
                },
                {"uuid": "research", "id": None, "labels": ["Concept"], "name": "临时研究词"},
            ],
            "relations": [
                {
                    "source": "chain",
                    "target": "concept",
                    "data": {"uuid": "mapping", "name": "IndustryChainMappedToConcept", "invalid_at": None},
                }
            ],
        }
        result = normalize_export(raw)
        self.assertEqual(len(result["entities"]), 2)
        self.assertEqual(result["structure"][0]["target"], raw["entities"][1]["id"])
        expired = copy.deepcopy(raw)
        expired["relations"][0]["data"]["invalid_at"] = "2026-09-08T00:00:00Z"
        self.assertEqual(normalize_export(expired)["structure"], [])
        raw["relations"][0]["target"] = "missing"
        with self.assertRaisesRegex(ValueError, "endpoint missing"):
            normalize_export(raw)

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
        self.assertTrue(validate_report(report, snapshot)["passed"], validate_report(report, snapshot))
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
            self.assertNotIn("公司直接判断", html)
            self.assertNotIn('href="#companies"', html)
            self.assertNotIn("2026 年 9 月 7 日全量冻结数据回放", html)
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

    def test_created_at_scope_survives_freeze_and_query(self):
        _, snapshot = fixture()
        snapshot["selection_time_field"] = "created_at"
        snapshot["events"][0].update(created_at="2026-09-08T01:00:00Z", valid_at="2030-01-01T00:00:00Z")
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / "input"
            manifest = freeze(snapshot, run)
            result = query(run, "macroeconomics", "events")
            self.assertEqual(manifest["selection_time_field"], "created_at")
            self.assertEqual(result["selection_time_field"], "created_at")
            self.assertEqual(result["items"][0]["valid_at"], "2030-01-01T00:00:00Z")
            self.assertEqual(result["items"][0]["created_at"], "2026-09-08T01:00:00Z")

    def test_live_rejects_unknown_time_field_before_connecting(self):
        with self.assertRaisesRegex(ValueError, "time_field"):
            export_live(Path("/nonexistent"), "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z", "published_at")

    def test_live_rejects_invalid_time_before_connecting(self):
        with self.assertRaises(ValueError):
            export_live(Path("/nonexistent"), "2026-09-01", "2026-09-02")


if __name__ == "__main__":
    unittest.main()
