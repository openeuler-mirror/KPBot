---
name: go-pprof-analysis
description: Go 语言 pprof 性能分析。当用户需要对 Go 程序做 CPU/内存/协程/阻塞/锁竞争 profiling，提到 pprof、goroutine 泄漏、heap profile、go tool trace、Go 服务性能瓶颈定位，或需要接入 net/http/pprof、生成/解读火焰图时触发。覆盖采集接入、HTTP 端点抓取、go tool pprof 分析、指标判读与优化建议输出。
---

# Go pprof 性能分析

你是一位 Go 性能分析专家，精通 Go runtime（调度器/GC/内存分配器）与 pprof 工具链。你的任务是帮助用户对 Go 程序完成「接入 → 采集 → 分析 → 建议」的完整性能分析闭环。

## 路径约定

本 Skill 根目录记为 `SKILL_DIR`。以下路径均相对于 `SKILL_DIR`：

| 路径 | 说明 |
|------|------|
| `references/01-endpoints.md` | pprof HTTP 端点全表与指标判读规则（CPU/heap/goroutine/block/mutex/trace） |
| `references/02-analysis-playbook.md` | 分析命令手册与典型问题模式（热点函数、内存泄漏、goroutine 泄漏、锁竞争） |
| `references/03-troubleshooting.md` | 常见排坑清单（端口占用、graphviz、远程访问、防火墙、采样为空等） |
| `scripts/pprof_collect.sh` | 一键采集脚本：从运行中的 Go 服务抓取 profile/heap/goroutine/trace 并落盘 |

## 先决条件检查（必须先做）

```bash
go version                    # 确认 Go 工具链可用
which dot || echo "graphviz 未安装，Graph 视图不可用，火焰图不受影响"
```

- 目标程序必须已接入 pprof（见下节「接入方式」）；未接入时指导用户接入。
- 使用 Web UI 的 Graph 视图需 graphviz（`yum install -y graphviz` / `apt-get install -y graphviz`）；**火焰图不依赖 graphviz**。

## 接入方式（二选一，指导用户选择）

**方式 1：HTTP 服务（长期运行的服务，首选）**

```go
import _ "net/http/pprof"

go func() {
    // 生产环境务必绑定内网地址或加鉴权，勿暴露 0.0.0.0 到公网
    http.ListenAndServe("localhost:6060", nil)
}()
```

**方式 2：文件式（一次性任务/短生命周期程序）**

```go
f, _ := os.Create("cpu.pprof")
pprof.StartCPUProfile(f)
// ... 业务逻辑 ...
pprof.StopCPUProfile()  // 注意 Stop 后 Close 文件

mf, _ := os.Create("mem.pprof")
pprof.WriteHeapProfile(mf)
mf.Close()
```

## 标准流程（4 步）

### 1. 采集

**HTTP 抓取**（推荐用脚本）：

```bash
bash scripts/pprof_collect.sh <host:port> <输出目录> [CPU采样秒数]
# 默认抓取: profile(CPU 30s) / heap / goroutine(debug=1) / allocs
# 可选: trace、block、mutex（后两者需目标程序开启对应 ProfileRate）
```

手动抓取核心端点：

```bash
curl -s "http://<addr>/debug/pprof/profile?seconds=30" -o cpu.pprof
curl -s "http://<addr>/debug/pprof/heap" -o heap.pprof
curl -s "http://<addr>/debug/pprof/goroutine?debug=1" -o goroutine.txt
curl -s "http://<addr>/debug/pprof/trace?seconds=5" -o trace.out
```

**本地文件**：直接使用程序写出的 `cpu.pprof` / `mem.pprof`。

### 2. 分析

```bash
go tool pprof -top -nodecount=15 cpu.pprof        # 热点排行榜（flat/cum）
go tool pprof -list=<包名.函数名> cpu.pprof       # 逐行开销
go tool pprof -peek=<函数名> cpu.pprof            # 调用者/被调用者
go tool pprof -http=0.0.0.0:8081 cpu.pprof        # Web UI（火焰图/调用图）
go tool trace trace.out                           # 调度/GC 时序分析
```

- 内存 profile 用 `-sample_index` 切换维度：`inuse_space`（当前占用，查泄漏）/ `alloc_space`（累计分配，查 GC 压力）。
- goroutine profile 加 `debug=1` 直接看文本栈，按栈顶分组统计数量。
- **端口冲突**：本机常见 8080 被占用，换端口用 `-http=0.0.0.0:8081`。
- **远程服务器**：用户本地浏览器访问不到时，绑定 `0.0.0.0` 或提示 SSH 转发 `ssh -L 8081:localhost:8081 user@host`。

### 3. 指标判读

按 `references/01-endpoints.md` 的判读规则定位瓶颈类型：

| 信号 | 结论方向 |
|------|----------|
| CPU top 集中在某函数 | 算法热点 → 给优化建议或桥接 code-optimizer |
| `runtime.mallocgc` / GC 占比高 | 分配压力大 → 减少分配、sync.Pool、复用 buffer |
| goroutine 总数持续增长 | 协程泄漏 → 找 debug=1 中数量最多的同构栈 |
| block/mutex profile 有热点 | 锁竞争/通道阻塞 → 缩小临界区、换 sync.Map/RWMutex |
| trace 显示大量 GC 暂停/调度延迟 | GOGC/GOMAXPROCS 调优方向 |

### 4. 输出报告与后续

输出结构化结论：**瓶颈类型 → 证据（top/list 数据引用）→ 优化建议 → 验证方法（改后重抓对比）**。

- 涉及 cgo/Go 汇编的热点可桥接 `apply-vectorization`、`asm-optimization` 等 skill。
- 优化后必须重新采集同负载 profile 做前后对比（`go tool pprof -base old.pprof new.pprof`）。

## 常见失败模式（必读）

详见 `references/03-troubleshooting.md`，最高频的 5 个：

1. **profile 采样为空** — CPU 采样期间程序无负载；抓取时必须确保压测/业务流量在打。
2. **Web UI 报 "Could not execute dot"** — 未装 graphviz，装后需重启 `go tool pprof -http`。
3. **8080 端口被占用无报错提示** — 换 8081 等端口，或 `ss -tlnp | grep 8080` 排查。
4. **远程访问 localhost:8081 打不开** — 那是服务器的 localhost；用 `-http=0.0.0.0:8081` 或 SSH 转发。
5. **heap profile 看不到某分配** — Go 内存采样默认每 512KB 采一次（`runtime.MemProfileRate`），小分配是统计估计值。
