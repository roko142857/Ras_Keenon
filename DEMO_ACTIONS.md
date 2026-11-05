# 演示动作入口

2026-11-03 更新 `ras_keenon_demos`：12 个独立动作脚本，以及后续 LLM 工具调用所需的 Python / HTTP 接口。

完整说明见 [动作脚本与 LLM 接口文档](src/ras_keenon_demos/README.md)。

机器人上先启动一次：

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
ros2 launch ras_keenon_demos demos.launch.py backend:=hardware
```

再从另一个终端调用单个动作：

```bash
ros2 run ras_keenon_demos reset
ros2 run ras_keenon_demos wave_large --cycles 2
ros2 run ras_keenon_demos shake_hand --cycles 2
ros2 run ras_keenon_demos turn_head --yaw-deg -15
ros2 run ras_keenon_demos squat --height-m 0.03
ros2 run ras_keenon_demos stand
ros2 run ras_keenon_demos stop
```

已有手动控制器运行时，启动动作服务请使用 `start_controller:=false`。查看动作计划用 `--dry-run`。其他动作包括抬臂、点头、摇头、前进和旋转。

脚本位于 `src/ras_keenon_demos/scripts/`，可直接在操作电脑用 Python 运行，并指定 `--api-url http://192.168.3.20:8766`，不要求操作电脑安装 ROS。

LLM 可读取 `GET /tools` 的工具定义，再通过 `POST /tool_call` 提交动作；用 `call_id` 防止重复执行，用动作 ID 查询结果或取消整个序列。Python 入口为 `from ras_keenon_demos import ActionClient, tool_schemas`。

SDK 阻塞轨迹仍不能保证中途打断；取消操作会停止后续步骤。新动作只完成了 mock / 假控制器验证，未执行真机运动。

`wave` / `robot_wave` 已更名为 `shake_hand` / `robot_shake_hand`；新增 `wave_large` / `robot_wave_large` 用于肩肘腕配合的举高挥手。完整说明见 [WAVE_ACTIONS.md](WAVE_ACTIONS.md)。
