---
name: cubesandbox-restore-runtime-reuse
description: 优化 CubeSandbox VMM 中重复构造 Tokio 多线程 runtime 的恢复成本。当同一进程多次进入 restore_device_node 且跟踪显示 runtime 构造是可观测热点时使用。
---

# CubeSandbox restore runtime 复用

为同一 VMM 进程中的重复设备恢复保留 Tokio runtime，减少重复创建 worker 和 reactor 的成本。每次恢复独立持有自己的设备与快照数据，设备分组的执行顺序保持不变。

1. 按进程启动身份统计恢复调用次数、设备组规模、原 worker 数、runtime 构造耗时和恢复尾延迟。每个进程只恢复一次时，首轮构造成本仍在，复用没有命中。
2. 核对 `DeviceManager::restore_device_node` 的 `block_on` 在可允许阻塞的同步调用边界执行。确认目标 Tokio 版本、定时器/I/O 驱动、线程预算和常驻 worker 成本。
3. 按 [参考补丁](references/restore-runtime-reuse.patch) 与 [实施与复现](references/runbook.md) 接入。同 worker 数复用同一个 runtime。切换配置时先等在途调用完成并释放旧池，再按新配置构建。线程创建失败作为恢复错误返回。每组先校验快照，返回前收回已提交的设备任务。
4. 验证冷启动、同进程重复恢复、错误后再恢复，以及受限线程预算下切换配置。分别记录预热与计量实例，清理后核对活跃实例回到基线。用实际构造次数和恢复时间归因，再测 c100/n100 整批 `total_time`。

交付调用进程、构造计数、设备顺序和错误验证、原始基准与产物回退方法。没有重复构造证据时结束诊断。
