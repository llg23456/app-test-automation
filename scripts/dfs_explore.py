"""
深度优先：对每个 path 冷启动导航到位，再按编号顺序递归 path+[1], path+[2], …
先走深再走兄弟；新控件单独截图逻辑与 BFS 共用。
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Set, Tuple

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from scripts.exploration_common import (  # noqa: E402
    ExplorationContext,
    PathStepRecord,
    build_exploration_context,
)
from scripts.utils import print_with_color  # noqa: E402


def dfs(ctx: ExplorationContext, path: list, tried_edges: Set[Tuple[str, str]]) -> None:
    if not ctx.exploration_budget_left():
        return

    fp, elems, shot = ctx.navigate_to_path(path)
    if shot == "ERROR" or not elems:
        return

    tag = "dfs_" + ("_".join(str(x) for x in path) if path else "root")
    ctx.record_new_controls(elems, shot, tag)

    for i in range(1, len(elems) + 1):
        if not ctx.exploration_budget_left():
            return
        uid = elems[i - 1].uid
        key = (fp, uid)
        if key in tried_edges:
            continue
        tried_edges.add(key)

        if not ctx.try_consume_exploration_step("DFS 递归进入子路径"):
            return

        ctx.path_log.append(
            PathStepRecord(
                step_index=ctx.exploration_steps,
                mode="dfs",
                action="enter",
                detail=f"child path +[{i}] uid={uid}",
                screen_fp_before=fp,
                screen_fp_after=None,
                path_indices=path + [i],
            )
        )
        ctx.append_jsonl({"type": "dfs_enter", "path": path, "child_index": i, "fp": fp})

        dfs(ctx, path + [i], tried_edges)


def run_dfs(ctx: ExplorationContext) -> None:
    tried_edges: Set[Tuple[str, str]] = set()
    dfs(ctx, [], tried_edges)
    ctx.write_report_md()


def main():
    parser = argparse.ArgumentParser(description="DFS 结构化界面探索")
    parser.add_argument("--app", required=True, help="应用包名")
    parser.add_argument("--root_dir", default="./", help="项目根目录")
    parser.add_argument("--max_steps", type=int, default=None, help="最大探索步数")
    args = parser.parse_args()

    ctx = build_exploration_context(args.app, args.root_dir, "dfs", max_steps=args.max_steps)
    run_dfs(ctx)
    print_with_color(f"DFS 结束，目录: {ctx.task_dir}", "green")


if __name__ == "__main__":
    main()
