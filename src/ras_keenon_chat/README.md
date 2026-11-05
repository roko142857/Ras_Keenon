# LLM 对话网页与机器人预设动作

更新：2026-11-03。网页包含对话输入、持久化对话记录、LLM 请求/响应回执，以及机器人动作调用回执。它使用 OpenAI 兼容的 **Chat Completions + Function Calling** 接口，不绑定具体供应商或模型。

## 1. 调用链

```text
浏览器 http://192.168.3.20:8770
  → ras_keenon_chat 后端
     ↔ 你配置的 LLM API（模型文本 / tool_calls / tool 结果）
     → ras_keenon_demos 动作 API :8766
        → ras_keenon_commands 控制器 :8765
           → SDK / 底盘
```

模型返回的是 `robot_shake_hand`、`robot_turn_head` 等工具调用，由本地后端解析、校验并执行。模型不直接连接局域网机器人。动作执行完后，后端将工具结果回传给模型，生成后续对话回复。

当前已有 12 个工具：复位、大幅挥手、单肘摆手、抬臂、转头、点头、摇头、下蹲、直立、前进、旋转、停止。参数定义复用 `ras_keenon_demos` 的 `tool_schemas()`；网页没有另一套动作实现，也不新增使能或 base_ready 门槛。

## 2. 启动网页

首次部署请先按第 6 节构建新包；已经安装时，在机器人上：

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
ros2 launch ras_keenon_chat chat.launch.py
```

电脑浏览器打开 **http://192.168.3.20:8770**。该命令只启动对话网页，复用已有 `http://127.0.0.1:8766` 动作服务，不会额外初始化 SDK。

没有启动动作服务时，可在另一个终端执行：

```bash
# 已有 manual.launch.py 控制器：只启动动作服务，复用已有控制器
ros2 launch ras_keenon_demos demos.launch.py start_controller:=false

# 没有任何控制器：启动控制器及动作服务
ros2 launch ras_keenon_demos demos.launch.py backend:=hardware
```

也可以让聊天 launch 一次启动完整链路（此时不要另外重复启动控制器）：

```bash
ros2 launch ras_keenon_chat chat.launch.py start_actions:=true backend:=hardware
```

先查看界面与完整交互流程：

```bash
ros2 launch ras_keenon_chat chat.launch.py demo:=true
```

演示模式明确显示横幅，使用本地规则模拟模型和动作回执，**不调用真实 LLM，也不驱动真实机器人**。即使同时传入 `start_actions:=true`，演示模式也不会启动控制器。真实 API 模式尚未配置时会返回可见错误，不会自动切换成演示。

## 3. 配置 LLM API

点击网页右上角“模型设置”：

- 运行模式：真实 API 模式。
- API Base URL：服务商给出的兼容地址，如 `https://服务地址/v1`；也支持完整 `/chat/completions` 地址。
- 模型名称：填写你实际有权限调用、且支持 Function Calling 的模型名。
- API Key：按服务商要求填写；不需要密钥的本地模型可以留空。
- 动作服务地址：后端运行在机器人上时通常是 `http://127.0.0.1:8766`。

设置保存后即可在对话框输入“挥两次手”“向右转头”“下蹲3厘米”等。没有动作要求的普通聊天不会自动触发动作。一个模型响应中有多个工具调用时，后端逐个等待执行结果，避免新动作替换前一个动作。

也可在启动前用后端环境变量配置：

```bash
export LLM_BASE_URL='https://服务地址/v1'
export LLM_MODEL='你的模型名称'
read -rsp 'LLM API Key: ' LLM_API_KEY
export LLM_API_KEY
printf '\n'
ros2 launch ras_keenon_chat chat.launch.py
```

设置中的 API Key 只保存在后端内存，不写 SQLite，不通过配置 GET 接口返回网页。Base URL、模型名、动作地址与模式会保存；Key 留空表示保留当前内存中的 Key。服务重启后重新填写，或通过 `LLM_API_KEY` 提供。

如果网页后端运行在操作电脑，动作服务地址应改成 `http://192.168.3.20:8766`。浏览器始终请求同源网页后端，不直接携带 API Key 调用云模型。

## 4. 对话、记录与回执

左侧可新建、搜索和切换对话；刷新页面或正常重启服务后记录仍在。中间输入框支持 Enter 发送、Shift+Enter 换行。右侧可以筛选“全部 / 模型 / 动作”并展开完整 JSON 回执。

模型回执显示：

- 模型名、模型响应 ID、供应商请求 ID（如果返回）。
- HTTP 状态、实际请求耗时、finish_reason。
- 输入/输出/总 Token（供应商返回 usage 时展示；不返回时显示空值）。
- 工具调用数量、原始响应及错误详情，已对当前配置的 API Key 做脱敏。

动作回执显示工具名、参数、动作任务 ID、执行进度、最终状态和错误。演示回执会被标记，不展示虚构的付费 Token 用量。

“取消本轮”会取消当前回合及其正在等待的动作；即使模型晚到返回，取消回合也不会再派发动作。“停止动作”可独立于正在进行的模型请求调用，取消当前对话服务的未完成回合并请求动作服务停止，回执中保留结果。

动作状态成功表示接口返回成功；`arrival_verified=false` 仍表示没有独立机械到位确认。厂商 SDK 阻塞轨迹不能保证中途打断。取消后不会自动返回起点或补发剩余动作。

## 5. 数据与服务参数

默认数据库位于 `/home/peanut/ras_keenon_ws/run/chat/chat.sqlite3`。它记录对话、任务状态、回执以及不含 Key 的设置。右下角导出按钮可下载当前对话及回执 JSON。服务重启时，未完成回合标记为已中断，不会自动重新请求模型或重放动作。

```bash
# 更换网页端口、动作服务地址或数据库目录
ros2 launch ras_keenon_chat chat.launch.py port:=8771 \
  action_url:=http://127.0.0.1:8766 \
  data_dir:=/home/peanut/ras_keenon_ws/run/chat_other
```

可用 HTTP 接口：

| 方法与路径 | 功能 |
|---|---|
| `GET /api/config` / `POST /api/config` | 读取无密钥配置 / 更新设置 |
| `GET /api/sessions` / `POST /api/sessions` | 列出 / 新建对话 |
| `GET /api/sessions/<id>` | 消息、回合状态与回执 |
| `POST /api/chat` | `{session_id, message}`，立即返回回合 ID |
| `POST /api/cancel` | `{turn_id}`，取消本轮 |
| `POST /api/stop` | `{session_id}`，停止并记录控制回执 |
| `GET /api/tools` | 12 个预设动作的工具定义 |

网页约 700 ms 更新一次状态；当前版本使用非流式模型请求，完整文本返回后显示。等待期间仍能看到请求回执、进行状态，并可取消或停止。

## 6. 构建与验证

```bash
cd /home/peanut/ras_keenon_ws
source /opt/ros/humble/setup.bash
source install/setup.bash
colcon build --symlink-install --packages-select ras_keenon_chat
source install/setup.bash
python3 -m unittest discover -s src/ras_keenon_chat/test -p 'test_*.py' -v
```

检查使用本地假 LLM API 和假机器人控制器，覆盖真实 HTTP 协议的工具调用闭环、顺序执行、取消后防止迟到动作、错误回执、Key 脱敏与持久化。它们不需要真实模型 Key，不产生真实机器人运动。

本地开发不依赖 Node 构建或第三方 Python Web 框架：

```bash
cd /home/roko/文档/ChatGPT/Keenon
PYTHONPATH=ras_keenon_ws/src/ras_keenon_chat:ras_keenon_ws/src/ras_keenon_demos \
  python3 -m ras_keenon_chat.server --demo --port 8770
```

## 7. 兼容协议依据

实现遵循 [OpenAI 官方 Function Calling 文档](https://developers.openai.com/api/docs/guides/function-calling) 和 [Chat Completions 请求格式](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)：向模型提供工具定义，接收 `tool_calls`，将执行结果以 `role=tool` 和匹配的 `tool_call_id` 回传。当前使用 `strict=false` 保留既有动作参数的可选默认值，并请求 `parallel_tool_calls=false`；即便服务商返回多个调用，应用层仍顺序执行。

不同供应商支持情况以其兼容接口为准。当前未实现 Responses、Anthropic 原生或 Gemini 原生协议，也未加入语音识别/语音合成；这些可在现有对话输入输出层继续扩展。


操作电脑也可通过工作区一键入口运行（不要求 ROS）：

```bash
cd /home/roko/文档/ChatGPT/Keenon
bash ras_keenon_ws/start_chat_web.sh
# 或先体验明确标注的演示模式
bash ras_keenon_ws/start_chat_web.sh --demo
```

本机地址为 http://127.0.0.1:8770 。一键脚本默认动作地址是机器人 `192.168.3.20:8766`，电量控制器地址为 `192.168.3.20:8765`；可分别用 `--action-url`、`--controller-url` 覆盖。演示模式不会查询真实动作或电量接口。

## 电量与供电状态

页面顶部的电量卡片每 5 秒读取 `/api/battery`，也可点击“刷新电量”。聊天后端直接代理控制器的 `/battery`，不经过动作服务或 LLM。ROS launch 可用 `controller_url:=http://192.168.3.20:8765` 指定远端控制器；未指定时默认本机 `controller_port`。

当前机器人上报 0% 但电池状态未确认，充电状态为未知；没有已验证的适配器直连字段，因此显示“外接电源：未确认”。原始充电码 49 的枚举未确认，不自动映射为充电或插电。完整说明与查询接口见 [BATTERY_UI.md](../../BATTERY_UI.md)。
