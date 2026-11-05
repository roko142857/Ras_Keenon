# 原厂建图接口：2026-11-05 新增核查

本轮恢复 SSH 后，从底盘 192.168.64.20:8080 实际提供的网页代码继续追踪。新找到 AboutModal 资源中的建图实现，而不是只在 Home/index 中搜索。原始资源哈希、调用片段在 `verification/robot_deployment_20261105/factory_protocol_observed.json`，实际 ROS 类型和静态采样在 `factory_protocol_probe.json`。

## 已找到的调用协议

| 用途 | 原厂客户端调用 | 证据 |
| --- | --- | --- |
| 返回导航模式 | `/web_command`，`mainCmd: 0, subCmd: 0` | Home 的 goNav 实现 |
| 开始 Gmapping | 同一服务，`mainCmd: 1, subCmd: 0` | AboutModal 的 Gmapping Map 实现 |
| 开始 Cartographer | 同一服务，`mainCmd: 2, subCmd: 0` | AboutModal 的 Carto Map 实现 |
| 继续已有地图建图 | 同一服务，`mainCmd: 3, subCmd: 0` | AboutModal 的 Edit Floor 实现 |
| 地图选点导航 | 发布 `/move_base_simple/goal`，`geometry_msgs/PoseStamped`，frame=map | Home 的原厂选点发布实现 |

ROS 自省确认 `/web_command` 当前已注册，类型 `web_backend/web_command_srv`，请求为 int64 mainCmd/subCmd，响应为 bool result/int64 code。原厂网页检查 result，再把 code 当模式值；**不能据此假定请求编号必然等于返回模式码**。上述控制请求本轮均未发送。

原厂选点入口可避免手填 MoveBaseActionGoal 的厂商枚举，但没有客户端自定的 goal_id。项目要用于探索器，仍需关联底盘实际生成的 goal_id、结果、取消回执和任务所有者，不能直接替换发送话题就宣布闭环完成。原网页硬编码的旧时间戳也不能不经 TF/源时钟核对就照抄。

## 当前真实状态

本次 7 秒静态窗口收到 `/robot_state.mode=0`（原厂网页将 0 按导航模式处理）、空导航目标列表、零 `/motor_speed`。`/mapping_status` 在此窗口仍无样本；现有地图是已发布地图，并非本次新建。

全局规划器为 kplanner/KPlannerROS，局部为 TEB。原厂两套 costmap 的 footprint 配置一致，顶点为：

```json
[[0.295,0.146],[0.2,0.239],[-0.186,0.239],[-0.266,0.159],[-0.266,-0.159],[-0.186,-0.239],[0.2,-0.239],[0.295,-0.146]]
```

这约为 0.561×0.478 m 的**配置轮廓**；另有 robot_radius=0.46 参数。它们都不是本轮测量的整机含手臂外廓或停车余量。原始定位仍含非标准 covariance[5]=99.99 和旧源时钟，不能按普通协方差直接判断质量。

网页/API/LLM 状态查询新增 factory_mode，保留模式代码与接收年龄。只把已确认的 0 解释为导航模式，其余代码显示原值与待实测语义；连接中断显示最后收到状态。传输继续只允许订阅和退订。

## 继续完成方案 A 的具体顺序

1. 将本次已取得的 `/web_command` 客户端协议用于独立测试会话，记录 result/code、mode、地图源与 TF 的对应关系；原地图数据库和原生 SLAM 数据先完整备份。
2. 在现场小范围测试中验证建图期间是否仍接受导航目标、发布持续更新的栅格与可靠位姿；若互斥则方案 A 不能原样闭环。
3. 选择原厂 simple-goal 转换路径或取得 Action 枚举，建立唯一目标关联、取消/停车回读、手动抢占，实测机身外廓与停车距离。
4. 补原厂建图数据的保存/恢复事务。原网页结束扫描后向 database/config_map 插入栅格，与原生可续建状态不是同一件事。
5. 接入已有 frontier 探索器，完成限定区域、失败重选、多目标、结束保存和重载验收。

当前部署仍是 native-readonly；新的协议证据缩小了未知范围，不等于已执行或验收自主探索。没有新增 E/base_ready 或手动底盘速度门槛。
