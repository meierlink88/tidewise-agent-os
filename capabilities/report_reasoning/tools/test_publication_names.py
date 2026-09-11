import copy
import unittest

from .publication_names import project


class PublicationNamesTest(unittest.TestCase):
    def test_display_fields_only_and_original_is_unchanged(self):
        report = {
            "schema_version": "report-publication/v5",
            "units": [
                {"source_id": "GPR1", "title": "正式地缘名称", "summary": "正式地缘名称不替换"},
                {"source_id": "MEC1", "name": "正式宏观名称"},
                {"source_id": "ICH1", "title": "正式产业链名称", "name": "正式产业链名称"},
                {"source_id": "CND1", "name": "正式节点名称"},
                {"source_id": "CON1", "title": "概念名称"},
                {"source_id": "COM1", "name": "公司名称"},
            ],
            "graph": {"nodes": [{"source_id": "CND1", "name": "正式节点名称"}]},
            "variable_signals": [{"signal": "正式产业链名称", "evidence_ids": ["EVD1"]}],
        }
        original = copy.deepcopy(report)
        catalog = {"entities": [{"id": prefix + "1", "short_name": prefix} for prefix in ("GPR", "MEC", "ICH", "CND")]}
        out, receipt = project(report, catalog)
        self.assertEqual(report, original)
        self.assertEqual(out["graph"]["nodes"][0]["name"], "CND")
        self.assertEqual(len(receipt["changes"]), 6)
        for change in reversed(receipt["changes"]):
            parts = change["path"].strip("/").split("/")
            current = out
            for part in parts[:-1]:
                current = current[int(part)] if isinstance(current, list) else current[part]
            current[parts[-1]] = change["before"]
        self.assertEqual(out, original)

    def test_missing_blank_and_duplicate_names_fail(self):
        report = {"schema_version": "report-publication/v5", "unit": {"source_id": "GPR1", "title": "原名"}}
        for rows in ([], [{"id": "GPR1", "short_name": " "}], [{"id": "GPR1"}]):
            with self.assertRaisesRegex(ValueError, "Missing.*GPR1"):
                project(report, {"entities": rows})
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            project(report, {"entities": [{"id": "GPR1"}, {"id": "GPR1"}]})
        with self.assertRaisesRegex(ValueError, "v5"):
            project({"schema_version": "report-publication/v6-draft"}, {"entities": []})
