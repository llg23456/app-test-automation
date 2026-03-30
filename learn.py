import argparse
import datetime
import os
import time
from scripts.utils import print_with_color
import json
from scripts.self_explorer import SelfExplorer


arg_desc = "AppAgent - exploration phase"
parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description=arg_desc)
parser.add_argument("--root_dir", default="./")
parser.add_argument("--testcase", type=str, help="Path to testcase JSON file")
parser.add_argument("--app", default="com.santiaotalk.im", help="Target app package name")
parser.add_argument("--task_desc", default="Test app functionality", help="Task description")

if __name__ == "__main__":
    args = parser.parse_args()
    
    app = args.app
    root_dir = args.root_dir
    testcase_path = args.testcase
    task_desc = args.task_desc
    
    print_with_color(f"Connecting to device...", "yellow")
    
    print_with_color(f"Starting app: {app}", "yellow")
    
    # 初始化 SelfExplorer 以便使用 start_app 方法
    explorer = SelfExplorer(app, task_desc, root_dir)
    
    # 使用 start_app 方法启动应用
    print_with_color("启动应用...", "yellow")
    explorer.controller.start_app()
    
    # 再次等待
    time.sleep(2)
    
    # 关键：如果传入了 testcase，使用步骤执行模式
    if testcase_path:
        print_with_color(f"加载测试用例: {testcase_path}", "yellow")
        explorer.load_testcase(testcase_path)
        explorer.run_step_by_step()  # 新的执行方式
    else:
        print_with_color("使用自主探索模式", "yellow")
        explorer.run_autonomous()  # 原有的自主探索模式

