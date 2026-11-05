# 复位、机械臂排查与底盘速度参数

更新：2026-11-03。适用于 `/home/peanut/ras_keenon_ws` 的当前直接控制入口。

后续排查已确认机械臂调度存在“SDK 读数为度、规划器按弧度解释”的单位错误，已修复并安装，需重启硬件控制器加载。详见 [机械臂角度单位修复](ARM_JOINT_UNITS_FIX.md)。下文关于初次检查时服务未运行的描述保留为历史记录。

## 复位动作

新增独立脚本 `src/ras_keenon_demos/scripts/reset.py`、ROS 命令 `reset`、动作 API `reset` 和 LLM 工具 `robot_reset`。

| 部件 | 复位目标 |
|---|---|
| 底盘 | 先发送停止，不改变定位坐标 |
| 头部 | yaw、pitch 均为 0° |
| 右臂 | 七轴均为 0 rad |
| 左臂 | 七轴均为 0 rad |
| 腰部 | 最后保持下蹲，默认高度 0.03 m、方向 `-` |

腰部是归零要求中的例外：腰部全零对应直立，因此按要求使用下蹲目标。下蹲高度是相对直立的下降量，不是机器人总高度。每只机械臂从本次读数逐步回零，单步最大关节变化 15°，双臂顺序执行；不改写编码器零点或标定。夹爪、手指目前没有接入该控制器，不包含在复位中。

机器人终端 1，启动控制器和动作服务：

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
ros2 launch ras_keenon_demos demos.launch.py backend:=hardware
```

如果已经启动 `manual.launch.py backend:=hardware`，只启动动作服务，复用原控制器：

```bash
ros2 launch ras_keenon_demos demos.launch.py start_controller:=false
```

机器人终端 2，加载环境后执行：

```bash
# 只读当前姿态并打印计划
ros2 run ras_keenon_demos reset --dry-run

# 执行默认复位
ros2 run ras_keenon_demos reset

# 修改最终下蹲高度和 SDK 速度系数
ros2 run ras_keenon_demos reset --height-m 0.1 --direction=- --speed-factor 0.1
```

`--height-m 0` 表示最终直立；默认是下蹲 3 厘米。`--settle-s` 默认 0.3 秒，表示头部命令后的等待。动作成功表示接口返回成功，`arrival_verified=false`，不等于独立验证了机械到位。

接口调用示例：

```python
from ras_keenon_demos import ActionClient
robot = ActionClient('http://192.168.3.20:8766')
result = robot.run('reset', {})
# LLM 工具参数也可省略，沿用默认值
job = robot.call_tool('robot_reset', {}, call_id='dialogue-reset-001')
```

上面两行分别演示普通调用和工具调用，实际集成时选择一种，避免重复复位。HTTP 请求为：

```bash
curl -s http://192.168.3.20:8766/actions \
  -H 'Content-Type: application/json' \
  -d '{"action":"reset","parameters":{},"request_id":"reset-001"}'
```

网页对话输入“请复位”即可让模型使用 `robot_reset`。工具目录由同一动作定义自动生成，目前共 11 个工具；离线演示模式也支持复位并显示回执。

## 机械臂状态与调度排查

本次通过 SSH 读取到：没有运行中的直接控制器或 SDK 桥接进程；8765、8766、8770 服务端口均未监听；`can0`–`can5` 为 `DOWN / STOPPED`。原始记录在 `verification/arm_status_initial_20261103.json`。这是检查时的状态，不能据此追溯此前每一次调度失败的原因，也不能判定电机已损坏。SDK 未运行时 CAN 关闭可以是正常退出后的状态。

已核对真实 SDK 静态库的初始化分支及关节映射：当前固定版本中 `robot_init(1)` 初始化双臂，`2` 仅右臂，`3` 仅左臂；头文件注释与库实现不一致，不能按注释直接改成 `3`。右臂七轴对应电机 ID 16–22，左臂 23–29。

此次改进：

1. 机械臂失败回执包含侧别和真实 `sdk_return`，不再只有泛化错误。当前 SDK 成功返回码为 1；失败时保留错误，不自动重发。
2. 机械臂桥接等待时间由 30 秒增加到 180 秒；动作客户端和 ROS 命令等待时间相应加长，避免较慢的阻塞轨迹过早被判超时。SDK 真正卡住仍需根据日志排查。
3. `GET /readings` 增加双臂每轴回调序号、反馈年龄和 `feedback_fresh`，不会把七个缓存零值当成在线证据。连续 1 秒内有七轴反馈时该侧为 fresh；此字段只用于诊断，不增加动作前置条件。
4. 控制器同时显示 `body_command` 和 `body_sample_age_s`。阻塞运动期间 READ 不刷新，应结合正在执行的命令判断旧快照。
5. 新增只读 `arm_status` 命令，复用已有控制器，不额外初始化 SDK，不发送运动命令。

保持终端 1 的硬件控制器运行，在另一个机器人终端执行：

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
ros2 run ras_keenon_commands arm_status --local-system
```

也可以从本地电脑读取机器人已有服务：

```bash
python3 ras_keenon_ws/src/ras_keenon_commands/ras_keenon_commands/arm_status.py \
  --controller-url http://192.168.3.20:8765
```

| 观察结果 | 含义与下一步 |
|---|---|
| 无法连接 8765 | 控制器未运行或网络/端口不可达，先查看启动终端 |
| `backend=mock` | 当前只模拟，不会驱动机械臂；真机启动需 `backend:=hardware` |
| `body` 为空 | 控制器仅连接了底盘，需 `components:=body` 或 `all` |
| 七轴为空，`body_error` 非空 | SDK 启动或通信失败，查看对应错误和启动日志 |
| 缓存角度全零，回调序号仍为零 | 尚未收到该侧关节反馈，不能认定机械臂在线 |
| `body_command=arm` 且样本变旧 | SDK 在执行阻塞轨迹，结果返回前不能单凭旧快照判断掉线 |
| 回执 `sdk_return != 1` | SDK 拒绝或执行失败；结合控制器原始日志确认关节范围、通信或电机错误 |

当前诊断不独立读取电机供电状态或电机故障码，相关字段为 `null`；反馈 fresh 也不能证明无故障。本次没有为检查而初始化执行器或发送机械臂运动，尚未完成机械臂实机运动验收。启用硬件控制器后，再用上述命令即可取得运行时证据。

## 取消底盘 0.03 速度上限

已取消当前链路中手动配置、参数化指令、演示脚本和 LLM 工具 Schema 的 0.03 上限，也移除了旧兼容控制代码对配置值的这一校验。

| 入口 | 参数规则 |
|---|---|
| 手动按键 `speed` / `turn_speed` | 有限非负数，方向由按键决定 |
| `base vx wz seconds` | vx、wz 为有限有符号数 |
| `forward/back/left/right speed seconds` | speed 为有限非负数，方向由命令决定 |
| `move_forward --speed-mps` | 有限非负数，无应用层最大值 |
| `rotate --speed-radps` | 有限非负数，无应用层最大值 |

示例，在已启动的命令终端输入 `base 0.1 0 1`，或执行：

```bash
ros2 run ras_keenon_demos move_forward --speed-mps 0.1 --duration-s 1
ros2 run ras_keenon_demos rotate --speed-radps 0.2 --duration-s 1
```

当前按键默认线速度为 0.1 m/s、角速度为 0.1 rad/s；动作脚本默认速度仍为 0.02。取消上限不会自动提高默认值。底盘固件自身的速度范围或限幅未改动，实际速度仍需现场测量。

## 部署后生效

更改已运行进程后需重启控制器、动作服务和聊天服务，重新加载工具定义。机器人工作区中构建命令：

```bash
cd /home/peanut/ras_keenon_ws
source /opt/ros/humble/setup.bash
colcon build --base-paths src --symlink-install --executor sequential --packages-select \
  ras_keenon_control ras_keenon_command_native ras_keenon_commands ras_keenon_demos ras_keenon_chat
source install/setup.bash
```

本次测试记录与实际部署情况追加在根目录 `工作日志.md` 和 `verification/reset_validation_20261103.json` 中。
