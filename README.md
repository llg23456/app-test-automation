
## 🔆 简介

本项目是一个基于大语言模型的多模态智能体框架，用于在智能手机上操作各类应用。

智能体通过简化的动作空间（如点击、滑动）模拟人手操作，无需应用提供系统后端接口，因而能适配更多 App。

智能体的核心是一种学习方式：既可通过自主探索学习使用新应用，也可通过观察人类演示学习；过程中会积累知识库，供后续在不同应用中执行更复杂的任务。


## 🚀 快速开始

下面说明如何快速使用 `gpt-4-vision-preview`（或 `qwen-vl-max`）作为智能体，在 Android 应用上替你完成指定任务。

### ⚙️ 步骤 1：环境准备

1. 在电脑上安装 [Android Debug Bridge](https://developer.android.com/tools/adb)（adb），用于在 PC 上与 Android 设备通信。

2. 准备一台 Android 真机，在「设置 → 开发者选项」中开启 **USB 调试**。

3. 用 USB 数据线将手机连接到电脑。

4. （可选）若没有真机，仍可使用模拟器。推荐安装 [Android Studio](https://developer.android.com/studio/run/emulator)，在其设备管理器中创建模拟器；可从网上下载 APK，拖入模拟器安装。  
   本框架会像操作真机一样操作模拟器上的应用。

   第三方模拟器（如 **MuMu、雷电** 等）同样可用：装好 adb 后执行 `adb devices`，确保设备状态为 `device`（而不是 `unauthorized`）。常见设备串号为 `emulator-5554`，请在 `config.yaml` 的 `app.device` 中填写与实际一致。

   <img width="570" alt="Screenshot 2023-12-26 at 22 25 42" src="https://github.com/mnotgod96/AppAgent/assets/27103154/5d76b810-1f42-44c8-b024-d63ec7776789">

5. 克隆本仓库并安装依赖。本项目脚本均为 **Python 3**，请先安装 Python 3。

```bash
cd AppAgent
pip install -r requirements.txt
```

### 🤖 步骤 2：配置智能体

智能体需要能同时理解**文本 + 屏幕图像**的多模态模型。实验中我们曾使用 `gpt-4-vision-preview` 作为决策模型。

请在项目根目录编辑 `config.yaml`。使用前至少需要配置：

1. **OpenAI API Key**：从 OpenAI 获取可用密钥，才能调用 GPT-4V 等视觉模型。  
2. **请求间隔（REQUEST_INTERVAL）**：两次调用之间的秒数，用于控制频率，请按账号额度自行调整。

`config.yaml` 中其余项均有注释，可按需修改。

> 注意：GPT-4V 按量计费，本项目里单次请求约 $0.03 量级，请合理使用。

也可改用 **通义千问-VL**（`qwen-vl-max`）作为多模态模型；当前可免费使用，但在本场景下效果通常弱于 GPT-4V。

使用通义时，需注册阿里云并 [开通 DashScope 并创建 API Key](https://help.aliyun.com/zh/dashscope/developer-reference/activate-dashscope-and-create-an-api-key?spm=a2c4g.11186623.0.i1)，填入 `config.yaml` 的 `DASHSCOPE_API_KEY`，并将 `MODEL` 从 `OpenAI` 改为 `Qwen`。

若需接入自有模型，请在 `scripts/model.py` 中按现有接口实现新的模型类。

### 📱 模拟器建议（可选）

- **分辨率**长期固定为一种（例如 **1080 × 1920**、DPI 480），便于点击坐标与截图结果可复现；不要在不同分辨率之间频繁切换。  
- **帧率**：界面自动化用 **60 FPS** 一般足够，更高帧率非必需。  
- 确保设备上的 `ANDROID_SCREENSHOT_DIR`、`ANDROID_XML_DIR` 目录已存在（例如 `/sdcard`）。


## 如何运行

请在**项目根目录**（包含 `learn.py` 与 `config.yaml` 的目录）下执行命令。在 Windows 上若未配置 `python3`，可将命令中的 `python3` 换成 `python`。

### 测试用例（按步骤执行）

使用 `testcase.json`（每条用例需包含键 `XXX`、`步骤:`、`预期:`）。脚本会启动应用，再按步骤调用大模型执行。

```bash
python3 learn.py --testcase ./testcase.json
```

可选参数：`--app <包名>`、`--root_dir ./`、`--task_desc "..."` 等。

### 自主探索（原版）

不传 testcase 时，会走 `scripts/self_explorer.py` 中的 `run_autonomous()` 自主探索逻辑。

```bash
python3 learn.py
```

### 结构化 BFS / DFS 探索

按**广度优先（BFS）**或**深度优先（DFS）**遍历带编号的可交互控件：会截屏、为每个新发现的控件单独存图，并把路径与日志写到 `apps/<应用包名>/demos/<本次运行>/`。探索步数上限默认读取 `config.yaml` 中的 `MAX_ROUNDS`，也可用 `--max_steps` 覆盖。

```bash
python3 scripts/bfs_explore.py --app com.example.app --root_dir ./
python3 scripts/dfs_explore.py --app com.example.app --root_dir ./
```

可选：`--max_steps 20`。公共逻辑在 `scripts/exploration_common.py`。运行结束后可在对应 demo 目录查看 `report_bfs.md` / `report_dfs.md` 以及 `log_bfs.jsonl` / `log_dfs.jsonl`。
