---
name: sonic-go-pool-optimization
description: 当 Go 微服务使用 Sonic 进行高频 JSON encode/decode、临时 buffer/encoder/decoder 对象在 GC 后被清空、堆内存颠簸时使用。输出 Sonic 池清单、热点证据和组件级验证指标。
---

# Sonic Go Pool Optimization

本 skill 是 `sonic-go` 组件专题：只负责识别 Sonic 编解码路径上的池对象、热点和组件级验证，不重复通用 `sync.Pool` 参数矩阵，也不修改 Sonic 业务代码。

## 何时触发

- 服务依赖 `github.com/bytedance/sonic`，JSON encode/decode 是主要吞吐路径。
- pprof 热点落在 Sonic encoder/decoder、buffer 分配或 `sync.(*Pool).Get`/`Put`。
- 编解码临时对象在 GC 后被清空，首次 `Get` 频繁走 `New`，RSS/GC 抖动明显。
- 用户允许使用 Go runtime 补丁重编译，并接受单变量 A/B 验证。

不进入本 skill：

- 服务不使用 Sonic，或 JSON 不是热点路径。
- 需要修改 Sonic 源码/业务编解码逻辑（交给 code-optimizer）。
- 池对象是长生命周期大对象，或内存余量不足。

## 依赖与分工

- `go-syncpool-optimization`：通用 `poolretain`/`poolprealloc` 参数决策和风险边界。
- `go-runtime-patch-build`：补丁构建、工具链/服务重编译、二进制等价、回退。
- `go-pprof-analysis`：CPU/heap profile 采集与判读。
- `microservice-ab-testing`：固定负载、单变量 A/B、指标、正确性、判定。

本 skill 只提供 Sonic 组件证据、池清单和组件指标，不复制上述 skill 的实现细节。

## 必读 Reference

- `references/sonic-pool-inventory.md` — Sonic 池清单方法、对象大小估算、热点定位和组件验证指标。

## 输入证据

- Sonic 版本：`go list -m github.com/bytedance/sonic`、`go version -m <binary>`。
- 编解码配置：encoder/decoder 选项、buffer 复用方式、是否启用 Sonic 的池/缓存路径。
- 池清单：池数量、对象类型/大小、`New` 函数、`Get`/`Put` 位置、对象生命周期。
- pprof：CPU top、heap `alloc_space`/`inuse_space`、`gctrace` 输出。
- 运行环境：`GOMAXPROCS`、容器 CPU limit、`GOGC`、`GOMEMLIMIT`。
- 基线：QPS、P50/P99、错误率、RSS、GC 次数与暂停时间。

## Workflow

### Step 0：确认 Sonic 路径

- 用 `go list -m`/`go version -m` 确认服务依赖 Sonic 及其版本。
- 用 pprof 确认 encode/decode 或 buffer 分配在热点中占主要部分。
- 排除业务算法、连接池、日志等其它解释。

### Step 1：池清单

- 按 `references/sonic-pool-inventory.md` 扫描 Sonic 相关的 `sync.Pool` 和 buffer 复用点。
- 记录池对象类型、单对象大小、`New` 函数、`Get`/`Put` 位置和生命周期。
- 标记 `New == nil` 的池（不适用 `poolprealloc`）和长生命周期大对象（不进入本 skill）。

### Step 2：交给通用参数 skill

- 把池清单、对象大小和 `GOMAXPROCS` 交给 `go-syncpool-optimization`，由它输出 `knob_plan`。
- 本 skill 不自行定义 `poolretain`/`poolprealloc` 的取值规则。

### Step 3：执行与验证

- 补丁/构建/重编译交给 `go-runtime-patch-build`。
- A/B 交给 `microservice-ab-testing`，组件指标使用本 skill 的 encode/decode 吞吐、`alloc_space`/`inuse_space`、RSS、错误率。
- 正确性至少覆盖 JSON round-trip 和核心接口 smoke test。

### Step 4：判定与回退

- 正向收益且 RSS 可控：保留，记录 Sonic 版本、池清单、生效参数和验证证据。
- 负收益、RSS/OOM 超阈值或正确性失败：回退，恢复基线工具链和二进制。
- 组件级收益无法与通用参数区分时，标记为交互结果，不重复归因。

## 分支、终止与交回

- Sonic 版本/依赖无法确认，或 JSON 不在热点路径：终止本专题，交回版本证据和排除理由，转交通用 `go-syncpool-optimization` 或 code-optimizer。
- pprof 不可用但 `gctrace`/`runtime.MemStats` 可用：`status=degraded`，只输出池清单和待验证参数，标记 `verified=false`。
- 缺少 `go-syncpool-optimization`、`go-runtime-patch-build` 或 `microservice-ab-testing`：`status=blocked_dependency`，交回缺失依赖和下一跳 skill。
- 池对象为长生命周期大对象、内存余量不足，或需要改 Sonic/业务代码：终止本专题，交回拒绝原因和替代路径。
- 正确性失败或 RSS/OOM 超阈值：回退并终止本轮；交回 `rollback_result` 和停止原因。
- 正常完成：交回 `sonic_version`、`pool_inventory`、`hotspot_evidence`、`knob_plan_ref`、`component_metrics` 和 `correctness_evidence`。

## 组件指标

- encode/decode 吞吐或业务 QPS。
- P50/P99、错误率。
- `alloc_space`、`inuse_space`、RSS 峰值/稳态。
- GC 次数/暂停（由 `go-pprof-analysis` 或 `gctrace` 提供）。

不记录或引用其它环境的历史性能数字；收益必须来自本轮 A/B。

## 输出字段

- `sonic_version`
- `pool_inventory`
- `hotspot_evidence`
- `knob_plan_ref`
- `component_metrics`
- `correctness_evidence`
- `candidate_actions`

## 拒绝条件

- 无法确认 Sonic 版本或无法定位编解码热点。
- 池对象为长生命周期大对象，或容器内存余量不足。
- 缺少 `go-syncpool-optimization`、`go-runtime-patch-build` 或 `microservice-ab-testing`。
- 需要修改 Sonic 源码或业务编解码逻辑才能解决。

## 边界

- 不复制通用 `sync.Pool` 参数矩阵；参数由 `go-syncpool-optimization` 提供。
- 不写补丁构建/重编译/回退步骤；由 `go-runtime-patch-build` 负责。
- 不修改 Sonic 或业务代码；需要代码改写时交给 code-optimizer。
- 不写机型信息、历史性能数字或其它环境的对比数据。
