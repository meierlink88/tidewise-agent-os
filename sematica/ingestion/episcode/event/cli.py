"""CLI entry adapter for the shared Event Candidate Pipeline."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sematica.ingestion.episcode.event.contracts import EventCandidateRequest
from sematica.ingestion.runtime import create_runtime_pipeline, load_ingestion_config

TERMINAL_STATUSES = {"SUCCEEDED", "NEEDS_REVIEW", "FAILED"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    submit = commands.add_parser("submit", help="Submit one Event Candidate JSON document")
    submit.add_argument("input", help="JSON file path or '-' for stdin")
    submit.add_argument("--wait", action="store_true", help="Process and wait for a terminal status")
    submit.add_argument("--timeout", type=float, default=300.0)

    status = commands.add_parser("status", help="Read one Pipeline submission status")
    status.add_argument("submission_id")
    return parser


def _read_request(path: str) -> EventCandidateRequest:
    content = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return EventCandidateRequest.model_validate_json(content)


async def _run(args: argparse.Namespace) -> int:
    pipeline, _, _ = create_runtime_pipeline(load_ingestion_config())
    try:
        if args.command == "status":
            status = pipeline.get_status(args.submission_id)
            if status is None:
                print(json.dumps({"error": "not_found"}))
                return 1
            print(status.model_dump_json(indent=2))
            return 0

        acceptance = pipeline.submit(_read_request(args.input))
        if not args.wait:
            print(acceptance.model_dump_json(indent=2))
            return 0

        loop = asyncio.get_running_loop()
        deadline = loop.time() + args.timeout
        while loop.time() < deadline:
            await pipeline.process_pending(limit=1)
            status = pipeline.get_status(acceptance.submission_id)
            if status is not None and status.status in TERMINAL_STATUSES:
                print(status.model_dump_json(indent=2))
                return 0 if status.status == "SUCCEEDED" else 2
            await asyncio.sleep(0.25)
        print(json.dumps({"error": "timeout", "submission_id": acceptance.submission_id}))
        return 3
    finally:
        await pipeline.close()


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
