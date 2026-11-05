# Keenon 周期直接控制与参数指令

2026-10-30 更新。机器人工作区为 `/home/peanut/ras_keenon_ws`，本地项目中有同名源码副本。

## 改动后的控制方式

`ras_keenon_control/manual.launch.py` 现在启动新的直接控制器与浏览器面板。动作不再检查 E 使能、`base_ready`、速度回读、控制源、epoch 或旧的演示时长窗口，也不再因为 `/cmd_vel` 回显而解除使能。后退、左右旋转与头部控制直接可用。

输入采用“当前按下的键集合”：按下设置状态，保持按键则持续执行，松开发出停止事件。普通 SSH 终端只给出字符及重复字符，不提供真实松键，因此真正的按下/松开控制使用浏览器键盘事件或页面按钮。无需按 E。

默认调度如下：

| 环节 | 默认周期 | 具体行为 |
|---|---:|---|
| ROS 输入处理 | 2 ms，500 Hz 目标 | 读取最新按键/指令，计算底盘目标，处理定时指令结束 |
| 底盘输出 | 20 ms，约 50 Hz | 持续发布当前 `/cmd_vel`，松键额外唤醒线程发送零速度 |
| 头部目标更新 | 50 ms，约 20 Hz | 按设定角速度增量更新目标；松键发送当前位置保持 |
| 腰/机械臂 | 新目标到来时调用 | 使用 SDK 轨迹接口，不以 500 Hz 重复提交阻塞轨迹 |

2 ms 是 Linux + Python/ROS 的软定时目标，不代表 CAN、底盘或机械关节具有 2 ms 硬实时响应。ROS 的调度抖动计数可在页面读数和退出后 `run/direct_cycle_stats.json` 中查看；报告中的 `mean_ms/max_ms` 为实际输入回调间隔。

## 启动手动控制

先正常退出之前的控制 launch，避免两个进程同时持有同一 SDK 或底盘连接。新程序仍保留进程互斥锁，这是设备独占要求，不是使能或底盘就绪条件。

在机器人 SSH 终端执行：

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
ros2 launch ras_keenon_control manual.launch.py backend:=hardware
```

然后在操作电脑浏览器打开 **http://192.168.3.20:8765**。确认页面显示“真实硬件模式”。点击页面空白处即可使用键盘；在指令输入框中输入时不触发移动快捷键。

页面顶部同时显示电量、更新时间和供电状态，支持自动刷新及“刷新电量”。当前底盘的电池字段未完整填充，0% 不代表已确认电池耗尽；适配器直连状态尚未确认。数据来源与独立查询接口见 [BATTERY_UI.md](BATTERY_UI.md)。

| 按键 | 动作 |
|---|---|
| W / S | 前进 / 后退，保持期间持续输出 |
| A / D | 左转 / 右转，保持期间持续输出 |
| H / L | 头向左 / 向右 |
| I / K | 头上仰 / 下俯 |
| R / F | 直立 / 下蹲目标 |
| 空格 / 停止按钮 | 清空按键、底盘发送零速度、取消后续部件指令 |

支持 W+A 等组合键。松开某个键会撤销对应分量，仍按住的键继续生效。关闭页面、切换焦点、窗口隐藏会清空按键；网络中断时，按键心跳默认 300 ms 未刷新则清空（`key_timeout`）。底盘线程自身默认在 250 ms 内没有收到上层刷新时也输出零（`base_timeout`）。这些是输入结束处理，不需要人工复位或重新使能。

手动控制统一读取 `src/ras_keenon_commands/config/manual.yaml`。底盘线速度 `speed`（m/s）、角速度 `turn_speed`（rad/s）、输入周期 `cycle_ms`、底盘发送周期 `base_period_ms`、按键心跳超时 `key_timeout`（秒）、底盘刷新超时 `base_timeout`（秒），以及头部更新周期、头部角速度和下蹲目标都在此文件中。当前工作区配置为底盘 `0.1 m/s`、旋转 `0.1 rad/s`、头部 `5 deg/s`，F 下蹲 `0.03 m`。

修改配置后重启 launch 生效。源码默认配置通过包安装到 `share/ras_keenon_commands/config/manual.yaml`；普通安装修改源码后需重新构建 `ras_keenon_commands`。也可以复制为独立文件，通过绝对路径加载，后续编辑该文件只需重启：

```bash
ros2 launch ras_keenon_control manual.launch.py backend:=hardware \
  config:=/home/peanut/ras_keenon_ws/src/ras_keenon_commands/config/manual.yaml
```

配置中的 `/**/direct_controller` 可匹配自定义 namespace。配置文件缺失会直接报错。命令行显式传入的运动参数优先于配置文件，未传入的参数使用文件值；仍可临时覆盖：

```bash
ros2 launch ras_keenon_control manual.launch.py backend:=hardware \
  cycle_ms:=2.0 base_period_ms:=20.0 body_period_ms:=50.0 \
  speed:=0.02 turn_speed:=0.02 head_rate:=5.0 squat_height:=0.03
```

这些参数在启动时读取，不支持运行中热更新。网页和 ROS 按键集合都使用该速度配置；`base vx wz seconds` 参数指令仍使用指令中明确给出的速度和时长。旧 `ras_keenon_control/config/control.yaml` 属于历史控制器，当前手动入口不读取它。

手动按键速度 `speed` / `turn_speed` 接受有限、非负数，不再套用旧的 0.03 调试上限；方向由按键决定。头角仍为 ±20°、下蹲 0–0.273 m、臂关节绝对值不超过 3.2 rad、机械臂速度系数 0.1–1。参数化 `base/forward/back/left/right`、动作脚本和 LLM 工具现也取消了 0.03 速度上限：`base` 接受有限有符号速度，其余方向指令接受有限非负速度；应用层不再设置速度最大值，设备最终响应仍由底盘固件决定。没有 2 秒按键动作时长限制。参数底盘指令支持 0.001–3600 秒，并在到时后请求零速度。

只调底盘可用 `components:=base`，此时不加载会初始化双臂的 SDK；`components:=body` 只调部件；默认 `all`。

```bash
# 不连接硬件的完整演示
ros2 launch ras_keenon_control manual.launch.py backend:=mock
```

## 松键停止的实际范围

底盘松键发送零速度，头部松键发送 `setJointAngle(30/31, 当前 SDK 位置)`。这里的“立即”指立即提交停止事件，物理动作还受网络、驱动器与制动过程影响。

R/F 腰部目标和参数化机械臂命令使用厂商轨迹接口。当前 SDK 的腰部 `moveJ_w` 没有对应停止函数；手臂 `moveJ_l/r_block` 为阻塞调用。程序接受松键/停止事件，取消排队目标与后续发送，但不保证中断已进入 SDK 的腰或臂轨迹。不会为了松键而在另一个线程并发调用未经验证的 SDK 停止函数。

## 头腰参数指令

已经启动手动控制器时，最简单的方法是在网页指令框输入。也可以在第二个 SSH 终端连接同一 ROS 控制器：

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
ros2 launch ras_keenon_control command.launch.py start_controller:=false
```

在 `ras>` 下输入，兼容本地 SOP 中的头腰指令思路：

```text
h -10 1.2      # 头部 yaw 绝对角 -10 度；提交后等候 1.2 秒
v -5 1.2       # 头部 pitch 绝对角 -5 度
head -8 2 0    # 同时设置 yaw/pitch，不附加等候
s 0.03 -       # 下蹲 3 cm，负方向弯腰
sd +           # 设置此服务端后续 s 的默认弯腰方向
s 0.05         # 使用默认方向，下蹲 5 cm
b              # 现有 SOP 入箱预置：s 0.20 +
stand          # s 0，直立
base 0.02 0 3  # 前进 3 秒，随后底盘发送零速度
base 0 -0.02 1 # 右转 1 秒
arm left 0 0 0 0.1 0 0 0 0.1
stop           # 停止，取消后续命令
 g             # 读取部件/底盘输出与调度统计
help
q
```

h/v 的等候参数是提交命令后的等待，不是轨迹速度或到达确认。返回成功表示 SDK 调用已返回，不代表经过独立测量确认到位。`g` 的头部反馈新鲜度仅供观察，不会作为动作门槛。

也可以独立启动并执行一条指令：

```bash
ros2 launch ras_keenon_control command.launch.py backend:=hardware command:='h -10 1.2'
```

独立单次命令结束后 launch 退出。默认 `components:=auto`：单条底盘指令只连接底盘，单条头腰臂指令只连接部件 SDK，交互终端连接全部部件。已有控制器运行时应加 `start_controller:=false`，复用现有服务；不应再启动第二个硬件控制器。

## 后续视觉控制接口

新入口位于 `/ras_keenon_commands`，不使用旧 `/ras_keenon` 的使能/控制源协议。可调用服务，或发布命令字符串：

```bash
# 无 header、epoch 或 enable 字段
ros2 service call /ras_keenon_commands/execute \
  ras_keenon_command_interfaces/srv/RunCommand \
  "{request_id: 'vision-head-001', instruction: 'head -5 2 0'}"

ros2 topic pub --once /ras_keenon_commands/command std_msgs/msg/String \
  "{data: 'base 0.02 0 0.2'}"

ros2 service call /ras_keenon_commands/stop std_srvs/srv/Trigger '{}'
```

`request_id` 仅用于避免同一请求重复执行；新的动作请生成新 ID。新输入替代旧输入；没有人工/视觉控制源切换过程。需要持续底盘控制时，可连续提交短时 `base` 命令，或向 `/ras_keenon_commands/keys` 发布同浏览器的按键集合：

```bash
ros2 topic pub --once /ras_keenon_commands/keys std_msgs/msg/String \
  '{data: "{\"client\":\"vision-01\",\"seq\":1,\"keys\":[\"w\"]}"}'
```

上面只展示消息结构：**实际代码必须每次递增 `seq`**，固定序号重复发布不刷新心跳；上述单条按下消息在 300 ms 后自动清空。停止消息用 `keys: []`，同样递增序号。这个序号只解决网络乱序导致“旧按下包覆盖新松开包”，不是机器人状态条件。

视觉与按键动作采用最新输入优先。`g/help/sd` 不取消动作；新的运动目标会替代先前目标，`stop` 取消目标。新 SDK helper 位于 `ras_keenon_command_native`，头部命令直接设置目标，没有旧版反馈到达等待或小步距离拒绝条件。

## 验证与重新构建

单元测试使用假 SDK；ROS/HTTP 集成测试使用 `backend:=mock`，不会连接 CAN、初始化真实 SDK 或移动机器人。集成日志位于 `verification/direct_mock_report.json`、`verification/direct_mock_launch.log`、`verification/command_oneshot_mock.log`、`verification/command_interactive_mock.log`。真机运动与真实机械停止距离尚未测试。

```bash
cd /home/peanut/ras_keenon_ws
source /opt/ros/humble/setup.bash
colcon build --symlink-install --executor sequential \
  --packages-select ras_keenon_command_interfaces ras_keenon_command_native \
                    ras_keenon_control ras_keenon_commands
source install/setup.bash
ROS_DOMAIN_ID=167 ROS_LOCALHOST_ONLY=1 python3 tests/direct_mock_integration.py
ROS_DOMAIN_ID=167 ROS_LOCALHOST_ONLY=1 python3 tests/manual_config_mock_integration.py
```

旧文档中的 E/Z/X、`allow_motion`、`allow_reverse`、`allow_turn`、`demo_mode`、`select_source` 等属于旧控制器；当前 `manual.launch.py` 不加载它们。当前使用方法以本文件为准。

### 本次验证记录

40 项假 SDK / 单元检查通过；ROS 与 HTTP 联合验证覆盖持续按键、松键、乱序包、心跳丢失、参数指令与交互终端退出停止。浏览器已实测键盘按下/松开和参数表单，截图见 [模拟控制面板](verification/direct_panel_mock.jpg)。实测周期数字以 `verification/direct_mock_report.json` 为准，均来自 mock，不代表真实机械响应时间。
