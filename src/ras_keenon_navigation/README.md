# 本地自主探索与原厂协议适配

本包为方案 A 的本地部署：模拟地图、前沿选择、导航、区域、命名点/路线、暂停/继续、任务与地图导出。
运行时仅使用无网络模拟器；NativeAdapter 通过注入传输进行协议测试，不创建机器人连接。
完整本地启动、测试与真机待办见工作区根目录 LOCAL_EXPLORATION.md。

无需 ROS：在工作区运行 `bash start_exploration_web.sh`，打开 http://127.0.0.1:18770/navigation 。
ROS 2：`ros2 launch ras_keenon_navigation exploration.launch.py web:=true`，导航 API 默认 18780。

原厂配置模板 config/native_profile.template.json 中未知枚举保持 null，未提供硬件启动入口。
导出是模拟栅格、YAML 与报告，未保存原厂 SLAM 轨迹/数据库，不能冒充真机验收。


2026-11-05 增加 `native_view.launch.py`：部署在机器人后只订阅原厂地图/定位/导航状态，GUI 与 LLM 不开放运动操作。运行 `ros2 launch ras_keenon_navigation native_view.launch.py`，网页端口 18770；本地探索默认 mock 保持不变。详见工作区根目录 ROBOT_NAVIGATION_DEPLOYMENT.md。

原厂只读状态新增 factory_mode（模式码、已知含义与接收年龄）；mode=0 按原厂客户端解释为导航模式，其他代码保留待实测。原厂新取得的建图协议证据见工作区根目录 FACTORY_MAPPING_PROTOCOL.md，尚未开放自主探索。
