# ras_keenon_ws

Keenon/XMAN-R1 的 ROS 2 Humble 控制工作区。当前入口使用周期直接控制，按下保持、松开发送停止事件，无 E 使能、base_ready、控制源选择或 epoch 动作门槛。

完整操作方法、周期说明、参数指令和视觉接口见 **[DIRECT_CONTROL.md](DIRECT_CONTROL.md)**。

手动控制页和对话网页均显示电量及供电状态，每 5 秒刷新，也可点击“刷新电量”。当前底盘上报 0% 但电池状态未确认，适配器直连信号尚未确认；字段说明与查询方法见 [BATTERY_UI.md](BATTERY_UI.md)。

## 在机器人上启动

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
ros2 launch ras_keenon_control manual.launch.py backend:=hardware
```

浏览器打开 http://192.168.3.20:8765 。W/S 前后、A/D 旋转、H/L 转头、I/K 俯仰、R/F 直立/下蹲、空格停止。无需先按 E。页面同时有可按住的按钮和参数指令输入框。

默认输入循环 2 ms、底盘发送 20 ms、头部更新 50 ms。可用 `cycle_ms`、`base_period_ms`、`body_period_ms` 参数调整；这不是硬实时保证。当前 manual.yaml 的按键默认速度为 0.1 m/s、0.1 rad/s，头部 5 deg/s。只连接底盘用 `components:=base`。

手动运动参数统一在 [`src/ras_keenon_commands/config/manual.yaml`](src/ras_keenon_commands/config/manual.yaml) 配置，包括线速度、角速度、控制周期和停止超时。启动时默认读取包内配置，也可传 `config:=/绝对路径/manual.yaml` 指定文件；修改后重启生效。显式命令行参数可覆盖配置值。安装与生效方式见 [DIRECT_CONTROL.md](DIRECT_CONTROL.md)。

R/F 每次按下提交一个腰部目标。腰和机械臂的 SDK 轨迹为阻塞调用，松键只能取消后续发送，不能保证中断已提交轨迹；底盘松键发送零速度，头部松键发送当前位置保持。

## 参数化命令

```bash
# 独立执行一条命令，然后退出
ros2 launch ras_keenon_control command.launch.py backend:=hardware command:='h -10 1.2'

# 已有手动控制器时，在另一终端复用它
ros2 launch ras_keenon_control command.launch.py start_controller:=false
```

交互命令示例：`h -10 1.2`、`v -5 1.2`、`s 0.03 -`、`sd +`、`b`、`stand`、`base 0.02 0 3`、`g`、`stop`、`help`、`q`。新指令替代旧运动指令。

## 包结构与接口

- `ras_keenon_commands`：直接输入循环、真实按键事件网页、参数终端、底盘与部件工作线程。
- `ras_keenon_command_interfaces`：不带 header/epoch 的 `RunCommand` 服务。
- `ras_keenon_command_native`：直接调用匹配的厂商 SDK，独占部件连接。
- `ras_keenon_control`：当前 `manual.launch.py` / `command.launch.py` 统一启动入口；历史 Python 控制器源码保留供参考，当前入口不加载其状态机。
- `ras_keenon_interfaces` / `ras_keenon_native`：历史控制器使用的接口与 SDK helper。

视觉系统可使用 `/ras_keenon_commands/execute`、`/ras_keenon_commands/command`、`/ras_keenon_commands/keys`，或调用 `/ras_keenon_commands/stop`。接口示例见完整文档。

## 编译与验证

```bash
cd /home/peanut/ras_keenon_ws
source /opt/ros/humble/setup.bash
colcon build --symlink-install --executor sequential \
  --packages-select ras_keenon_command_interfaces ras_keenon_command_native \
                    ras_keenon_control ras_keenon_commands
source install/setup.bash
python3 tests/run_mock_checks.py
```

默认 `backend:=mock` 不连接真实 SDK、CAN 或底盘。测试覆盖假 SDK、周期输入、持续按键与松键、ROS/HTTP 指令、参数 launch、交互终端退出停止。验证记录见 `verification/`。真机运动尚未测试。

## 独立演示与对话动作接口

新增挥手、抬臂、转头、点头、摇头、下蹲、直立、前进、旋转和停止脚本。启动与 LLM 工具接口见 [DEMO_ACTIONS.md](DEMO_ACTIONS.md)。

## LLM 对话网页

新增含对话框、对话历史和模型/动作回执的网页，支持可配置 OpenAI 兼容 API。启动及配置见 [LLM_CHAT.md](LLM_CHAT.md)。

复位、机械臂诊断和取消底盘 0.03 速度上限见 [RESET_AND_ARM_DIAGNOSTICS.md](RESET_AND_ARM_DIAGNOSTICS.md)。复位入口为 `ros2 run ras_keenon_demos reset`，LLM 工具名为 `robot_reset`。

2026-11-03 机械臂单位问题已定位：SDK getter 返回度，运动目标为弧度；桥接已统一转换。运行中的旧控制器需重启加载，见 [ARM_JOINT_UNITS_FIX.md](ARM_JOINT_UNITS_FIX.md)。

大幅挥手入口为 `ros2 run ras_keenon_demos wave_large`；原单肘挥手已更名为 `shake_hand`。动作步骤、参数与 LLM 接口见 [WAVE_ACTIONS.md](WAVE_ACTIONS.md)。


## 2026-11-05 恢复部署与原厂模式反馈

本轮机器人重启后恢复真机只读网页，最终 launch PID 11526，地址 http://192.168.3.20:18770/navigation 。新增 factory_mode，当前原厂 mode=0 为导航模式；本地和机上各 38 项相关测试通过，Humble 两包重建通过。取得 web_command 建图命令及 simple-goal 入口，见 FACTORY_MAPPING_PROTOCOL.md；启动、备份与后续未完成项见 ROBOT_NAVIGATION_DEPLOYMENT.md。没有发送控制服务/运动或新初始化 SDK，完整自主探索仍待现场闭环验证。截图和校验在 verification/robot_deployment_20261105/。


## 2026-11-05 手动网页原厂建图集成

8765 手动页已增加 Gmapping 新建、实时图像、原厂新楼层保存回读、取消、PNG 快照和回执，使用 MANUAL_MAPPING.md。数据位于 run/mapping/hardware，launch PID 23767、SDK23816、独立建图后台25340；原厂只读11526保留。源码备份 backups/manual_mapping_20261105_052243。Humble 三包构建通过，机上54项测试通过；本地54项导航/建图、22项直接控制回归及浏览器/ROS模拟流程通过。完整真实 SLAM 与保存仍待操作者首次现场验收；本轮没有自动提交建图或驾驶请求，控制器更新包含原有停车和 SDK 初始化。详见 verification/manual_mapping_20261105/ 的哈希、日志、运行状态与截图。
