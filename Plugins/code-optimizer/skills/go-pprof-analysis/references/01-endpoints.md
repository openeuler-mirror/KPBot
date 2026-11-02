# pprof HTTP 端点全表与指标判读

`import _ "net/http/pprof"` 后，`/debug/pprof/` 下可用的全部端点：

| 端点 | 指标 | 内容 | 查看方式 |
|------|------|------|----------|
| `/debug/pprof/profile?seconds=N` | CPU 采样 | N 秒内的 CPU 时间分布（默认 100Hz 采样） | `go tool pprof` |
| `/debug/pprof/heap` | 堆内存 | inuse_space / alloc_space / inuse_objects / alloc_objects | `go tool pprof -sample_index=...` |
| `/debug/pprof/allocs` | 历史分配 | 累计分配采样（heap 的另一种视角） | `go tool pprof` |
| `/debug/pprof/goroutine` | 协程 | 所有 goroutine 数量与栈；`?debug=1` 输出可读文本栈 | 浏览器直接看 / `go tool pprof` |
| `/debug/pprof/block` | 阻塞 | channel/select/sync 原语等待（需 `runtime.SetBlockProfileRate(1)`） | `go tool pprof` |
| `/debug/pprof/mutex` | 锁竞争 | mutex 争用（需 `runtime.SetMutexProfileFraction(1)`） | `go tool pprof` |
| `/debug/pprof/threadcreate` | 线程创建 | OS 线程创建栈 | `go tool pprof` |
| `/debug/pprof/trace?seconds=N` | 执行 trace | 调度/GC/系统调用完整时序 | `go tool trace` |
| `/debug/pprof/cmdline` | 命令行 | 进程启动参数 | 浏览器 |
| `/debug/pprof/symbol` | 符号 | 符号表查询 | 浏览器 |

## 各指标判读规则

### CPU（profile）
- `flat` = 函数自身耗时，`cum` = 含被调用者。排序先看 flat 找热点函数，再看 cum 找热点路径。
- top 中出现大量 `runtime.*`（`mallocgc`/`gcBgMarkWorker`）→ 不是业务慢，是分配/GC 拖慢，转查 heap。
- `Duration` 远小于采样秒数且 Total samples 很少 → 采样期间没负载，数据无效。

### heap
四个维度，用 `-sample_index` 切换：
- `inuse_space`：当前存活内存 → **查内存泄漏**看这个
- `alloc_space`：累计分配总量 → **查 GC 压力**看这个
- `inuse_objects` / `alloc_objects`：对象数版本
- Go 内存是采样制（默认 `runtime.MemProfileRate = 512*1024`），数值为统计估计。

### goroutine
- 先抓 `?debug=1` 文本，按栈顶函数分组数数量；同构栈数量异常多 = 泄漏点。
- 对比两次抓取（间隔几分钟）数量持续增长 → 确认泄漏，看栈中阻塞在 `chan receive`/`select`/`sync.Wait` 的位置。
- Web UI 中该 profile 的 VIEW 视图可区分 `goroutine`（存活）与 `wait` 维度。

### block / mutex
- 前提：目标程序必须开启开关，否则永远为空：
  ```go
  runtime.SetBlockProfileRate(1)      // 1 = 每次阻塞都记录，生产可调大降低开销
  runtime.SetMutexProfileFraction(1)  // 1/N 采样
  ```
- 数值含义：delay（累计等待时间）与 count（次数）。block 热点指向 channel 设计问题；mutex 热点指向锁粒度问题。

### trace
- `go tool trace trace.out` 打开 Web UI：看 Goroutine analysis（调度延迟）、Network/Sync blocking profile、GC 暂停时间线。
- 适用场景：延迟毛刺、GC STW 怀疑、CPU 利用率低但 profile 无热点（大量时间在等锁/等 IO）。
