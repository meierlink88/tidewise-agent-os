"""CLI for the graph-only GeopoliticRivalry demo initializer."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sematica.initialization.geopolitic.projection import (
    build_plan,
    execute_plan,
    inspect_graph_state,
    load_catalog,
    verify_state,
)
from sematica.projection.runtime import ProjectionError, create_graphiti, load_graphiti_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize geopolitical demo nodes in Graphiti")
    parser.add_argument("--env-file", type=Path, help="private Graphiti runtime environment")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="validate the packaged demo catalog without graph access")
    commands.add_parser("run", help="idempotently write demo nodes without deleting graph data")
    commands.add_parser("verify", help="verify the planned demo nodes in Graphiti")
    return parser


async def _main(args: argparse.Namespace) -> dict[str, object]:
    plan = build_plan(load_catalog())
    if args.command == "plan":
        return {**plan.summary(), "preflight_validated": True}

    graphiti = create_graphiti(load_graphiti_config(args.env_file))
    try:
        if args.command == "run":
            nodes_written, relations_written, removed = await execute_plan(
                graphiti,
                plan,
                progress=lambda completed, total: print(f"embedded {completed}/{total}", file=sys.stderr, flush=True),
            )
            return {
                **verify_state(plan, await inspect_graph_state(graphiti, plan)),
                "nodes_written": nodes_written,
                "relations_written": relations_written,
                "removed_before_write": removed,
                "write_mode": "graphiti-demo-bulk-no-llm-no-delete",
            }
        return verify_state(plan, await inspect_graph_state(graphiti, plan))
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
