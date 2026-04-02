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


def parse_ui_bounds_size_from_xml(xml_path: str) -> Tuple[int, int]:
    """
    取 UI 坐标系宽高（与 uiautomator bounds 同坐标系）。
    1) 优先 bounds=\"[0,0][W,H]\" 中面积最大者（整屏根，可排除状态栏 [0,0][1080,72]）。
    2) 否则用全文 max(x2)、max(y2)。仅弹窗树时 max_y2 可能只有 ~1014，需配合 scale 里用 wm 比例修正。
    """
    try:
        with open(xml_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(6_000_000)
    except OSError:
        return 0, 0
    best00_area = 0
    best00_wh: Tuple[int, int] = (0, 0)
    for m in re.finditer(r'bounds="\[0,0\]\[(\d+),(\d+)\]"', content):
        w, h = int(m.group(1)), int(m.group(2))
        if w < 200 or h < 400:
            continue
        a = w * h
        if a > best00_area:
            best00_area = a
            best00_wh = (max(1, w), max(1, h))
    if best00_wh[0] > 0 and best00_wh[1] > 0:
        return best00_wh

    max_x2, max_y2 = 0, 0
    best_area = 0
    best_wh: Tuple[int, int] = (0, 0)
    for m in re.finditer(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', content):
        x1, y1, x2, y2 = map(int, m.groups())
        max_x2 = max(max_x2, x2)
        max_y2 = max(max_y2, y2)
        w, h = x2 - x1, y2 - y1
        if w > 0 and h > 0:
            area = w * h
            if area > best_area:
                best_area = area
                best_wh = (max(1, w), max(1, h))
    if max_x2 >= 100 and max_y2 >= 100:
        return max(1, max_x2), max(1, max_y2)
    return best_wh if best_area > 0 else (0, 0)


def scale_ui_coords_to_touch(
    x: int,
    y: int,
    xml_path: str,
    dev_w: int,
    dev_h: int,
) -> Tuple[int, int]:
    """将 uiautomator bounds 中心点映射到当前设备 input 坐标（含边界夹紧）。"""
    uw, uh = parse_ui_bounds_size_from_xml(xml_path)
    if uw > 0 and uh > 0 and dev_w > 0 and dev_h > 0:
        # 仅含弹窗/裁切层时 max_y2 可能只有 ~1014，但元素坐标仍是全屏 1080×1920 空间；用 wm 与宽度推算整屏高度
        if uh < 1400 and uw >= 400:
            uh2 = int(round(uw * dev_h / dev_w))
            if uh2 >= 800:
                uh = uh2
        if uw != dev_w or uh != dev_h:
            x = int(round(x * dev_w / uw))
            y = int(round(y * dev_h / uh))
    if dev_w > 0:
        x = max(0, min(dev_w - 1, x))
    if dev_h > 0:
        y = max(0, min(dev_h - 1, y))
    return x, y


def _short_uid_for_report(detail: str, max_len: int = 72) -> str:
    """从 PathStepRecord.detail 中抽出 uid 显示，过长则尾部截断。"""
    m = re.search(r"uid=(.+)$", detail, re.DOTALL)
    if m:
        u = m.group(1).strip()
        if len(u) > max_len:
            return "…" + u[-max_len:]
        return u
    return detail[:max_len] if len(detail) > max_len else detail


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
    if is_probable_chat_conversation_screen(xml_path):
        return False
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


def _bbox_key(bbox: Tuple[Tuple[int, int], Tuple[int, int]]) -> Tuple[int, int, int, int]:
    tl, br = bbox
    return (tl[0], tl[1], br[0], br[1])


def _parse_bounds_tuple(bounds_str: str) -> Optional[Tuple[int, int, int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if not m:
        return None
    return tuple(map(int, m.groups()))


def _find_bottom_input_top_y(xml_path: str, uh: int) -> Optional[int]:
    """底部输入区 EditText 的上边界 y（仅取靠屏幕下半部分的输入框）。"""
    best: Optional[int] = None
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception:
        return None
    for elem in root.iter():
        att = elem.attrib
        cls = att.get("class", "")
        b = att.get("bounds")
        if not b or "EditText" not in cls:
            continue
        t = _parse_bounds_tuple(b)
        if not t:
            continue
        _, y1, _, _ = t
        if y1 < int(uh * 0.55):
            continue
        if best is None or y1 < best:
            best = y1
    return best


def is_probable_chat_conversation_screen(xml_path: str) -> bool:
    """
    启发式：单聊/会话页 — 底部有输入框，且上方有大块 RecyclerView/ListView 作为消息区。
    用于与「仅顶部搜索 + 联系人列表」等界面区分。
    """
    uw, uh = parse_ui_bounds_size_from_xml(xml_path)
    if uh <= 0:
        return False
    input_top = _find_bottom_input_top_y(xml_path, uh)
    if input_top is None:
        return False
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception:
        return False
    for elem in root.iter():
        att = elem.attrib
        cls = att.get("class", "")
        b = att.get("bounds")
        if not b:
            continue
        t = _parse_bounds_tuple(b)
        if not t:
            continue
        x1, y1, x2, y2 = t
        h = y2 - y1
        if "RecyclerView" not in cls and "ListView" not in cls:
            continue
        if h < int(uh * 0.16):
            continue
        if y1 < int(uh * 0.05):
            continue
        if y2 > input_top + 35:
            continue
        if y1 < int(uh * 0.45) and y2 > int(uh * 0.22):
            return True
    return False


def should_trust_fp_parent_match(
    cur_fp: str,
    target_fp: str,
    parent_path: List[int],
    xml_path: str,
) -> bool:
    """
    判断「当前指纹 == 父屏指纹」是否可信。
    父路径为空表示目标为根（消息列表）：若 XML 已是单聊页，指纹可能与列表根相同，不能视为已回到父屏。
    """
    if cur_fp != target_fp:
        return False
    if len(parent_path) != 0:
        return True
    return not is_probable_chat_conversation_screen(xml_path)


def get_chat_message_list_rect(xml_path: str) -> Optional[Tuple[int, int, int, int]]:
    """
    会话页消息列表区域 (x1,y1,x2,y2)，用于过滤中间区域的重复短按 / 多余长按。
    若无法判定则返回 None。
    """
    if not is_probable_chat_conversation_screen(xml_path):
        return None
    uw, uh = parse_ui_bounds_size_from_xml(xml_path)
    if uh <= 0:
        return None
    input_top = _find_bottom_input_top_y(xml_path, uh)
    if input_top is None:
        return None
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception:
        return None
    best: Optional[Tuple[int, int, int, int]] = None
    best_area = 0
    for elem in root.iter():
        att = elem.attrib
        cls = att.get("class", "")
        b = att.get("bounds")
        if not b:
            continue
        t = _parse_bounds_tuple(b)
        if not t:
            continue
        x1, y1, x2, y2 = t
        h = y2 - y1
        if "RecyclerView" not in cls and "ListView" not in cls:
            continue
        if h < int(uh * 0.14):
            continue
        if y2 > input_top + 40:
            continue
        if y1 < int(uh * 0.04):
            continue
        y2c = min(y2, input_top - 2)
        if y2c <= y1 + 20:
            continue
        area = (x2 - x1) * (y2c - y1)
        if area > best_area:
            best_area = area
            best = (x1, y1, x2, y2c)
    if best is None:
        y1 = int(uh * 0.11)
        y2 = max(y1 + 24, input_top - 2)
        if y2 > y1:
            return (0, y1, uw, y2)
    return best


def _center_in_rect(cx: int, cy: int, rect: Tuple[int, int, int, int]) -> bool:
    x1, y1, x2, y2 = rect
    return x1 <= cx <= x2 and y1 <= cy <= y2


def filter_elem_list_for_chat_message_area(
    elem_list: List[AndroidElement],
    xml_path: str,
    configs: dict,
) -> List[AndroidElement]:
    """
    聊天会话页：消息区（列表+时间等）短按通常无新状态，全部跳过；
    长按仅保留一条，避免对每条消息重复长按。
    顶栏（返回/更多）、底栏（输入框/+/表情等）保留。
    """
    if not configs.get("EXPLORATION_CHAT_SKIP_MESSAGE_TAPS", True):
        return elem_list
    rect = get_chat_message_list_rect(xml_path)
    if rect is None:
        return elem_list
    out: List[AndroidElement] = []
    long_kept = False
    skipped_tap = 0
    skipped_long = 0
    for elem in elem_list:
        tl, br = elem.bbox
        cx = (tl[0] + br[0]) // 2
        cy = (tl[1] + br[1]) // 2
        if not _center_in_rect(cx, cy, rect):
            out.append(elem)
            continue
        kind = getattr(elem, "interaction_kind", "tap")
        if kind == "tap":
            skipped_tap += 1
            continue
        if kind == "long_press":
            if not long_kept:
                out.append(elem)
                long_kept = True
            else:
                skipped_long += 1
            continue
        out.append(elem)
    if skipped_tap or skipped_long:
        print_with_color(
            f"聊天会话页消息区过滤：跳过短按 {skipped_tap} 项，长按仅保留 1 条（省略重复长按 {skipped_long}）",
            "yellow",
        )
    return out


def maybe_append_synthetic_chat_long_press(
    elem_list: List[AndroidElement],
    xml_path: str,
    configs: dict,
    useless_list: Set[str],
) -> List[AndroidElement]:
    """
    许多 IM 聊天气泡在无障碍树里不写 long-clickable，导致只有短按枚举。
    在已判定为会话页且当前列表中仍无任何长按时，在消息区中心注入 1 个合成长按目标。
    """
    if not configs.get("EXPLORATION_CHAT_SYNTHETIC_LONG_PRESS", True):
        return elem_list
    if not configs.get("EXPLORATION_CHAT_SKIP_MESSAGE_TAPS", True):
        return elem_list
    if "__synthetic_chat_message_longpress__" in useless_list:
        return elem_list
    rect = get_chat_message_list_rect(xml_path)
    if rect is None:
        return elem_list
    if any(getattr(e, "interaction_kind", "tap") == "long_press" for e in elem_list):
        return elem_list
    x1, y1, x2, y2 = rect
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    half = min(120, max(32, (x2 - x1) // 6, (y2 - y1) // 8))
    tl = (max(0, cx - half), max(0, cy - half))
    br = (min(x2, cx + half), min(y2, cy + half))
    # 插在列表最前，使 BFS 优先尝试消息区长按，避免先枚举大量无意义的顶栏/底栏短按
    elem_list.insert(
        0,
        AndroidElement(
            "__synthetic_chat_message_longpress__",
            (tl, br),
            "synthetic",
            "long_press",
        ),
    )
    print_with_color(
        "聊天页无障碍树未声明 long-clickable：已注入 1 次合成长按（消息列表区域中心，编号为 1）",
        "yellow",
    )
    return elem_list


def build_elem_list(xml_path: str, useless_list: Set[str], configs: dict) -> List[AndroidElement]:
    """可点击 + 去重后的可聚焦；可选再追加 long-clickable（长按），用于聊天气泡等需长按出菜单的场景。"""
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

    if configs.get("EXPLORATION_INCLUDE_LONG_CLICKABLE", True):
        longclick_list: List[AndroidElement] = []
        traverse_tree(xml_path, longclick_list, "long-clickable", True)
        long_bbox_added: Set[Tuple[int, int, int, int]] = set()
        for elem in longclick_list:
            if elem.uid in useless_list:
                continue
            k = _bbox_key(elem.bbox)
            if k in long_bbox_added:
                continue
            long_bbox_added.add(k)
            elem_list.append(
                AndroidElement(
                    elem.uid + "__longpress",
                    elem.bbox,
                    "long-clickable",
                    "long_press",
                )
            )
    elem_list = filter_elem_list_for_chat_message_area(elem_list, xml_path, configs)
    elem_list = maybe_append_synthetic_chat_long_press(elem_list, xml_path, configs, useless_list)
    elem_list = filter_drop_fullscreen_more_dialog_root(elem_list, xml_path)
    return elem_list


def _is_fullscreen_more_dialog_root_uid(uid: str) -> bool:
    """右上角「+」展开菜单：全屏 RelativeLayout id_more_dialog 会占满编号且点在空白处会关菜单。"""
    if "more_dialog" not in uid and "id_more_dialog" not in uid:
        return False
    if "1080_1848" in uid or "RelativeLayout_1080" in uid:
        return True
    return "id_more_dialog_0" in uid and "RelativeLayout" in uid


def filter_drop_fullscreen_more_dialog_root(elem_list: List[AndroidElement], xml_path: str) -> List[AndroidElement]:
    try:
        raw = open(xml_path, encoding="utf-8").read()
    except OSError:
        return elem_list
    if "right_list" not in raw and "id_right_list" not in raw:
        return elem_list
    out = [e for e in elem_list if not _is_fullscreen_more_dialog_root_uid(e.uid)]
    return out if len(out) >= 2 else elem_list


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
    """前向 tap：step_{n}_before.png / step_{n}_after.png 文件名。"""
    screenshot_before: Optional[str] = None
    screenshot_after: Optional[str] = None


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
    """最近一次成功 capture_screen 的 XML，用于与 bounds 同坐标系缩放到触摸坐标。"""
    last_xml_path: str = ""
    """BFS 根路径 [] 冷启动得到的消息列表指纹；用于底栏 tap 前对齐到同一屏，避免停在通话/好友时编号错位。"""
    root_baseline_fp: str = ""

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
        self.last_xml_path = xml_path
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

    def _touch_center_from_bbox(
        self, tl: Tuple[int, int], br: Tuple[int, int], xml_path: Optional[str] = None
    ) -> Tuple[int, int]:
        x_raw = (tl[0] + br[0]) // 2
        y_raw = (tl[1] + br[1]) // 2
        xp = xml_path or self.last_xml_path
        dw, dh = int(self.controller.width or 0), int(self.controller.height or 0)
        if xp and os.path.isfile(xp) and dw and dh:
            uw, uh = parse_ui_bounds_size_from_xml(xp)
            x, y = scale_ui_coords_to_touch(x_raw, y_raw, xp, dw, dh)
            if uw and uh and (uw != dw or uh != dh):
                print_with_color(
                    f"UI→触摸缩放: 中心 ({x_raw},{y_raw}) → ({x},{y})，bounds {uw}x{uh} → 设备 {dw}x{dh}",
                    "blue",
                )
            return x, y
        x, y = x_raw, y_raw
        if dw > 0:
            x = max(0, min(dw - 1, x))
        if dh > 0:
            y = max(0, min(dh - 1, y))
        return x, y

    def tap_elem_index(self, elem_list: List[AndroidElement], index_one_based: int) -> bool:
        if index_one_based < 1 or index_one_based > len(elem_list):
            print_with_color(f"tap 下标越界: {index_one_based}", "red")
            return False
        tl, br = elem_list[index_one_based - 1].bbox
        x, y = self._touch_center_from_bbox(tl, br)
        kind = getattr(elem_list[index_one_based - 1], "interaction_kind", "tap")
        if kind == "long_press":
            print_with_color("执行长按（long-clickable 探索项）", "blue")
            ret = self.controller.long_press(x, y)
        else:
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
            x, y = self._touch_center_from_bbox(tl, br)
            ret = self.controller.long_press(x, y)
            return ret != "ERROR"
        if act_name == "swipe":
            area = int(res[1])
            swipe_dir = res[2]
            dist = res[3]
            tl, br = elem_list[area - 1].bbox
            x, y = self._touch_center_from_bbox(tl, br)
            ret = self.controller.swipe(x, y, swipe_dir, dist)
            return ret != "ERROR"
        return False

    def back_key_once(self) -> None:
        self.controller.back()
        time.sleep(1.0)

    def sync_root_path_to_baseline_fp(
        self,
        fp_baseline: str,
        fp_current: str,
        elems: List[AndroidElement],
        shot: str,
    ) -> Tuple[str, List[AndroidElement], str]:
        """
        path==[] 时：若当前为主 Tab 之一但指纹≠冷启动根基准（常见于上一操作停在通话/好友/我的），
        点一次「消息」底栏回到与编号表一致的列表屏。
        """
        if not fp_baseline or fp_current == fp_baseline or shot == "ERROR":
            return fp_current, elems, shot
        xp = self.last_xml_path
        if not xp or not os.path.isfile(xp):
            return fp_current, elems, shot
        h = int(self.controller.height or 0)
        if h <= 0:
            h = 1920
        if not is_clean_main_tab_home(xp, h):
            return fp_current, elems, shot
        print_with_color("根路径：当前指纹与消息列表基准不一致，先点「消息」底栏对齐控件编号…", "yellow")
        self.append_jsonl(
            {
                "type": "bfs_sync_message_tab_for_baseline",
                "fp_was": fp_current[:24],
                "fp_target": fp_baseline[:24],
            }
        )
        menu_main_idx: Optional[int] = None
        for j, e in enumerate(elems):
            if "rl_menu_main" in e.uid:
                menu_main_idx = j + 1
                break
        if menu_main_idx is None:
            return fp_current, elems, shot
        self.tap_elem_index(elems, menu_main_idx)
        self.sleep_interval()
        shot2, _, fp2, elems2 = self.capture_screen("root_sync_msg_tab")
        if shot2 == "ERROR":
            return fp_current, elems, shot
        return fp2, elems2, shot2

    def _ensure_back_from_chat_before_path_replay(self) -> None:
        """
        不重放冷启动（cold_start=False）时，若仍停留在单聊/会话页，先按返回回到消息列表。
        否则控件编号对应的是聊天页上的项，会把底栏/输入区误当成「列表第几项」去点。
        """
        for attempt in range(5):
            shot, xml_path, _, _ = self.capture_screen(f"pre_replay_chat_{attempt}")
            if shot == "ERROR":
                return
            if not is_probable_chat_conversation_screen(xml_path):
                return
            print_with_color(
                "重放路径前检测到仍在单聊/会话页，按返回键回到消息列表…",
                "yellow",
            )
            self.append_jsonl({"type": "pre_replay_back_from_chat", "attempt": attempt})
            self.back_key_once()

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
            if should_trust_fp_parent_match(cur_fp, target_fp, parent_path, xml_path):
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
            if should_trust_fp_parent_match(fp_after, target_fp, parent_path, xml_path2):
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
                    if should_trust_fp_parent_match(fp3, target_fp, parent_path, xml_path3):
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
            _, xml_v, _, _ = self.capture_screen("back_replay_verify")
            if xml_v != "ERROR" and should_trust_fp_parent_match(fp_r, target_fp, parent_path, xml_v):
                return True

        print_with_color("仍无法对齐父屏，最后手段：冷启动并重放路径…", "yellow")
        fp_r, _, shot = self.navigate_to_path(parent_path)
        if shot == "ERROR" or fp_r != target_fp:
            return False
        _, xml_v2, _, _ = self.capture_screen("back_cold_verify")
        if xml_v2 != "ERROR":
            return should_trust_fp_parent_match(fp_r, target_fp, parent_path, xml_v2)
        return True

    def navigate_to_path(
        self,
        path: List[int],
        cold_start: bool = True,
        *,
        _retried_cold: bool = False,
    ) -> Tuple[str, List[AndroidElement], str]:
        """冷启动应用并按 path 依次点击编号（不计探索步数）；返回 (fingerprint, elem_list, screenshot_path)。"""
        if cold_start:
            self.controller.start_app()
            time.sleep(2)
        else:
            self._ensure_back_from_chat_before_path_replay()
        for depth, idx in enumerate(path):
            prefix = f"nav_{depth}_i{idx}"
            shot, _, _, elems = self.capture_screen(prefix)
            if shot == "ERROR" or not elems:
                print_with_color(f"导航失败 depth={depth} idx={idx}", "red")
                return "", [], "ERROR"
            if idx < 1 or idx > len(elems):
                if not _retried_cold:
                    print_with_color(
                        f"导航下标非法: {idx} len={len(elems)}，冷启动重试一次 path…",
                        "yellow",
                    )
                    self.append_jsonl(
                        {
                            "type": "nav_path_index_retry_cold_start",
                            "path": path,
                            "depth": depth,
                            "bad_index": idx,
                            "len_elems": len(elems),
                        }
                    )
                    return self.navigate_to_path(path, cold_start=True, _retried_cold=True)
                print_with_color(f"导航下标非法: {idx} len={len(elems)}", "red")
                self.append_jsonl(
                    {
                        "type": "nav_path_index_invalid",
                        "path": path,
                        "depth": depth,
                        "bad_index": idx,
                        "len_elems": len(elems),
                    }
                )
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
        ]
        if self.mode_name == "bfs":
            lines.extend(self._write_bfs_report_sections())
        else:
            lines.append("## 路径与操作")
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

    def _write_bfs_report_sections(self) -> List[str]:
        """BFS：主表仅前向 tap，每步 step_{n}_before + step_{n}_after。"""
        out: List[str] = []
        out.append("## 前向探索主表（每步：操作前 / 操作后；长按用于 long-clickable 菜单）")
        out.append("")
        out.append(
            "| 步号 | 动作 | 操作前 | 操作后 | 路径编号 | 指向控件 | 指纹(前→后) |"
        )
        out.append("| --- | --- | --- | --- | --- | --- | --- |")
        for rec in self.path_log:
            if rec.mode != "bfs" or rec.action not in ("tap", "long_press"):
                continue
            path_str = "→".join(str(x) for x in rec.path_indices) if rec.path_indices else "—"
            img_b = "—"
            img_a = "—"
            if rec.screenshot_before:
                img_b = f"![]({rec.screenshot_before})"
            if rec.screenshot_after:
                img_a = f"![]({rec.screenshot_after})"
            uid_short = _short_uid_for_report(rec.detail)
            fp_a = rec.screen_fp_before[:8] if rec.screen_fp_before else ""
            fp_b = (rec.screen_fp_after or "")[:8] if rec.screen_fp_after else ""
            act_label = "长按" if rec.action == "long_press" else "短按"
            out.append(
                f"| {rec.step_index} | {act_label} | {img_b} | {img_a} | `{path_str}` | {uid_short} | `{fp_a}` → `{fp_b}` |"
            )
        out.append("")
        out.append("## 路径与操作（文本明细）")
        for rec in self.path_log:
            if rec.mode != "bfs" or rec.action not in ("tap", "long_press"):
                continue
            path_str = "→".join(str(x) for x in rec.path_indices) if rec.path_indices else "(根)"
            sb = rec.screenshot_before or "—"
            sa = rec.screenshot_after or "—"
            out.append(
                f"- Step {rec.step_index} | 前 `{sb}` 后 `{sa}` | {rec.detail} | path=[{path_str}] | "
                f"fp {rec.screen_fp_before[:8]}… → {(rec.screen_fp_after or '')[:8]}…"
            )
        return out


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
