# 部署与验收记录

日期：2026-10-30（北京时间）。

- 真机工作空间：`/home/peanut/ras_keenon_ws`。
- 本地源码副本：本文件所在目录。
- 系统：Ubuntu 22.04 / aarch64 / ROS 2 Humble / 系统 Python 3.10。
- `ras_keenon_interfaces`、`ras_keenon_native`、`ras_keenon_control` 已在机器人上成功构建。
- 原生桥接链接既有 `/home/peanut/homebody/vendor/ql_sdk`，没有替换或重新安装 SDK。
- 链接厂商 SDK 的原生桥接程序本轮未运行，未调用真实 `robot_init`；原生协议测试仅链接临时 fake SDK。
- 初版 15 项软件单测及 ROS mock 检查通过；根据后续真机使用日志增加了故障回归与修复，最新结果见 `verification/results.json` 和测试日志。
- `verification/results.json` 保存结果，其他 `.log` 保存各组输出；`source_manifest.json` 保存源码逐文件哈希。
- 默认 launch 使用 mock，不会连接底盘、SDK 或相机；默认演示模式就绪后自动使能，不自动生成动作。
- 硬件模式、前进/后退/转向、头腰与双臂微动代码已部署，但本轮不声称完成真机运动验收。
- 未安装开机自启，软件验证结束后关闭所有本次 mock 节点。

用法、按键、硬件开放参数、视觉入口和实际停止边界见 [README](README.md)。

修复版最终软件结果：25 项单测通过；ROS 接口、普通模式键盘、Ctrl+C、默认演示模式四组检查通过。硬件启动命令可简化为 `ros2 launch ras_keenon_control manual.launch.py backend:=hardware`。默认无须 E，空格只停车，Z/X 明确禁用，手动长按连续；`demo_mode:=false` 保留以前的手动使能和有限脉冲流程。真机动作未在本轮重试。
