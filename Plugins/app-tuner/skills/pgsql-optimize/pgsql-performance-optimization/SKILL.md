---
name: pgsql-performance-optimization
description: >
  PostgreSQL 17/18 性能优化实施：GUC 调参（计划缓存 plan_cache_mode、提交链
  commit_delay/synchronous_commit、bgwriter/checkpoint 组、autovacuum）、
  NUM_BUFFER_PARTITIONS 编译期扩容（生日碰撞数学、补丁与重编）、shared_buffers
  元数据代价（哈希表/描述符工作集）、HNSW/pgvector 检索路径优化（ef_search、
  pg_prewarm、CAS 竞争观测）、编译选项（LTO/LSE/outline-atomics）；每项含机制、
  适用条件、风险、回滚与 A/B 验证口径（≥3 轮、<5% 判噪声）；含
  verify_build_opts.sh。触发词：PG 调优、PG 性能优化、NUM_BUFFER_PARTITIONS、
  shared_buffers、commit_delay、plan_cache_mode、HNSW 优化、LTO、LSE。
license: MulanPSL-2.0
compatibility: PostgreSQL 17.5 / 18.3（tag REL_17_5 / REL_18_3）实测口径；aarch64/鲲鹏为主，方法通用
metadata:
  skill_type: optimization
  category: guideline
  domain: relational_database
  activation_mode: manual
  side_effect_free: "false"
  version: "1.0.0"
---

# PostgreSQL 17/18 性能优化

> 定位：主动执行类（改 GUC/打补丁/换编译）。**先有证据再动手**：每项优化的入口
> 都要求 hotspot-analysis 的归因证据对上机制；每项变更走单变量 + A/B + 回滚。

## 何时使用

- 热点归因完成后要把证据变成改动（调参/补丁/编译选项）；
- 评估候选收益与风险（要不要做、先做哪个）；
- 负结果记录与回滚（防重复试错）。

## 核心纪律

1. **先有基线**：同一数据/配置/绑核/压测口径，≥3 轮（与 test-verification 协议
   一致，含逐轮明细归档）；
2. **一次只改一个变量**：GUC、编译选项、源码补丁分开验证；
3. **正确性门禁**：冒烟/对账/make check 不通过直接否决，不看性能；
4. **收益 <5% 判噪声**；负收益必须回滚并记录；
5. **记录身份**：版本 tag、configure 串、二进制 md5、GUC diff——A/B 可复现；
6. **机制优先**：先消除竞争结构（锁等待/淘汰链/自旋），再谈指令级微优化；
7. **补丁/产物可回退**：补丁原件与审计记录放源码树外（`${PATCH_DIR}` 默认
   `${PG_INSTALL}/patches`）；重编只 `make clean` 清构建产物，**禁止
   `git clean -xfd` 清整棵源码树**——会删补丁、审计记录与本地文件，使回滚失效。

## 变量表（与 build-deployment/test-verification 同口径）

| 变量 | 含义 | 默认值 |
|------|------|--------|
| `${PG_SRC}` / `${PG_INSTALL}` / `${PG_CONFIG}` | 源码树 / 安装前缀 / pg_config | `/opt/postgresql` / `/opt/pgsql` / `${PG_INSTALL}/bin/pg_config` |
| `${BENCH_OUT}` / `${BIN_BACKUP_DIR}` / `${PATCH_DIR}` | 压测输出 / 二进制备份 / 补丁目录（**必须在源码树外**） | `./sysbench_out` / `/opt/pgsql/binbak` / `${PG_INSTALL}/patches`（即 `/opt/pgsql/patches`，不在 `${PG_SRC}` 内） |

## 参考索引（渐进披露）

| 文件 | 内容 |
|------|------|
| `references/buffer-and-lock-optimizations.md` | NUM_BUFFER_PARTITIONS 扩容（生日碰撞数学/补丁）、shared_buffers 元数据代价、验证协议 |
| `references/hnsw-and-vector-optimizations.md` | HNSW 检索路径、ef_search/pg_prewarm、CAS 竞争观测、pgvector 口径 |
| `references/wal-and-plan-optimizations.md` | 计划缓存、提交链（组提交/持久性权衡）、bgwriter/checkpoint 组、autovacuum |
| `references/compiler-flags-and-verification.md` | LTO/LSE/outline-atomics、产物验证口径（脚本使用说明） |

## 输出契约

每轮优化产出：

1. **目标与证据**：对应哪个机制（hotspot 归因链）、预期方向；
2. **变更清单**：GUC diff / 补丁文件 / configure 串，单变量；
3. **A/B 结果**：三负载 TPS/p95/err 逐轮明细与汇总、波动与判定；
4. **判定与回滚**：有效（保留）/噪声（记录）/劣化（回滚命令）。

## 维护约定

- 每项候选必须写清适用条件与反例（负结果保留，防重复试错）；
- 实测数字只在与素材同口径的系列内引用，跨系列不横比；
- 机制表述与 pgsql-architecture 同口径。

## 交接说明

姊妹 skill：`pgsql-architecture`（机制知识库）、`pgsql-build-deployment`（编译
部署）、`pgsql-test-verification`（测试验证）、`pgsql-hotspot-analysis`（热点
分析）。同属 pgsql-optimize 领域分组，由同一系列 PR 交付（源码位于
`Plugins/app-tuner/skills/pgsql-optimize/`，`install.sh` 会把五个成员平铺安装到
`<tool>/skills/<name>/`，安装后入口即可用——该安装行为随分组 DOMAIN.md 合入后
生效；此前 install.sh 对缺 DOMAIN.md 的领域分组按设计整组跳过，入口暂不可用属
预期窗口期）。找不到姊妹入口时把优化记录（变更/A/B/判定）交回维护者。
