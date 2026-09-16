#!/usr/bin/env python3
"""Codex-owned report workflow checkpoints. No model calls or automatic scheduling."""

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bind_story_evidence import bind

VERSION = "codex-investment-workflow/v1"
WIRE = "report-publication/v6"
LANES = {
    "geopolitics": ["geopolitical_stories"],
    "macroeconomics": ["macroeconomic_stories"],
    "industry": ["concept_analyses", "industry_chain_analyses"],
}
KINDS = [k for ks in LANES.values() for k in ks]
PRESET = "geopolitical_war_room"
GEOPOLITICAL_MARKET = "中国A股市场"


def require(ok, message):
    if not ok:
        raise ValueError(message)


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "timezone_required")
    return result


def now():
    return datetime.now(UTC).isoformat()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def parse(raw):
    def reject(_):
        raise ValueError("non_finite_number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)


def read(path):
    return parse(Path(path).read_bytes())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def archive(root, name, source):
    raw = Path(source).read_bytes()
    path = root / name
    require(not path.exists() or path.read_bytes() == raw, "artifact_already_exists_with_different_content")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"path": name, "sha256": sha(raw)}


def artifact(root, receipt):
    path = (root / receipt["path"]).resolve()
    require(path.is_relative_to(root.resolve()), "artifact_outside_run")
    raw = path.read_bytes()
    require(sha(raw) == receipt["sha256"], "artifact_hash_changed")
    return parse(raw)


@contextmanager
def locked(root):
    require((root / "state.json").is_file(), "initialize_run_first")
    with (root / ".lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = read(root / "state.json")
        require(state["version"] == VERSION, "unsupported_state")
        for key in ("scope", "snapshot"):
            artifact(root, state[key])
        for item in state["methods"]:
            require(sha(Path(item["path"]).read_bytes()) == item["sha256"], "method_changed_new_batch_required")
        yield state


def persist(root, state):
    write(root / "state.json", state)


def initialize(root, scope_path, snapshot_path):
    scope, snapshot = read(scope_path), read(snapshot_path)
    for key in (
        "environment",
        "start",
        "end",
        "timezone",
        "market",
        "research_base_url",
        "data_base_url",
        "source_identity",
        "method_files",
    ):
        require(scope.get(key), "missing_scope_" + key)
    start, end = timestamp(scope["start"]), timestamp(scope["end"])
    require(start < end <= datetime.now(UTC), "invalid_window")
    require(snapshot.get("selection_time_field") == "created_at", "created_at_snapshot_required")
    require(snapshot["window"] == {"start": scope["start"], "end": scope["end"]}, "snapshot_window_mismatch")
    ids = set()
    selected = []
    for event in snapshot["events"]:
        require(event["id"] not in ids, "duplicate_event_id")
        ids.add(event["id"])
        time = timestamp(event["created_at"])
        require(start <= time <= end, "event_outside_query_window")
        if time < end:
            selected.append(event)
    keep = {e["id"] for e in selected}
    signals = []
    for signal in snapshot["signals"]:
        sources = set(signal["event_ids"])
        if sources & keep:
            require(sources <= keep, "signal_cross_window_source_closure")
            signals.append(signal)
    snapshot = copy.deepcopy(snapshot)
    snapshot["events"], snapshot["signals"] = selected, signals
    snapshot["window"] = {"start": scope["start"], "end": scope["end"]}
    snapshot["end_exclusive"] = True
    for key in ("research_base_url", "data_base_url"):
        safe_url(scope[key])
    methods = []
    for filename in scope["method_files"]:
        path = Path(filename).resolve()
        methods.append({"path": str(path), "sha256": sha(path.read_bytes())})
    root.mkdir(parents=True, exist_ok=False)
    scope_receipt = archive(root, "scope.json", scope_path)
    write(root / "input/snapshot.json", snapshot)
    # Exact hash convention used by the existing analyst CLI.
    snapshot_hash = sha(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode())
    write(
        root / "input/manifest.json",
        {
            "format": "codex-analyst-input/v1",
            "snapshot_hash": snapshot_hash,
            "source_kind": snapshot["source_kind"],
            "window": snapshot["window"],
            "selection_time_field": "created_at",
        },
    )
    state = {
        "version": VERSION,
        "batch_id": str(uuid.uuid4()),
        "created_at": now(),
        "scope": scope_receipt,
        "snapshot": {"path": "input/snapshot.json", "sha256": sha((root / "input/snapshot.json").read_bytes())},
        "input_source_sha256": sha(Path(snapshot_path).read_bytes()),
        "methods": methods,
        "selection": None,
        "research": {},
        "lanes": {},
        "publication": None,
    }
    persist(root, state)
    return state


def select(root, state, selection_path):
    require(state["selection"] is None, "selection_already_frozen")
    snapshot = artifact(root, state["snapshot"])
    selection = read(selection_path)
    events = {e["id"] for e in snapshot["events"]}
    require(selection["snapshot_sha256"] == state["snapshot"]["sha256"], "selection_snapshot_mismatch")
    reviewed = selection["event_reviews"]
    require(len(reviewed) == len(events) and {e["event_id"] for e in reviewed} == events, "event_review_coverage")
    catalog = snapshot["entities"]
    seen = set()
    for item in reviewed:
        require(bool(item["reason"].strip()) and isinstance(item["story_ids"], list), "event_review_required")
        for sid in item["story_ids"]:
            require(catalog.get(sid, {}).get("type") == "GeopoliticRivalry", "unknown_story_id")
    for story in selection["stories"]:
        sid = story["story_id"]
        require(sid not in seen and re.fullmatch(r"GPR[A-Za-z0-9_-]+", sid), "duplicate_or_invalid_story")
        seen.add(sid)
        require(catalog.get(sid, {}).get("type") == "GeopoliticRivalry", "unknown_story_id")
        require(story["name"] == catalog[sid]["name"], "story_name_identity_mismatch")
        expected = {e["event_id"] for e in reviewed if sid in e["story_ids"]}
        require(expected and set(story["event_ids"]) == expected, "story_event_binding")
        require(story["decision"] in ("research", "skip") and story["reason"].strip(), "story_decision_required")
    require(seen == {sid for e in reviewed for sid in e["story_ids"]}, "missing_story_decision")
    state["selection"] = archive(root, "selection.json", selection_path)
    persist(root, state)


def chosen(root, state):
    require(state["selection"] is not None, "selection_required")
    return [s for s in artifact(root, state["selection"])["stories"] if s["decision"] == "research"]


def research_complete(root, state):
    for story in chosen(root, state):
        receipt = state["research"].get(story["story_id"], {})
        require(receipt.get("status") == "completed", "all_research_must_complete")
        raw = (root / receipt["report_path"]).read_bytes()
        require(sha(raw) == receipt["report_sha256"], "research_report_hash_changed")


def safe_url(base):
    parsed = urlsplit(base)
    require(
        parsed.scheme in ("http", "https")
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment,
        "invalid_service_url",
    )
    return base.rstrip("/")


class HTTPStatus(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__("http_status_" + str(code))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http(method, base, route, token, body=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(safe_url(base) + "/" + route.lstrip("/"), data=body, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            return response.status, parse(response.read())
    except HTTPError as exc:
        # Do not persist untrusted response bodies that may contain credentials.
        raise HTTPStatus(exc.code) from None
    except (URLError, TimeoutError, OSError):
        raise ValueError("transport_result_unknown") from None


def research(root, state, sid, poll=False, transport=http):
    stories = {s["story_id"]: s for s in chosen(root, state)}
    require(sid in stories, "story_not_selected")
    scope = artifact(root, state["scope"])
    receipt = state["research"].get(sid)
    token = os.environ.get("TIDEWISE_RESEARCH_API_KEY", "")
    if not poll:
        require(receipt is None, "do_not_repeat_research_post_use_saved_run_or_reconcile_unknown")
        user_vars = {
            "crisis": stories[sid]["name"],
            "market": GEOPOLITICAL_MARKET,
            "story_id": sid,
            "research_date": "",
            "event_window_start": scope["start"],
            "event_window_end": scope["end"],
        }
        receipt = {"status": "submitting", "user_vars": user_vars, "origin": scope["research_base_url"]}
        state["research"][sid] = receipt
        persist(root, state)  # Crash or ambiguous POST can never silently create a second run.
        try:
            _, response = transport(
                "POST", receipt["origin"], "swarm/runs", token, encoded({"preset_name": PRESET, "user_vars": user_vars})
            )
            require(
                response.get("preset_name") == PRESET and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", response.get("id", "")),
                "research_response_identity",
            )
            receipt.update(status="running", run_id=response["id"])
        except (ValueError, KeyError, TypeError):
            receipt["status"] = "unknown"
            persist(root, state)
            raise ValueError("research_dispatch_unknown_no_automatic_retry") from None
    else:
        require(receipt and receipt.get("run_id"), "known_run_id_required")
        require(receipt["status"] in ("running", "completed"), "research_terminal_state")
        _, detail = transport("GET", receipt["origin"], "swarm/runs/" + quote(receipt["run_id"], safe=""), token)
        require(
            detail.get("id") == receipt["run_id"]
            and detail.get("preset_name") == PRESET
            and all(detail.get("user_vars", {}).get(k) == v for k, v in receipt["user_vars"].items()),
            "research_detail_identity_mismatch",
        )
        status = detail["status"]
        require(status in ("pending", "running", "completed", "failed", "cancelled"), "research_status_unknown")
        if status == "completed":
            report = detail.get("final_report")
            require(isinstance(report, str) and report.strip(), "empty_final_report")
            raw = report.encode()
            name = "research/" + sid + "/report.md"
            path = root / name
            require(not path.exists() or path.read_bytes() == raw, "immutable_final_report_changed")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            receipt.update(status=status, report_path=name, report_sha256=sha(raw))
        elif status in ("failed", "cancelled"):
            receipt["status"] = status
    persist(root, state)
    return receipt


def research_bind(root, state, sid, run_id, proof_path, transport=http):
    require(sid in {s["story_id"] for s in chosen(root, state)}, "story_not_selected")
    receipt = state["research"].get(sid, {})
    require(
        receipt.get("status") in ("unknown", "submitting") and not receipt.get("run_id"),
        "only_unknown_dispatch_can_be_reconciled",
    )
    require(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id), "invalid_run_id")
    proof = read(proof_path)
    expected_hash = sha(encoded({"preset_name": PRESET, "user_vars": receipt["user_vars"]}))
    require(
        proof.get("run_id") == run_id
        and proof.get("story_id") == sid
        and proof.get("request_sha256") == expected_hash
        and proof.get("source_reference"),
        "server_creation_evidence_required",
    )
    _, detail = transport(
        "GET",
        receipt["origin"],
        "swarm/runs/" + quote(run_id, safe=""),
        os.environ.get("TIDEWISE_RESEARCH_API_KEY", ""),
    )
    require(
        detail.get("id") == run_id
        and detail.get("preset_name") == PRESET
        and all(detail.get("user_vars", {}).get(k) == v for k, v in receipt["user_vars"].items()),
        "research_detail_identity_mismatch",
    )
    receipt["reconciliation"] = archive(root, "research/" + sid + "/reconciliation.json", proof_path)
    receipt.update(run_id=run_id, status="running")
    persist(root, state)
    return receipt


def no_legacy(value):
    if isinstance(value, dict):
        require(
            not set(value) & {"variable_assessments", "macro_impacts", "industry_chains", "affected_nodes"},
            "legacy_or_internal_fields_in_v6",
        )
        for item in value.values():
            no_legacy(item)
    elif isinstance(value, list):
        for item in value:
            no_legacy(item)


def unit_check(unit):
    no_legacy(unit)
    require(unit.get("local_key") and unit.get("source_id") and unit.get("title"), "unit_identity_required")
    detail = unit["detail"]
    require(not detail.get("companies"), "company_product_not_allowed")
    require("industry_chains" not in detail and "macro_impacts" not in detail, "legacy_detail_not_allowed")
    require(detail.get("reasonings"), "empty_detail")
    keys, targets = set(), set()
    for reasoning in detail["reasonings"]:
        key = reasoning["local_key"]
        require(key not in keys, "duplicate_reasoning_key")
        keys.add(key)
        require(reasoning.get("assessment") and reasoning.get("reasoning_summary"), "reasoning_content_required")
        for asset in reasoning["affected_assets"]:
            target = (key, asset["local_key"])
            require(target not in targets, "duplicate_asset_key")
            targets.add(target)
    for ref in unit["summary"]["affected_refs"]:
        require(set(ref) == {"reasoning_local_key", "local_key"}, "legacy_or_invalid_summary_ref")
        require((ref["reasoning_local_key"], ref["local_key"]) in targets, "dangling_summary_ref")


def review_bound(path, file_path):
    review = read(path)
    require(
        review.get("status") == "passed"
        and review.get("artifact_sha256") == sha(Path(file_path).read_bytes())
        and review.get("findings") == []
        and review.get("reviewer"),
        "hash_bound_semantic_review_required",
    )
    return review


def lane_pack(root, state, lane, report_path, review_path, evidence_pages=None):
    require(lane not in state["lanes"], "lane_already_frozen_new_batch_required_for_revision")
    research_complete(root, state)
    if lane != "geopolitics":
        require("geopolitics" in state["lanes"], "geopolitical_package_barrier")
    report = read(report_path)
    require(report.get("schema_version") == WIRE, "formal_v6_required_project_before_pack")
    require(not report.get("company_analyses"), "company_product_not_allowed")
    for kind in KINDS:
        require(isinstance(report.get(kind), list), "all_collections_required")
        require(kind in LANES[lane] or not report[kind], "lane_contains_sibling_results")
        identities, keys = set(), set()
        for unit in report[kind]:
            unit_check(unit)
            require(unit["source_id"] not in identities and unit["local_key"] not in keys, "duplicate_unit_identity")
            identities.add(unit["source_id"])
            keys.add(unit["local_key"])
    if lane == "geopolitics":
        require(
            all(u["summary"].get("evidence_ids") for u in report["geopolitical_stories"]),
            "geopolitical_story_evidence_required",
        )
        require(
            {u["source_id"] for u in report["geopolitical_stories"]} == {s["story_id"] for s in chosen(root, state)},
            "selected_story_package_coverage",
        )
    if lane == "geopolitics" and report["geopolitical_stories"]:
        require(evidence_pages is not None, "geopolitical_evidence_pages_required")
        page_paths = sorted(Path(evidence_pages).glob("*.json"))
        checked, mapping = bind(report, [read(p) for p in page_paths], artifact(root, state["snapshot"]))
        require(checked == report, "geopolitical_evidence_binding_mismatch")
        for page in page_paths:
            archive(root, "lanes/geopolitics/evidence-pages/" + page.name, page)
        write(
            root / "lanes/geopolitics/evidence-binding.json",
            {"report_sha256": sha(Path(report_path).read_bytes()), "stories": mapping},
        )
    review_bound(review_path, report_path)
    ref = archive(root, "lanes/" + lane + "/report.json", report_path)
    ref["review"] = archive(root, "lanes/" + lane + "/review.json", review_path)
    state["lanes"][lane] = ref
    persist(root, state)


def assemble(root, state):
    require(set(state["lanes"]) == set(LANES), "three_lane_packages_required")
    research_complete(root, state)
    reports = {}
    for lane, ref in state["lanes"].items():
        reports[lane] = artifact(root, ref)
        review = artifact(root, ref["review"])
        require(review["artifact_sha256"] == ref["sha256"] and review["status"] == "passed", "stale_lane_review")
    metadata = {
        k: v
        for k, v in reports["geopolitics"].items()
        if k not in KINDS + ["company_analyses", "observations", "limitations"]
    }
    for report in reports.values():
        require(
            metadata
            == {
                k: v for k, v in report.items() if k not in KINDS + ["company_analyses", "observations", "limitations"]
            },
            "shared_metadata_mismatch",
        )
    scope = artifact(root, state["scope"])
    require(
        metadata.get("timezone") == scope["timezone"]
        and metadata.get("analysis_window") == {"start": scope["start"], "end": scope["end"]},
        "publication_window_mismatch",
    )
    require(metadata.get("report_type") == {"code": "investment_reasoning", "label": "投研推理报告"}, "report_type")
    result = copy.deepcopy(metadata)
    for field in ("observations", "limitations"):
        result[field] = []
        seen = set()
        for lane in LANES:
            require(isinstance(reports[lane].get(field), list), "report_notes_array_required")
            for item in reports[lane][field]:
                identity = encoded(item)
                if identity not in seen:
                    result[field].append(copy.deepcopy(item))
                    seen.add(identity)
    for lane, kinds in LANES.items():
        for kind in kinds:
            result[kind] = copy.deepcopy(reports[lane][kind])
    result["company_analyses"] = []
    require(any(result[k] for k in KINDS), "empty_report_not_publishable")
    request = {"publisher_report_id": "codex-" + state["batch_id"], "report": result}
    path = root / "candidate.json"
    require(not path.exists() or path.read_bytes() == encoded(request), "candidate_exists_with_different_content")
    write(path, request)
    state["candidate"] = {"path": "candidate.json", "sha256": sha((root / "candidate.json").read_bytes())}
    persist(root, state)
    return request


def validate(root, state, data_repo):
    artifact(root, state["candidate"])
    require(not state["publication"], "already_published_or_unknown")
    result = subprocess.run(
        [
            "go",
            "run",
            "./data-service/backend/cmd/report-storage",
            "--validate-publication",
            str((root / "candidate.json").resolve()),
        ],
        cwd=data_repo,
        capture_output=True,
        text=True,
        timeout=180,
    )
    version = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=data_repo, text=True).strip()
    receipt = {
        "request_sha256": state["candidate"]["sha256"],
        "validator_commit": version,
        "passed": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    write(root / "contract-validation.json", receipt)
    state["validation"] = {
        "path": "contract-validation.json",
        "sha256": sha((root / "contract-validation.json").read_bytes()),
    }
    persist(root, state)
    return receipt


def publish(root, state, preflight_path, transport=http):
    artifact(root, state["candidate"])
    validation = artifact(root, state["validation"])
    require(
        validation["passed"] and validation["request_sha256"] == state["candidate"]["sha256"], "valid_contract_required"
    )
    preflight = read(preflight_path)
    scope = artifact(root, state["scope"])
    require(
        preflight.get("request_sha256") == state["candidate"]["sha256"]
        and preflight.get("status") == "passed"
        and preflight.get("environment") == scope["environment"]
        and preflight.get("data_base_url") == scope["data_base_url"]
        and preflight.get("deployed_contract_verified") is True
        and preflight.get("evidence_verified") is True,
        "target_and_evidence_preflight_required",
    )
    token = os.environ.get("DATA_SERVICE_BEARER_TOKEN", "")
    require(token, "DATA_SERVICE_BEARER_TOKEN_required")
    previous = state["publication"]
    require(
        not previous or previous["status"] in ("submitting", "unknown", "published_unverified"),
        "publication_not_retryable",
    )
    attempts = (previous or {}).get("attempts", 0)
    require(attempts < 2, "bounded_replay_limit_reconcile_receipts")
    preflight_ref = archive(root, f"preflight-{attempts + 1}.json", preflight_path)
    state["publication"] = {
        **(previous or {}),
        "preflight": preflight_ref,
        "status": "submitting",
        "attempts": attempts + 1,
        "request_sha256": state["candidate"]["sha256"],
    }
    persist(root, state)
    try:
        code, response = transport(
            "POST",
            scope["data_base_url"],
            "api/data/v1/report-publications",
            token,
            (root / "candidate.json").read_bytes(),
        )
        data = response.get("result", response)
        require(
            code in (200, 201)
            and data.get("report_id")
            and data.get("published_at")
            and isinstance(data.get("replayed"), bool),
            "publication_receipt_invalid",
        )
        if previous and previous.get("report_id"):
            require(
                data["report_id"] == previous["report_id"]
                and timestamp(data["published_at"]) == timestamp(previous["published_at"])
                and data.get("replayed") is True,
                "publication_replay_mismatch",
            )
        state["publication"].update(
            status="published_unverified",
            report_id=data["report_id"],
            published_at=data["published_at"],
            replayed=data.get("replayed", False),
        )
        write(root / f"publication-receipt-{attempts + 1}.json", response)
    except HTTPStatus as exc:
        state["publication"]["status"] = "rejected" if 400 <= exc.code < 500 else "unknown"
        state["publication"]["http_status"] = exc.code
        persist(root, state)
        raise
    except (ValueError, KeyError, TypeError):
        state["publication"]["status"] = "unknown"
        persist(root, state)
        raise ValueError("publication_result_unknown_same_package_only") from None
    persist(root, state)
    return state["publication"]


def complete(root, state, review_path):
    artifact(root, state["candidate"])
    receipt = state["publication"] or {}
    require(
        receipt.get("status") == "published_unverified" and receipt.get("replayed") is True,
        "publication_and_replay_required",
    )
    capture = read(root / "readback/index.json")
    require(
        capture.get("capture_complete") is True
        and capture.get("request_sha256") == state["candidate"]["sha256"]
        and capture.get("report_id") == receipt["report_id"],
        "complete_readback_capture_required",
    )
    for item in capture["responses"].values():
        artifact(root, item)
    review = read(review_path)
    require(
        review.get("status") == "passed"
        and review.get("request_sha256") == state["candidate"]["sha256"]
        and review.get("report_id") == receipt["report_id"]
        and review.get("all_pages_verified") is True
        and review.get("all_fields_verified") is True
        and review.get("evidence_scopes_verified") is True
        and review.get("same_package_replay_verified") is True,
        "full_readback_review_required",
    )
    require(review.get("artifacts"), "readback_artifacts_required")
    for item in review["artifacts"]:
        artifact(root, item)
    state["readback"] = archive(root, "readback-review.json", review_path)
    state["publication"]["status"] = "completed"
    persist(root, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("init")
    p.add_argument("--scope", required=True)
    p.add_argument("--snapshot", required=True)
    p = commands.add_parser("select")
    p.add_argument("--selection", required=True)
    for name in ("research-submit", "research-poll"):
        p = commands.add_parser(name)
        p.add_argument("--story-id", required=True)
    p = commands.add_parser("research-bind")
    p.add_argument("--story-id", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--proof", required=True)
    p = commands.add_parser("pack")
    p.add_argument("--lane", choices=LANES, required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--review", required=True)
    p.add_argument("--evidence-pages", help="Complete story-events/v1 pages; required for nonempty geopolitical lane")
    commands.add_parser("assemble")
    commands.add_parser("status")
    p = commands.add_parser("validate")
    p.add_argument("--data-repo", required=True)
    p = commands.add_parser("publish")
    p.add_argument("--preflight", required=True)
    p = commands.add_parser("complete")
    p.add_argument("--review", required=True)
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize(args.run, args.scope, args.snapshot)
        else:
            with locked(args.run) as state:
                if args.command == "select":
                    select(args.run, state, args.selection)
                elif args.command == "research-bind":
                    research_bind(args.run, state, args.story_id, args.run_id, args.proof)
                elif args.command.startswith("research-"):
                    research(args.run, state, args.story_id, args.command == "research-poll")
                elif args.command == "pack":
                    lane_pack(args.run, state, args.lane, args.report, args.review, args.evidence_pages)
                elif args.command == "assemble":
                    assemble(args.run, state)
                elif args.command == "validate":
                    result = validate(args.run, state, args.data_repo)
                    require(result["passed"], "data_contract_rejected_see_contract_validation")
                elif args.command == "publish":
                    publish(args.run, state, args.preflight)
                elif args.command == "complete":
                    complete(args.run, state, args.review)
                elif args.command == "status":
                    print(
                        json.dumps(
                            {
                                "batch_id": state["batch_id"],
                                "selection_frozen": state["selection"] is not None,
                                "research": {k: v["status"] for k, v in state["research"].items()},
                                "lanes": list(state["lanes"]),
                                "publication": state["publication"],
                            },
                            ensure_ascii=False,
                        )
                    )
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        # No credentials or server bodies in CLI diagnostics.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(json.dumps({"error": message}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
