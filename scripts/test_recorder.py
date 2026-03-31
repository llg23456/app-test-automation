import argparse
import datetime
import hashlib
import os
import shutil
import sys
import time

from and_controller import list_all_devices, AndroidController, traverse_tree
from config import load_config
from utils import print_with_color, draw_bbox_multi

arg_desc = "AppAgent - DFS Auto Recorder"
parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description=arg_desc)
parser.add_argument("--demo")
parser.add_argument("--root_dir", default="./")
parser.add_argument("--max_steps", type=int, default=200)
args = vars(parser.parse_args())

app = "com.santiaotalk.im"
demo_name = args["demo"]
root_dir = args["root_dir"]
max_steps = args["max_steps"]

configs = load_config()

if not demo_name:
    demo_timestamp = int(time.time())
    demo_name = datetime.datetime.fromtimestamp(demo_timestamp).strftime(f"dfs_demo_{app}_%Y-%m-%d_%H-%M-%S")

work_dir = os.path.join(root_dir, "apps")
if not os.path.exists(work_dir):
    os.mkdir(work_dir)

work_dir = os.path.join(work_dir, app)
if not os.path.exists(work_dir):
    os.mkdir(work_dir)

demo_dir = os.path.join(work_dir, "demos")
if not os.path.exists(demo_dir):
    os.mkdir(demo_dir)

task_dir = os.path.join(demo_dir, demo_name)
if os.path.exists(task_dir):
    shutil.rmtree(task_dir)
os.mkdir(task_dir)

raw_ss_dir = os.path.join(task_dir, "raw_screenshots")
os.mkdir(raw_ss_dir)

xml_dir = os.path.join(task_dir, "xml")
os.mkdir(xml_dir)

labeled_ss_dir = os.path.join(task_dir, "labeled_screenshots")
os.mkdir(labeled_ss_dir)

record_path = os.path.join(task_dir, "record.txt")
record_file = open(record_path, "w", encoding="utf-8")

device_list = list_all_devices()
if not device_list:
    print_with_color("ERROR: No device found!", "red")
    sys.exit()

print_with_color("List of devices attached:\n" + str(device_list), "yellow")
if len(device_list) == 1:
    device = device_list[0]
    print_with_color(f"Device selected: {device}", "yellow")
else:
    print_with_color("Please choose the Android device to start demo by entering its ID:", "blue")
    device = input()

controller = AndroidController(device)
width, height = controller.get_device_size()
if not width and not height:
    print_with_color("ERROR: Invalid device size!", "red")
    sys.exit()

print_with_color(f"Screen resolution of {device}: {width}x{height}", "yellow")


def get_elem_list(xml_path):
    clickable_list = []
    focusable_list = []

    traverse_tree(xml_path, clickable_list, "clickable", True)
    traverse_tree(xml_path, focusable_list, "focusable", True)

    elem_list = clickable_list.copy()

    for elem in focusable_list:
        bbox = elem.bbox
        center = ((bbox[0][0] + bbox[1][0]) // 2, (bbox[0][1] + bbox[1][1]) // 2)
        close = False
        for e in clickable_list:
            bbox2 = e.bbox
            center2 = ((bbox2[0][0] + bbox2[1][0]) // 2, (bbox2[0][1] + bbox2[1][1]) // 2)
            dist = ((center[0] - center2[0]) ** 2 + (center[1] - center2[1]) ** 2) ** 0.5
            if dist <= configs["MIN_DIST"]:
                close = True
                break
        if not close:
            elem_list.append(elem)

    return elem_list


def get_state_signature(elem_list):
    """
    用当前页面可交互元素的 uid + bbox 构造一个状态签名，
    用来近似判断是否回到了同一页面。
    """
    items = []
    for elem in elem_list:
        items.append(f"{elem.uid}:{elem.bbox}")
    raw = "|".join(sorted(items))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def capture_state(step):
    screenshot_path = controller.get_screenshot(f"{demo_name}_{step}", raw_ss_dir)
    xml_path = controller.get_xml(f"{demo_name}_{step}", xml_dir)

    if screenshot_path == "ERROR" or xml_path == "ERROR":
        return None, None, None, None

    elem_list = get_elem_list(xml_path)
    labeled_path = os.path.join(labeled_ss_dir, f"{demo_name}_{step}.png")
    draw_bbox_multi(
        screenshot_path,
        labeled_path,
        elem_list,
        True
    )
    state_sig = get_state_signature(elem_list)
    return screenshot_path, xml_path, elem_list, state_sig


def tap_elem(elem):
    tl, br = elem.bbox
    x = (tl[0] + br[0]) // 2
    y = (tl[1] + br[1]) // 2
    return controller.tap(x, y)


# DFS 相关数据结构
# visited_actions[state_sig] = set(uid)
visited_actions = {}
# stack 里记录 DFS 路径，便于回退
stack = []

step = 0

print_with_color("Starting app...", "yellow")
ret = controller.start_app()
if ret == "ERROR":
    print_with_color("ERROR: failed to start app", "red")
    sys.exit()

time.sleep(3)

while step < max_steps:
    # 每 10 步重启一次 app
    if step > 0 and step % 10 == 0:
        print_with_color(f"Step {step}: restarting app...", "yellow")
        if hasattr(controller, "restart_app"):
            ret = controller.restart_app()
        else:
            # 如果你只实现了 start_app，没有 stop_app，就退化成先 back 若干次再启动
            ret = controller.start_app()

        if ret == "ERROR":
            print_with_color("ERROR: failed to restart app", "red")
            break
        time.sleep(3)
        stack.clear()

    step += 1
    print_with_color(f"Step {step}", "yellow")

    screenshot_path, xml_path, elem_list, state_sig = capture_state(step)
    if screenshot_path is None:
        print_with_color("ERROR: failed to capture current state", "red")
        break

    if state_sig not in visited_actions:
        visited_actions[state_sig] = set()

    # 找当前页面中还没点过的元素
    candidate_idx = None
    for idx, elem in enumerate(elem_list):
        if elem.uid not in visited_actions[state_sig]:
            candidate_idx = idx
            break

    if candidate_idx is not None:
        # 选中一个新元素，执行 tap，进入更深一层
        elem = elem_list[candidate_idx]
        visited_actions[state_sig].add(elem.uid)

        print_with_color(f"DFS tap -> {elem.uid}", "blue")
        ret = tap_elem(elem)
        if ret == "ERROR":
            print_with_color("ERROR: tap execution failed", "red")
            break

        record_file.write(f"tap({candidate_idx + 1}):::{elem.uid}\n")
        record_file.flush()

        # 压栈，表示这是 DFS 向下走的一步
        stack.append((state_sig, elem.uid))

        time.sleep(2)
    else:
        # 当前页面没有可继续的新元素了，回退
        print_with_color("No more unexplored elements on current screen, backtracking...", "yellow")
        ret = controller.back()
        if ret == "ERROR":
            print_with_color("ERROR: back execution failed", "red")
            break

        record_file.write("back\n")
        record_file.flush()

        time.sleep(2)

        if stack:
            stack.pop()
        else:
            print_with_color("DFS traversal finished: stack empty.", "yellow")
            break

record_file.write("stop\n")
record_file.close()

print_with_color(f"DFS record phase completed. {step} steps were recorded.", "yellow")
print_with_color(f"Record file saved to: {record_path}", "yellow")