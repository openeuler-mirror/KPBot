# kptools-node-profiler 使用指南

## 1. 三条采集路径

| 路径 | 注入方式 | 产物 | 适用 |
|------|---------|------|------|
| `node --prof` | 启动参数 | isolate-*.log（V8 ticks） | 零依赖默认路径；`--prof-process` 文本汇总 |
| `node --cpu-prof` | 启动参数（Node ≥ 12） | *.cpuprofile（Chrome DevTools 格式） | 可视化分析 |
| 0x | 启动包装（npm） | 交互式火焰图 HTML | 需 npm，体验最好 |

## 2. 能力边界

- **无 attach**：V8 不支持对运行中进程开启 CPU profiling（`kill -USR2` 仅翻转日志）；对运行中进程 perf 只能看 native 帧
- `--prof` 采样间隔约 1ms：高 QPS 网关有可测开销，低峰采集
- isolate-*.log 写入进程 CWD；容器需确认挂载
- `--cpu-prof` 默认进程退出时写出；长驻服务注意优雅退出

## 3. prof-process 输出解读

- `[Summary]`: ticks 分布（JavaScript / C++ / GC / 其他）
- `[JavaScript]`: JS 函数热点
- `[C++]`: C++/V8 内部热点；占比高而 JS 低 → IO/序列化/原生库
- `[Bottom up]`: 自底向上调用（定位谁调用热点）
- ticks 为采样计数，近似 CPU 时间份额

## 4. 语言路由

无前缀火焰图请求先做语言判定（主文档 §3.5）：cmdline 含 node → 本 SKILL；python → kptools-py-spy；java → kptools-devkit java-perf；其余 → kptools-devkit tuner hotspot
