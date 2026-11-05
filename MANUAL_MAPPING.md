# 手动控制网页：原厂 Gmapping 建图

2026-11-05 实施。网页入口仍为 http://192.168.3.20:8765 。本功能由操作者驾驶机器人采集地图，当前不提供真机自主探索。

## 启动

已运行新版控制器时直接刷新网页。确认服务未运行后，在机器人终端执行：

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
ros2 launch ras_keenon_control manual.launch.py backend:=hardware
```

该入口同时启动独立建图后台，默认回环地址 `127.0.0.1:18781`。启动进程只连接原厂、订阅状态/图像并查询地图列表，不启动 SLAM，不发送导航目标。实际开始由页面按钮触发。

启动入口会复用同一回环地址上后端一致的建图服务；也可用 `start_mapping:=false` 明确复用已有后台。端口可用 `mapping_port:=...`，代理可用 `mapping_url:=http://127.0.0.1:...`。两边后端应一致，mock 控制器不会向 hardware 建图后台提交操作。

## 页面操作

1. 确认页面为“真实硬件模式”和“原厂 Gmapping”，顶部原厂模式为导航（0）。地图此时显示已有导航图。
2. 填写地图名称、楼栋、新楼层编号与楼层说明。已有楼层会列出，选择未占用编号；首版不覆盖旧楼层、不修改默认楼层。编号范围沿用原厂规则和 int16：-9..32767、非零、非 200 的倍数。
3. 点击“开始 Gmapping 建图”。回执先显示请求是否受理；原厂接受后等待本次 SLAM 图像，确认接收后显示“手动建图中”。地图名称和新楼层等元数据随会话保留。
4. 用 W/S 前后、A/D 转向或页面按住按钮驾驶。H/L/I/K、R/F 与参数命令继续使用；无需 E/base_ready，速度配置和原有控制周期保留。输入表单时暂停快捷键。
5. 松键或空格停止驾驶，SLAM 继续采集。完成后点击“结束并保存”：控制器清除当前输入、停止当前动作，后台冻结地图、创建原厂新图并回读核验，随后按原厂流程切换新楼层并返回导航模式。
6. 只有图像、摘要和楼层元数据回读一致，且模式/楼层反馈完成，才显示“原厂地图已保存”。“请求成功”或“保存结果待确认”不等于保存闭环完成。
7. “取消本次建图”停止当前动作并恢复进入前楼层/导航模式，不删除已有数据库地图；如果本次地图已经写入原厂数据库，也不会擅自删除这条记录。

原厂初次建图、实际驾驶、保存和取消回退的现场效果仍需由操作者首次使用验收。本次软件部署不会自动点击任何真实建图按钮。

## 图源与导出

- 建图使用 `/gmapping_map/to_web/compressed`，导航使用 `/map/to_web/compressed`；原厂未推送导航 PNG 时可用原始 `/map` 生成仅供查看的导航画面。旧 `/map` 不作为本次 SLAM 图像用于保存。
- 原厂 PNG 原始字节保留，原厂 MD5 按原始 PNG 计算；不经浏览器缩放或重新编码再入库，以免破坏其附带数据。
- 保存期间及保存后显示冻结版本，明确标为“冻结快照”，不会用旧导航图替换保存内容。
- “导出本次快照”提供 PNG、report.json 和 sha256.json 下载；这不是 PGM/YAML 米制地图或原生可续建 SLAM 状态。
- Cartographer、pbstream 保存/恢复、继续建图、自动导航探索尚未开放。

## 回执、重连和重复请求

建图操作独立于手动周期执行。开始/保存/取消返回操作 ID，后续状态和原厂返回码显示在回执中。保存超时后保留已提交标记，重试先查数据库；无法确认时保持待确认，不重复插入。

页面刷新恢复会话、表单和回执，不重发建图命令。后台重启保留历史会话并标为待确认，查询原厂实际状态；不会自动开始、取消或保存建图。断线时仍按现有按键心跳规则停车，页面保留来源与接收时间；重连不自动驾驶。

## 数据和接口

默认数据目录：`~/ras_keenon_ws/run/mapping/hardware/`（模拟为 mock 子目录）。

- `state.json`：最新会话和操作记录。
- `sessions/<id>/before_maps.json`：启动前当前楼层的原厂地图记录备份，**不含原生 pbstream**。
- `sessions/<id>/session.json`、`frozen.png`：会话和待保存/已保存图像。
- `exports/<id>/`：PNG 快照、报告、摘要。

手动页同源接口：GET `/mapping/state`、`/mapping/maps`、`/mapping/map.png`；POST `/mapping/start`、`/mapping/finish`、`/mapping/cancel`、`/mapping/export`、`/mapping/refresh`。写操作携带 `request_id`，后续操作还携带 `session_id`；它们用于请求去重与会话关联，不是手动使能按钮。

只读检查：

```bash
curl --fail --max-time 5 http://127.0.0.1:8765/mapping/state | python3 -m json.tool
curl --fail --max-time 5 http://127.0.0.1:8765/mapping/maps | python3 -m json.tool
```

## 验证范围

本地已完成原厂模拟流程和大帧 WebSocket 保存/回读测试，并使用 ROS mock 控制器验证持续按键、松键、停止旧会话以及保存前停止底盘。浏览器验证表单焦点、创建、结束保存、导出、刷新恢复和取消。

真机部署阶段只读取状态、原厂现有地图/列表并检查页面；真实 Gmapping 启动、驾驶建图和新图写入不在自动部署验收中。相关源码备份、测试、构建和运行证据保存在 `verification/manual_mapping_20261105/`，实际部署结果以该目录中的运行报告为准。


## 本次部署结果

2026-11-05 已部署至 `/home/peanut/ras_keenon_ws`，网页 http://192.168.3.20:8765 。手动 launch PID 23767，控制器 PID 23791、SDK 桥接 PID 23816；独立建图后台 PID 25340，PID 文件 `run/mapping_server.pid`，手动 launch PID 文件 `run/manual_mapping_launch.pid`。这些 PID 是部署时记录，重启后应查询实际进程。

源码备份：`/home/peanut/ras_keenon_ws/backups/manual_mapping_20261105_052243`，含本轮后续订阅修正备份。启动日志为 `verification/manual_mapping_20261105/hardware_runtime.log` 与 `mapping_runtime.log`。未设置开机自启。

本地 54 项导航/建图测试、22 项直接控制回归通过，机器人 54 项导航/建图测试通过；另对最后的明确消息类型订阅重复进行本地/机器人大帧 WebSocket 检查，各 1 项通过。两端三个 ROS 包构建成功，已有建图后台的启动复用检查通过。约 900 KB PNG 保存和回读测试使用本机原厂模拟器，未连接真实执行接口。

真机只读核对了已有 4000×4000 原厂 PNG 和摘要，以及页面真实 944×864 导航地图、楼层列表、电量和反馈。真实页面与完整按钮截图为 `verification/manual_mapping_20261105/hardware_panel.png` 和 `hardware_panel_full.png`。当前展示的是既有地图，未据此宣称新建图已成功。

本次未通过自动化提交原厂 SLAM 启动/取消、地图插入或导航/驾驶请求。空闲更新手动控制器包含原有停车与 SDK 重新初始化；最后只重载建图后台，保留手动/SDK 进程。首次收尾曾采样到 S 输入和 -0.1 m/s 输出，指令来源未归因，本次验收脚本未发送该请求；后续多次采样已为空键、零输出。原 18770/18780 只读地图服务保留。

真实 Gmapping 启动、新图更新、原厂保存和取消回退仍需由操作者首次点击和驾驶验收；软件与模拟通过不等于真实闭环已通过。遇到原厂拒绝或回读未确认时，页面保留真实错误和待确认状态。

## 回退

先结束或取消自己开启的建图会话，再在空闲状态退出本次手动 launch。停止独立建图服务前核对 `run/mapping_server.pid` 对应的命令行，避免停止其他程序。服务退出本身不会向原厂发送取消建图请求。

按 `verification/manual_mapping_20261105/deployment.json` 清单恢复备份中的旧源文件，再重建 commands/navigation/control 三个包。原控制配置、原厂数据库、旧地图、聊天数据没有被本次安装覆盖。
