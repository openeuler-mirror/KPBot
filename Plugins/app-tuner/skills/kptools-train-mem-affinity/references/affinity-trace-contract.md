# thread-affinity 采集输出数据契约

> `collect_rank_mem.py --affinity-dir` 消费 kptools-thread-affinity-collector
> （MR !247 评审中）的采样目录。本文明确其产物 schema，使联合判读的
> 数据假设可校验；文件缺失/分节全缺时采集器输出 WARN（观测缺口可见，
> 不静默降级）。

## topology_raw.txt

`=== <section>` 分节的纯文本：

| 分节 | 行格式 | 本 skill 用途 |
|------|--------|--------------|
| `node_cpulist` | `<node>|<cpulist>`（如 `0|0-31`） | cpu→node 映射（线程落点归 node） |
| `npu_pci` | `<bdf>|<node>`（如 `0000:8f:00.0|2`） | NPU 设备 node 集合（**producer 版本相关**：旧版 vendor 0x19e5 全列未做 class 双校验、可能含 Huawei 网卡；现行已双校验——消费侧一律以本地 双校验集合优先，两种版本下都安全） |

- 分节缺失/行格式不合法时该行跳过；两分节全缺（含文件缺失）→ WARN
  并降级（无 cpu→node 映射时联合表不出线程落点列）
- `node` 为负值（sysfs 未暴露归属）视同该设备不可判定
- **NPU 集合可信度**：producer 旧版为 vendor-only 全列（Huawei 网卡同为
  0x19e5 会被列入），现行 producer 已双校验——联合表判定**一律优先使用
  本 skill 采集侧的双校验集合**（vendor + class 0x120000/device id，
  见 SKILL.md 约束节）；本地无 NPU 集合时才沿用该快照集合并 WARN
  标注"未双校验"——对两种 producer 版本都安全

## summary.json

| 字段 | 类型 | 本 skill 用途 |
|------|------|--------------|
| `target.pid` | int | 与 rank 进程 PID 匹配（匹配失败时联合表标注 unmatched） |
| `profiles[].busy_pct` | float | ≥50 的视为忙线程，参与线程主落点加权 |
| `profiles[].active_psr_top` | `[[cpu, count], ...]`（降序，前 4 项参与） | cpu 经 topology 映射归 node，count 加权 |

- `busy_pct` 语义：生命周期 CPU 占比；`active_psr_top` 为活跃样本
  psr 计数降序（完整口径见该 skill 的 SKILL.md 采样设计节）
- 解析失败 → WARN 并降级（`thread_primary_node` 不出）

## 联合判读行为

- 命中 `target.pid` == 某 rank PID：该 rank 附 `thread_join`
  （`thread_primary_node` + `thread_nodes` 加权分布），联合表输出
  "内存主 node vs 线程主落点 node vs NPU nodes" 判定
- 未命中：联合表头标注 "未匹配到 rank 进程"；**未匹配的 rank 显示
  "无数据（未匹配线程）"，不显示"干净"**（无数据 ≠ 已核对正常）
- 判定只是粗筛——按 methodology.md 核对证据与反例后才可写入结论
