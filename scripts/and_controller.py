import os
import re
import subprocess
from typing import List, Optional, Tuple
import xml.etree.ElementTree as ET
import time

from .config import load_config
from .utils import print_with_color


configs = load_config()


def _safe_subprocess_run(
    cmd: List[str],
    *,
    timeout: float,
    text: bool = True,
) -> Optional[subprocess.CompletedProcess]:
    """捕获 TimeoutExpired，避免 BFS 整条链路崩溃；模拟器上 uiautomator dump 易卡死。"""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        short = " ".join(cmd[:5]) + ("…" if len(cmd) > 5 else "")
        print_with_color(f"子进程超时 ({timeout:.0f}s)，已跳过: {short}", "yellow")
        return None


class AndroidElement:
    def __init__(self, uid, bbox, attrib, interaction_kind="tap"):
        self.uid = uid
        self.bbox = bbox
        self.attrib = attrib
        """tap：短按；long_press：长按（来自 long-clickable 节点）。"""
        self.interaction_kind = interaction_kind


def execute_adb(adb_command):
    print(adb_command)
    result = subprocess.run(adb_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    print_with_color(f"Command execution failed: {adb_command}", "red")
    print_with_color(result.stderr, "red")
    return "ERROR"


def list_all_devices():
    adb_command = "adb devices"
    device_list = []
    result = execute_adb(adb_command)
    if result != "ERROR":
        devices = result.split("\n")[1:]
        for d in devices:
            device_list.append(d.split()[0])

    return device_list


def get_id_from_element(elem):
    bounds = elem.attrib["bounds"][1:-1].split("][")
    x1, y1 = map(int, bounds[0].split(","))
    x2, y2 = map(int, bounds[1].split(","))
    elem_w, elem_h = x2 - x1, y2 - y1
    if "resource-id" in elem.attrib and elem.attrib["resource-id"]:
        elem_id = elem.attrib["resource-id"].replace(":", ".").replace("/", "_")
    else:
        elem_id = f"{elem.attrib['class']}_{elem_w}_{elem_h}"
    if "content-desc" in elem.attrib and elem.attrib["content-desc"] and len(elem.attrib["content-desc"]) < 20:
        content_desc = elem.attrib['content-desc'].replace("/", "_").replace(" ", "").replace(":", "_")
        elem_id += f"_{content_desc}"
    return elem_id


def traverse_tree(xml_path, elem_list, attrib, add_index=False):
    path = []
    for event, elem in ET.iterparse(xml_path, ['start', 'end']):
        if event == 'start':
            path.append(elem)
            if attrib in elem.attrib and elem.attrib[attrib] == "true":
                parent_prefix = ""
                if len(path) > 1:
                    parent_prefix = get_id_from_element(path[-2])
                bounds = elem.attrib["bounds"][1:-1].split("][")
                x1, y1 = map(int, bounds[0].split(","))
                x2, y2 = map(int, bounds[1].split(","))
                center = (x1 + x2) // 2, (y1 + y2) // 2
                elem_id = get_id_from_element(elem)
                if parent_prefix:
                    elem_id = parent_prefix + "_" + elem_id
                if add_index:
                    elem_id += f"_{elem.attrib['index']}"
                close = False
                for e in elem_list:
                    bbox = e.bbox
                    center_ = (bbox[0][0] + bbox[1][0]) // 2, (bbox[0][1] + bbox[1][1]) // 2
                    dist = (abs(center[0] - center_[0]) ** 2 + abs(center[1] - center_[1]) ** 2) ** 0.5
                    if dist <= configs["MIN_DIST"]:
                        close = True
                        break
                if not close:
                    elem_list.append(AndroidElement(elem_id, ((x1, y1), (x2, y2)), attrib))

        if event == 'end':
            path.pop()


class AndroidController:
    def __init__(self, device):
        self.device = device
        self.screenshot_dir = configs["ANDROID_SCREENSHOT_DIR"]
        self.xml_dir = configs["ANDROID_XML_DIR"]
        self.package_name = configs.get("app", {}).get("package_name", "com.santiaotalk.im")
        self.width, self.height = self.get_device_size()
        self.backslash = "\\"

    def _remote_join(self, base: str, name: str) -> str:
        b = base.rstrip("/").replace("\\", "/")
        return f"{b}/{name}"

    def _adb_pull(self, remote_path: str, local_path: str, *, quiet: bool = False) -> bool:
        """使用列表参数执行 pull，避免 Windows 下反斜杠与空格导致失败。"""
        local_path = os.path.abspath(local_path)
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        remote_path = remote_path.replace("\\", "/")
        r = _safe_subprocess_run(
            ["adb", "-s", self.device, "pull", remote_path, local_path],
            timeout=90,
            text=True,
        )
        if r is None:
            return False
        if r.returncode != 0:
            if not quiet:
                print_with_color(f"adb pull 失败: {remote_path} -> {local_path}", "red")
                if r.stderr:
                    print_with_color(r.stderr.strip(), "red")
            return False
        if not os.path.isfile(local_path) or os.path.getsize(local_path) == 0:
            if not quiet:
                print_with_color(f"pull 后本地文件缺失或为空: {local_path}", "red")
            return False
        return True

    def _is_valid_ui_xml_bytes(self, data: bytes) -> bool:
        if not data or len(data) < 60:
            return False
        head = data[:12000]
        return b"<hierarchy" in head or b"<Hierarchy" in head

    def _write_bytes_to_local(self, data: bytes, local_path: str) -> bool:
        local_path = os.path.abspath(local_path)
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        try:
            with open(local_path, "wb") as f:
                f.write(data)
        except OSError:
            return False
        return os.path.isfile(local_path) and os.path.getsize(local_path) > 0

    def _stream_remote_xml_to_local(self, remote_path: str, local_path: str, method: str) -> bool:
        """用 exec-out 或 shell cat 将远端 XML 流式写入本地（不依赖 adb pull）。"""
        remote_path = remote_path.replace("\\", "/")
        if method == "exec-out":
            cmd = ["adb", "-s", self.device, "exec-out", "cat", remote_path]
        else:
            cmd = ["adb", "-s", self.device, "shell", "cat", remote_path]
        r = _safe_subprocess_run(cmd, timeout=60, text=False)
        if r is None or r.returncode != 0:
            return False
        data = r.stdout
        if not isinstance(data, bytes):
            data = bytes(data) if data else b""
        if not self._is_valid_ui_xml_bytes(data):
            return False
        return self._write_bytes_to_local(data, local_path)

    def _try_fetch_remote_xml(self, remote_path: str, local_path: str) -> Tuple[bool, str]:
        """
        依次尝试 pull、exec-out cat、shell cat。返回 (成功, 方式名)。
        pull 在部分 Windows/模拟器上会失败，exec-out 往往更稳。
        """
        if self._adb_pull(remote_path, local_path, quiet=True):
            return True, "pull"
        if self._stream_remote_xml_to_local(remote_path, local_path, "exec-out"):
            return True, "exec-out"
        if self._stream_remote_xml_to_local(remote_path, local_path, "shell"):
            return True, "shell cat"
        return False, ""

    def _shell_cat_remote_to_local(self, remote_path: str, local_path: str) -> bool:
        ok, _ = self._try_fetch_remote_xml(remote_path, local_path)
        return ok

    def get_foreground_package(self):
        """当前前台应用包名（无法解析时返回 None）。"""
        try:
            r = subprocess.run(
                ["adb", "-s", self.device, "shell", "dumpsys", "window", "windows"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            for line in r.stdout.splitlines():
                if "mCurrentFocus" in line:
                    m = re.search(r"(?:u0|u\d+)\s+([\w\d.]+)/", line)
                    if m:
                        return m.group(1)
                    m = re.search(r"[\s{]([\w\d.]+)/[\w.]+", line)
                    if m and "." in m.group(1):
                        return m.group(1)
            r2 = subprocess.run(
                ["adb", "-s", self.device, "shell", "dumpsys", "activity", "activities"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            m = re.search(r"mResumedActivity.*? ([\w\d.]+)/", r2.stdout)
            if m:
                return m.group(1)
        except Exception as e:
            print_with_color(f"get_foreground_package: {e}", "red")
        return None

    def start_app(self):
        """冷启动目标应用（包名来自 config.yaml app.package_name）。"""
        package = self.package_name
        device = self.device
        print_with_color(f"启动应用: {package}", "yellow")

        subprocess.run(
            ["adb", "-s", device, "shell", "am", "force-stop", package],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)

        subprocess.run(
            [
                "adb",
                "-s",
                device,
                "shell",
                "monkey",
                "-p",
                package,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            capture_output=True,
            text=True,
        )

        print_with_color("等待应用启动...", "yellow")
        time.sleep(5)


    def get_device_size(self):
        adb_command = f"adb -s {self.device} shell wm size"
        result = execute_adb(adb_command)
        if result != "ERROR":
            return map(int, result.split(": ")[1].split("x"))
        return 0, 0

    def get_screenshot(self, prefix, save_dir):
        os.makedirs(save_dir, exist_ok=True)
        remote = self._remote_join(self.screenshot_dir, prefix + ".png")
        local = os.path.abspath(os.path.join(save_dir, prefix + ".png"))
        cap = subprocess.run(
            ["adb", "-s", self.device, "shell", "screencap", "-p", remote],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if cap.returncode != 0:
            print_with_color(f"screencap 失败: {cap.stderr or cap.stdout}", "red")
            return "ERROR"
        time.sleep(0.15)
        if not self._adb_pull(remote, local):
            return "ERROR"
        return local

    def _uiautomator_dump_to(self, remote_path: str, timeout: float = 28.0) -> str:
        """执行 dump；超时或失败不抛异常。remote_path 为空则不带路径参数。"""
        if remote_path and str(remote_path).strip():
            remote_path = remote_path.replace("\\", "/")
            cmd = ["adb", "-s", self.device, "shell", "uiautomator", "dump", remote_path]
            label = remote_path
        else:
            cmd = ["adb", "-s", self.device, "shell", "uiautomator", "dump"]
            label = "(默认路径)"
        r = _safe_subprocess_run(cmd, timeout=timeout, text=True)
        if r is None:
            return ""
        err = (r.stderr or "") + (r.stdout or "")
        if r.returncode != 0:
            print_with_color(f"uiautomator dump {label}: {err.strip()[:600]}", "yellow")
        return err

    def get_xml(self, prefix, save_dir):
        """
        只向固定远端文件 window_dump.xml 执行 dump，再写入本地 prefix.xml。
        避免对 nav_end_*.xml 等多路径反复 dump（模拟器上易卡死、且易超时）。
        """
        os.makedirs(save_dir, exist_ok=True)
        local = os.path.abspath(os.path.join(save_dir, prefix + ".xml"))
        remote = "/sdcard/window_dump.xml"

        for attempt in range(3):
            self._uiautomator_dump_to(remote, timeout=28.0)
            time.sleep(0.35 + attempt * 0.15)
            ok, how = self._try_fetch_remote_xml(remote, local)
            if ok:
                if how != "pull":
                    print_with_color(
                        f"get_xml `{prefix}.xml`: 已通过 {how} 写入（pull 失败时已回退）",
                        "yellow",
                    )
                return local
            self._uiautomator_dump_to("", timeout=28.0)
            time.sleep(0.4)
            ok, how = self._try_fetch_remote_xml(remote, local)
            if ok:
                print_with_color(f"get_xml `{prefix}.xml`: 无参 dump 后已用 {how} 拉取 {remote}", "yellow")
                return local
            time.sleep(0.6)

        print_with_color("尝试 /data/local/tmp/ui_dump.xml …", "yellow")
        tmp_remote = "/data/local/tmp/ui_dump.xml"
        self._uiautomator_dump_to(tmp_remote, timeout=28.0)
        time.sleep(0.4)
        ok, how = self._try_fetch_remote_xml(tmp_remote, local)
        if ok:
            print_with_color(f"get_xml: 已用 {how} 读取 {tmp_remote}", "yellow")
            return local

        print_with_color(
            "ERROR: get_xml 多次重试仍失败。可检查模拟器负载、"
            "或手动执行: adb shell uiautomator dump /sdcard/window_dump.xml",
            "red",
        )
        return "ERROR"

    def back(self):
        adb_command = f"adb -s {self.device} shell input keyevent KEYCODE_BACK"
        ret = execute_adb(adb_command)
        return ret

    def tap(self, x, y):
        adb_command = f"adb -s {self.device} shell input tap {x} {y}"
        ret = execute_adb(adb_command)
        return ret

    def text(self, input_str):
        input_str = input_str.replace(" ", "%s")
        input_str = input_str.replace("'", "")
        adb_command = f"adb -s {self.device} shell input text {input_str}"
        ret = execute_adb(adb_command)
        return ret

    def long_press(self, x, y, duration=2000):
        adb_command = f"adb -s {self.device} shell input swipe {x} {y} {x} {y} {duration}"
        ret = execute_adb(adb_command)
        return ret

    def swipe(self, x, y, direction, dist="medium", quick=False):
        unit_dist = int(self.width / 10)
        if dist == "long":
            unit_dist *= 3
        elif dist == "medium":
            unit_dist *= 2
        if direction == "up":
            offset = 0, -2 * unit_dist
        elif direction == "down":
            offset = 0, 2 * unit_dist
        elif direction == "left":
            offset = -1 * unit_dist, 0
        elif direction == "right":
            offset = unit_dist, 0
        else:
            return "ERROR"
        duration = 100 if quick else 400
        adb_command = f"adb -s {self.device} shell input swipe {x} {y} {x+offset[0]} {y+offset[1]} {duration}"
        ret = execute_adb(adb_command)
        return ret

    def swipe_precise(self, start, end, duration=400):
        start_x, start_y = start
        end_x, end_y = end
        adb_command = f"adb -s {self.device} shell input swipe {start_x} {start_x} {end_x} {end_y} {duration}"
        ret = execute_adb(adb_command)
        return ret
