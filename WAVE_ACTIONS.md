# 大幅挥手 wave_large 与单肘摆手 shake_hand

更新：2026-11-03。

本次代码已同步到机器人 `192.168.3.20`，动作与聊天两个包已构建通过。部署期间网络曾短暂不可达，恢复后已完成安装；使用新动作前需重启动作与聊天服务。新大幅轨迹尚未执行真机运动。

| 动作指令 | LLM 工具 | 行为 |
|---|---|---|
| `wave_large` | `robot_wave_large` | 肩、肘、腕配合，先展开、举高手臂，再在头顶上方往复摆动，默认收回起点 |
| `shake_hand` | `robot_shake_hand` | 原 `wave` 的单肘摆手，保持原参数和运动方式，默认返回起点 |

`wave` / `robot_wave` 已正式更名，不再注册；旧脚本和 API 调用需要更新名称。`shake_hand` 在本项目中表示原单肘摆手，不是与人握手。当前动作与 LLM 工具共 12 项。

## 部署包

本地已生成带校验和及备份安装程序的 [部署包](verification/wave_actions_update_20261103.tar.gz)。在操作电脑执行：

```bash
scp /home/roko/文档/ChatGPT/Keenon/ras_keenon_ws/verification/wave_actions_update_20261103.tar.gz peanut@192.168.3.20:/tmp/
ssh peanut@192.168.3.20
```

然后在机器人终端执行：

```bash
cd /tmp
tar -xzf wave_actions_update_20261103.tar.gz
python3 /tmp/wave_actions_update/apply_update.py \
  --workspace /home/peanut/ras_keenon_ws --build
source /home/peanut/ras_keenon_ws/install/setup.bash
```

安装程序先校验文件，备份旧源码及脚本，清理旧 `wave` 入口，再构建 `ras_keenon_demos`、`ras_keenon_chat`；不写入模型 Key、聊天数据库或手动速度配置，不启动或停止机器人进程。安装后按下一节重启动作服务和聊天服务。

## 启动及调用

更新后需重启动作服务和聊天服务，让它们重新加载动作目录、规划代码和 LLM 工具定义。已有硬件控制器可以继续复用，不需要为本次动作更新重启 SDK。

完整动作栈的启动命令：

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
ros2 launch ras_keenon_demos demos.launch.py backend:=hardware
```

如果已经在运行 `manual.launch.py backend:=hardware`，只启动动作服务：

```bash
ros2 launch ras_keenon_demos demos.launch.py start_controller:=false
```

如果原来运行的就是完整 `demos.launch.py`，在原终端退出旧 launch 后重新启动它；不要再开第二个硬件控制器。聊天服务重启命令为 `ros2 launch ras_keenon_chat chat.launch.py`，原配置和对话数据库保持不变。

另一个加载环境的终端：

```bash
# 读取当前姿态并打印计划，不发送动作
ros2 run ras_keenon_demos wave_large --side right --dry-run

# 右臂举高挥手两次，默认返回本轮起点
ros2 run ras_keenon_demos wave_large --side right --cycles 2 --timeout 600

# 左臂镜像动作
ros2 run ras_keenon_demos wave_large --side left --cycles 2 --timeout 600

# 原单肘摆手
ros2 run ras_keenon_demos shake_hand --side right --cycles 2

# 取消整个动作序列
ros2 run ras_keenon_demos stop
```

动作脚本也可以从本地电脑运行，不需要安装 ROS：

```bash
python3 ras_keenon_ws/src/ras_keenon_demos/scripts/wave_large.py \
  --api-url http://192.168.3.20:8766 --side right --cycles 2 --timeout 600
python3 ras_keenon_ws/src/ras_keenon_demos/scripts/shake_hand.py \
  --api-url http://192.168.3.20:8766 --cycles 2
```

## wave_large 的多关节轨迹

先读取所选侧的七轴起点，使用七轴绝对目标姿态，顺序执行以下阶段：

1. 向身体外侧展开手臂，整理上臂和手腕朝向。
2. 肩部 J2 抬举，肩部 J1 与肘部 J4 配合举高手臂。
3. 保持抬举高度，由肩部 J1、肘部 J4 和腕部 J6 往复摆动。
4. 摆动结束后回到举高中心目标。
5. 默认先向外收低，再返回本次读取的完整七轴起点。`--no-return-to-start` 则保持举高中心目标。

默认右臂关键点如下，表中为**度**，程序发送 SDK 前使用弧度：

| 阶段 | J1 / q[0] | J2 / q[1] | J3 / q[2] | J4 / q[3] | J5 / q[4] | J6 / q[5] | J7 / q[6] |
|---|---:|---:|---:|---:|---:|---:|---:|
| 向外展开 | -20 | 0 | 0 | -25 | 0 | 0 | 0 |
| 举高中心 | -20 | -75 | 0 | -25 | 0 | 0 | 0 |
| 摆动端点一 | -30 | -75 | 0 | -30 | 0 | -20 | 0 |
| 摆动端点二 | -10 | -75 | 0 | -20 | 0 | 20 | 0 |

左右采用参考模型的镜像符号，左臂关键点为上述角度逐轴取反。起点姿态不参与镜像，始终使用所选侧的本轮实际读数。J3/J5/J7 也会调整至关键点的中性方向；这不是只修改一个关节的相对动作。

每次 SDK 调用包含完整七轴目标，由 SDK 生成关节轨迹。相邻目标按最大关节变化 15° 分段，包括展开、举高、摆动、收回全过程；不会把肩部大角度一次跳成一个目标。不会移动头部、腰部或底盘，也没有新增 E 使能、base_ready 或校准状态前置条件。

## 参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--side` | `right` | `right` / `left` |
| `--shoulder-lift-deg` | 75 | J2 抬举目标幅值，60–90°，左右使用镜像符号 |
| `--elbow-bend-deg` | 25 | 举高中心的肘弯曲幅值，15–40° |
| `--amplitude-deg` | 10 | 肩部 J1 往复幅值，0–20°；肘部联动幅值为其一半 |
| `--wrist-amplitude-deg` | 20 | 腕部 J6 往复幅值，0–35° |
| `--cycles` | 2 | 往复次数，1–10 |
| `--speed-factor` | 0.1 | SDK 速度系数，0.1–1，不是固定角速度 |
| `--pause-s` | 0.2 | 举高及摆动端点停留秒数，0–3 |
| `--return-to-start` | true | 默认收回；使用 `--no-return-to-start` 保持举高目标 |
| `--timeout` | 240 | 命令客户端总等待秒数；较长动作可设为 600 |

## Python、HTTP 与 LLM 接口

```python
from ras_keenon_demos import ActionClient
robot = ActionClient('http://192.168.3.20:8766')

# 以下为不同接入方式，按需求选择，避免重复执行。
plan = robot.plan('wave_large', {'side':'right'})
result = robot.run('wave_large', {'side':'right','cycles':2}, timeout=600)
job = robot.call_tool('robot_wave_large', {'side':'left','cycles':2}, call_id='dialogue-large-001')
```

```bash
curl -s http://192.168.3.20:8766/actions \
  -H 'Content-Type: application/json' \
  -d '{"action":"wave_large","parameters":{"side":"right","cycles":2},"request_id":"large-001"}'
```

旧动作对应的 API 名称改为 `shake_hand`，LLM 名称改为 `robot_shake_hand`。工具 Schema 继续由同一目录自动生成。

网页普通“挥手”“大幅挥手”“举手过头顶挥手”使用 `robot_wave_large`；“原来的挥手”“小幅挥手”“屈肘摆手”使用 `robot_shake_hand`。离线演示模式也区分这两类动作。真实 API 模式要求模型支持工具调用。

## 模型验证与实际边界

本次读取了机器人 `whole_dh.urdf` 七轴参考模型及 `qinglang_sync.urdf` 简化头部模型，检查左右镜像方向和举高阶段。默认摆动期间参考模型的腕部高度最低约为肩部以上 **0.470 m**；简化头部模型估计头顶为肩部以上 **0.370 m**。这支持默认关键点的举高手臂意图，但两份参考模型没有完成统一实机标定，不能当作实测头部间隙或碰撞检查结果。

已完成动作规划、取消、左右镜像、回到起点、LLM 工具和 mock 调用验证。新大幅轨迹没有在真机上执行；`arrival_verified=false`，不能把接口返回成功当成机械到位确认。阻塞 SDK 调用无法保证中途打断，取消会阻止后续步骤，也不会自动发送返回姿态。

轨迹实现位于 `src/ras_keenon_demos/ras_keenon_demos/gestures.py`，分段规划位于 `plans.py`。参考文件、模型验证脚本、结果与本次读数保存在 `verification/wave_large_reference_20261103/` 和 `verification/wave_large_model_report_20261103.json`；工作日志会记录实际测试及部署情况。
