# 2026-11-05 真机导航部署

本次将方案 A 的软件包部署到机器人 `/home/peanut/ras_keenon_ws`，以 **native-readonly 原厂只读模式**运行。它订阅真实地图、定位原始值、导航状态、TF 和激光摘要，不初始化机械臂 SDK，不启动建图或导航目标。

网页地址为 [http://192.168.3.20:18770/navigation](http://192.168.3.20:18770/navigation)。后端 18780 仅在机器人回环监听，网页通过自身后端代理读取；原有手动 8765 和相机进程保留。

## 在机器人上启动

已有本次服务时直接打开网页，不重复启动。重新启动时：

```bash
source /opt/ros/humble/setup.bash
source /home/peanut/ras_keenon_ws/install/setup.bash
ros2 launch ras_keenon_navigation native_view.launch.py
```

数据目录默认 `~/ras_keenon_ws/run/native_view/`，本次 launch 日志、PID 记录位于 `verification/robot_deployment_20261105/` 与 `run/native_view_launch.pid`。未配置开机自启。

## 当前实际能力

- 页面标记“原厂只读”，数据来自底盘 rosbridge，不生成合成房间。
- 可查看真实栅格、连接/接收状态、原始导航状态；地图是已发布的当前地图，未确认是否对应当前房间。
- 位姿原始值保留供检查。定位语义未确认时不画成正常定位的机器人，不将 0,0 当作已验证位置。
- 可以把当前已发布栅格导出为 PGM/YAML/JSON 和报告；这不是启动/保存新的原厂 SLAM 会话，不含原厂可续建状态。
- LLM 工具只开放状态查询与栅格导出。离线规则可查询；真实模型 Key 未自动配置。
- 建图、导航、路线和手动接管 API 在该服务内不开放，现有手动控制器不受影响。

只读传输只允许 subscribe/unsubscribe，禁止 advertise、publish、call_service 等操作。关闭页面或退出服务不会向底盘发布零速、取消目标或调用 SDK。

## 尚未完成的真机探索部分

后续追踪原厂网页取得 `/web_command` 的建图请求编号与 `/move_base_simple/goal` 导航入口，详见 [FACTORY_MAPPING_PROTOCOL.md](FACTORY_MAPPING_PROTOCOL.md)。这条入口可绕开手填旧 ManualLoop/map_app_start/Action 枚举，但建图与导航执行闭环、并发、定位质量、实测整机 footprint/停车距离仍未验收。目前不能宣称已经支持“点击开始后自主探索建图”。

这些是数据与执行链尚未接通的具体缺口，并非额外的 E 使能或 base_ready 要求。合法客户端请求已取得，后续需完成单次建图/导航/取消/保存与现场验证，再接入已有探索器。不能用 rosapi 的空字符串、0、false 类型示例直接试发请求。

源数据时间与主机时间不同。页面显示接收状态，保留原始时间，不伪造同步时间。静态地图可能为 latched 一次发布，不因超过固定秒数自动认定地图不存在；连接断开会标记保留旧地图。

## 回退

本次源文件先备份、再校验哈希并构建两个包。具体备份目录与文件清单记录在部署报告 JSON 中。恢复源码前先正常退出本次 launch，按清单恢复备份，再重新构建 chat/navigation；不要停止其他用户进程或覆盖 run/chat 的设置/数据库。

本机原有模拟入口 `start_exploration_web.sh` 仍默认 mock，不会自动改为连接机器人。真机运行需要显式使用 `native_view.launch.py` 或 `--backend native-readonly --rosbridge-host ...`。


## 本次部署结果

2026-11-05 北京时间 11:59 启动成功，launch PID **19681**。源码备份为 `/home/peanut/ras_keenon_ws/backups/native_navigation_20261105_035153`；本次共安装 28 个文件，后续只更新了页面只读说明，原手动 launch PID 5637 保留。

机器人 Humble 构建 chat/navigation 两包通过，机器人测试 **37 项通过**；本地相关测试 37 项、聊天回归 28 项，共 **65 项通过**，另有本地大帧 WebSocket 端到端 **7 项检查通过**。真实页面已验证地图和只读对话，原厂地图快照已导出。

实测地图为 944×864、分辨率约 0.05 m，已知栅格面积约 323.4 m²；这只是已发布栅格的已知面积，不能当作已验收的当前房间面积或覆盖率。收到激光摘要 655 束，原始定位可读取但质量未验证；不绘制为已验证机器人位置。

中途 Wi-Fi/SSH 不可达，用户确认 IP 不变，连接恢复后继续构建和启动，没有重复覆盖源文件、重启原手动控制器或打断相机采集。没有发送建图、导航、取消或底盘速度请求，也没有新初始化 SDK。当前功能是**真机只读接入**，尚不是已验收的真机自主探索。

证据目录为本地/机上 `verification/robot_deployment_20261105/`；两端所存内容不同，本地保存构建/测试副本、部署清单和运行状态，机上另有备份及服务日志。上一轮最终截图未保存；本轮恢复后截图见 native_view.png。本地模拟软件的使用方式仍见 [LOCAL_EXPLORATION.md](LOCAL_EXPLORATION.md)。


## 上一轮收尾连接中断（本轮已恢复）

构建和运行成功后，收尾检查再次遇到 SSH banner 超时及 `No route to host`；本机到 192.168.3.20 的邻居解析为 FAILED，网页也无法访问。最后成功的 API 状态见 `verification/robot_deployment_20261105/final_state.json`，它是历史采样，不能据此保证服务此刻在线。当时最后文档补充同步与全文件复核未执行，截图未保存；当时的检查报告另保留为 validation_before_recovery.json，本轮恢复结果见下节。

网络恢复后先检查现有进程，避免重复启动：

```bash
ssh peanut@192.168.3.20
cd /home/peanut/ras_keenon_ws
cat run/native_view_launch.pid
ps -p "$(cat run/native_view_launch.pid)" -o pid,args
curl --fail --max-time 5 http://127.0.0.1:18780/health
curl --fail --max-time 5 http://127.0.0.1:18770/api/navigation/state
tail -n 60 verification/robot_deployment_20261105/native_view.log
```

若现有 launch 确已退出，再使用上文启动命令；无需重新安装软件，也不要为了本服务重启手动控制或相机。SSH 不可达本身不能证明服务崩溃。


## 本轮继续：连接恢复与补丁验收

2026-11-05 北京时间 12:36，服务以新 launch PID **11526** 启动。机器人在本轮连接前已重新启动，旧 PID 19681 不再有效；磁盘部署文件完整，无需重装。先恢复服务，再更新原厂模式订阅并重启本次只读 launch；原有手动控制 launch PID 5289 保留。

新增 `/robot_state` 只读订阅，网页显示“原厂：导航模式（0）”；状态 API 和 LLM 查询返回 factory_mode 的代码、说明、接收年龄。其他模式代码保留原值，不猜测对应的建图状态。38 项相关测试在本地、机器人各通过一次；Humble 两包重新构建通过，JavaScript 语法检查通过。

进一步追踪原厂 AboutModal 资源，已取得 `/web_command` 的导航/Gmapping/Cartographer/继续建图请求，以及原厂 simple-goal 导航入口。详见 [FACTORY_MAPPING_PROTOCOL.md](FACTORY_MAPPING_PROTOCOL.md)。这次只调用 rosapi 类型自省和订阅反馈，没有调用这些控制服务或发送目标。

地图显示、只读对话回执及当前模式已在真实网页验收，截图保存在 `verification/robot_deployment_20261105/native_view.png`。最新运行结果见 runtime_final.json，之前的 runtime.json/runtime_recovered.json 是历史状态。最终同步文件清单及检查见 deployment.json/final_validation.json。未配置开机自启；机器人重启后按上文命令启动。
