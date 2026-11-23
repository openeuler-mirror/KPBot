---
name: cubesandbox-monitor-connection-reuse
description: 优化 CubeSandbox 中 monitor_vm 启动时重复连接 Guest agent 的开销。当创建流程已为当前实例安装有效的 ttrpc 连接，且跟踪显示健康监控再次执行 vsock CONNECT 时使用。
---

# CubeSandbox monitor 连接复用

monitor 启动时对当前实例已安装的 ttrpc 连接做一次有界探测。探测成功后复用传输，缺少句柄或探测失败时沿用原 CONNECT 路径，重连错误传回创建或恢复调用方。

1. 沿 `connect_agent → self.conn → monitor_vm → HealthClient` 核对连接来源。按沙箱实例与恢复代关联日志，确认同一实例在主流程建连成功后，又因启动 monitor 发起 CONNECT。只有重复握手落在创建响应前或造成可观测争用时，才进入性能实验。
2. 核对目标版本 ttrpc `Client::clone()` 的传输共享能力及关闭语义。仅从当前沙箱实例取得句柄，在锁内 clone，释放锁后再执行任何健康 RPC。旧实例退出、传输更换或恢复失败时，检查 monitor 与句柄的所有权。
3. 先审阅 [参考补丁](references/monitor-connection-reuse.patch)，按 [实施与复现](references/runbook.md) 检查源码并构建。启动探测与周期健康检查分别验收，保持周期检查配置、退出通知和 READY 状态转换。
4. 运行包内 `monitor_vm(true)` 和旧连接失效用例。目标机用真实传输验证健康与业务 RPC 确实重叠，记录 CONNECT 次数、启动探测耗时、monitor 启动耗时及 c100/n100 的整批 `total_time`。

交付目标版本与变更 diff、连接所有权核对、故障用例结果、原始基准报告和回退方式。无法验证当前实例身份或共享传输行为时，保留原建连路径。
