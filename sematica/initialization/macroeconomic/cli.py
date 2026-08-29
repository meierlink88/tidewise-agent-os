"""图谱专用宏观经济政策目录初始化 CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sematica.initialization.macroeconomic.projection import (
    build_plan,
    execute_plan,
    inspect_graph_state,
    load_catalog,
    resolve_country_nodes,
    verify_state,
)
from sematica.projection.runtime import ProjectionError, create_graphiti, load_graphiti_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="初始化宏观经济政策动作目录")
    parser.add_argument("--env-file", type=Path, help="Graphiti 私有运行时环境文件")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="不访问图，仅校验打包的目录")
    commands.add_parser("run", help="幂等写入政策节点与国家关系")
    commands.add_parser("verify", help="校验图中政策节点与国家关系")
    return parser


async def _main(args: argparse.Namespace) -> dict[str, object]:
    base_plan = build_plan(load_catalog())
    if args.command == "plan":
        return {**base_plan.summary(), "preflight_validated": True}

    graphiti = create_graphiti(load_graphiti_config(args.env_file))
    try:
        plan = await resolve_country_nodes(graphiti, base_plan)
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
        print(json.dumps({"ok": False, "error": "已中断"}, ensure_ascii=False), file=sys.stderr)
        return 130
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
