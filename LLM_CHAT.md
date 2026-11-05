# LLM 对话网页

2026-11-03 新增 `ras_keenon_chat`，提供对话框、持久化历史记录、LLM 回执及机器人动作回执。

完整配置与使用方式见 [对话网页帮助文档](src/ras_keenon_chat/README.md)。

页面顶部现有电量与供电状态卡片，直接读取底层控制器，不需要 API Key 或动作服务。当前数据与适配器连接状态的确认方法见 [BATTERY_UI.md](BATTERY_UI.md)。

已有控制器（8765）与动作服务（8766）运行时，只启动网页：

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
ros2 launch ras_keenon_chat chat.launch.py
```

浏览器访问 **http://192.168.3.20:8770**，在“模型设置”中填写 OpenAI 兼容 API 的 Base URL、模型名和 API Key。动作服务为 `http://127.0.0.1:8766`，可复用已有 `ras_keenon_demos`。

三个服务的连接顺序为：聊天网页 **8770 → 动作服务 8766 → 底层控制器 8765 → SDK / 底盘**。只启动手动控制器和聊天网页时，仍然缺少动作服务。

| 当前已运行的服务 | 聊天启动命令 |
|---|---|
| 都未启动 | `ros2 launch ras_keenon_chat chat.launch.py start_actions:=true backend:=hardware` |
| 已有手动控制器 8765，没有动作服务 8766 | `ros2 launch ras_keenon_chat chat.launch.py start_actions:=true start_controller:=false` |
| 控制器 8765、动作服务 8766 均已运行 | `ros2 launch ras_keenon_chat chat.launch.py` |

已有手动控制器和聊天网页，只缺动作服务时，在另一终端补启动：

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
ros2 launch ras_keenon_demos demos.launch.py start_controller:=false \
  controller_url:=http://127.0.0.1:8765
```

默认 `chat.launch.py` 的 `start_actions` 为 false，所以它不会自动补动作服务。已经有 8766 服务时使用表格第三行，避免重复启动。

只读检查三个服务：

```bash
curl -sS http://127.0.0.1:8765/readings
curl -sS http://127.0.0.1:8766/health
curl -sS http://127.0.0.1:8770/api/health
```

遇到 `Connection refused` 应先确认具体 URL：8766 未监听表示动作服务未运行，不能直接推断 8765 控制器或机器人关节离线。

先体验不调用模型、不驱动机器人的演示：

```bash
ros2 launch ras_keenon_chat chat.launch.py demo:=true
```

API Key 只在后端内存保存。真实模型连接需要用户提供实际 API 配置；验证使用本地假 API 和 mock，未执行真实机器人动作。

操作电脑也可直接启动，无需安装 ROS：

```bash
cd /home/roko/文档/ChatGPT/Keenon
PYTHONPATH=ras_keenon_ws/src/ras_keenon_chat:ras_keenon_ws/src/ras_keenon_demos \
  python3 -m ras_keenon_chat.server --port 8770 \
  --action-url http://192.168.3.20:8766 \
  --controller-url http://192.168.3.20:8765
```

此时浏览器打开 http://127.0.0.1:8770 。只体验界面可追加 `--demo`。真实 API 配置在网页“模型设置”中填写；动作服务暂不可达时，普通聊天仍可运行，动作连接错误会显示在回执中。

本地一键启动：`bash ras_keenon_ws/start_chat_web.sh`，先体验演示可加 `--demo`。首次在机器人使用 ROS launch 前，请先构建 `ras_keenon_chat` 新包。
