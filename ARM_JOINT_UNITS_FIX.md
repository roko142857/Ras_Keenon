# 机械臂调度失败：角度单位修复

日期：2026-11-03。依据用户提供的 `arm_status` 输出，以及对机器人当前服务和固定 SDK 的只读检查。

## 已确认的原因

双臂均在线并持续反馈，控制器、动作服务和聊天服务也在运行。用户反馈中两侧七轴的 `feedback_fresh` 均为 true，反馈年龄约 63–67 ms；`body_error` 为空，底盘输出为零。电机供电状态和故障码没有独立查询，`null` 表示未知，不能解释为断电或无故障。

问题出在软件的角度单位边界：这个固定版本的 `getJointAngle_r/l/w()` 返回**度**，而 `moveJ_r/l_block()`、`moveJ_w()` 的目标参数使用**弧度**。之前桥接程序直接透传了 getter 数组，动作规划器却将其视为弧度并检查 ±3.2 rad。

例如右臂第三轴 `176.815338135` 实际约为 **176.82° = 3.086 rad**，此前被当成了 176.82 rad。因此挥手、抬臂和复位会在生成计划时失败，还没进入机械臂 SDK 运动调用。`body_error` 只反映底层控制器错误，不能显示这类上层计划错误；原诊断命令也漏查了单位，所以出现 `findings: []`。

已在实际运行中的动作服务调用只生成计划的 `POST /plan`，没有发送运动命令。`wave` 和 `reset` 均复现 HTTP 400：

```text
right contains missing/nonfinite/out-of-range joint readings
```

这确认单位问题会直接阻断当前调度；不代表已排除所有电机或机械问题。

## 修复内容与接口单位

- 在当前原生桥接 `ras_keenon_command_native` 和旧兼容桥接 `ras_keenon_native` 的读数边界统一执行 `degree × π / 180`。
- SDK 运动目标继续使用弧度，避免二次转换；头部 `yaw_deg`、`pitch_deg` 继续使用度。
- 旧兼容控制中的关节增量比较也使用转换后的弧度读数。
- 当前桥接保留 `right_deg`、`left_deg`、`waist_deg` 原始度数，并增加明确的单位字段。
- 诊断命令增加单位标记和异常范围检查，能识别仍在运行的旧桥接程序。
- 假 SDK 改为模拟真实的“读度、写弧度”，补充用户这份实际姿态的回归测试；没有放宽关节范围来绕过单位错误。

| 反馈字段 | 修复后的单位 |
|---|---|
| `body.right`、`body.left` | 弧度，供动作规划使用 |
| `body.waist` | 弧度 |
| `body.right_deg`、`body.left_deg`、`body.waist_deg` | SDK 原始度数，供诊断查看 |
| `body.arm_angle_unit`、`body.waist_angle_unit` | `rad` |
| `body.sdk_feedback_angle_unit` | `deg` |
| `body.yaw_deg`、`body.pitch_deg` | 度 |

SDK 的固定归档 SHA256 为 `77cdc6c9e4e37dbd0d3738bdfe7cdca615e8bb751c1c82d5c4cb22db29dfb6b1`。根据库中的 `getJointAngle_r()` 和 `moveJ_r_block()` 实现，后者将弧度目标乘以 180/π 后再与 getter 的度数比较；不是根据数值大小猜测单位。

## 如何加载修复

源文件和新二进制已安装到机器人。编译使用独立的 `build_arm_units` / `install_arm_units` 目录，之后原子替换默认安装所指向的二进制。旧运行进程仍持有原来的程序映像，因此**必须重启硬件控制器后才会输出转换后的弧度**。本次没有中断用户控制进程，没有初始化第二个 SDK，没有执行真实运动。

在原先运行 `demos.launch.py` 的终端按 Ctrl+C，待旧控制器和 SDK 进程退出后，重新启动：

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
ros2 launch ras_keenon_demos demos.launch.py backend:=hardware
```

不要在旧硬件控制器仍运行时再启动一份。现有聊天网页服务可以继续运行。

另一终端加载环境后先做只读验证：

```bash
ros2 run ras_keenon_commands arm_status --local-system
ros2 run ras_keenon_demos wave --dry-run
ros2 run ras_keenon_demos reset --dry-run
```

确认 `arm_angle_unit` 为 `rad`，原先约 176.82 的读数变为约 3.086，同时 `right_deg` 中仍保留约 176.82；`--dry-run` 应能生成动作计划。当前姿态如果变化，角度数值会相应变化。

## 验证记录

- 本地控制/原生假 SDK/诊断测试：50 项通过。
- 动作测试：21 项通过，包括用用户提供的姿态复现失败，转换后生成挥手和复位计划。
- 机器人：两个原生包独立编译通过。
- 运行中的真实服务：通过只读 `readings` 与只生成计划的接口确认原故障；尚未重启用户进程，因此未将旧进程反馈宣称为修复后的运行结果。
- 未进行真实机械臂运动或真实云模型调用。

原始反馈和证据位于 `verification/arm_user_feedback_20261103.json`、`arm_units_readonly_20261103.json`、`arm_plan_before_units_fix_20261103.json`、`arm_units_deployment_20261103.json`。部署前备份位于机器人 `/home/peanut/ras_keenon_ws/backups/arm_units_20261103_081039`。
