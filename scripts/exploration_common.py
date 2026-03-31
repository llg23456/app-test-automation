"""
结构化 BFS/DFS 界面探索的共享逻辑：截屏、控件指纹、路径回放、返回栈与报告。
与 self_explorer 解耦，便于单独维护。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import datetime
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

from . import prompts
from .config import load_config
from .and_controller import AndroidController, AndroidElement, list_all_devices, traverse_tree
from .model import OpenAIModel, QwenModel, parse_explore_rsp
from .utils import print_with_color, draw_bbox_multi


def _sanitize_uid_for_file(uid: str, max_len: int = 80) -> str:
    s = re.sub(r"[^\w\-.]", "_", uid)
    return s[:max_len] if len(s) > max_len else s


# 「+」菜单展开时仍可 back 关闭；检测到这些文案则认为不是「干净主 Tab」，应允许继续 back
_PLUS_MENU_MARKERS = ("发起群聊", "添加朋友", "扫一扫")
# 全屏搜索等子流程，应优先 back / 点取消，不按主 Tab 跳过逻辑处理
_FULLSCREEN_SEARCH_MARKERS = ("请输入用户昵称", "请输入用户昵称或群名称", "群名称")
# 底部主导航四个 Tab（名称需在界面文案中出现）
_MAIN_TAB_LABELS = ("消息", "通话", "好友", "我的")


def _bounds_mid_y(bounds_str: str) -> Optional[int]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if not m:
        return None
    y1, y2 = int(m.group(2)), int(m.group(4))
    return (y1 + y2) // 2


def is_clean_main_tab_home(xml_path: str, screen_height: int) -> bool:
    """
    判断是否为底部四个 Tab 可见、且无「+」弹出菜单、非全屏搜索页的「主界面」状态。
    此时再按系统返回容易退出应用，应改为重放路径而非 KEYCODE_BACK。
    """
    if screen_height <= 0:
        screen_height = 1920
    bottom_zone = int(screen_height * 0.58)
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception:
        return False
    tabs_in_nav = set()
    for elem in root.iter():
        att = elem.attrib
        text = (att.get("text") or "") + (att.get("content-desc") or "")
        if not text.strip():
            continue
        for m in _PLUS_MENU_MARKERS:
            if m in text:
                return False
        for m in _FULLSCREEN_SEARCH_MARKERS:
            if m in text:
                return False
        b = att.get("bounds")
        if not b:
            continue
        mid_y = _bounds_mid_y(b)
        if mid_y is None or mid_y < bottom_zone:
            continue
        for tab in _MAIN_TAB_LABELS:
            if tab in text:
                tabs_in_nav.add(tab)
    return len(tabs_in_nav) >= 3


def build_elem_list(xml_path: str, useless_list: Set[str], configs: dict) -> List[AndroidElement]:
    """与 self_explorer 一致：可点击 + 去重后的可聚焦。"""
    clickable_list: List[AndroidElement] = []
    focusable_list: List[AndroidElement] = []
    traverse_tree(xml_path, clickable_list, "clickable", True)
    traverse_tree(xml_path, focusable_list, "focusable", True)
    elem_list: List[AndroidElement] = []
    for elem in clickable_list:
        if elem.uid not in useless_list:
            elem_list.append(elem)
    for elem in focusable_list:
        if elem.uid in useless_list:
            continue
        bbox = elem.bbox
        center = (bbox[0][0] + bbox[1][0]) // 2, (bbox[0][1] + bbox[1][1]) // 2
        close = False
        for e in clickable_list:
            bb = e.bbox
            center_ = (bb[0][0] + bb[1][0]) // 2, (bb[0][1] + bb[1][1]) // 2
            dist = (abs(center[0] - center_[0]) ** 2 + abs(center[1] - center_[1]) ** 2) ** 0.5
            if dist <= configs["MIN_DIST"]:
                close = True
                break
        if not close:
            elem_list.append(elem)
    return elem_list


def screen_fingerprint(xml_path: str) -> str:
    """用可交互控件 uid 集合生成稳定屏幕指纹。"""
    clickable_list: List[AndroidElement] = []
    focusable_list: List[AndroidElement] = []
    traverse_tree(xml_path, clickable_list, "clickable", True)
    traverse_tree(xml_path, focusable_list, "focusable", True)
    uids = sorted({e.uid for e in clickable_list} | {e.uid for e in focusable_list})
    raw = "|".join(uids)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


@dataclass
class PathStepRecord:
    step_index: int
    mode: str
    action: str
    detail: str
    screen_fp_before: str
    screen_fp_after: Optional[str]
    path_indices: List[int]


@dataclass
class ExplorationContext:
    app: str
    root_dir: str
    mode_name: str
    configs: dict
    controller: AndroidController
    mllm: object
    task_dir: str
    useless_list: Set[str] = field(default_factory=set)
    seen_control_uids: Set[str] = field(default_factory=set)
    """仅统计「探索」动作（前向 tap、模型辅助返回的一次操作），不含系统 back 与冷启动重放。"""
    exploration_steps: int = 0
    max_exploration_steps: int = 20
    path_log: List[PathStepRecord] = field(default_factory=list)
    jsonl_path: str = ""
    report_md_path: str = ""

    def request_interval(self) -> float:
        return float(self.configs.get("REQUEST_INTERVAL", 10))

    def sleep_interval(self) -> None:
        time.sleep(self.request_interval())

    def try_consume_exploration_step(self, reason: str = "") -> bool:
        self.exploration_steps += 1
        if self.exploration_steps > self.max_exploration_steps:
            print_with_color(f"达到最大探索步数 {self.max_exploration_steps}，停止。{reason}", "yellow")
            return False
        return True

    def exploration_budget_left(self) -> bool:
        return self.exploration_steps < self.max_exploration_steps

    def append_jsonl(self, obj: dict) -> None:
        if not self.jsonl_path:
            return
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def capture_screen(self, prefix: str) -> Tuple[str, str, str, List[AndroidElement]]:
        """截图 + dump XML，返回 (shot_path, xml_path, fingerprint, elem_list)。"""
        shot = self.controller.get_screenshot(prefix, self.task_dir)
        xml_path = self.controller.get_xml(prefix, self.task_dir)
        if shot == "ERROR" or xml_path == "ERROR":
            return "ERROR", "ERROR", "", []
        fp = screen_fingerprint(xml_path)
        elems = build_elem_list(xml_path, self.useless_list, self.configs)
        return shot, xml_path, fp, elems

    def record_new_controls(
        self,
        elem_list: List[AndroidElement],
        screenshot_raw: str,
        tag_prefix: str,
    ) -> List[str]:
        """每个首次出现的控件单独保存一张带编号标注的图（仅该控件一个标签）。"""
        saved: List[str] = []
        dark = self.configs.get("DARK_MODE", False)
        for idx, elem in enumerate(elem_list, start=1):
            if elem.uid in self.seen_control_uids:
                continue
            self.seen_control_uids.add(elem.uid)
            name = f"{tag_prefix}_newctl_{idx}_{_sanitize_uid_for_file(elem.uid)}.png"
            out = os.path.join(self.task_dir, name)
            draw_bbox_multi(screenshot_raw, out, [elem], dark_mode=dark)
            saved.append(out)
            self.append_jsonl({
                "type": "new_control",
                "file": name,
                "uid": elem.uid,
                "label_index": idx,
                "exploration_step": self.exploration_steps,
            })
        return saved

    def tap_elem_index(self, elem_list: List[AndroidElement], index_one_based: int) -> bool:
        if index_one_based < 1 or index_one_based > len(elem_list):
            print_with_color(f"tap 下标越界: {index_one_based}", "red")
            return False
        tl, br = elem_list[index_one_based - 1].bbox
        x = (tl[0] + br[0]) // 2
        y = (tl[1] + br[1]) // 2
        ret = self.controller.tap(x, y)
        return ret != "ERROR"

    def execute_parsed_action(self, res: List, elem_list: List[AndroidElement]) -> bool:
        """执行 parse_explore_rsp 结果（不含 FINISH）。"""
        act_name = res[0]
        if act_name == "tap":
            area = int(res[1])
            return self.tap_elem_index(elem_list, area)
        if act_name == "text":
            input_str = res[1]
            ret = self.controller.text(input_str)
            return ret != "ERROR"
        if act_name == "long_press":
            area = int(res[1])
            tl, br = elem_list[area - 1].bbox
            x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
            ret = self.controller.long_press(x, y)
            return ret != "ERROR"
        if act_name == "swipe":
            area = int(res[1])
            swipe_dir = res[2]
            dist = res[3]
            tl, br = elem_list[area - 1].bbox
            x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
            ret = self.controller.swipe(x, y, swipe_dir, dist)
            return ret != "ERROR"
        return False

    def back_key_once(self) -> None:
        self.controller.back()
        time.sleep(1.0)

    def llm_navigate_back(self, labeled_image_path: str, elem_list: List[AndroidElement]) -> bool:
        """模型在带编号截图上选择返回方式（tap/text/long_press/swipe）；消耗 1 次探索步数。"""
        prompt = prompts.back_navigation_template
        print_with_color("系统返回无效，请求模型选择返回操作...", "yellow")
        ok, rsp = self.mllm.get_model_response(prompt, [labeled_image_path])
        if not ok:
            print_with_color(str(rsp), "red")
            return False
        res = parse_explore_rsp(rsp)
        if res[0] == "FINISH":
            return True
        if res[0] == "ERROR":
            return False
        if not self.try_consume_exploration_step("模型返回导航"):
            return False
        self.sleep_interval()
        ok = self.execute_parsed_action(res, elem_list)
        return ok

    def try_path_replay_instead_of_back(
        self,
        xml_path: str,
        cur_fp: str,
        target_fp: str,
        parent_path: List[int],
    ) -> Optional[bool]:
        """
        若当前已在底部 Tab 主界面干净态：不再按 KEYCODE_BACK（避免误退桌面），也不做冷启动杀进程；
        若需要回到父屏指纹，仅在本应用内按 parent_path 重放点击（cold_start=False）。
        返回 True=已到目标指纹；False=重放失败或仍未匹配；None=不适用（非主 Tab 干净态）。
        """
        h = int(self.controller.height or 0)
        if h <= 0:
            h = 1920
        if not is_clean_main_tab_home(xml_path, h):
            return None
        print_with_color(
            "主 Tab 干净界面：跳过系统返回与冷启动，仅在应用内重放父路径点击（不杀进程）…",
            "yellow",
        )
        self.append_jsonl(
            {
                "type": "skip_back_main_tab_home",
                "cur_fp": cur_fp[:24],
                "target_fp": target_fp[:24],
            }
        )
        fp_r, _, shot = self.navigate_to_path(parent_path, cold_start=False)
        if shot == "ERROR":
            return False
        return fp_r == target_fp

    def maybe_recover_outside_app(self, parent_path: List[int], target_fp: str) -> Optional[bool]:
        """
        若前台已不是目标应用（例如连按返回到了模拟器桌面），则冷启动并导航回 parent_path。
        返回 None=仍在目标应用内无需处理；True=已恢复且指纹匹配；False=已恢复但指纹仍不符或失败。
        """
        pkg = self.controller.get_foreground_package()
        if not pkg or pkg == self.app:
            return None
        print_with_color(
            f"检测到前台包为 {pkg}（目标 {self.app}），已退出应用，冷启动并回父路径…",
            "yellow",
        )
        self.append_jsonl({"type": "recover_outside_app", "foreground": pkg, "target": self.app})
        self.controller.start_app()
        time.sleep(2)
        fp_r, _, shot = self.navigate_to_path(parent_path, cold_start=False)
        if shot == "ERROR":
            return False
        return fp_r == target_fp

    def navigate_back_to_fingerprint(self, target_fp: str, parent_path: List[int]) -> bool:
        """先 KEYCODE_BACK；主 Tab 干净界面则跳过后退改为重放路径；界面未回到目标则模型介入；退出应用则冷启动重放。"""
        for attempt in range(8):
            shot, xml_path, cur_fp, elems = self.capture_screen(f"back_nav_{attempt}")
            if shot == "ERROR":
                return False
            if cur_fp == target_fp:
                return True

            tr = self.try_path_replay_instead_of_back(xml_path, cur_fp, target_fp, parent_path)
            if tr is True:
                return True
            if tr is False:
                return False

            rec = self.maybe_recover_outside_app(parent_path, target_fp)
            if rec is True:
                return True

            fp_before = cur_fp
            self.back_key_once()
            self.sleep_interval()

            rec2 = self.maybe_recover_outside_app(parent_path, target_fp)
            if rec2 is True:
                return True

            shot2, xml_path2, fp_after, elems2 = self.capture_screen(f"back_after_key_{attempt}")
            if shot2 == "ERROR":
                return False
            if fp_after == target_fp:
                return True

            tr2 = self.try_path_replay_instead_of_back(xml_path2, fp_after, target_fp, parent_path)
            if tr2 is True:
                return True
            if tr2 is False:
                return False

            rec3 = self.maybe_recover_outside_app(parent_path, target_fp)
            if rec3 is True:
                return True

            if fp_after == fp_before and elems2:
                labeled = os.path.join(self.task_dir, f"back_labeled_{self.exploration_steps}_{attempt}.png")
                draw_bbox_multi(shot2, labeled, elems2, dark_mode=self.configs.get("DARK_MODE", False))
                if self.llm_navigate_back(labeled, elems2):
                    shot3, xml_path3, fp3, _ = self.capture_screen(f"back_after_llm_{attempt}")
                    if fp3 == target_fp:
                        return True
                    tr3 = self.try_path_replay_instead_of_back(xml_path3, fp3, target_fp, parent_path)
                    if tr3 is True:
                        return True
                    if tr3 is False:
                        return False
                    rec4 = self.maybe_recover_outside_app(parent_path, target_fp)
                    if rec4 is True:
                        return True

        print_with_color("返回失败，先尝试不重载进程重放父路径…", "yellow")
        fp_r, _, shot = self.navigate_to_path(parent_path, cold_start=False)
        if shot != "ERROR" and fp_r == target_fp:
            return True
        print_with_color("仍无法对齐父屏，最后手段：冷启动并重放路径…", "yellow")
        fp_r, _, shot = self.navigate_to_path(parent_path)
        return shot != "ERROR" and fp_r == target_fp

    def navigate_to_path(self, path: List[int], cold_start: bool = True) -> Tuple[str, List[AndroidElement], str]:
        """冷启动应用并按 path 依次点击编号（不计探索步数）；返回 (fingerprint, elem_list, screenshot_path)。"""
        if cold_start:
            self.controller.start_app()
            time.sleep(2)
        for depth, idx in enumerate(path):
            prefix = f"nav_{depth}_i{idx}"
            shot, _, _, elems = self.capture_screen(prefix)
            if shot == "ERROR" or not elems:
                print_with_color(f"导航失败 depth={depth} idx={idx}", "red")
                return "", [], "ERROR"
            if idx < 1 or idx > len(elems):
                print_with_color(f"导航下标非法: {idx} len={len(elems)}", "red")
                return "", [], "ERROR"
            self.tap_elem_index(elems, idx)
            self.sleep_interval()
        tag = "_".join(str(x) for x in path) if path else "root"
        shot, _, fp, elems = self.capture_screen(f"nav_end_{tag}")
        if shot == "ERROR":
            return "", [], "ERROR"
        return fp, elems, shot

    def write_report_md(self) -> None:
        lines = [
            f"# 结构化{self.mode_name}探索报告",
            f"- 应用: {self.app}",
            f"- 目录: {self.task_dir}",
            f"- 探索步数: {self.exploration_steps} / {self.max_exploration_steps}",
            f"- 已发现控件 uid 数: {len(self.seen_control_uids)}",
            "",
            "## 路径与操作",
        ]
        for rec in self.path_log:
            path_str = "→".join(str(x) for x in rec.path_indices) if rec.path_indices else "(根)"
            lines.append(
                f"- Step {rec.step_index} [{rec.mode}] {rec.action} | {rec.detail} | "
                f"path=[{path_str}] | fp {rec.screen_fp_before[:8]}… → "
                f"{(rec.screen_fp_after or '')[:8]}…"
            )
        lines.append("")
        lines.append("## JSONL 明细")
        lines.append(f"- `{os.path.basename(self.jsonl_path)}`")
        content = "\n".join(lines)
        with open(self.report_md_path, "w", encoding="utf-8") as f:
            f.write(content)
        print_with_color(f"报告已写入: {self.report_md_path}", "green")


def create_mllm(configs: dict):
    if configs["MODEL"] == "OpenAI":
        return OpenAIModel(
            base_url=configs["OPENAI_API_BASE"],
            api_key=configs["OPENAI_API_KEY"],
            model=configs["OPENAI_API_MODEL"],
            temperature=configs["TEMPERATURE"],
            max_tokens=configs["MAX_TOKENS"],
        )
    if configs["MODEL"] == "Qwen":
        return QwenModel(api_key=configs["DASHSCOPE_API_KEY"], model=configs["QWEN_MODEL"])
    print_with_color(f"ERROR: Unsupported model {configs['MODEL']}", "red")
    sys.exit(1)


def pick_device(configs: dict) -> str:
    device_list = list_all_devices()
    if not device_list:
        print_with_color("ERROR: No device found!", "red")
        sys.exit(1)
    configured = configs.get("app", {}).get("device")
    if configured and configured in device_list:
        return configured
    if "emulator-5554" in device_list:
        return "emulator-5554"
    if len(device_list) == 1:
        return device_list[0]
    print_with_color("请选择设备 ID:", "blue")
    return input().strip()


def build_exploration_context(
    app: str,
    root_dir: str,
    mode_name: str,
    max_steps: Optional[int] = None,
) -> ExplorationContext:
    configs = load_config()
    device = pick_device(configs)
    print_with_color(f"使用设备: {device}", "yellow")

    work_dir = os.path.join(root_dir, "apps", app)
    os.makedirs(work_dir, exist_ok=True)
    demo_dir = os.path.join(work_dir, "demos")
    os.makedirs(demo_dir, exist_ok=True)
    ts = int(time.time())
    task_name = datetime.datetime.fromtimestamp(ts).strftime(f"{mode_name}_%Y-%m-%d_%H-%M-%S")
    task_dir = os.path.join(demo_dir, task_name)
    os.makedirs(task_dir, exist_ok=True)

    controller = AndroidController(device)
    w, h = controller.get_device_size()
    if not w or not h:
        print_with_color("ERROR: Invalid device size!", "red")
        sys.exit(1)

    mllm = create_mllm(configs)
    ms = max_steps if max_steps is not None else int(configs.get("MAX_ROUNDS", 20))

    jsonl_path = os.path.join(task_dir, f"log_{mode_name}.jsonl")
    report_md_path = os.path.join(task_dir, f"report_{mode_name}.md")

    ctx = ExplorationContext(
        app=app,
        root_dir=root_dir,
        mode_name=mode_name,
        configs=configs,
        controller=controller,
        mllm=mllm,
        task_dir=task_dir,
        max_exploration_steps=ms,
        jsonl_path=jsonl_path,
        report_md_path=report_md_path,
    )
    with open(jsonl_path, "w", encoding="utf-8") as f:
        f.write("")
    print_with_color(f"任务目录: {task_dir}", "yellow")
    return ctx
