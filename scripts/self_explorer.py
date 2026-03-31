import argparse
import ast
import datetime
import json
import os
import re
import sys
import time

from . import prompts
from .config import load_config
from .and_controller import list_all_devices, AndroidController, traverse_tree
from .model import parse_explore_rsp, parse_reflect_rsp, OpenAIModel, QwenModel
from .utils import print_with_color, draw_bbox_multi

class SelfExplorer:
    def __init__(self, app, task_desc, root_dir):
        self.configs = load_config()
        
        # 初始化模型
        if self.configs["MODEL"] == "OpenAI":
            self.mllm = OpenAIModel(base_url=self.configs["OPENAI_API_BASE"],
                                   api_key=self.configs["OPENAI_API_KEY"],
                                   model=self.configs["OPENAI_API_MODEL"],
                                   temperature=self.configs["TEMPERATURE"],
                                   max_tokens=self.configs["MAX_TOKENS"])
        elif self.configs["MODEL"] == "Qwen":
            self.mllm = QwenModel(api_key=self.configs["DASHSCOPE_API_KEY"],
                                 model=self.configs["QWEN_MODEL"])
        else:
            print_with_color(f"ERROR: Unsupported model type {self.configs['MODEL']}!", "red")
            sys.exit()
        
        self.app = app
        self.root_dir = root_dir
        self.task_desc = task_desc
        self.testcase_steps = []  # 存储 JSON 步骤
        self.current_step_index = 0  # 当前执行到第几步
        
        # 初始化工作目录
        self.work_dir = os.path.join(root_dir, "apps")
        os.makedirs(self.work_dir, exist_ok=True)
        self.work_dir = os.path.join(self.work_dir, app)
        os.makedirs(self.work_dir, exist_ok=True)
        self.demo_dir = os.path.join(self.work_dir, "demos")
        os.makedirs(self.demo_dir, exist_ok=True)
        self.demo_timestamp = int(time.time())
        self.task_name = datetime.datetime.fromtimestamp(self.demo_timestamp).strftime("self_explore_%Y-%m-%d_%H-%M-%S")
        self.task_dir = os.path.join(self.demo_dir, self.task_name)
        os.makedirs(self.task_dir, exist_ok=True)
        self.docs_dir = os.path.join(self.work_dir, "auto_docs")
        os.makedirs(self.docs_dir, exist_ok=True)
        self.explore_log_path = os.path.join(self.task_dir, f"log_explore_{self.task_name}.txt")
        self.reflect_log_path = os.path.join(self.task_dir, f"log_reflect_{self.task_name}.txt")
        
        # 初始化设备
        device_list = list_all_devices()
        if not device_list:
            print_with_color("ERROR: No device found!", "red")
            sys.exit()
        print_with_color(f"List of devices attached:\n{str(device_list)}", "yellow")
        
        # 设备选择逻辑
        device = None
        # 如果配置中指定了设备，优先使用
        configured_device = self.configs.get("app", {}).get("device")
        if configured_device and configured_device in device_list:
            device = configured_device
            print_with_color(f"使用配置文件中的设备: {device}", "yellow")
        elif "emulator-5554" in device_list:
            device = "emulator-5554"
            print_with_color(f"自动选择设备: {device}", "yellow")
        elif len(device_list) == 1:
            device = device_list[0]
            print_with_color(f"Device selected: {device}", "yellow")
        else:
            print_with_color("Please choose the Android device to start demo by entering its ID:", "blue")
            device = input()
        
        self.controller = AndroidController(device)
        self.width, self.height = self.controller.get_device_size()
        if not self.width and not self.height:
            print_with_color("ERROR: Invalid device size!", "red")
            sys.exit()
        print_with_color(f"Screen resolution of {device}: {self.width}x{self.height}", "yellow")
        
        # 其他变量
        self.useless_list = set()
        self.last_act = "None"
    
    def load_testcase(self, testcase_path):
        """加载 JSON 测试用例"""
        with open(testcase_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # 解析 result_cases.json 格式
            self.testcase_steps = self._parse_steps(data)
    
    def _parse_steps(self, data):
        """解析你的 JSON 格式（针对 result_cases.json 的结构）"""
        steps = []
        # 处理你的格式：XXX、前置条件、步骤、预期
        for item in data:
            case_name = item.get("XXX", [""])[0]
            step_list = item.get("步骤:", [])
            expected_list = item.get("预期:", [])
            
            for i, step_desc in enumerate(step_list):
                steps.append({ 
                    "case_name": case_name,
                    "step_id": i + 1,
                    "description": step_desc,
                    "expected": expected_list[i] if i < len(expected_list) else None
                })
        return steps
    
    def run_step_by_step(self):
        """按步骤执行（替代原有的自主探索）"""
        for idx, step in enumerate(self.testcase_steps):
            self.current_step_index = idx
            print(f"\n{'='*50}")
            print(f"执行步骤 {idx+1}/{len(self.testcase_steps)}: {step['description']}")
            print(f"预期结果: {step['expected']}")
            
            # 执行单步（带重试机制）
            success = self._execute_single_step(step)
            
            if not success:
                print(f"步骤 {idx+1} 执行失败，停止后续步骤")
                break
            
            # 步骤间等待
            time.sleep(2)
        
        print("\n所有步骤执行完成")
    
    def _execute_single_step(self, step, max_retry=3):
        """执行单步操作"""
        for attempt in range(max_retry):
            try:
                # 1. 获取截图和控件树（复用原有逻辑）
                time.sleep(2.0)
                step_num = self.current_step_index + 1
                screenshot_before = self.controller.get_screenshot(f"step_{step_num}_before", self.task_dir)
                time.sleep(1.0)
                xml_path = self.controller.get_xml(f"step_{step_num}", self.task_dir)
                
                if screenshot_before == "ERROR" or xml_path == "ERROR":
                    print_with_color("ERROR: Failed to get screenshot or xml", "red")
                    continue
                
                # 2. 解析控件并在截图上标记数字（复用原有逻辑）
                clickable_list = []
                focusable_list = []
                traverse_tree(xml_path, clickable_list, "clickable", True)
                traverse_tree(xml_path, focusable_list, "focusable", True)
                elem_list = []
                for elem in clickable_list:
                    if elem.uid in self.useless_list:
                        continue
                    elem_list.append(elem)
                for elem in focusable_list:
                    if elem.uid in self.useless_list:
                        continue
                    bbox = elem.bbox
                    center = (bbox[0][0] + bbox[1][0]) // 2, (bbox[0][1] + bbox[1][1]) // 2
                    close = False
                    for e in clickable_list:
                        bbox = e.bbox
                        center_ = (bbox[0][0] + bbox[1][0]) // 2, (bbox[0][1] + bbox[1][1]) // 2
                        dist = (abs(center[0] - center_[0]) ** 2 + abs(center[1] - center_[1]) ** 2) ** 0.5
                        if dist <= self.configs["MIN_DIST"]:
                            close = True
                            break
                    if not close:
                        elem_list.append(elem)
                
                marked_image_path = os.path.join(self.task_dir, f"step_{step_num}_before_labeled.png")
                draw_bbox_multi(screenshot_before, marked_image_path, elem_list,
                                dark_mode=self.configs["DARK_MODE"])
                
                # 3. 构造 Prompt（关键：传入当前步骤描述）
                prompt = self._build_step_prompt(step, elem_list)
                
                # 4. 调用 LLM 决策
                print_with_color("Thinking about what to do in the next step...", "yellow")
                status, rsp = self.mllm.get_model_response(prompt, [marked_image_path])
                
                if not status:
                    print_with_color(rsp, "red")
                    continue
                
                # 记录日志
                with open(self.explore_log_path, "a") as logfile:
                    log_item = {"step": step_num, "prompt": prompt, "image": f"step_{step_num}_before_labeled.png",
                                "response": rsp}
                    logfile.write(json.dumps(log_item) + "\n")
                
                # 容错处理：检查 LLM 是否无法完成操作
                if "I'm sorry" in rsp or "can't assist" in rsp:
                    print_with_color("LLM 无法完成操作，当前页面可能不包含目标元素", "red")
                    print_with_color(f"当前截图路径: {marked_image_path}", "yellow")
                    print_with_color(f"LLM 响应: {rsp}", "magenta")
                    continue
                
                # 5. 解析 Action 并执行（复用原有逻辑）
                res = parse_explore_rsp(rsp)
                act_name = res[0]
                self.last_act = res[-1]
                res = res[:-1]
                
                if act_name == "FINISH":
                    return True
                
                area = None
                if act_name == "tap":
                    _, area = res
                    tl, br = elem_list[area - 1].bbox
                    elem_width = br[0] - tl[0]
                    elem_height = br[1] - tl[1]
                    
                    # 强制应用收藏按钮坐标调整
                    # 根据用户提供的正确坐标: (493, 580) 对应边界框 ((156, 416), (1280, 898))
                    if "收藏" in step['description']:
                        # 收藏按钮在菜单中的位置计算
                        x = tl[0] + int(elem_width * 0.3)  # 左侧30%位置（收藏按钮）
                        y = tl[1] + int(elem_height * 0.34)  # 上方34%位置（图标中心）
                        print_with_color(f"检测到收藏操作，调整坐标到'收藏'位置: ({x}, {y}) 元素边界框: {elem_list[area - 1].bbox}", "yellow")
                    else:
                        # 普通元素按中心
                        x = (tl[0] + br[0]) // 2
                        y = (tl[1] + br[1]) // 2
                    
                    # 对于菜单操作，增加等待时间确保菜单完全弹出
                    if "收藏" in step['description'] or "按钮" in step['description']:
                        print_with_color("等待菜单完全弹出...", "yellow")
                        time.sleep(1.5)
                    
                    print_with_color(f"点击坐标: ({x}, {y}) 元素边界框: {elem_list[area - 1].bbox}", "blue")
                    ret = self.controller.tap(x, y)
                    if ret == "ERROR":
                        print_with_color("ERROR: tap execution failed", "red")
                        continue
                    
                    # 对于收藏操作，等待后截屏捕捉成功提示
                    if "收藏" in step['description']:
                        print_with_color("等待1秒后截屏捕捉收藏成功提示...", "yellow")
                        # 等待1秒确保提示出现
                        time.sleep(1)
                        favorite_success_screenshot = self.controller.get_screenshot(f"favorite_success_{self.current_step_index+1}", self.task_dir)
                        print_with_color(f"收藏成功提示截图保存到: {favorite_success_screenshot}", "yellow")
                elif act_name == "text":
                    _, input_str = res
                    ret = self.controller.text(input_str)
                    if ret == "ERROR":
                        print_with_color("ERROR: text execution failed", "red")
                        continue
                elif act_name == "long_press":
                    _, area = res
                    tl, br = elem_list[area - 1].bbox
                    elem_width = br[0] - tl[0]
                    elem_height = br[1] - tl[1]
                    
                    # 对于消息列表项，调整坐标到绿色气泡位置
                    if elem_width > 1000:
                        # 自己发的消息（绿色气泡）通常在右侧，按在右侧 60-70% 位置
                        # 更靠右以确保按在绿色气泡上
                        x = tl[0] + int(elem_width * 0.65)
                    else:
                        # 正常元素按中心
                        x = (tl[0] + br[0]) // 2
                    
                    # Y 坐标：按在消息气泡的中心位置，避开时间戳
                    y = tl[1] + int(elem_height * 0.5)
                    
                    print_with_color(f"长按坐标: ({x}, {y})，原始边界: {elem_list[area - 1].bbox}", "blue")
                    ret = self.controller.long_press(x, y)
                    if ret == "ERROR":
                        print_with_color("ERROR: long press execution failed", "red")
                        continue
                    # 等待菜单完全弹出
                    print_with_color("等待菜单完全弹出...", "yellow")
                    time.sleep(3)  # 改为 3 秒
                elif act_name == "swipe":
                    _, area, swipe_dir, dist = res
                    tl, br = elem_list[area - 1].bbox
                    x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
                    ret = self.controller.swipe(x, y, swipe_dir, dist)
                    if ret == "ERROR":
                        print_with_color("ERROR: swipe execution failed", "red")
                        continue
                else:
                    print_with_color(f"ERROR: Undefined act {act_name}!")
                    continue
                
                time.sleep(self.configs["REQUEST_INTERVAL"])
                
                # 6. 验证步骤是否成功（可选）
                if step['expected']:
                    verified = self._verify_step(step['expected'])
                    if verified:
                        print_with_color("✓ 步骤验证通过", "green")
                        return True
                    else:
                        print_with_color("✗ 验证未通过，重试...", "red")
                        continue
                
                return True
                
            except Exception as e:
                print_with_color(f"执行出错: {e}，重试 {attempt+1}/{max_retry}", "red")
                time.sleep(1)
        
        return False
    
    def _build_step_prompt(self, step, elem_list):
        """构造针对当前步骤的 Prompt"""
        # 基础 prompt（来自原代码）
        base_prompt = prompts.self_explore_task_template
        
        # 替换基础占位符
        base_prompt = re.sub(r"<task_description>", step['description'], base_prompt)
        base_prompt = re.sub(r"<last_act>", self.last_act, base_prompt)
        
        # 关键：加入当前步骤指引
        step_instruction = f"""
当前需要执行的步骤：{step['description']}
预期结果：{step['expected']}

请根据屏幕截图和标记的数字，完成上述步骤。只执行这一步，不要进行其他操作。

重要提示：
1. 如果需要长按消息，请优先选择绿色气泡的消息（自己发送的消息）
2. 长按需要持续几秒钟才能触发菜单
3. 菜单弹出后，选择正确的选项完成操作
4. 注意：操作菜单中，"收藏"按钮在从左数第2个位置（复制右边，转发左边）
5. 点击时请按在"收藏"图标上，不要按在菜单中心

可点击元素列表：
{self._format_clickable_list(elem_list)}
"""
        
        return base_prompt + step_instruction
    
    def _format_clickable_list(self, elem_list):
        """格式化可点击元素列表"""
        formatted = ""
        for i, elem in enumerate(elem_list, 1):
            formatted += f"{i}. 元素ID: {elem.uid}, 位置: {elem.bbox}\n"
        return formatted
    
    def _verify_step(self, expected):
        """验证步骤是否成功"""
        # 等待操作完成
        time.sleep(2)
        
        # 获取执行后的截图
        screenshot_after = self.controller.get_screenshot(f"verify_step_{self.current_step_index+1}", self.task_dir)
        if screenshot_after == "ERROR":
            return False
        
        # 对于长按操作，验证是否弹出菜单
        if "长按" in expected or "弹出操作菜单" in expected:
            print_with_color("验证是否弹出操作菜单...", "yellow")
            # 使用LLM验证是否出现菜单
            if not self._verify_long_press_menu(screenshot_after):
                print_with_color("验证失败：未检测到操作菜单", "red")
                return False
        
        # 对于收藏操作，等待更长时间确保提示显示
        if "收藏" in expected:
            time.sleep(1)
            # 再次获取截图，检查是否有收藏成功提示
            screenshot_after_2 = self.controller.get_screenshot(f"verify_step_{self.current_step_index+1}_2", self.task_dir)
            if screenshot_after_2 == "ERROR":
                return False
            # 验证是否有收藏成功提示
            if not self._verify_favorite_success(screenshot_after_2):
                print_with_color("验证失败：未检测到收藏成功提示", "red")
                return False
        
        return True
    
    def _verify_long_press_menu(self, screenshot_path):
        """验证长按后是否出现操作菜单"""
        # 获取当前界面的 XML
        xml_path = self.controller.get_xml(f"verify_menu_{self.current_step_index+1}", self.task_dir)
        if xml_path == "ERROR":
            return False
        
        # 读取 XML 检查是否包含"收藏"、"转发"等菜单文字
        try:
            with open(xml_path, 'r', encoding='utf-8') as f:
                xml_content = f.read()
                # 检查是否出现菜单常见文字
                if '收藏' in xml_content or '转发' in xml_content or '删除' in xml_content:
                    print_with_color("✓ 检测到操作菜单（XML包含收藏/转发/删除）", "green")
                    return True
                else:
                    print_with_color("✗ 未检测到操作菜单", "red")
                    return False
        except Exception as e:
            print_with_color(f"验证出错: {e}", "red")
            return False
    
    def _verify_favorite_success(self, screenshot_path):
        """验证收藏操作是否成功"""
        # 使用之前保存的 favorite_success 截图
        favorite_success_screenshot = os.path.join(self.task_dir, f"favorite_success_{self.current_step_index+1}.png")
        
        # 如果 favorite_success 截图存在，使用它；否则使用默认截图
        if os.path.exists(favorite_success_screenshot):
            print_with_color(f"使用 favorite_success 截图进行验证: {favorite_success_screenshot}", "yellow")
            verify_screenshot = favorite_success_screenshot
        else:
            print_with_color(f"favorite_success 截图不存在，使用默认截图: {screenshot_path}", "yellow")
            verify_screenshot = screenshot_path
        
        prompt = """
        请检查截图：是否显示了收藏成功的提示（比如'成功'、'该收藏已添加'、'已收藏'、'收藏成功'等文字）？
        如果有收藏成功提示，返回：YES
        如果没有，返回：NO
        """
        status, rsp = self.mllm.get_model_response(prompt, [verify_screenshot])
        if status:
            print_with_color(f"收藏验证响应: {rsp}", "blue")
            return "YES" in rsp.upper()
        return False
    
    def run_autonomous(self):
        """原有自主探索逻辑"""
        round_count = 0
        doc_count = 0
        task_complete = False
        while round_count < self.configs["MAX_ROUNDS"]:
            round_count += 1
            print_with_color(f"Round {round_count}", "yellow")
            time.sleep(2.0)
            screenshot_before = self.controller.get_screenshot(f"{round_count}_before", self.task_dir)
            time.sleep(1.0)
            xml_path = self.controller.get_xml(f"{round_count}", self.task_dir)
            if screenshot_before == "ERROR" or xml_path == "ERROR":
                break
            clickable_list = []
            focusable_list = []
            traverse_tree(xml_path, clickable_list, "clickable", True)
            traverse_tree(xml_path, focusable_list, "focusable", True)
            elem_list = []
            for elem in clickable_list:
                if elem.uid in self.useless_list:
                    continue
                elem_list.append(elem)
            for elem in focusable_list:
                if elem.uid in self.useless_list:
                    continue
                bbox = elem.bbox
                center = (bbox[0][0] + bbox[1][0]) // 2, (bbox[0][1] + bbox[1][1]) // 2
                close = False
                for e in clickable_list:
                    bbox = e.bbox
                    center_ = (bbox[0][0] + bbox[1][0]) // 2, (bbox[0][1] + bbox[1][1]) // 2
                    dist = (abs(center[0] - center_[0]) ** 2 + abs(center[1] - center_[1]) ** 2) ** 0.5
                    if dist <= self.configs["MIN_DIST"]:
                        close = True
                        break
                if not close:
                    elem_list.append(elem)
            draw_bbox_multi(screenshot_before, os.path.join(self.task_dir, f"{round_count}_before_labeled.png"), elem_list,
                            dark_mode=self.configs["DARK_MODE"])

            prompt = re.sub(r"<task_description>", self.task_desc, prompts.self_explore_task_template)
            prompt = re.sub(r"<last_act>", self.last_act, prompt)
            base64_img_before = os.path.join(self.task_dir, f"{round_count}_before_labeled.png")
            print_with_color("Thinking about what to do in the next step...", "yellow")
            status, rsp = self.mllm.get_model_response(prompt, [base64_img_before])

            if status:
                with open(self.explore_log_path, "a") as logfile:
                    log_item = {"step": round_count, "prompt": prompt, "image": f"{round_count}_before_labeled.png",
                                "response": rsp}
                    logfile.write(json.dumps(log_item) + "\n")
                res = parse_explore_rsp(rsp)
                act_name = res[0]
                self.last_act = res[-1]
                res = res[:-1]
                if act_name == "FINISH":
                    task_complete = True
                    break
                if act_name == "tap":
                    _, area = res
                    tl, br = elem_list[area - 1].bbox
                    x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
                    ret = self.controller.tap(x, y)
                    if ret == "ERROR":
                        print_with_color("ERROR: tap execution failed", "red")
                        break
                elif act_name == "text":
                    _, input_str = res
                    ret = self.controller.text(input_str)
                    if ret == "ERROR":
                        print_with_color("ERROR: text execution failed", "red")
                        break
                elif act_name == "long_press":
                    _, area = res
                    tl, br = elem_list[area - 1].bbox
                    x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
                    ret = self.controller.long_press(x, y)
                    if ret == "ERROR":
                        print_with_color("ERROR: long press execution failed", "red")
                        break
                elif act_name == "swipe":
                    _, area, swipe_dir, dist = res
                    tl, br = elem_list[area - 1].bbox
                    x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
                    ret = self.controller.swipe(x, y, swipe_dir, dist)
                    if ret == "ERROR":
                        print_with_color("ERROR: swipe execution failed", "red")
                        break
                else:
                    break
                time.sleep(self.configs["REQUEST_INTERVAL"])
            else:
                print_with_color(rsp, "red")
                break

            screenshot_after = self.controller.get_screenshot(f"{round_count}_after", self.task_dir)
            if screenshot_after == "ERROR":
                break
            draw_bbox_multi(screenshot_after, os.path.join(self.task_dir, f"{round_count}_after_labeled.png"), elem_list,
                            dark_mode=self.configs["DARK_MODE"])
            base64_img_after = os.path.join(self.task_dir, f"{round_count}_after_labeled.png")

            if act_name == "tap":
                prompt = re.sub(r"<action>", "tapping", prompts.self_explore_reflect_template)
            elif act_name == "text":
                continue
            elif act_name == "long_press":
                prompt = re.sub(r"<action>", "long pressing", prompts.self_explore_reflect_template)
            elif act_name == "swipe":
                swipe_dir = res[2]
                if swipe_dir == "up" or swipe_dir == "down":
                    act_name = "v_swipe"
                elif swipe_dir == "left" or swipe_dir == "right":
                    act_name = "h_swipe"
                prompt = re.sub(r"<action>", "swiping", prompts.self_explore_reflect_template)
            else:
                print_with_color("ERROR: Undefined act!", "red")
                break
            prompt = re.sub(r"<ui_element>", str(area), prompt)
            prompt = re.sub(r"<task_desc>", self.task_desc, prompt)
            prompt = re.sub(r"<last_act>", self.last_act, prompt)

            print_with_color("Reflecting on my previous action...", "yellow")
            status, rsp = self.mllm.get_model_response(prompt, [base64_img_before, base64_img_after])
            if status:
                resource_id = elem_list[int(area) - 1].uid
                with open(self.reflect_log_path, "a") as logfile:
                    log_item = {"step": round_count, "prompt": prompt, "image_before": f"{round_count}_before_labeled.png",
                                "image_after": f"{round_count}_after.png", "response": rsp}
                    logfile.write(json.dumps(log_item) + "\n")
                res = parse_reflect_rsp(rsp)
                decision = res[0]
                if decision == "ERROR":
                    break
                if decision == "INEFFECTIVE":
                    self.useless_list.add(resource_id)
                    self.last_act = "None"
                elif decision == "BACK" or decision == "CONTINUE" or decision == "SUCCESS":
                    if decision == "BACK" or decision == "CONTINUE":
                        self.useless_list.add(resource_id)
                        self.last_act = "None"
                        if decision == "BACK":
                            ret = self.controller.back()
                            if ret == "ERROR":
                                print_with_color("ERROR: back execution failed", "red")
                                break
                    doc = res[-1]
                    doc_name = resource_id + ".txt"
                    doc_path = os.path.join(self.docs_dir, doc_name)
                    if os.path.exists(doc_path):
                        doc_content = ast.literal_eval(open(doc_path).read())
                        if doc_content[act_name]:
                            print_with_color(f"Documentation for the element {resource_id} already exists.", "yellow")
                            continue
                    else:
                        doc_content = {
                            "tap": "",
                            "text": "",
                            "v_swipe": "",
                            "h_swipe": "",
                            "long_press": ""
                        }
                    doc_content[act_name] = doc
                    with open(doc_path, "w") as outfile:
                        outfile.write(str(doc_content))
                    doc_count += 1
                    print_with_color(f"Documentation generated and saved to {doc_path}", "yellow")
                else:
                    print_with_color(f"ERROR: Undefined decision! {decision}", "red")
                    break
            else:
                print_with_color(rsp["error"]["message"], "red")
                break
            time.sleep(self.configs["REQUEST_INTERVAL"])

        if task_complete:
            print_with_color(f"Autonomous exploration completed successfully. {doc_count} docs generated.", "yellow")
        elif round_count == self.configs["MAX_ROUNDS"]:
            print_with_color(f"Autonomous exploration finished due to reaching max rounds. {doc_count} docs generated.",
                             "yellow")
        else:
            print_with_color(f"Autonomous exploration finished unexpectedly. {doc_count} docs generated.", "red")

if __name__ == "__main__":
    arg_desc = "AppAgent - Autonomous Exploration"
    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description=arg_desc)
    parser.add_argument("--app")
    parser.add_argument("--task_desc", required=True)
    parser.add_argument("--root_dir", default="./")
    parser.add_argument("--testcase", help="Path to testcase JSON file for step-by-step execution")
    args = vars(parser.parse_args())

    app = args["app"]
    root_dir = args["root_dir"]
    task_desc = args["task_desc"]
    testcase_path = args["testcase"]
    
    print_with_color(f"Task description: {task_desc}", "yellow")

    if not app:
        print_with_color("What is the name of the target app?", "blue")
        app = input()
        app = app.replace(" ", "")

    explorer = SelfExplorer(app, task_desc, root_dir)
    
    if testcase_path:
        print_with_color(f"Loading testcase from {testcase_path}", "yellow")
        explorer.load_testcase(testcase_path)
        explorer.run_step_by_step()
    else:
        explorer.run_autonomous()
