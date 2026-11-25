# Sonic 池清单与热点定位

本文件说明如何为 Sonic 编解码路径建立池清单。只记录本环境的对象类型、大小和调用位置，不记录历史性能数字。

## 1. 确认 Sonic 版本

```bash
go list -m github.com/bytedance/sonic
go version -m <binary> | grep sonic
```

记录 Sonic 版本、Go 版本和二进制身份。不同 Sonic 版本的内部池/缓存实现可能不同，跨版本复用结论前先重新确认。

## 2. 定位编解码路径

- 从业务入口找到 JSON encode/decode 调用点（HTTP handler、RPC handler、消息消费等）。
- 确认使用的是 Sonic 的 encoder/decoder，而不是标准库 `encoding/json` 或其它库。
- 用 pprof 热点确认 encode/decode、buffer 分配或池操作在热点中占主要部分；空载 profile 不作为判据。

## 3. 池清单模板

| 池位置 | 对象类型 | 单对象大小 | `New` 是否存在 | `Get`/`Put` 位置 | 生命周期 | 备注 |
|---|---|---|---|---|---|---|
| `<文件:函数>` | `<类型>` | `<bytes>` | 是/否 | `<调用点>` | 短/长 | 是否 Sonic 内部池 |

要求：

- 单对象大小按实际类型估算（含 slice 容量、结构体字段和对齐开销）。
- `New == nil` 的池不适用 `poolprealloc`。
- 长生命周期大对象不进入本 skill；优先考虑业务侧复用或 code-optimizer。
- 记录池数量；总预分配内存按池逐个求和：`Σ_i(N_i × GOMAXPROCS × 单对象大小_i)`，不能假设所有池同构。

## 4. 热点证据

```bash
go tool pprof -top -nodecount=30 <cpu-profile>
go tool pprof -sample_index=alloc_space <heap-profile>
go tool pprof -sample_index=inuse_space <heap-profile>
GODEBUG=gctrace=1 <service>
```

关注：

- encode/decode 函数及 Sonic 内部 buffer 分配。
- `alloc_space` 高、`inuse_space` 低（颠簸特征）。
- GC 次数/暂停是否随请求量上升。
- 池 `Get` 未命中后调用 `New` 的频率。

## 5. 组件验证指标

- encode/decode 吞吐或业务 QPS。
- P50/P99、错误率。
- `alloc_space`、`inuse_space`、RSS 峰值/稳态。
- JSON round-trip 正确性和核心接口 smoke test。

## 6. 交接

- 把池清单和对象大小交给 `go-syncpool-optimization`，由它输出 `knob_plan`。
- 把补丁/构建交给 `go-runtime-patch-build`，把 A/B 交给 `microservice-ab-testing`。
- 本文件不定义 `poolretain`/`poolprealloc` 的取值规则，也不记录历史收益数字。
