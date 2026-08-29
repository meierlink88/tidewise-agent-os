"""Command-line boundary for authoritative Graphiti projections."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sematica.projection.country_region import (
    build_plan,
    execute_plan,
    inspect_graph_state,
    load_countries,
    verify_state,
)
from sematica.projection.runtime import ProjectionError, create_graphiti, load_config


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project Country and Region facts into Graphiti")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="private runtime environment (default: .runtime/graphiti.env)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="validate Data API facts and print the write plan")
    run = commands.add_parser("run", help="validate then execute deterministic bulk writes")
    run.add_argument(
        "--limit",
        type=_positive_int,
        help="write only the first N triplets for smoke tests",
    )
    run.add_argument(
        "--replace",
        action="store_true",
        help="delete only the fixed projection group before a complete authoritative rebuild",
    )
    commands.add_parser("verify", help="compare the complete graph projection with the Data API")
    return parser


async def _main(args: argparse.Namespace) -> dict[str, object]:
    config = load_config(args.env_file)
    countries = await load_countries(config)
    plan = build_plan(countries)

    if args.command == "plan":
        first = plan.triplets[0] if plan.triplets else None
        return {
            **plan.summary(),
            "first_triplet": None
            if first is None
            else {
                "source": first.source.name,
                "relation": first.edge.name,
                "target": first.target.name,
            },
            "preflight_validated": True,
        }

    graphiti = create_graphiti(config)
    try:
        if args.command == "run":
            if args.replace and args.limit is not None:
                raise ProjectionError("--replace cannot be combined with --limit")
            nodes_written, edges_written, removed = await execute_plan(
                graphiti,
                plan,
                limit=args.limit,
                replace=args.replace,
                progress=lambda completed, total: print(f"embedded {completed}/{total}", file=sys.stderr, flush=True),
            )
            state = await inspect_graph_state(graphiti)
            result: dict[str, object] = {
                **plan.summary(),
                "nodes_written": nodes_written,
                "relations_written": edges_written,
                "write_mode": "graphiti-namespace-bulk-no-llm-resolution",
                "replaced": args.replace,
                "removed_before_write": removed,
                "graph_nodes": len(state["nodes"]),
                "graph_relations": len(state["edges"]),
            }
            if args.limit is None:
                result.update(verify_state(plan, state))
            else:
                result["verified"] = False
                result["verification_scope"] = "smoke-only"
            return result

        state = await inspect_graph_state(graphiti)
        return verify_state(plan, state)
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
