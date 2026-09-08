"""AgentOS data CLI for a Codex analyst; does not execute Codex."""

import argparse
import json
import sys
from pathlib import Path

from capabilities.report_reasoning.internal.contracts import BRANCHES
from capabilities.report_reasoning.internal.storage import read, write
from capabilities.report_reasoning.internal.validation import validate_report
from capabilities.report_reasoning.tools import data


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyst data queries and contract checks; no reasoning execution")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    sources = p.add_mutually_exclusive_group(required=True)
    sources.add_argument("--source", type=Path)
    sources.add_argument("--live", action="store_true")
    p.add_argument("--event-root", type=Path)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--time-field", choices=("valid_at", "created_at"), default=None)
    p.add_argument("--run", type=Path, required=True)
    q = commands.add_parser("query")
    q.add_argument("--run", type=Path, required=True)
    q.add_argument("--branch", choices=BRANCHES, required=True)
    q.add_argument("--resource", choices=("events", "signals", "evidences", "entities", "structure"), required=True)
    q.add_argument("--id", dest="identity")
    q.add_argument("--text")
    q.add_argument("--offset", type=int, default=0)
    q.add_argument("--limit", type=int, default=100)
    v = commands.add_parser("validate")
    v.add_argument("--run", type=Path, required=True)
    v.add_argument("--report", type=Path, required=True)
    v.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            if args.live:
                if not all((args.event_root, args.start, args.end)):
                    raise ValueError("--live requires --event-root, --start and --end")
                result = data.prepare_live(
                    args.event_root, args.start, args.end, args.run, args.time_field or "valid_at"
                )
            else:
                if args.time_field is not None:
                    raise ValueError("--time-field is only supported with --live; imported source keeps its own scope")
                result = data.prepare(args.source, args.run)
        elif args.command == "query":
            result = data.query(args.run, args.branch, args.resource, args.identity, args.text, args.offset, args.limit)
        else:
            result = validate_report(read(args.report), data.load_snapshot(args.run))
            if args.receipt:
                if args.receipt.resolve() in {
                    args.report.resolve(),
                    (args.run / "snapshot.json").resolve(),
                    (args.run / "manifest.json").resolve(),
                }:
                    raise ValueError("receipt must not overwrite report or input")
                write(args.receipt, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("passed") is False else 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
