---
name: cross-arch-regression-audit
description: >
  工作场景：跨架构软件（x86_64 ↔ aarch64）的移植性能退化审计——盘点架构分支与架构默认值差异，
  考古每个差异的引入理由，量化热点路径成本，输出"无理由差异 + 可数成本"双门槛过滤后的候选
  优化点清单。同样适用于"定制/发行版内核与上游 vanilla 的默认值漂移"审计（同方法、同门槛）。
  适用于移植后性能回退排查、双架构行为不一致对齐、虚拟化/模拟层跨架构路径审计。
  配套脚本 arch_diff_scan.py 自动盘点 Rust cfg(target_arch) 与 C/C++ ifdef 架构差异。
  不适用：单架构内常规热点优化（用 analyze-hotspot）；纯正确性移植问题（编译错误/语义错误）。
---

# 跨架构移植性能退化审计

跨架构移植软件中，系统性性能退化的高发区是**没人质疑的架构特异默认值**：移植时为
新架构随手选择的路径/设备/参数，往往没有与成熟架构对齐，也没有书面理由，而差异本身
藏在 `cfg(target_arch)` / `#ifdef` 分支里，常规热点分析只看单侧代码永远不会发现它们。

成熟侧（通常是被移植的源架构）的选择已经过大规模生产验证；当移植侧的差异**考不出
引入理由**、且**落在热点路径上可数成本**时，向成熟机制对齐就是高置信度候选优化。

本 skill 覆盖两个差异面，方法与门槛完全相同：

1. **架构间**：同一代码树里 x86_64 与 aarch64 的分支/默认值差异（主差异面）；
2. **基线间**：定制/发行版构建与上游 vanilla 的默认值漂移（如同名内核 config 的
   差异），适合"排除一切用户态后"的系统性劣化排查。

## 五步审计法

### 1. 定方向：识别 parity baseline

1. 判断哪一侧是原生/成熟侧：git 历史（先支持谁）、上游主干默认值、社区部署规模。
2. 输出方向声明（如 "x86_64 为成熟侧，aarch64 为移植侧"），后续所有对齐以成熟侧为基准。
3. 不假设 x86_64 永远是成熟侧：ARM 原生软件移植到 x86_64 时方向相反；双架构同期原生
   开发的项目可能两侧各有基准，按子系统分别判断。
4. 基线间差异面没有"移植侧"：以**上游 vanilla 默认值**为基准，定制/发行版差异逐项
   问"为什么改、改了谁受益"。

### 2. 盘差异：枚举全部架构分支

1. 跑 `scripts/arch_diff_scan.py`（用法见下节）拿到机械可枚举的差异清单。
2. **按语言补齐覆盖矩阵**——脚本只机械覆盖 Rust 与 C/C++，Go build tag、汇编、
   构建脚本、内核 config 等需人工盘点，完整矩阵见
   [diff-taxonomy.md](references/diff-taxonomy.md)。
3. 补充人工盘点（脚本覆盖不到的语义层）：
   - per-arch 默认值表（console、调度参数、缓冲与预读策略、超时与重试参数）；
   - 设备模型与交互路径选择（每架构各模拟什么设备、走什么通道）；
   - 构建配置差异（feature flag、编译选项、链接库版本）。
4. 此阶段**只枚举不判断**，输出全量差异清单。分类学见
   [diff-taxonomy.md](references/diff-taxonomy.md)。

### 3. 考古理由：justify or flag

对每个差异问"为什么存在"：

1. `git log` / `git blame` 追引入 commit，读 commit message 与关联 issue/PR；
2. 读差异处的代码注释与文档；
3. 结论三选一：
   - **有 stated reason 且理由仍成立** → 归档为合理差异（D 类），不再跟踪；
   - **考不出任何理由**（移植时随手带进来）→ 进入候选池；
   - **理由已过时**（针对的硬件/版本早已不存在）→ 进入候选池并记录过时依据。

实测锚点：CubeSandbox aarch64 guest console 用 `ttyAMA0`（PL011）即属于"移植带进、
无 stated reason"——上游合入修复时确认了这一点（见案例库）。

### 4. 量化成本：热点路径可数证据

候选差异必须落在**可证明的热点路径**上，并给出可数证据；否则降级为观察项：

- 热点路径：业务负载路径、启动/恢复路径、同步请求路径（被压测卡住的链路）；
- 可数证据：MMIO/PIO exit 计数（`perf kvm stat`、KVM debugfs）、每字节 trap 次数、
  trace 事件计数、单实例延迟分解、宽限期/锁等待耗时；
- 判定：数不出成本的差异**不报优化点**。证据清单与口径见
  [evidence-gates.md](references/evidence-gates.md)。

### 5. 对照验证：向成熟机制对齐

1. 改造为成熟侧同款机制（或证明成熟侧机制可移植后移植它），其余变量全部固定；
2. 准备回退路径后再切换；重启后核对**运行时生效证据**，不把"配置已改"当"优化已生效"；
3. A/B 对照报告收益、退化与不确定性；未实测时 `gain_pct=null`，不承诺固定收益。

## 高价值差异域（优先盘点）

| 差异域 | 为什么高发 | 典型信号 |
|---|---|---|
| guest console / 串口路径 | 每字节输出的 exit 次数 × 输出字节数 | 模拟 UART 每字节 2 次用户态 exit |
| 模拟设备 vs 半虚拟化设备 | 移植侧常默认纯模拟设备，成熟侧早换成 virtio | exit 计数高、吞吐低 |
| 内核默认值（调度/RCU/预读） | 发行版或移植分支随手改默认，无人复核 | 与 vanilla diff 默认值 |
| 页大小 / folio / 预读策略 | 影响缓存行争用与 TLB 行为 | 共享 refcount 缓存行乒乓 |
| 设备线程化模型 | 差的路径在 vCPU 线程同步处理 I/O | vCPU 线程耗时异常 |

## 脚本用法（scripts/arch_diff_scan.py）

```sh
# 盘点代码树全部架构分支差异（Rust cfg / C-C++ ifdef / 自定义关键词）
python3 scripts/arch_diff_scan.py --root /path/to/repo
python3 scripts/arch_diff_scan.py --root . --keyword console --keyword earlyprintk
python3 scripts/arch_diff_scan.py --root . --format tsv --output scan.tsv
python3 scripts/arch_diff_scan.py --root . --output scan.json   # Windows 推荐：落盘免编码坑
python3 scripts/arch_diff_scan.py --root . --include-docs      # 关键词也扫 .md/.txt/.rst（考古用）
```

- 输出每条差异：文件、行号、类型（rust-cfg / c-ifdef / keyword）、架构归属、代码片段；
- **纯文档文件（.md/.txt/.rst）默认不参与 keyword 匹配**（信噪比），需要考古 docs/
  changelog 时用 `--include-docs` 打开；
- 控制台代码页损坏非 ASCII 输出时（如 Windows GBK），一律用 `--output` 落盘（UTF-8）；
- 脚本只做**枚举**，不判断是否有理由、是否值得优化——判断在第 3、4 步；
- 大型代码树按子系统分目录多次跑，配合 `--keyword` 聚焦默认值类差异。

## 输出契约

审计报告结构（候选点字段模板见
[evidence-gates.md](references/evidence-gates.md)）：方向声明、全量差异清单（含分类与
考古结论）、双门槛过滤后的候选优化点、观察项与合理差异归档、未实测声明（候选点实测前
一律 `gain_pct=null`）。

## Anti-patterns

| # | 反模式 | 正确做法 |
|---|--------|---------|
| 1 | 见架构差异就报优化点 | 双门槛：无 stated reason + 热点路径可数成本 |
| 2 | 把"配置已改"当"优化已生效" | 重启后核对运行时证据（日志/计数/cmdline） |
| 3 | 冷路径差异当优化点 | 先证明负载路径再谈优化 |
| 4 | 双方实现都错时硬选一边 | 报告并建议上游重构，不冒充 parity 修复 |
| 5 | 用 CPU 利用率下降证明业务收益 | 业务指标 + 事件计数双证据 |
| 6 | 假设 x86_64 永远是成熟侧 | 第 1 步先定方向，再谈对齐 |
| 7 | 考古不到理由就编一个 | 考不到就是没有，如实记录进候选池 |
| 8 | 只审计单侧代码 | 必须双架对照盘点，单侧视角看不见差异 |

## References

- [diff-taxonomy.md](references/diff-taxonomy.md) — 差异分类学（A 默认值 / B 模拟机制 / C 能力假设 / D 合理差异）、高价值差异域与盘点方法
- [evidence-gates.md](references/evidence-gates.md) — 双门槛判定标准、可数证据清单、误报清单与报告模板
- [case-studies.md](references/case-studies.md) — 实证案例库：guest console 退化（公开 PR 全链）、RCU 模式默认值漂移、预读与随机访问冲突、死参数负样本
