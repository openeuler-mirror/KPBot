---
name: microservice-go-syncpool-optimization
description: 面向 Go 微服务中 sync.Pool 对象创建/回收频繁、GC 后池被清空导致堆内存颠簸的场景，通过给 Go runtime 打补丁新增 GODEBUG=poolretain 与 GODEBUG=poolprealloc 两个控制开关，重编译 Go 工具链和业务二进制，并按单变量 A/B 验证吞吐与内存水位，作为 kpbot-app-tuner 的子 skill 使用。
---

# Microservice Go sync.Pool Optimization

当 Go 微服务的堆内存颠簸可归因到 `sync.Pool` 对象在 GC 后被清空、复用率低，且允许修改 Go runtime 源码并重编译时使用本子 skill。本 skill 只做运行时补丁、参数选择与 A/B 验证，不修改业务代码。

## 何时触发

满足任一条件即进入本 skill：

- 目标服务是 Go 程序（`go version -m <binary>` 可确认），且 pprof 热点包含 `runtime.mallocgc`、`runtime.gcDrain`、`sync.(*Pool).Get` 或 `sync.(*Pool).Put`。
- heap profile 呈“高分配、低驻留”特征：`alloc_space` 高、`inuse_space` 低，GC 次数或暂停时间偏高。
- 业务在编解码、序列化、协议栈等热路径大量使用 `sync.Pool` 缓存临时对象，且对象在 GC 两轮后被清空，首次 `Get` 频繁走 `New`。
- 用户允许修改 Go runtime 源码、重编译 Go 工具链和业务二进制（`approved_execute` 且已获得重编译授权）。

不进入本 skill：

- 不能重编译，或拿不到 Go runtime 源码基线。
- 池对象为长生命周期大对象，或容器内存余量不足。
- 业务侧可通过减少分配、复用对象直接解决（优先走 code-optimizer）。

## 必读 Reference

- `references/go-runtime-patch-playbook.md` — 补丁组成、Go 基线记录、版本适配、`make.bash`、服务重编译、二进制等价门控和回退。
- `references/syncpool-godebug-knobs.md` — `poolretain` 与 `poolprealloc` 的参数矩阵、生效方式、内存代价、交互和拒绝条件。
- `references/microservice-ab-playbook.md` — 证据采集、A/B 分组步骤、内存水位监控、正确性验证和归档。
- `../../references/prerequisites.md` — 公共依赖、权限和降级策略。

> 打补丁和重编译前必须加载 `go-runtime-patch-playbook.md`；参数取值前必须加载 `syncpool-godebug-knobs.md`。

## Input Modes

### 独立运行（standalone）

- **触发**：未提供 `evidence_snapshot_dir` 和 `environment_backup_dir`。
- **行为**：自采集 Go 基线、二进制身份、pprof、池清单和运行环境；采集后询问压测用例。
- **压测**：有压测用例走 precise 模式（基线 → 补丁 → 重编译 → 复测 → 保留/回退）；无压测用例走 quick 模式，只给参数建议并标注 `verified=false`。
- **执行**：逐项询问补丁范围、构建方式和参数取值。

### 主 SKILL 调用（subagent）

- **触发**：主 SKILL 提供 `evidence_snapshot_dir` 和/或 `environment_backup_dir`。
- **行为**：直接读取主 SKILL 已采集的证据，不重复采集。
- **执行**：只输出 `candidate_actions`，重编译和参数变更交由主流程串行执行验证。

## 输入证据

- Go 基线：`go version`、`cat "$GOROOT/VERSION"`、`git -C "$GOROOT" describe --tags --always`、源码包 sha256。
- 二进制身份：`go version -m <binary>`、sha256、`/proc/<pid>/exe`、`cmdline`、`maps`。
- pprof：CPU top、heap 的 `alloc_space`/`inuse_space`、`GODEBUG=gctrace=1` 输出。
- 池清单：池数量、对象类型与大小、`New` 函数、`Get`/`Put` 比例、对象生命周期。
- 运行环境：`GOMAXPROCS`、容器 CPU limit、`GOGC`、`GOMEMLIMIT`、现有 `GODEBUG`。
- 验收基线：QPS、P50/P99、错误率、RSS、GC 次数与暂停时间。

## Workflow

### Step 0：基线与版本锁定

- 记录 `go version`、`$GOROOT/VERSION`、`git describe`、源码包 sha256 和补丁文件 hash。
- 记录基线二进制身份、启动参数、环境变量和部署形态。
- 版本不一致或源码来源不明时，停止在分析阶段，不进入构建。

### Step 1：颠簸定界

- 用 pprof 和 `gctrace` 确认分配热点落在 `sync.Pool` 相关路径。
- 用池清单确认对象是短生命周期临时对象，且 GC 后复用率明显下降。
- 排除业务算法、序列化库配置、连接池等其它解释后，才进入运行时补丁。

### Step 2：补丁与工具链构建

- 先把基线 Go 源码树复制到独立构建根目录，在构建根目录应用补丁；基线 `$GOROOT` 保持只读。
- 按 `go-runtime-patch-playbook.md` 校验私有补丁与当前 Go 基线是否匹配；补丁路径由本轮执行参数传入，不写入仓库。
- 不匹配时先做版本适配，禁止直接套用其它 Go 版本的补丁。
- 在构建根目录执行 `make.bash`，记录新工具链产物 hash 和构建根目录路径；基线工具链保留用于回退。

### Step 3：服务重编译

- 用 `$BUILD_ROOT/bin/go` 全量清理后重编译业务二进制，记录 `go version -m` 和产物 hash。
- 重编译前后除工具链和补丁外不得同时改变编译参数、运行库、启动参数或配置。
- 启动后复核 `/proc/<pid>/exe`、`cmdline`、`maps` 和健康检查。

### Step 4：参数取值

- 先单独验证 `poolretain=1`，再单独验证 `poolprealloc=N`，最后验证组合。
- `poolprealloc` 从一个保守值开始逐档上调，每档记录 RSS 和 GC 变化。
- 容器内存余量不足时禁止上调 `poolprealloc`；`poolretain=1` 无硬上限，必须设置 RSS 观察阈值。

### Step 5：正确性验证

- 运行 `go test sync` 中与 Pool 相关的用例，并确认补丁自带的 Pool 单测通过。
- `-race` 仅用于功能验证，不用于性能测量。
- 服务 smoke test、核心接口回归和长稳测试通过后才允许进入 A/B。

### Step 6：A/B 与内存水位

- 按 `microservice-ab-playbook.md` 固定负载、预热、并发和时长。
- 每轮只变更一个参数，记录 QPS、P50/P99、错误率、RSS、GC 次数与暂停时间。
- RSS 超过预设阈值、出现 OOM 或错误率上升时立即停止并回退。

### Step 7：保留或回退

- 正向收益且内存水位可控时保留，记录生效参数和配置位置。
- 负收益、噪声区且内存代价不可接受时回退到基线工具链和二进制。
- 归档 `forward_cmd`/`reverse_cmd`、日志、pprof、环境快照和轮次结果。

## 参数矩阵

| 参数 | 默认 | 取值 | 作用 | 内存代价 |
|---|---|---|---|---|
| `poolretain` | 0 | 0/1 | 非 0 时 GC 阶段跳过 `sync.Pool` 清理，池对象跨 GC 存活 | 池内对象不再随 GC 释放，驻留内存上升，无硬上限 |
| `poolprealloc` | 0 | 0..私有补丁内置上限（每 P，超出由补丁截断） | 池的 per-P 本地缓存首次创建时预分配 N 个对象，需 `Pool.New != nil` | 约 `N × GOMAXPROCS × 单对象大小` |

详细语义、交互和验证方法见 `references/syncpool-godebug-knobs.md`。

## Candidate Action Contract

每个 `candidate_actions[]` 必须包含 `action_id`、`title`、`category`、`priority`、`change_mode`、`requires_root`、`risk`、`implementation_plan`、`validation_plan`、`rollback`、`expected_effect`、`expected_gain_metric`、`rejection_criteria`、`evidence_refs`。

- `implementation_plan` 必须写明补丁文件、Go 基线、构建命令和重编译命令。
- `rollback` 必须写明原工具链、原二进制、原环境变量和恢复后的身份复核步骤。
- 每个参数一个 action，禁止把 `poolretain` 和 `poolprealloc` 合并在同一轮首次验证。

## 输出字段

至少输出：

- `go_baseline_version`
- `toolchain_source`
- `patch_scope`
- `pool_inventory`
- `knob_plan`
- `memory_watermark`
- `correctness_evidence`
- `binary_equivalence_check`
- `candidate_actions`
- `further_optimization_potential`

证据不足时输出 `status=degraded|blocked` 和最小补采命令，不得仅凭经验承诺收益。

## 拒绝条件

- 无法记录 Go 基线版本、源码来源或补丁 hash。
- 不允许重编译工具链或业务二进制。
- 目标二进制使用 `-race` 或 `-cover` 构建。
- 池对象为长生命周期大对象，或容器内存余量不足以承担 `poolretain` 的驻留增长。
- `poolprealloc` 预分配量按 `N × GOMAXPROCS × 单对象大小` 估算后超过内存余量。
- 业务代码存在更直接的减分配方案，且改动风险低于运行时补丁。

## 边界

- 本 skill 不修改业务代码；需要改业务代码时优先交给 code-optimizer。
- 本 skill 不调整 `GOGC`、`GOMEMLIMIT` 等 GC 策略，这些属于 application-config-optimization。
- 本 skill 只负责 `sync.Pool` 相关的 runtime 补丁和两个 GODEBUG 开关，不扩展到其它 runtime 修改。
- 本 skill 不生成、不保存补丁原文；补丁由外部私有制品提供，仓库只保留行为级说明。
- 基线 `$GOROOT` 只读；补丁应用、工具链构建和回退都在隔离构建根目录完成。
- 补丁跨 Go 版本复用时，先做版本适配，不直接套用。
