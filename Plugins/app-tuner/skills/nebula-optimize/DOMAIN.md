# NebulaGraph 优化域（app-tuner 侧）

NebulaGraph 3.8.0 这个应用从**架构理解 → 编译部署 → 正确性/性能验证 → 热点归因 →
配置/源码优化**的完整调优链路。五个成员各自独立可调用，组合起来覆盖一次完整的
Nebula 性能优化闭环。

> `nebula-optimize/` 本身是源码树中的领域分组，不是独立 skill；安装时五个成员会被
> 平铺到 `<tool>/skills/<skill-name>/`，由 Claude Code / OpenCode 一层发现。

## 成员

| Skill | 职责 | 典型入口 |
|-------|------|---------|
| [`nebula-architecture`](nebula-architecture/SKILL.md) | 三服务架构、查询/写入链路、Thrift/端口、MetaClient、Raft、RocksDB Key 编码、线程与 folly 模型、配置地图、源码路径（被动只读知识库） | “这条查询怎么走到 RocksDB”“Key 怎么编码”“Go2 为什么瓶颈在 storaged” |
| [`nebula-build-deployment`](nebula-build-deployment/SKILL.md) | aarch64 编译、third-party 3.3/jemalloc、standalone/1P 分布式、NUMA/端口/配置模板、LDBC 导入、备份恢复、版本切换与回滚；含只读 `preflight_check.sh` | “从零部署”“恢复备份”“换二进制/升级” |
| [`nebula-test-verification`](nebula-test-verification/SKILL.md) | ctest/TCK/nGQL 冒烟/数据对账、LDBC 五场景 k6、双 k6 直连、C++/Python 基准、A/B 口径与噪声判定；含只读 `smoke_test.sh` | “优化有没有效果”“功能有没有坏”“压测怎么跑” |
| [`nebula-hotspot-analysis`](nebula-hotspot-analysis/SKILL.md) | perf/火焰图/off-CPU/Topdown/HTTP stats、线程与 folly 模型、五场景热点迁移与归因；含 dry-run `collect_profile.sh` | “慢在哪”“为什么 graphd CPU 高”“为什么 Go3 CPU 低” |
| [`nebula-performance-optimization`](nebula-performance-optimization/SKILL.md) | 部署/线程/缓存/WAL/compaction 配置、LTO/LSE/jemalloc、Value SSO/BFS flat_map/哈希/迭代器/schema 缓存及负结果；含 `verify_build_opts.sh` | “怎么调”“改哪里”“收益多少”“负收益怎么回滚” |

## 域内路由

```
只想理解系统            → nebula-architecture
要从零装/恢复/升级      → nebula-build-deployment
要证明对/证明快         → nebula-test-verification
要知道慢在哪/为什么慢   → nebula-hotspot-analysis
要把证据变成改动        → nebula-performance-optimization
```

一次典型的优化闭环：

```
architecture（理解链路）
  → build-deployment（起环境、导数据、验收）
  → test-verification（基线 + 正确性门禁）
  → hotspot-analysis（定位瓶颈机制）
  → performance-optimization（单变量优化）
  → test-verification（同口径 A/B）
  → 保留或回滚
```

## 与插件内其他 skill 的边界

| 问题 | 去哪里 |
|------|--------|
| OS governor/THP/大页/irqbalance/ulimit | `kpbot-app-tuner` 的 `os-optimization` |
| BIOS SMT/C-State/NUMA/内存频率 | `bios-optimization` |
| 网卡队列/RSS/RPS/中断 | `network-optimization` |
| CPU 绑核/NUMA 布局/线程亲和 | `cpu-affinity-optimization` |
| 通用 C/C++ 向量化/循环/汇编 | `kpbot-code-optimizer` |
| Nebula 专属 Key/容器/Value/迭代器/schema 改动 | 本域 `nebula-performance-optimization` |
| 纯部署/编译/导数据 | 本域 `nebula-build-deployment` |
| 纯压测/正确性/A/B | 本域 `nebula-test-verification` |

## 准入判据

新增成员须满足：

- 结论落在 **NebulaGraph 应用自身**（架构、部署、验证、热点、配置/源码优化），
  不是通用 OS/网络/编译理论。
- 有明确的输入/输出契约、停止条件、失败处理与回滚方式。
- 涉及变更的内容必须区分只读采集、dry-run、低风险在线变更、需重启变更、需重编译变更。
- 有可复现的验证入口（evals 或 skill 内 `cases/`）。
- 不写入个人环境绝对路径、私网 IP、凭据；示例统一用变量占位。

## 安装与校验

- 领域分组只存在于源码树；`install.sh` 会将五个成员平铺安装。
- 完整性校验（结构、链接、锚点、evals、敏感信息）：
  `bash nebula-architecture/cases/skill-integrity/run.sh`
- 所有成员均提供 `evals/evals.json`；变更后应重跑完整性用例与仓库测试。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/nebula-optimize/**`
