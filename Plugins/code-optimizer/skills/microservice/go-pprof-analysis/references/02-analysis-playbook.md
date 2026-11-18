# 分析命令手册与典型问题模式

## go tool pprof 核心命令

```bash
# 交互式（进入后可用 top / peek / list / traces / web 等子命令）
go tool pprof cpu.pprof

# 非交互常用
go tool pprof -top -nodecount=15 cpu.pprof      # 热点排行
go tool pprof -list=main.fib cpu.pprof          # 指定函数逐行开销（正则匹配）
go tool pprof -peek=fib cpu.pprof               # 函数的上下游调用关系
go tool pprof -traces cpu.pprof                 # 原始采样栈

# 前后对比（优化验证必用）
go tool pprof -base before.pprof after.pprof -top

# Web UI（火焰图 / 调用图 / Top / Source / Disasm）
go tool pprof -http=0.0.0.0:8081 cpu.pprof
```

Web UI 视图速查（URL 直达）：
- `/ui/flamegraph` — 火焰图：横条越宽开销越大，上下堆叠是调用关系，点击放大
- `/ui/` + VIEW → Graph — 调用图（依赖 graphviz 的 `dot`）
- VIEW → Source — 逐行源码开销
- 顶部 SAMPLE 菜单 — 切换指标维度（heap 的 inuse_space/alloc_space 等）

## go tool trace 核心命令

```bash
go tool trace trace.out
# 打开后重点看：
#   View trace by proc   — 各 P 上的调度/GC 时间线
#   Goroutine analysis   — 每个 goroutine 的执行/阻塞/调度等待分解
#   Synchronization blocking profile — 同步阻塞热力
```

## 典型问题模式速查

### 模式 1：业务函数 CPU 热点
症状：top 中某业务函数 flat 占比高。
动作：`-list` 看逐行开销定位到具体语句 → 算法优化/缓存/减少反射与 interface 开销。涉及 cgo 或 `*.s` 汇编热点时桥接 code-optimizer 的向量化/汇编 skill。

### 模式 2：GC / 分配压力
症状：CPU top 中 `runtime.mallocgc`、`runtime.gcBgMarkWorker`、`runtime.scanobject` 合计占比高（>20% 即显著）。
动作：heap 的 `alloc_space` 找分配大户 → 减少临时对象、`sync.Pool`、预分配 slice/map 容量、避免 `fmt.Sprintf` 热路径。可配合 `GOGC` / `GOMEMLIMIT` 调参。

### 模式 3：goroutine 泄漏
症状：`/debug/pprof/goroutine?debug=1` 总数持续增长不回落。
动作：文本栈按栈顶分组计数，找数量最大且单调增长的同构栈 → 常见原因：channel 无消费者、忘了 close、`time.Tick` 未 Stop、context 未取消。

### 模式 4：锁竞争
症状：mutex profile 有热点，或 trace 中大量 sync blocking。
动作：缩小临界区、读多写少换 `sync.RWMutex`、按 key 分片锁、无状态计数换 `atomic`、读多场景考虑 `sync.Map`。

### 模式 5：CPU 利用率低但延迟高
症状：CPU profile 样本很少（程序大部分时间在等待）。
动作：这不是 CPU 问题 —— 抓 block profile 和 trace，查锁/通道/IO 等待；检查 `GOMAXPROCS` 与容器 CPU limit 是否匹配（`uber/automaxprocs`）。

## 优化前后对比规范

1. 相同负载、相同采样时长各抓一次 profile（before/after）。
2. `go tool pprof -base before.pprof after.pprof -top` 看差值，负值 = 减少。
3. 结论必须附数据：函数名 + flat/cum 前后数值 + 百分比变化。
