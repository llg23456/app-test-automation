"""
广度优先：按路径队列扩展界面，顺序尝试当前屏上编号 1..N 的控件。
每步冷启动导航到 path，再 tap 子控件；新屏入队；返回父屏用 KEYCODE_BACK，失败则模型辅助。
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import deque
from typing import List, Set, Tuple

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from scripts.exploration_common import (  # noqa: E402
    ExplorationContext,
    PathStepRecord,
    build_exploration_context,
)
from scripts.utils import print_with_color  # noqa: E402


def run_bfs(ctx: ExplorationContext) -> None:
    tried_edges: Set[Tuple[str, str]] = set()
    visited_states: Set[str] = set()

    fp0, elems0, shot0 = ctx.navigate_to_path([])
    if shot0 == "ERROR":
        print_with_color("无法获取初始界面", "red")
        return
    visited_states.add(fp0)
    ctx.record_new_controls(elems0, shot0, "bfs_root")

    frontier: deque[List[int]] = deque([[]])

    while frontier and ctx.exploration_budget_left():
        path = frontier.popleft()
        fp, elems, shot = ctx.navigate_to_path(path)
        if shot == "ERROR" or not elems:
            continue
        tag = "bfs_" + ("_".join(str(x) for x in path) if path else "root")
        ctx.record_new_controls(elems, shot, tag)

        for i in range(1, len(elems) + 1):
            if not ctx.exploration_budget_left():
                break
            uid = elems[i - 1].uid
            key = (fp, uid)
            if key in tried_edges:
                continue
            tried_edges.add(key)

            if not ctx.try_consume_exploration_step("BFS 前向 tap"):
                break

            ok = ctx.tap_elem_index(elems, i)
            if not ok:
                ctx.append_jsonl({"type": "bfs_tap_failed", "path": path, "index": i})
                continue

            ctx.sleep_interval()
            shot_new, _, fp_new, elems_new = ctx.capture_screen(
                f"bfs_tap_{'_'.join(str(x) for x in path)}_{i}"
            )
            if shot_new == "ERROR":
                ctx.navigate_back_to_fingerprint(fp, path)
                continue

            ctx.record_new_controls(elems_new, shot_new, f"{tag}_child_{i}")

            ctx.path_log.append(
                PathStepRecord(
                    step_index=ctx.exploration_steps,
                    mode="bfs",
                    action="tap",
                    detail=f"index={i} uid={uid}",
                    screen_fp_before=fp,
                    screen_fp_after=fp_new,
                    path_indices=path + [i],
                )
            )
            ctx.append_jsonl(
                {
                    "type": "bfs_forward",
                    "path": path,
                    "tap_index": i,
                    "fp_before": fp,
                    "fp_after": fp_new,
                }
            )

            if fp_new not in visited_states:
                visited_states.add(fp_new)
                frontier.append(path + [i])

            ok_back = ctx.navigate_back_to_fingerprint(fp, path)
            if not ok_back:
                print_with_color("返回父屏失败：先不杀进程重放 path，再不行再冷启动…", "yellow")
                fp_s, _, shot_s = ctx.navigate_to_path(path, cold_start=False)
                if shot_s == "ERROR":
                    ctx.navigate_to_path(path)

    ctx.write_report_md()


def main():
    parser = argparse.ArgumentParser(description="BFS 结构化界面探索")
    parser.add_argument("--app", required=True, help="应用包名，如 com.example.app")
    parser.add_argument("--root_dir", default="./", help="项目根目录，用于 apps/<app>/demos")
    parser.add_argument("--max_steps", type=int, default=None, help="最大探索步数，默认读 config MAX_ROUNDS")
    args = parser.parse_args()

    ctx = build_exploration_context(args.app, args.root_dir, "bfs", max_steps=args.max_steps)
    run_bfs(ctx)
    print_with_color(f"BFS 结束，目录: {ctx.task_dir}", "green")


if __name__ == "__main__":
    main()
