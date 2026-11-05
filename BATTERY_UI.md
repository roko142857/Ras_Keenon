# 网页电量与供电状态

2026-11-03 更新。本地源码与机器人 `/home/peanut/ras_keenon_ws` 已同步，真实硬件控制器已加载本次更新。

## 网页使用

- 手动控制：[http://192.168.3.20:8765](http://192.168.3.20:8765)。
- 对话控制：[http://192.168.3.20:8770](http://192.168.3.20:8770)。

刷新已打开的页面后，顶部卡片显示电量、更新时间、外接电源和充电状态。页面可见时每 **5 秒**查询，也可点击“刷新电量”；刷新按钮读取最新缓存，不向电池发送测量或充电命令。底盘电量实际发布间隔约 10–11 秒，因此点击后数值不一定立即改变。悬停供电状态可查看原始充电状态码及解释。

状态仅用于显示，不增加使能、base_ready 或速度限制，不修改 2 ms 输入、20 ms 底盘、50 ms 头部的原调度配置。无需配置 LLM API Key，不消耗模型 Token，也不依赖 8766 动作服务。当前没有新增自然语言查询电量的 LLM 工具。

## 当前读数如何理解

实机验收时两个网页收到相同数据：

| 字段 | 当前读数 | 含义与显示 |
|---|---|---|
| `/battery_state.percentage` | `0.0` | ROS 标准比例值，转为 0%；显示“底盘上报 0%” |
| `/battery_state.present` | `false` | 电池存在状态未确认；不能据此断言电池已取下或耗尽 |
| 电压、电流、容量等 | 均为 `0` | 当前发布端没有提供可用的独立佐证 |
| `/battery_state.power_supply_status` | `0` | 标准枚举 UNKNOWN；显示“充电：未知” |
| `/charge_state_fromSTM32.state` | `49` | 厂商充电原始状态，枚举尚未确认；只展示原码，不推断适配器连接 |
| `power.adapter_connected` | `null` | 未取得已验证的适配器直连信号；显示“外接电源：未确认” |

**目前未发现经过验证、能够直接表示“电源适配器已插入”的接口字段。** 检查范围包括已发布 ROS 话题、相关 ROS 参数、可读厂商网页代码、现有项目文档和主机 Linux 电源设备。主机 `/sys/class/power_supply` 为空，UPower 仅枚举聚合 DisplayDevice，没有可用的 AC `online` 设备。这不等于断言底层硬件或私有协议绝对没有此信号。

已发现 `/charge_status`、`/charge_report_toUI` 等相关话题，但短时只读订阅未收到可供确认的样本。`state=49` 的语义没有协议依据；旧记录中接线变化前后也出现过相同值，因此不将 49、ASCII 字符“1”或“充电中”直接映射为适配器已连接。即使未来能确认“充电中”，也仍需区分充电桩与直接接适配器。

## 数据链路与接口

```text
ROS 1 底盘 rosbridge 192.168.64.20:9091
  ├─ /battery_state              sensor_msgs/BatteryState
  └─ /charge_state_fromSTM32     keenon_charge_msgs/ChargeFB
                 ↓ 独立只读订阅线程及缓存
控制器 8765 /battery → 手动网页
                 └→ 聊天后端 8770 /api/battery → 对话网页
```

订阅线程不发送 `publish`、控制服务、充电任务或底盘运动命令。读取电量不初始化额外的机械臂 SDK。使用硬件手动控制器本身仍会按原有流程初始化 SDK。

机器人终端只读查询：

```bash
curl -sS http://127.0.0.1:8765/battery | python3 -m json.tool
curl -sS http://127.0.0.1:8770/api/battery | python3 -m json.tool
```

`GET /readings` 中也包含 `battery` 字段。供电回执位于 `battery.power`，例如：

```json
{
  "adapter_connected": null,
  "adapter_status": "unconfirmed",
  "charging_status": "unknown",
  "battery_supply_status": 0,
  "raw_charge_state": 49,
  "raw_charge_fresh": true,
  "raw_charge_mapping": "unverified"
}
```

外接电源三态接口已预留：`true` 显示已连接，`false` 显示未连接，`null` 显示未确认。当前后端始终返回 `null`，不会凭充电码猜测连接。原始充电码独立计时，充电心跳不会使过期电量重新变为有效。

标准 `BatteryState.power_supply_status` 枚举为：0 未知、1 充电中、2 放电中、3 未充电、4 已满电。当前仅在电池读数新鲜且 `present=true` 时使用其确定值；过期或未确认时显示未知。此枚举不包含“适配器直连”分类。

## 无数据、断线与演示

- 尚无有效读数、连接失败或字段无效时显示“—”，不会补成 0%。
- 已有读数在断线后保留为“上次上报”，并标记数据过期；超过 30 秒未更新也会过期。
- `backend:=mock` 或聊天 `demo:=true` 明确显示模拟状态，不查询真实电池，不伪造剩余电量。
- 更新时间采用本机接收时刻。底盘 ROS 时间戳未与主机同步，不能直接作为网页更新时间。

## 启动与部署

已有控制器与聊天服务时只需刷新网页，无需再次启动同一服务。本次已将缺失服务按原配置启动，并在空闲时重载由本次启动的手动控制器；当前手动 launch PID 为 `32121`，聊天 launch PID 为 `24763`。保留既有动作服务，没有设置开机自启。

重启或服务退出后，可在机器人两个终端分别执行：

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
ros2 launch ras_keenon_control manual.launch.py backend:=hardware
```

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
ros2 launch ras_keenon_chat chat.launch.py
```

正常退出旧控制器后再启动，避免重复占用 SDK。聊天服务不需为静态网页更新而重启，重启会清除其仅保存在内存的 API Key。

聊天后端与控制器不在同一台主机时，需要显式指定控制器地址：

```bash
ros2 launch ras_keenon_chat chat.launch.py \
  controller_url:=http://192.168.3.20:8765
```

不使用 ROS 的本地启动脚本 `start_chat_web.sh` 已默认使用机器人 8765；可通过 `--controller-url` 覆盖。直接运行 Python 时也可添加同名参数。未显式配置时，ROS launch 按 `controller_port` 推导本机地址。

首次从源码安装新增网页资源时，需要构建：

```bash
cd /home/peanut/ras_keenon_ws
source /opt/ros/humble/setup.bash
colcon build --symlink-install --executor sequential \
  --packages-select ras_keenon_commands ras_keenon_chat
source install/setup.bash
```

本次构建已完成。更新前机器人备份为 `backups/battery_ui_20261103_094506` 和 `backups/power_status_20261103_100343`，均有 `COLCON_IGNORE`。当前控制器日志为 `verification/power_manual_runtime_20261103.log`，聊天日志为 `verification/battery_chat_runtime_20261103.log`。

## 后续确认适配器信号

先取得厂商 `keenon_charge_msgs/ChargeFB.state` 协议枚举，或确认是否存在 adapter/AC 输入专用位。随后由现场人员在机器人静止时切换供电条件，分别记录电池供电、充电桩充电、直接连接适配器及满电状态下的原始回执，确认该位能区分这些场景。仅有一个 49 样本不能完成标定。

确认来源和语义后，在 `ras_keenon_commands/battery.py` 中接入适配器专用信号并维护有效期，填充 `adapter_connected`；两个网页现有组件即可显示已连接或未连接。协议尚未确认时继续保留 `null`。

## 验证与证据

本地与机器人电量订阅单元测试各 **8 项通过**；聊天测试 **28 项通过**；本机 ROS 2 Jazzy 与机器人 ROS 2 Humble 的 mock 集成各 **14 项通过**。测试覆盖未知/0% 区分、有效期、断线、原始 49 不误判、标准充电不误判适配器、只读订阅及网页代理。

真实硬件两端查询已确认返回同一缓存及原始充电码 49。浏览器已验证手动页真实数据、聊天页模拟数据、刷新按钮及状态说明。没有调用真实模型或提交运动/预设动作；硬件控制器启动和重载包含原有 SDK 初始化及正常停机流程。

证据在 `verification/`：`power_live_web_validation_20261103.json`、`power_ros_master_audit_20261103.json`、`power_status_deployment_20261103.json`、`battery_*tests_20261103.log`、`battery_*ros*20261103.*`。真实手动页截图为 `battery_power_manual_live_20261103.png`，聊天截图 `battery_power_chat_preview_20261103.png` 使用本地模拟数据，不能当作额外真机测量。
