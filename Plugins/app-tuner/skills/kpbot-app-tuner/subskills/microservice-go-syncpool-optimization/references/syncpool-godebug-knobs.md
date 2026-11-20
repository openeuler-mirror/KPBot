# sync.Pool GODEBUG 参数矩阵

本文件定义补丁新增的两个开关：`poolretain` 与 `poolprealloc`。参数只在进程启动时解析，运行中修改环境变量无效，必须重启服务。

## 1. 基线机制

Go 原生 `sync.Pool` 使用 per-P 本地缓存加 victim 两层缓存。每次 GC 会把当前缓存降为 victim，下一次 GC 清空 victim，因此对象最多跨一轮 GC 存活。短生命周期小对象密集创建时，池会被周期性清空，首次 `Get` 重新走 `New`，表现为堆内存颠簸。

## 2. 参数总表

| 参数 | 默认 | 取值 | 生效方式 | 适用 Go 版本 | 作用点 | 作用 | 内存代价 | 风险 |
|---|---|---|---|---|---|---|---|---|
| `poolretain` | 0 | 0/1 | `GODEBUG=poolretain=1` | 补丁基线版本，现场以 `$GOROOT/VERSION` 记录值为准 | GC 清理路径 | 非 0 时跳过 `sync.Pool` 清理，池对象跨 GC 存活 | 池内对象驻留，内存无硬上限 | 长生命周期大对象导致 RSS 持续上涨甚至 OOM |
| `poolprealloc` | 0 | 0..私有补丁内置上限（每 P）；超出由补丁截断 | `GODEBUG=poolprealloc=N` | 同补丁基线 | 池首次创建 per-P 缓存时 | per-P 缓存首次创建时预分配 N 个对象，要求 `Pool.New != nil` | 约 `N × GOMAXPROCS × 单对象大小` | 值过大导致启动期内存冲高或 OOM |

## 3. poolretain

- 行为：`poolretain` 非 0 时，GC 阶段跳过 `sync.Pool` 清理；为 0 时保持原生行为。
- 影响范围：只跳过 `sync.Pool` 清理，boringcrypto、unique map、sudog 缓存等其它清理不受影响。
- 与 GC 的关系：`GOGC`、`GOMEMLIMIT` 仍然决定 GC 触发时机；`poolretain` 只改变池对象是否在 GC 时释放。
- 内存风险：没有上限。必须在验证前设定 RSS 阈值，并确认池对象大小可控。
- 验证：`GODEBUG=gctrace=1` 观察 GC 次数与堆大小；pprof `inuse_space` 观察池对象驻留；长稳观察 RSS 是否收敛。
- 回退：去掉 `GODEBUG` 中的 `poolretain` 并重启。

## 4. poolprealloc

- 行为：池首次创建 per-P 缓存时，按 `poolprealloc` 预分配 `N × GOMAXPROCS` 个对象；实现内置安全上限，超出由补丁截断。
- 前提：`Pool.New != nil`；`New` 为 nil 的池不做预分配。
- 顺序：预分配在池本地缓存对外可见之前完成，并避免在调用 `New` 时持有会影响 GC 的内部状态。
- 与 GOMAXPROCS 的关系：`GOMAXPROCS` 变化会重新分配 per-P 数组，并再次触发预分配；容器内 `GOMAXPROCS` 与 CPU limit 不一致时要先修正。
- 内存估算：`N × GOMAXPROCS × 单对象大小`，乘以池数量得到总量上限。
- 验证：pprof `alloc_space` 首次分配量、RSS 启动峰值、GC 次数、首次 `Get` 的 `New` 调用次数。
- 回退：`poolprealloc=0` 或移除该键并重启。

## 5. 生效方式与优先级

- `GODEBUG` 在进程启动时解析；支持 `//go:debug` 与 go.mod `godebug` 指令的 Go 版本，可把默认值固化到构建产物（以现场 Go 版本支持为准）。
- 优先级：环境变量 > 构建期默认值；测试覆盖值只在单测中生效。
- 同一进程内两个开关互不替代：`poolretain` 控制回收，`poolprealloc` 控制创建。

## 6. 取值建议

- 首轮只开 `poolretain=1`，确认内存水位可接受。
- 第二轮单独验证 `poolprealloc`，从保守值开始逐档上调，每档记录 RSS 和 GC。
- 第三轮验证组合，组合轮标记为交互验证，不用于单独归因。
- 每轮只变更一个参数，禁止首轮同时开启两个开关。

## 7. 观测与拒绝条件

观测：

```bash
GODEBUG=gctrace=1,poolretain=1,poolprealloc=64 <service>
go tool pprof -sample_index=inuse_space <heap-profile>
go tool pprof -sample_index=alloc_space <heap-profile>
```

拒绝：

- 池对象为长生命周期大对象。
- 容器内存余量小于 `N × GOMAXPROCS × 单对象大小` 加安全余量。
- 无法在启动前设置 RSS 阈值或无法观察长稳。
- 业务侧存在更直接的减分配方案。
