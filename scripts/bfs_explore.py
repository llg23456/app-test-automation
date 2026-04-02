"""
广度优先：按路径队列扩展界面，顺序尝试当前屏上编号 1..N 的控件。
对队列中每条 path 先冷启动导航到位；同一路径上尝试多个子控件时，从第二个起在每次 tap 前用应用内重放 path（cold_start=False）刷新控件列表，避免停留在子页仍用上一屏的编号与 bounds。返回父屏用 KEYCODE_BACK，失败则模型辅助。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
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
    ctx.root_baseline_fp = fp0
    visited_states.add(fp0)
    ctx.record_new_controls(elems0, shot0, "bfs_root")

    frontier: deque[List[int]] = deque([[]])

    while frontier and ctx.exploration_budget_left():
        path = frontier.popleft()
        fp, elems, shot = ctx.navigate_to_path(path)
        if shot == "ERROR" or not elems:
            continue
        if not path and ctx.root_baseline_fp:
            fp, elems, shot = ctx.sync_root_path_to_baseline_fp(
                ctx.root_baseline_fp, fp, elems, shot
            )
            if shot == "ERROR" or not elems:
                continue
        tag = "bfs_" + ("_".join(str(x) for x in path) if path else "root")
        ctx.record_new_controls(elems, shot, tag)

        n = len(elems)
        # 部分 IM 进入会话后 accessibility 树仍像消息列表（无 EditText、指纹不变），
        # navigate_back 会误判已回根。若刚点了会话行且 fp 未变，点底栏前强制一次 BACK。
        likely_stale_chat_after_list_row = False
        for i in range(1, n + 1):
            if i > 1:
                fp, elems, shot = ctx.navigate_to_path(path, cold_start=False)
                if shot == "ERROR" or not elems:
                    break
                if i > len(elems):
                    ctx.append_jsonl(
                        {
                            "type": "bfs_resync_skip_index",
                            "path": path,
                            "tap_index": i,
                            "len_after_resync": len(elems),
                        }
                    )
                    break
            if not path and ctx.root_baseline_fp:
                fp, elems, shot = ctx.sync_root_path_to_baseline_fp(
                    ctx.root_baseline_fp, fp, elems, shot
                )
                if shot == "ERROR" or not elems:
                    break
                if i > len(elems):
                    ctx.append_jsonl(
                        {
                            "type": "bfs_resync_skip_after_msg_sync",
                            "tap_index": i,
                            "len_after_resync": len(elems),
                        }
                    )
                    break
            if (
                not path
                and i >= 4
                and likely_stale_chat_after_list_row
            ):
                print_with_color(
                    "根路径：会话行点击后指纹未变，点底栏前强制一次返回键（UI 树常滞后于真实界面）…",
                    "yellow",
                )
                ctx.append_jsonl(
                    {
                        "type": "bfs_force_back_before_bottom_menu_after_stale_list_row",
                        "path": path,
                        "tap_index": i,
                    }
                )
                ctx.back_key_once()
                likely_stale_chat_after_list_row = False
                fp, elems, shot = ctx.navigate_to_path(path, cold_start=False)
                if shot == "ERROR" or not elems:
                    break
                if i > len(elems):
                    ctx.append_jsonl(
                        {
                            "type": "bfs_resync_skip_after_force_back",
                            "tap_index": i,
                            "len_after_resync": len(elems),
                        }
                    )
                    break
            if not ctx.exploration_budget_left():
                break
            uid = elems[i - 1].uid
            key = (fp, uid)
            if key in tried_edges:
                continue
            tried_edges.add(key)

            next_step = ctx.exploration_steps + 1
            shot_before = ctx.controller.get_screenshot(f"step_{next_step}_before", ctx.task_dir)
            if shot_before == "ERROR":
                ctx.append_jsonl({"type": "bfs_step_before_screenshot_failed", "step": next_step})
                continue
            shot_before_name = os.path.basename(shot_before)

            if not ctx.try_consume_exploration_step("BFS 前向操作"):
                break

            ok = ctx.tap_elem_index(elems, i)
            if not ok:
                ctx.append_jsonl(
                    {
                        "type": "bfs_tap_failed",
                        "path": path,
                        "index": i,
                        "step": next_step,
                        "screenshot_before": shot_before_name,
                    }
                )
                continue

            ctx.sleep_interval()
            if getattr(elems[i - 1], "interaction_kind", "tap") == "long_press":
                time.sleep(1.2)
            shot_new, _, fp_new, elems_new = ctx.capture_screen(f"step_{next_step}_after")
            if shot_new == "ERROR":
                ctx.navigate_back_to_fingerprint(fp, path)
                continue
            shot_after_name = os.path.basename(shot_new)

            ctx.record_new_controls(elems_new, shot_new, f"{tag}_child_{i}")

            act = getattr(elems[i - 1], "interaction_kind", "tap")
            if act not in ("tap", "long_press"):
                act = "tap"

            ctx.path_log.append(
                PathStepRecord(
                    step_index=next_step,
                    mode="bfs",
                    action=act,
                    detail=f"index={i} uid={uid}",
                    screen_fp_before=fp,
                    screen_fp_after=fp_new,
                    path_indices=path + [i],
                    screenshot_before=shot_before_name,
                    screenshot_after=shot_after_name,
                )
            )
            ctx.append_jsonl(
                {
                    "type": "bfs_forward",
                    "step": next_step,
                    "path": path,
                    "tap_index": i,
                    "interaction": act,
                    "uid": uid,
                    "screenshot_before": shot_before_name,
                    "screenshot_after": shot_after_name,
                    "fp_before": fp,
                    "fp_after": fp_new,
                }
            )

            if (
                not path
                and i == 3
                and fp_new == fp
                and "talk_list" in uid
            ):
                likely_stale_chat_after_list_row = True

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
