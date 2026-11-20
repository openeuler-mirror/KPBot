# AgentENV 证据与验证约定

## 资源定位

在安装后的专项 Skill 中，本文件位于 `references/validation.md`，共享采集脚本
位于 `scripts/collect_environment.sh`。直接读取仓库中的专项 Skill 时，从其目录
向上两层进入 `kptools-agentenv-tuning/`，使用 `../../references/validation.md` 和
`../../scripts/collect_environment.sh`。命令中的路径始终按实际 Skill 目录解析。

## 证据分层

| 层级 | 可得结论 | 不能据此宣称 |
|---|---|---|
| 安装、文档与脚本检查 | Skill 可发现、资源可读、脚本正常退出或降级 | ARM 功能或性能通过 |
| ARM 环境采集 | OS/架构、可见配置、节点视角、拓扑事实 | 补丁生效、Host 权限或业务收益 |
| 功能验证 | ublk I/O、沙箱恢复、锁等待唤醒等实际功能通过 | 性能提高 |
| 业务 A/B | 在明确指标、规格与负载下的实测差异 | 对其他平台/负载的普遍收益 |

采集结果需匹配当前节点、运行内核和测试轮次。角色声明不能代替部署拓扑；
补丁名、文件中存在符号或内核版本相同，也不能单独证明运行时路径生效。
缺少证据时保留 unknown；Linux 非 ARM 环境不进入这组 ARM 性能策略。

## 现场证据交叉检查

- 使用 hostname、boot_id、运行内核、采集时间及 PID/stat 关联当前节点与实例。
  `/proc/PID/exe` 可能因进程能力或访问策略不可读；CPU/cgroup 状态可单独保留，
  但身份未知时不能把其他进程或历史仓库当作当前目标。
- 分开记录运行内核配置、已加载驱动和磁盘上可安装模块。外置/回移模块可以在
  `CONFIG_BLK_DEV_UBLK` 未开启的内核上提供设备；对照 loaded srcversion、
  `modinfo` 的路径/vermagic/srcversion 和构建来源，不只依据配置决定重编。
- ublk 设备可见、当前用户可读写和真实 ioctl/I/O 成功是不同证据层级。
  模块元数据缺失时标记 unknown，不能自动改判为功能不可用。
- topology 中缺失字段保留 unknown。cluster ID 不是跨 socket 的全局广播域标识。
  cgroup 必须按 v1/v2、实际挂载根和目标 membership 解释，不能套用固定目录。
- 节点 PSI 和 perf 安全参数只是诊断输入，不证明目标调度瓶颈或已获得采样权限。
  发现 sudo/capability runner 时先列出所需权限，不因脚本已有而自动执行。

## 建立基线

1. 明确测试的是沙箱创建、恢复、存储 I/O 还是业务吞吐，记录计时起止点。
2. 固定 Host/Guest vCPU、内存、cpuset、并发数、镜像、存储后端及背景负载。
3. 保存实际运行内核、配置摘要、源码/补丁标识和 Hypervisor 版本。
4. 先做功能 smoke test，再按同一 warmup 和采样时长运行基线。
5. 按项目要求确定预热、样本预算与重复轮次，记录原始结果及离散程度；
   测量不足或噪声大时调整方案，不预设固定次数。

没有真实 AgentENV 测试命令时，只完成诊断与实施计划，不用 CPU 自旋、
普通磁盘吞吐或任意 microbenchmark 替代沙箱恢复收益。
机制相关微基准可以辅助分析，但结果只覆盖该负载，不能改称未执行的业务收益。
通用实施和消融步骤见 `optimization-process.md`。

## 单项对照

- 一次改变一个可归因的变量。Pvspinlock、HZ、TLBI 不合并为一次内核变更。
- 内核构建的工具链、其他配置和补丁固定；保留配置 diff 与构建产物身份。
- 重启前确认旧内核/启动项可恢复；重启后重新采集环境并检查功能。
- HZ 轮次不同时修改抢占模型、NO_HZ、CPU 绑定或存储配置。
- TLBI 若需要同时更改 cpuset 和内核机制，尽量分成“原始布局→仅绑定→绑定+机制”
  三组。无法拆分时标记 `attribution_method=confounded`。
- Pvspinlock 观察锁等待与唤醒、物理 CPU 利用率、后台负载和业务指标。
- HZ 观察调度等待、恢复 P50/P95/P99、吞吐及 tick/CPU 开销。
- TLBI 观察正确性、页表失效路径开销及业务指标；单独的 TLB miss 计数不能证明
  广播范围已缩小。

吞吐收益为 `(after - before) / before * 100%`；延迟降低为
`(before - after) / before * 100%`。记录指标单位和方向，before 为零时不计算。
报告每轮相对上一有效配置的收益，以及最终相对初始基线的收益，不能累加百分比。
功能失败或明确回退优先恢复；差异落在测量噪声内时标记 inconclusive。

## Skill 迭代对照

改动前保留原包，在同一 ARM 节点对原版和候选版运行相同回归检查并保存日志。
检查新增证据的内容和 unknown/权限降级行为，不只检查文件是否存在。
针对本轮发现的场景扩充评估提示词，同时保留原有负面案例。

区分三类结果：采集器回归、模型独立评估、真实业务 A/B。某类未运行就明确标记；
新要求的通过率或采集耗时不是优化收益，不能填进业务 `gain_pct`。
无独立模型运行时，不将人工顺序检查标成盲评或模型成功率。

## 结果契约

每个专项输出 JSON，附可读结论。以下是缺少实现证据时的示例：

```json
{
  "skill": "agentenv-pvspinlock",
  "status": "blocked",
  "validation_level": "environment",
  "target": {
    "node_role": "guest",
    "kernel": "observed-kernel-release"
  },
  "findings": ["Guest 环境已采集，Host 的 Pvspinlock 支持尚未确认"],
  "evidence_paths": ["/absolute/path/to/evidence/environment.txt"],
  "candidate_actions": [],
  "missing_inputs": ["匹配的 Guest/Host 补丁来源与运行时使能证据"],
  "before_metrics": null,
  "after_metrics": null,
  "gain_pct": null,
  "attribution_method": "not_measured",
  "rollback_result": "not_needed"
}
```

- `status`：`analyzed` / `blocked` / `not_applicable` / `validated` / `inconclusive` / `reverted`。
- `validation_level`：`static` / `environment` / `functional` / `performance`。
- `candidate_actions` 每项记录 `id`、目标层级、当前证据、实施步骤、验证步骤、回退步骤
  和所需权限。尚未落实的命令标记待补齐，不得执行占位符。
- `validated` 要求对应验证层级的真实日志；功能使能的 `gain_pct` 为 null。
- 性能结果包含指标名、方向、单位、各次测量值、聚合方式、实际命令和原始日志路径。
- 按实际场景记录负载、样本含义、预热/有效样本数和对照组。
  如有冷热分层，明确处理规则；组均值不当作单 VM 尾延迟。
- 只填写当前测试产生的 `before_metrics`、`after_metrics` 和 `gain_pct`。
  多种负载或指标的收益不能直接合并。
- 未实施、未实测或归因混杂时，不填写单项性能收益。
