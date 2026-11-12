# 诊断模式工作流（现象 → 排查树）

输入：用户的症状描述。先区分四类入口，再执行。

## 第 0 步 · 事实收集（必做，一次性拿到）

```bash
# --- 硬件拓扑 ---
lscpu | grep -E 'NUMA|Socket|Model name'
# DPDK 源码仓 usertools 下（目标环境有源码仓时）；无源码仓用 lscpu 手工核对
usertools/cpu_layout.py 2>/dev/null || usertools/cpu_layout
lspci -nn | grep -i eth
for d in <使用的PCI地址>; do
  cat /sys/bus/pci/devices/0000:$d/numa_node
  lspci -s 0000:$d -vv | grep LnkSta
done
dmidecode -t memory | grep -E 'Locator|Speed' | head -30

# --- 内核/OS 状态 ---
cat /proc/cmdline
cat /sys/devices/system/cpu/isolated
grep Huge /proc/meminfo
cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort -u
systemctl is-active irqbalance
grep -E 'NMI|LOC' /proc/interrupts

# --- 应用/EAL ---
# 完整启动命令行（让用户提供或从脚本/容器 yaml 拿）
ps -eLo pid,psr,comm | grep -i <app>

# --- 运行时统计 ---
# testpmd: show port stats / show port xstats
# 或 dpdk-proc-info / telemetry（见 observability-benchmark.md）
```

缺失项无法直接采集时，向用户索要输出，不要跳过。

## 第 1 步 · 症状分类

| 入口 | 走向 |
|------|------|
| A. 吞吐/PPS 低于预期 | 树 A |
| B. 时延高/毛刺 | 树 B |
| C. 丢包 | 树 C |
| D. 启动失败/OOM | 树 D |
| E. CPU 满 / 异常空转 | 树 E |

## 树 A · 吞吐不足

1. 基线存在？没有 → 先建基线（M8 方法），所有后续对比基于它。
2. 过 M10 速查表（`references/antipatterns-checklist.md`，命中率高，先排雷）。
3. 逐层判据：
   - CPU 满？→ M6 热点分析（perf top）→ M5 vector 路径确认；若确认热点在业务代码，
     移交 code-optimizer 的 `dpdk-optimization` skill。
   - CPU 不满 → 队列倾斜？（每队列分布）→ RSS/flow（M5）。
   - PCIe/链路跑满？（LnkSta × bps 换算）→ 硬件层（M1）。
   - 跨 NUMA？（三点一线核对）→ M4。
4. 修复建议按"改动最小 + 逐项可回退"排序，每项附验证命令与预期指标变化。

## 树 B · 时延高

1. 均值高还是尾部高？（要 P99/P99.9 分布，M8 latencystats）
2. 尾部毛刺 → M2 隔离核查（context-switches、irq、THP、swap）。
3. 均值高 → M6 逐包成本 + M5 路径（offload 降级、burst 过大排队）。
4. 省电机制误开？（monitor/pause scheme 在低时延场景应为关，M7）。

## 树 C · 丢包

1. 丢在哪一层？（xstats 分层判据，M8）：
   - imissed → 树 C1：rxd/burst/worker 阻塞（M5）。
   - nombuf → 树 C2：mbuf 池耗尽或泄漏（M4；先查泄漏再加大池）。
   - ierrors → 树 C3：设备错误，按驱动 xstats 明细与文档查。
   - 对端/链路层 → 流控反压、CRC（ethtool -S 基线，M1/M5）。
2. 突发相关？突发+imissed → 吸收能力不足。
3. 修复后用同流量模型回归（imissed 应归零）。

## 树 D · 启动失败/OOM

1. EAL 日志逐行读（大页不足/file-prefix 冲突/设备探测失败各有固定报错）。
2. 过 M10 启动/内存类表。
3. 多进程场景核对 file-prefix、大页配置一致性（M9）。

## 树 E · CPU 异常

1. worker 满 → A 树 CPU 满分支。
2. worker 空 → 上游没包/队列没绑上（--lcores 映射与启动日志核对，M3）。
3. 非 worker 核高 → housekeeping 核过载（irqbalance、业务进程混部，M2）。

## 输出格式（诊断报告）

```text
## 瓶颈结论
<一句话结论>
## 证据链
<逐条：证据(命令输出摘录) → 推断>
## 修复建议
<按优先级列表：改动 → 验证命令 → 预期指标 → 回退方法>
## 未排除项
<UNVERIFIED 假设与后续验证方法>
```
