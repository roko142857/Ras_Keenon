# 独立演示动作与 LLM 调用接口

更新：2026-11-03。这个包使用现有 `ras_keenon_commands` HTTP 接口，不另外初始化 SDK。提供 12 个独立脚本、Python 客户端、异步动作 API 和供 LLM 使用的工具定义。

## 1. 启动

在机器人上：

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
# 完整启动：原有控制器 + 动作服务
ros2 launch ras_keenon_demos demos.launch.py backend:=hardware
```

控制面板仍在 `http://192.168.3.20:8765`，新增动作 API 在 `http://192.168.3.20:8766`。不需要 E 使能、base_ready 或控制源选择。

如果已经启动了 `manual.launch.py`，复用已有控制器：

```bash
ros2 launch ras_keenon_demos demos.launch.py start_controller:=false \
  controller_url:=http://127.0.0.1:8765
```

不要为动作服务再开一个硬件控制器。默认 `backend:=mock` 用于演示验证；它不会连接硬件。也可以只运行动作服务：

```bash
ros2 run ras_keenon_demos action_server \
  --controller-url http://127.0.0.1:8765 --host 0.0.0.0 --port 8766
```

## 2. 每个脚本对应一个动作

下面命令在机器人已 source 的终端执行。`ros2 run` 后的名称也各有一个同名 `.py` 文件，位于此包的 `scripts/` 目录。

| 动作 | 命令示例 | 结束行为 |
|---|---|---|
| 多关节举高挥手 | `ros2 run ras_keenon_demos wave_large --side right --cycles 2` | 肩肘腕配合，举高手臂往复，默认返回起点 |
| 单肘摆手（原 wave） | `ros2 run ras_keenon_demos shake_hand --side right --cycles 2` | 抬肘、小幅往复、默认返回本次起点 |
| 抬臂 | `ros2 run ras_keenon_demos raise_arm --lift-deg 30` | 默认保持抬起目标 |
| 转头 | `ros2 run ras_keenon_demos turn_head --yaw-deg -15` | 默认保持该角度，未指定 pitch 时保留俯仰 |
| 点头 | `ros2 run ras_keenon_demos nod --cycles 2` | 返回动作前的头部角度 |
| 摇头 | `ros2 run ras_keenon_demos shake_head --cycles 2` | 返回动作前的头部角度 |
| 下蹲 | `ros2 run ras_keenon_demos squat --height-m 0.03 --direction=-` | 保持下蹲目标 |
| 复位 | `ros2 run ras_keenon_demos reset` | 头部、双臂归零；腰部默认下蹲 3 cm |
| 直立 | `ros2 run ras_keenon_demos stand` | 腰部目标回到直立 |
| 前进 | `ros2 run ras_keenon_demos move_forward --duration-s 1` | 到时底盘输出零速度 |
| 旋转 | `ros2 run ras_keenon_demos rotate --direction left --duration-s 1` | 到时底盘输出零速度 |
| 停止 | `ros2 run ras_keenon_demos stop` | 取消整个演示序列，并停止当前底层指令 |

每个脚本可单独执行，例如本地电脑在项目中运行：

```bash
cd /home/roko/文档/ChatGPT/Keenon
python3 ras_keenon_ws/src/ras_keenon_demos/scripts/shake_hand.py \
  --api-url http://192.168.3.20:8766 --cycles 2
python3 ras_keenon_ws/src/ras_keenon_demos/scripts/turn_head.py \
  --api-url http://192.168.3.20:8766 --yaw-deg 10
python3 ras_keenon_ws/src/ras_keenon_demos/scripts/squat.py \
  --api-url http://192.168.3.20:8766 --height-m 0.03
```

同步客户端默认等待最多 240 秒，较长动作请通过 `--timeout` 或 `robot.wait(id, timeout=...)` 调整；对话系统也可以异步提交并查询。

这些客户端只用 Python 标准库，不要求操作电脑安装 ROS。动作服务仍需在机器人运行。`--help` 查看每个动作的全部参数；`--dry-run` 读取当前姿态并打印计划，不发送运动命令：

```bash
ros2 run ras_keenon_demos shake_hand --dry-run
ros2 run ras_keenon_demos turn_head --yaw-deg 15 --return-to-start
ros2 run ras_keenon_demos shake_hand --lift-deg 30 --amplitude-deg 5 \
  --speed-factor 0.1 --pause-s 0.2 --cycles 2
```

挥手的当前实现是“屈肘招手”，不是经过示教的肩/腕多轴人形挥手。它沿用本地《XMAN-R1 机械臂指令指南》已记录的方向：读取本轮该侧七轴起点，仅改变肘轴 `q[3]`，减小角度表示抬起，其余六轴保持起点目标。抬起/返回和往复过程均拆成不超过 15° 的目标变化。默认抬起 30°、往复幅度 5°、速度系数 0.1；同一次动作不复用历史起点。

这里不把速度系数当作角速度，也不把 SDK 返回当作独立到位确认。左臂大角度招手及整套新动作尚未做真机运动验收；已有文档中验证过的是右臂肘轴抬起与返回，以及左臂小角度移动。

## 3. Python 调用接口

在已安装此包的环境，或设置 `PYTHONPATH=ras_keenon_ws/src/ras_keenon_demos` 后：

```python
from ras_keenon_demos import ActionClient

robot = ActionClient('http://192.168.3.20:8766')

# 同步：等到动作服务返回最终结果
result = robot.run('shake_hand', {'side': 'right', 'cycles': 2})
print(result['state'])  # succeeded / failed / cancelled

# 异步：适合对话系统先回复，再观察动作完成
job = robot.submit('turn_head', {'yaw_deg': -10}, request_id='dialogue-7-action-1')
print(job['id'])
print(robot.status(job['id']))
result = robot.wait(job['id'])

# 取消指定动作，或停止全部演示
robot.cancel(job['id'])
robot.stop()
```

返回结果包含 `id`、`action`、规范化后的 `parameters`、`state`、步骤总数/完成数、起止时间、错误信息和实际动作计划。`result.arrival_verified` 当前始终为 `false`，成功表示本轮底层命令返回成功。服务内保留最近 128 项任务记录，重启后不保留；需要跨重启去重时由对话系统保存调用记录。

## 4. HTTP 接口

| 方法与路径 | 功能 |
|---|---|
| `GET /health` | 动作服务进程信息；不是机器人动作前置状态 |
| `GET /actions` | 动作目录、默认参数和范围 |
| `GET /tools` | 可交给 LLM 的工具定义 |
| `POST /plan` | 读取当前姿态，生成计划，不执行 |
| `POST /actions` | 提交动作，立即返回动作 ID |
| `GET /actions/<id>` | 查询执行进度和结果 |
| `POST /actions/<id>/cancel` | 取消指定动作 |
| `POST /stop` | 取消整个序列并发出底层停止 |
| `POST /tool_call` | 转发 LLM 的工具调用 |

```bash
curl -s http://192.168.3.20:8766/actions \
  -H 'Content-Type: application/json' \
  -d '{"action":"shake_hand","parameters":{"cycles":2},"request_id":"demo-shake_hand-001"}'

curl -s http://192.168.3.20:8766/stop \
  -H 'Content-Type: application/json' -d '{}'
```

同一 `request_id`、同一动作和参数返回同一个任务，不重复执行。用相同 ID 提交不同参数会返回错误。任务状态为 `queued → running → succeeded/failed`，取消时经过 `cancelling → cancelled`；排队动作取消时可直接进入 `cancelled`。

## 5. LLM 对话接入

`tool_schemas()` 或 `GET /tools` 提供 `robot_shake_hand`、`robot_wave_large`、`robot_raise_arm`、`robot_turn_head`、`robot_nod`、`robot_shake_head`、`robot_squat`、`robot_stand`、`robot_reset`、`robot_move_forward`、`robot_rotate`、`robot_stop`。每个定义含 `name`、`description`、`parameters` JSON Schema。对话 SDK 需要的外层格式由适配层补上；当前包不绑定 LLM 厂商、不保存 API key、不自动调用云模型。

```python
from ras_keenon_demos import ActionClient, tool_schemas

robot = ActionClient('http://192.168.3.20:8766')
functions = tool_schemas()  # 挂到你使用的对话模型工具配置

# 在收到模型工具调用之后转发；arguments 可为 dict 或 JSON 字符串
ack = robot.call_tool(
    name='robot_shake_hand',
    arguments={'side': 'right', 'cycles': 2},
    call_id='provider-tool-call-123',
)
# 把 ack 作为工具结果交回对话系统；不要此时声称机械动作已经完成
final_result = robot.wait(ack['id']) if 'id' in ack else ack
# 再将最终 state / error 交给对话系统
```

`robot_stop` 立即返回停止回执，不创建动作 ID；其他运动工具返回异步任务。

示例桥接程序 `examples/llm_action_bridge.py` 从标准输入逐行读取工具调用 JSON，向动作服务转发并输出任务回执：

```bash
cd /home/peanut/ras_keenon_ws/src/ras_keenon_demos
python3 examples/llm_action_bridge.py --print-tools
printf '%s\n' '{"name":"robot_shake_hand","arguments":{"cycles":2},"call_id":"dialogue-001"}' \
  | python3 examples/llm_action_bridge.py --api-url http://192.168.3.20:8766
```

协议只接受已登记动作与参数，不执行模型输出的 shell 或任意 SDK 函数。`call_id` 沿用模型提供的稳定工具调用 ID，可避免同一轮重复发送造成动作重放。无需先做使能/控制源切换。一轮对话需要多个连续动作时，适配层应逐个等待完成后再提交下一个动作；并发提交会按最新动作优先取消前项。

## 6. 取消与人工操作

动作服务按最新动作优先：新动作取消正在执行的序列，替代尚未开始的动作。取消或失败后不继续剩余步骤，也不自动执行“返回起点”。Ctrl+C 结束动作客户端时也会请求取消该任务。

需要停止整套演示时，使用本包的 `stop.py`、`ActionClient.stop()` 或动作 API `/stop`。旧控制面板上的停止只作用于底层当前指令，无法通知另一个进程中尚未发出的演示步骤，因此不要把它当作整套序列的取消接口。人工临时接管前先取消演示序列。

厂商腰部和机械臂的阻塞轨迹仍可能在取消后继续运行到 SDK 返回。该包取消后续步骤，不保证中断已进入 SDK 的物理轨迹。底盘停止和头部保持沿用现有直接控制器。

## 7. 构建与验证

```bash
cd /home/peanut/ras_keenon_ws
source /opt/ros/humble/setup.bash
source install/setup.bash
colcon build --symlink-install --packages-select ras_keenon_demos
source install/setup.bash
python3 -m unittest discover -s src/ras_keenon_demos/test -p 'test_*.py' -v
ROS_DOMAIN_ID=169 ROS_LOCALHOST_ONLY=1 \
  python3 src/ras_keenon_demos/test/ros_demo_integration.py
```

单元检查使用假控制器；ROS 集成检查明确使用 `backend:=mock`。不启动真实 SDK，不驱动机器人运动。集成报告保存在工作区 `verification/demos_mock_report.json`。

以后扩展动作时，在 `catalog.py` 登记参数与说明，在 `plans.py` 构造有限步骤，再添加同名脚本与 console entry point。动作目录、工具 schema、参数校验会共享同一份定义。

## 复位与速度参数更新（2026-11-03）

`reset` 按顺序停止底盘、设置头部 yaw/pitch 为 0°、将右臂和左臂各七轴设置为 0 rad，最后将腰部设置为下蹲 0.03 m、负方向弯腰。双臂各目标变化最多 15°，使用 SDK 自身轨迹执行；不是机械零点或编码器校准。可用 `--height-m 0.1 --direction=- --speed-factor 0.1` 自定义。`--height-m 0` 表示复位后直立。

`robot_reset` 已自动进入 LLM 工具目录，网页可以输入“请复位”。Python 用 `robot.run("reset")`，HTTP 用 `POST /actions` 的 `{"action":"reset","parameters":{}}`。

`move_forward --speed-mps` 与 `rotate --speed-radps` 已取消旧 0.03 上限，只要求有限非负数。默认值仍为 0.02，不因取消上限而自动加速。底盘固件是否限幅需在现场实测。

机械臂状态只读检查：`ros2 run ras_keenon_commands arm_status --local-system`。请保持已有控制器运行；此命令不会初始化 SDK。详细状态解释见工作区根目录 `RESET_AND_ARM_DIAGNOSTICS.md`。

## 多关节举高挥手与动作更名（2026-11-03）

新增 `wave_large` / `robot_wave_large`。默认右臂，先向外展开、肩肘配合举高，再以肩部 J1、肘部 J4、腕部 J6 往复摆动；肩部 J2 控制抬举。默认返回本轮起点，`--no-return-to-start` 则保持举高目标。使用独立脚本 `scripts/wave_large.py` 或 `ros2 run ras_keenon_demos wave_large --side right --cycles 2`。

原 `wave` / `robot_wave` 已正式更名为 `shake_hand` / `robot_shake_hand`，保持原先单肘摆手行为及参数；这里的 shake_hand 不表示与人握手。旧名称不再注册，请更新调用端。工具目录现在有 12 项。网页普通“挥手”默认使用 wave_large，明确“小幅挥手 / 屈肘摆手”使用 shake_hand。

两项均支持 `--dry-run`、Python、HTTP、LLM 调用及整个序列取消。大动作可能较长，可通过 `--timeout 600` 调整客户端等待。参考模型已核对举高阶段，尚未完成真机大幅动作验收。详细角度表、参数和启动方式见工作区根目录 `WAVE_ACTIONS.md`。
