---
name: pggraph-performance-optimization
description: >
  pgGraph 0.1.8 性能优化（配置 + 编译 + 源码）：连接/并发与连接池、PostgreSQL shared_buffers/
  work_mem/jit/synchronous_commit、csr_readonly vs mutable_overlay、graph.memory_limit_mb/
  build_batch_size/edge_buffer_size/persist_on_build/auto_load/query_freshness、NUMA/CCL/SMT；
  Rust release/LTO/codegen/target-cpu/SVE 自动向量化；源码层 UnionFind 迭代+path halving、
  node_deleted 快速路径、component_sizes Vec、traverse 数值排序、hydrate/String/SPI/JSON、
  反向 CSR 共享、CRC 跳过等候选，以及实测收益、适用条件、风险和回滚。用于把热点证据转成
  单变量优化并验证；不修改 OS/BIOS/网卡（交给 kpbot-app-tuner 子技能），不做通用 Rust/C++
  微架构开发（交给 kpbot-code-optimizer）。触发词：pgGraph 优化、pgGraph 调优、pgGraph 参数、
  pgGraph 源码优化、UnionFind、node_deleted、component_sizes、target-cpu、SVE、反向 CSR、
  result_checksum、QPS 提升。
license: MulanPSL-2.0
compatibility: pgGraph 0.1.8；PostgreSQL 18.3、Rust 1.96.0、pgrx 0.19.1、aarch64/鲲鹏实测；x86_64 方法通用但收益不同
metadata:
  skill_type: tool_assisted
  category: tuning
  domain: graph_database
  version: "1.0.0"
---

# pgGraph 性能优化（配置 + 编译 + 源码）

## 简介

优化不是“把参数调大/把代码改快”，而是：

```
基线 → 瓶颈归类（hotspot）→ 选择唯一杠杆 → 单变量变更 → 正确性门禁 → 同口径 A/B → 保留/回滚
```

本 skill 提供四类杠杆：

1. **部署/连接**：连接池、活跃并发、NUMA/CCL/SMT、PG 内存参数。
2. **配置**：build 模式、memory limit、batch、edge buffer、persist/auto_load、freshness。
3. **编译/Rust**：release/LTO、`target-cpu`、SVE 自动向量化。
4. **源码**：UnionFind、node_deleted、component_sizes、数值排序、hydrate/String/SPI、
   反向 CSR/CRC 等候选。

原则：**部署/配置杠杆通常比源码杠杆大一个数量级；先把瓶颈定位对，再动手。**

## 何时触发

- 已通过 `pggraph-hotspot-analysis` 找到瓶颈，需要把“热点”变成“可验证的改动”。
- 要调连接数/连接池、PG 内存、build 模式、内存限制、batch、edge buffer、freshness。
- 要切换 release/LTO/`target-cpu`，评估 SVE 自动向量化。
- 要实现/评估源码优化（UnionFind、node_deleted、component_sizes、排序、hydrate/String）。
- 优化后出现负收益，需要判断回滚、换杠杆还是归因错误。

**不适用**：OS/BIOS/网卡/CPU 亲和性等系统层 → `kpbot-app-tuner` 的 `os-optimization` /
`bios-optimization` / `network-optimization` / `cpu-affinity-optimization`；
通用 Rust/C++ 微架构/向量化开发 → `kpbot-code-optimizer`；部署/编译本身 →
`pggraph-build-deployment`；验证方法 → `pggraph-test-verification`。

> 交接说明：上述姊妹 skill 与本 skill 同属 pggraph-optimize 领域分组，由同一系列 PR 交付
> （源码位于 `Plugins/app-tuner/skills/pggraph-optimize/`，`install.sh` 会把五个成员平铺
> 安装到 `<tool>/skills/<name>/`，安装后入口即可用——该安装行为随系列第 1 个 PR 交付的
> `pggraph-optimize/DOMAIN.md`（install.sh 依赖它识别分组）合入后生效；此前 install.sh
> 对缺 DOMAIN.md 的领域分组按设计整组跳过，已合入成员暂不安装，属预期窗口期）。若当前环境找不到对应入口
> （未安装/未合入），把优化候选清单、A/B 数据与结论连同待办交回维护者，由其安装对应
> skill 或人工继续。

## 核心纪律

1. **先有基线**：同一数据/配置/绑核/压测口径，≥3 轮。
2. **一次只改一个变量**：参数、编译选项、源码补丁分开验证。
3. **正确性门禁**：SQL JOIN/不变量/checksum 不通过，直接否决，不看性能。
4. **分场景判定**：不能用 component_stats 的提升掩盖 traverse 的回退。
5. **收益 <5% 视为噪声**；负收益必须回滚并记录。
6. **记录身份**：`.so` SHA256/commit、编译参数、数据集、工件、连接/绑核。
7. **区分“已实测”与“候选/预期”**：反向 CSR 共享、CRC 跳过等未实现项不得写成已收益。

## 参考索引

| 文件 | 何时读 |
|------|--------|
| `references/config-and-deployment.md` | 连接/并发、PG 参数、build 模式、GUC、NUMA/CCL/SMT、容量扫描 |
| `references/compiler-and-rust.md` | release/LTO、`target-cpu`、SVE 自动向量化、指令集验证、可移植性 |
| `references/source-optimizations.md` | UnionFind、node_deleted、component_sizes、数值排序、hydrate/String/SPI、反向 CSR/CRC |
| `references/negative-results-and-decisions.md` | 负结果（密集位图/预取/SVE gather）、回滚、决策矩阵、记录模板 |
| `scripts/verify_build_opts.sh` | 只读巡检（恒 exit 0、缺失降级提示）；`--strict` 作门禁：pg_config/graph.so/objdump 任一缺失即非零退出 |

## 实测收益速查（源资料，特定环境）

| 优化 | 目标 | 实测变化 | 状态 |
|------|------|----------|------|
| UnionFind 迭代 + path halving | component_stats | -18.6% | 已实测 |
| node_deleted 快速路径 | component_stats/BFS | component -19.6% | 已实测 |
| component_sizes HashMap→Vec | component_stats | component -6.8%；总 -1.0% | 已实测 |
| traverse 排序键 node_id→node_idx | 2-hop traverse | -20~29% | 已实测 |
| A+B+C+H 合并* | TOTAL | 1405→972ms（-30.8%），吞吐 +42.5% | 已实测 |
| `target-cpu=native` 的 SVE 自动向量化** | TOTAL | -2.5% | 已实测 |
| 手动 SVE gather find | component | 慢 20% | 负结果 |
| SVE gather is_active | BFS | 慢 9~39% | 负结果 |
| ARM prfm 预取 | BFS | 慢 18% | 负结果 |
| 密集位图替代 RoaringBitmap | BFS | 慢 28% | 负结果 |
| 反向 CSR 持久化/mmap 共享 | 并发私有内存 | 预期大幅下降 | **未实现候选** |
| 跳过 CRC/trust checksum | 加载 | 预期减少加载 | **未实现候选，有风险** |

数字只在同一数据集/环境/口径内比较；源资料存在 3.18M/34.5M 与 100K/500K 两种规模口径，
不可横比。

> \* **代号定义**（与本表前四行一一对应，全文通用）：A = UnionFind 迭代 + path halving、
> B = node_deleted 快速路径、C = component_sizes HashMap→Vec、H = traverse 排序键
> node_id→node_idx。A+B+C+H 指四项叠加。
>
> \*\* **两套测量系列，不可互串**：A+B+C+H 的 1405→972ms（-30.8%）来自默认轮次系列
> （源资料 source-optimizations §收益表）；SIMD 的 948.7→925.2ms（-2.5%）来自
> "A+B+C+H 基础上、40 次迭代"系列（compiler-and-rust §target-cpu 实测表，其基线
> 948.7ms 与前一系列的 972ms 差 2.4%，属不同轮次/迭代数的口径差）。决策矩阵中
> "-30.8% vs -2.5%"为两系列各自的相对收益对照，不代表两系列绝对值可比。

## 输出契约

优化结论至少给出：

1. **基线**：身份、数据集、口径、每查询 median/p95、TOTAL/QPS。
2. **瓶颈与杠杆**：hotspot 证据 → 选择的唯一变量；为什么不是别的。
3. **变更**：参数/编译/补丁 diff；影响面与风险。
4. **正确性**：SQL JOIN/不变量/checksum 结果；失败即回滚。
5. **性能**：同口径 A/B、轮次、离散度、每场景变化。
6. **结论**：保留/回滚/待定；回滚方式；负结果记录。

## 维护约定

- 路径/端口/用户/数据变量化；不写个人 home、挂载盘、私网 IP；凭据不进 argv/文件。
- 源码补丁注明文件/函数/适用版本；先备份再改；保留原始对照。
- 任何“预期收益”必须标注未验证；不要把计划写成结论。
- 变更后重跑正确性用例与完整性校验。

## 完整性校验

用例位于姊妹 skill `pggraph-architecture/cases/skill-integrity/`（随系列首 PR 交付）：

```bash
# 源码树中，且 pggraph-architecture 已合入（系列首 PR）时可执行：
bash ../pggraph-architecture/cases/skill-integrity/run.sh
# 缺失的姊妹 skill 记为 pending 而非失败；退出码 0 = 已合入成员全部通过
```

若环境中不存在该脚本（架构 skill 未合入/安装平铺布局），跳过本节，不要把
"No such file" 误报为校验失败。
