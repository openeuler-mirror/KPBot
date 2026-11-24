# 微服务域

线上服务进程的性能优化。本域按"改代码 / 改配置"分层：**改实现**的热路径优化属于本域；
**改配置/改 OS 参数/换库**的应用调优属于 app-tuner 侧的同名域（另一个 PR 迁入）。

## 成员

| Skill | 职责 |
|---|---|
| [`go-pprof-analysis`](go-pprof-analysis/SKILL.md) | Go 服务 pprof 分析：CPU / heap / goroutine / block / mutex 五类 profile，goroutine 泄漏、`go tool trace`、服务侧瓶颈定位 |

本域后续迁入（逐个独立 PR）：

- code-optimizer 侧：`go-gc-bottleneck-diagnosis`（Go GC 瓶颈诊断：gctrace + GOMEMLIMIT 证伪，堆峰值/next-GC goal/GOGC/STW 四判据，注入生效验证与透传坑）、`dpdk-optimization`（数据面转发热路径代码的五阶段优化）
- app-tuner 侧（`Plugins/app-tuner/skills/microservice/`）：`nginx-optimization`（L1 环境 / L2 配置 / L3 鲲鹏加速库分层调优）、
  `kptools-dpdk-tuning`（DPDK 应用诊断 + 顾问双模式）

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/code-optimizer/skills/microservice/**`
