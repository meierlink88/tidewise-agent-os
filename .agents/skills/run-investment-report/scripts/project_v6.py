#!/usr/bin/env python3
"""Explicit internal draft -> formal v6 projection before lane packing; never publishes."""

import argparse
import copy
import importlib.util
from pathlib import Path
from typing import Any

from workflow import KINDS, WIRE, encoded, read, require, sha, unit_check, write


def strip_internal(value, path="", removed=None):
    removed = [] if removed is None else removed
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            target = path + "/" + key
            if key == "variable_assessments":
                removed.append({"path": target, "sha256": sha(encoded(item))})
            else:
                result[key] = strip_internal(item, target, removed)
        return result
    if isinstance(value, list):
        return [strip_internal(item, path + "/" + str(i), removed) for i, item in enumerate(value)]
    return value


def project(source, converter, expected_sha, name_catalog):
    raw = Path(converter).read_bytes()
    require(sha(raw) == expected_sha, "converter_version_not_frozen")
    require(
        source.get("schema_version") in ("report-publication/v6-draft", "report-publication/v5"),
        "explicit_internal_draft_or_legacy_v5_required",
    )
    require(
        not source.get("geopolitical_stories") and not source.get("company_analyses"),
        "macro_industry_only_no_geopolitical_or_company_products",
    )
    for kind in KINDS:
        for unit in source.get(kind, []):
            require(not unit["detail"].get("companies"), "company_product_not_allowed")
    removed: list[dict[str, Any]] = []
    working = strip_internal(copy.deepcopy(source), removed=removed)
    # The provider converter accepts the old structural shape, not draft version labels.
    # This is in-memory adaptation only; no v5 publication or checkpoint is produced.
    names = {}
    for item in name_catalog["entities"]:
        require(item["id"] not in names, "duplicate_name_catalog_identity")
        names[item["id"]] = item
    name_changes = []

    def display_names(value, path=""):
        if isinstance(value, dict):
            identity = value.get("source_id", "")
            if isinstance(identity, str) and identity.startswith(("GPR", "MEC", "ICH", "CND")):
                fields = [k for k in ("name", "title") if k in value]
                if fields:
                    short = names.get(identity, {}).get("short_name")
                    require(isinstance(short, str) and short.strip(), "missing_graph_short_name")
                    for key in fields:
                        name_changes.append(
                            {"path": path + "/" + key, "source_id": identity, "before": value[key], "after": short}
                        )
                        value[key] = short
            for key, item in value.items():
                display_names(item, path + "/" + key)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                display_names(item, path + "/" + str(i))

    display_names(working)
    working["schema_version"] = "report-publication/v5"
    spec = importlib.util.spec_from_file_location("approved_report_unifier", converter)
    if spec is None or spec.loader is None:
        raise ValueError("converter_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result, bindings = module.convert(working)
    result["report_type"] = {"code": "investment_reasoning", "label": "投研推理报告"}
    require(result["schema_version"] == WIRE, "converter_output_version")
    for kind in KINDS:
        result.setdefault(kind, [])
        for unit in result[kind]:
            unit_check(unit)
    result.setdefault("company_analyses", [])
    return result, {
        "removed_internal_fields": removed,
        "reference_bindings": bindings,
        "report_type_before": source.get("report_type"),
        "report_type_after": result["report_type"],
        "converter_sha256": expected_sha,
        "name_changes": name_changes,
        "name_catalog_sha256": sha(encoded(name_catalog)),
        "note": "Structural projection only. Empty reasoning_blocks remain empty; no fabricated metrics.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("source", "converter", "converter-sha256", "name-catalog", "output"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args()
    output = Path(args.output)
    require(not output.exists() and not output.with_suffix(".projection.json").exists(), "output_exists")
    result, receipt = project(read(args.source), args.converter, args.converter_sha256, read(args.name_catalog))
    write(output, result)
    receipt.update(source_sha256=sha(Path(args.source).read_bytes()), output_sha256=sha(output.read_bytes()))
    write(output.with_suffix(".projection.json"), receipt)
    print("Projected formal v6 candidate; Data Service validation still required.")


if __name__ == "__main__":
    main()
