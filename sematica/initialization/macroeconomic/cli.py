"""Explicit projection of an operator-exported macroeconomic snapshot."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from app.settings import default_model
from sematica.graphiti.runtime import create_agentos_graphiti
from sematica.initialization.macroeconomic.projection import (
    build_plan,
    execute_plan,
    inspect_state,
    load_snapshot,
    verify,
)
from sematica.projection.runtime import ProjectionError


async def run(args):
    nodes = build_plan(load_snapshot(args.snapshot))
    if args.command == "plan":
        return {"validated": True, "storylines": len(nodes)}
    dimension = int(os.environ["GRAPHITI_EMBEDDING_DIM"])
    graphiti = create_agentos_graphiti(default_model())
    try:
        if args.command == "run":
            return await execute_plan(graphiti, nodes, dimension)
        return verify(nodes, await inspect_state(graphiti, nodes), dimension)
    finally:
        await graphiti.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("command", choices=["plan", "run", "verify"])
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except ProjectionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (ValidationError, ValueError, OSError, KeyError):
        print("Invalid snapshot, runtime configuration, or projection state; no fallback catalog used", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
