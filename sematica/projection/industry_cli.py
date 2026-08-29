"""Command-line boundary for the independent Industry projection."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sematica.projection.industry import build_plan, execute_plan, inspect_graph_state, load_facts, verify_state
from sematica.projection.runtime import ProjectionError, create_graphiti, load_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project Industry hierarchy facts into Graphiti")
    parser.add_argument("--env-file", type=Path, help="private runtime environment")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="validate all paginated Industry facts")
    run = commands.add_parser("run", help="execute deterministic Namespace bulk writes")
    run.add_argument(
        "--replace",
        action="store_true",
        help="remove stale Industry facts owned by this projection before upsert",
    )
    commands.add_parser("verify", help="compare graph facts with the complete Industry API")
    return parser


async def _main(args: argparse.Namespace) -> dict[str, object]:
    config = load_config(args.env_file)
    plan = build_plan(await load_facts(config))
    if args.command == "plan":
        return {
            **plan.summary(),
            "first_industry": plan.nodes[0].name if plan.nodes else None,
            "preflight_validated": True,
        }

    graphiti = create_graphiti(config)
    try:
        if args.command == "run":
            nodes_written, edges_written, removed = await execute_plan(
                graphiti,
                plan,
                replace=args.replace,
                progress=lambda completed, total: print(f"embedded {completed}/{total}", file=sys.stderr, flush=True),
            )
            return {
                **verify_state(plan, await inspect_graph_state(graphiti)),
                "nodes_written": nodes_written,
                "relations_written": edges_written,
                "write_mode": "graphiti-namespace-bulk-no-llm-resolution",
                "replaced": args.replace,
                "removed_before_write": removed,
            }
        return verify_state(plan, await inspect_graph_state(graphiti))
    finally:
        await graphiti.close()


def main() -> int:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_main(args))
    except ProjectionError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "interrupted"}), file=sys.stderr)
        return 130
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
