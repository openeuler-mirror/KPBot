---
name: go-syncpool-optimization
description: 当 Go 服务 sync.Pool 创建/回收频繁、GC 后池被清空、堆内存颠簸，需要评估 GODEBUG=poolretain 与 poolprealloc 时使用。输出按池清单求和的参数计划、内存水位和候选动作。
---

# Go sync.Pool Optimization

本 skill 只负责 `sync.Pool` 的参数语义、取值决策和风险边界；补丁构建/重编译交给 `go-runtime-patch-build`，A/B 验证交给 `microservice-ab-testing`。具体业务组件的池清单和热点由对应组件 skill 提供。

## 何时触发

- 目标服务是 Go 程序，pprof 热点包含 `runtime.mallocgc`、`runtime.gcDrain`、`sync.(*Pool).Get`/`Put`。
- heap profile 呈“高分配、低驻留”：`alloc_space` 高、`inuse_space` 低，GC 次数或暂停偏高。
- 业务在编解码、序列化、协议栈等热路径大量使用 `sync.Pool`，对象在 GC 两轮后被清空，首次 `Get` 频繁走 `New`。
- 已获得 Go runtime 重编译授权，并能提供外部私有补丁。

不进入本 skill：

- 不能重编译，或拿不到 Go runtime 源码/补丁基线。
- 池对象为长生命周期大对象，或容器内存余量不足。
- 业务侧可通过减少分配、复用对象直接解决（优先走 code-optimizer）。

## 依赖与分工

- `go-runtime-patch-build`：负责补丁校验、隔离构建根目录、Go 工具链构建、服务重编译、二进制等价和回退。
- `microservice-ab-testing`：负责固定负载、单变量轮次、指标采集、正确性、判定和归档。
- `go-pprof-analysis`：负责 CPU/heap profile 采集与判读（按需调用）。

本 skill 不复制上述 skill 的实现细节；只输出参数计划和依赖调用点。

## 必读 Reference

- `references/syncpool-godebug-knobs.md` — `poolretain` / `poolprealloc` 的参数矩阵、生效方式、内存代价、交互和拒绝条件。
- `references/prerequisites.md` — Go 工具链、依赖 skill、压测和授权的缺失降级。

## 输入证据

- Go 基线与二进制身份：`go version`、`$GOROOT/VERSION`、`go version -m <binary>`、sha256。
- pprof：CPU top、heap 的 `alloc_space`/`inuse_space`、`GODEBUG=gctrace=1` 输出。
- 池清单：池数量、对象类型与大小、`New` 函数、`Get`/`Put` 比例、对象生命周期。
- 运行环境：`GOMAXPROCS`、容器 CPU limit、`GOGC`、`GOMEMLIMIT`、现有 `GODEBUG`。
- 压测基线：QPS、P50/P99、错误率、RSS、GC 次数与暂停时间。

## Workflow

### Step 0：颠簸定界

- 用 pprof 和 `gctrace` 确认分配热点落在 `sync.Pool` 相关路径。
- 用池清单确认对象是短生命周期临时对象，且 GC 后复用率明显下降。
- 排除业务算法、序列化库配置、连接池等其它解释后，才进入参数评估。

### Step 1：参数决策

- 先评估 `poolretain=1`：池对象跨 GC 存活，内存无硬上限，必须设置 RSS 阈值。
- 再评估 `poolprealloc=N`：仅 `Pool.New != nil` 生效；单个池约 `N × GOMAXPROCS × 单对象大小`，必须按池清单求和 `Σ_i(N_i × GOMAXPROCS × 单对象大小_i)` 并加安全余量，不能只看单个池。
- 每个参数单独输出一个 `candidate_action`；首轮不得同时开启两个开关。

### Step 2：交给依赖 skill 执行

- 将补丁文件、Go 基线、构建根目录计划交给 `go-runtime-patch-build`。
- 将变更命令、组件指标、正确性用例和回退命令交给 `microservice-ab-testing`。
- 本 skill 只保留参数决策、内存估算和拒绝条件。

### Step 3：判定与回退

- 正向收益且 RSS 可控：保留，记录生效参数和配置位置。
- 负收益、噪声区且内存代价不可接受，或 RSS/OOM 触发：回退。
- 回退由 `go-runtime-patch-build` 恢复基线工具链/二进制，并复核身份。

## 分支、终止与交回

- pprof 不可用但 `gctrace`/`runtime.MemStats` 可用：降级为 `status=degraded`，只输出参数建议和内存估算，标记 `verified=false`。
- 缺少 `go-runtime-patch-build` 或 `microservice-ab-testing`：`status=blocked_dependency`，交回参数建议、缺失依赖和下一跳 skill，不进入执行。
- 缺少重编译/重启授权：`status=blocked_authorization`，交回授权项和最小验证计划。
- 补丁与 Go 基线不匹配、池清单/对象大小缺失、RSS 阈值无法设定：终止本轮，交回 `reason`、`missing` 和最小补采命令。
- 任一轮 RSS/OOM 或正确性失败：立即回退，终止参数上调；交回 `rollback_result` 和停止原因。
- 正常完成：交回 `knob_plan`、`memory_watermark`、`correctness_evidence`、`candidate_actions` 和 `further_optimization_potential`。

## 参数矩阵

| 参数 | 默认 | 取值 | 作用 | 内存代价 |
|---|---|---|---|---|
| `poolretain` | 0 | 0/1 | 非 0 时 GC 阶段跳过 `sync.Pool` 清理，池对象跨 GC 存活 | 池内对象不再随 GC 释放，驻留内存上升，无硬上限 |
| `poolprealloc` | 0 | 0..私有补丁内置上限（每 P，超出由补丁截断） | 池的 per-P 本地缓存首次创建时预分配 N 个对象，需 `Pool.New != nil` | 约 `Σ(N_i × GOMAXPROCS × 单对象大小_i)`（按池清单求和，不能假设各池同构） |

详细语义、交互和验证方法见 `references/syncpool-godebug-knobs.md`。

## Candidate Action Contract

每个 `candidate_actions[]` 必须包含 `action_id`、`title`、`category`、`priority`、`change_mode`、`requires_root`、`risk`、`implementation_plan`、`validation_plan`、`rollback`、`expected_effect`、`expected_gain_metric`、`rejection_criteria`、`evidence_refs`。

- `implementation_plan` 必须引用 `go-runtime-patch-build` 作为构建/重编译执行方，不在本 skill 内写具体构建命令。
- `validation_plan` 必须引用 `microservice-ab-testing` 作为测量协议，不在本 skill 内重复 A/B 步骤。
- 每个参数一个 action，禁止把 `poolretain` 和 `poolprealloc` 合并在同一轮首次验证。

## 输出字段

- `go_baseline_version`
- `pool_inventory`
- `knob_plan`
- `memory_watermark`
- `correctness_evidence`
- `candidate_actions`
- `further_optimization_potential`

证据不足时输出 `status=degraded|blocked` 和最小补采命令，不得仅凭经验承诺收益。

## 拒绝条件

- 无法记录 Go 基线、二进制身份或补丁 hash。
- 没有可用的 `go-runtime-patch-build` 或 `microservice-ab-testing`。
- 不允许重编译工具链或业务二进制。
- 池对象为长生命周期大对象，或容器内存余量不足以承担 `poolretain` 的驻留增长。
- `poolprealloc` 预分配量按池清单求和 `Σ_i(N_i × GOMAXPROCS × 单对象大小_i)` 并加安全余量后超过内存余量。
- 业务代码存在更直接的减分配方案，且改动风险低于运行时补丁。

## 边界

- 不修改业务代码；需要改业务代码时交给 code-optimizer。
- 不调整 `GOGC`、`GOMEMLIMIT` 等 GC 策略；这些属于 application-config-optimization。
- 不定义补丁内容、不写构建/回退命令、不定义组件热点；分别由 `go-runtime-patch-build` 和组件 skill 负责。
- 缓存行争用识别：池结构或 runtime 热路径的 cache miss、跨核迁移、伪共享/真共享转 `go-cacheline-optimization`；本 skill 只做池参数。
- 不生成、不保存补丁原文；补丁由外部私有制品提供。
