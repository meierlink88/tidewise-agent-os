#!/usr/bin/env python3
"""Bounded, resumable readback capture. Content comparison remains a separate review."""

import argparse
import os
from urllib.parse import quote, urlencode

from workflow import KINDS, artifact, http, locked, read, require, sha, write


class BatchLimit(Exception):
    pass


def capture(root, state, maximum=20, transport=http):
    receipt = state["publication"] or {}
    require(receipt.get("status") == "published_unverified", "published_receipt_required")
    request = artifact(root, state["candidate"])
    scope = artifact(root, state["scope"])
    filename = root / "readback/index.json"
    index = (
        read(filename)
        if filename.exists()
        else {
            "request_sha256": state["candidate"]["sha256"],
            "report_id": receipt["report_id"],
            "responses": {},
            "capture_complete": False,
        }
    )
    require(
        index["request_sha256"] == state["candidate"]["sha256"] and index["report_id"] == receipt["report_id"],
        "readback_identity_mismatch",
    )
    token = os.environ.get("DATA_SERVICE_BEARER_TOKEN", "")
    require(token, "DATA_SERVICE_BEARER_TOKEN_required")
    calls = 0

    def get(route):
        nonlocal calls
        if route in index["responses"]:
            return artifact(root, index["responses"][route])
        if calls >= maximum:
            raise BatchLimit()
        calls += 1
        _, raw = transport("GET", scope["data_base_url"], route, token)
        result = raw.get("result", raw)
        name = "readback/" + sha(route.encode()) + ".json"
        write(root / name, result)
        index["responses"][route] = {"path": name, "sha256": sha((root / name).read_bytes())}
        write(filename, index)
        return result

    base = "api/data/v1/reports/" + quote(receipt["report_id"], safe="")
    scopes: dict[str, int] = {}

    def discover(value):
        if isinstance(value, dict):
            token = value.get("evidence_scope_token")
            if token:
                count = value.get("evidence_count")
                if not isinstance(count, int) or count < 0:
                    raise ValueError("invalid_evidence_scope_count")
                require(token not in scopes or scopes[token] == count, "conflicting_evidence_scope_count")
                scopes[token] = count
            for item in value.values():
                discover(item)
        elif isinstance(value, list):
            for item in value:
                discover(item)

    try:
        home = get(base + "/home")
        require(home["schema_version"] == request["report"]["schema_version"], "readback_schema_mismatch")
        discover(home)
        for kind in KINDS + ["company_analyses"]:
            cursor, visited, items = None, set(), []
            while True:
                query = {"limit": 100}
                if cursor:
                    query["cursor"] = cursor
                page = get(base + "/analyses/" + kind + "?" + urlencode(query))
                items.extend(page["items"])
                discover(page)
                cursor = page.get("next_cursor")
                if not cursor:
                    break
                require(cursor not in visited, "pagination_cursor_cycle")
                visited.add(cursor)
            expected = request["report"].get(kind, [])
            require(
                [x["local_key"] for x in items] == [x["local_key"] for x in expected], "readback_unit_order_or_count"
            )
            for unit in expected:
                detail = get(base + "/analyses/" + kind + "/" + quote(unit["local_key"], safe=""))
                discover(detail)
        for token, count in scopes.items():
            evidence = get(base + "/evidences?" + urlencode({"scope_token": token}))
            require(
                evidence["report_id"] == receipt["report_id"] and evidence["scope_token"] == token,
                "evidence_scope_identity",
            )
            require(len(evidence["items"]) == count, "evidence_scope_count_mismatch")
        index.update(
            capture_complete=True,
            scope_count=len(scopes),
            note="Collection order/count and Evidence scope counts verified; field/content review still required.",
        )
        write(filename, index)
    except BatchLimit:
        pass
    return {
        "capture_complete": index["capture_complete"],
        "new_requests": calls,
        "saved_responses": len(index["responses"]),
    }


def main():
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--max-requests", type=int, default=20)
    args = parser.parse_args()
    require(1 <= args.max_requests <= 100, "max_requests_1_to_100")
    with locked(args.run) as state:
        print(capture(args.run, state, args.max_requests))


if __name__ == "__main__":
    main()
