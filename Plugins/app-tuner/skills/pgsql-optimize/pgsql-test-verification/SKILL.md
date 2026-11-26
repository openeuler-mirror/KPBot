---
name: pgsql-test-verification
description: >
  PostgreSQL 17/18 正确性与性能验证：只读 SQL 冒烟（版本/扩展/计数/等待事件，含
  smoke_test.sh）、源码 ST/UT 测试（make check / check-world / src/test 的 isolation
  与 recovery、t/*.pl TAP 测试）、sysbench 三负载（oltp_read_only/read_write/
  write_only）基准与预热口径、观测扩展对账（pg_stat_statements 的 top SQL 与
  plans/total_calls、pg_buffercache、自研 pg_buffer_stats 视图）、A/B 同口径
  复测与噪声判定（≥3 轮、逐轮归档、<5% 视为噪声）。触发词：PG 冒烟、make check、
  TAP 测试、sysbench、PG 基准、pg_stat_statements、A/B 对比、正确性验证。
license: MulanPSL-2.0
compatibility: PostgreSQL 17.5 / 18.3、sysbench 1.0.20 实测口径；aarch64/鲲鹏与 x86_64 Linux 通用
metadata:
  skill_type: verification
  category: guideline
  domain: relational_database
  activation_mode: manual
  side_effect_free: "false"
  version: "1.0.0"
---

# PostgreSQL 17/18 测试验证

> 定位：主动执行类（只读冒烟 + 压测 + 源码测试）。正确性门禁先于性能：功能冒烟/
> 对账不通过直接否决，不看吞吐。本 skill 不做热点归因（→ pgsql-hotspot-analysis）
> 与优化决策（→ pgsql-performance-optimization）。

## 何时使用

- 部署/换二进制/打补丁后的功能冒烟（`scripts/smoke_test.sh`）；
- 源码级变更后跑 PG 自带 ST/UT（make check / TAP）；
- 建立性能基线与 A/B 对比（sysbench 三负载，同口径 ≥3 轮）；
- 用观测扩展对账（谁最耗时、缓冲池命中率、锁竞争视图）。

## 变量表（脚本与正文统一引用）

| 变量 | 含义 | 默认值 |
|------|------|--------|
| `${PG_BIN}` / `${PG_CONFIG}` | bin 目录 / pg_config | `/opt/pgsql/bin` / `${PG_BIN}/pg_config` |
| `${PGHOST}` / `${PG_PORT}` / `${PGUSER}` / `${DBNAME}` | 连接参数 | `127.0.0.1` / `5432` / `postgres` / `sbtest` |
| `${PGDATA}` | 数据目录（源码测试自起临时实例用） | `/opt/pgsql/data` |
| `${PG_SRC}` | 源码树（ST/UT） | `/opt/postgresql` |
| `${SYSBENCH_TABLES}` / `${SYSBENCH_ROWS}` | sysbench 规模 | `64` / `10000000` |
| `${BENCH_OUT}` / `${BENCH_SIDE}` | 压测输出根 / A-B 侧标识（base/opt，目录隔离防截断） | `./sysbench_out` / `base` |
| `${BENCH_THREADS}` / `${BENCH_TIME}` | sysbench 线程数 / 单轮时长（正整数，运行前校验） | `64` / `60` |
| `${EXPECT_WRITABLE}` | 冒烟 4b 写可用性检查开关（默认关；写负载/写环境须显式 =1——核 pg_is_in_recovery 与 transaction_read_only） | `0` |

凭据只经 `PGPASSWORD` 环境变量或 `~/.pgpass`；psql 不把口令放命令行（ps 可见）。

## 核心纪律

1. **正确性门禁先行**：冒烟（版本/扩展/计数/等待事件异常）不通过，性能数字无效；
2. **A/B 同口径**：两侧的 GUC（含观测扩展开关）、数据、预热、轮次、压测参数
   完全一致，只差目标变量——观测工具自身有成本（pgss_store 实测 ~1.8-5%），
   开关不一致即口径污染；
3. **≥3 轮 + 逐轮归档**：每场景逐轮结果（TPS/p95/err）入证据包，剔首轮预热，
   均值与波动同报；收益 <5% 一律判噪声（与 nebula/pggraph 家族同口径）；
4. **退出码即门禁**：smoke_test.sh 存在 FAIL 退出码非零，CI/脚本链据此拦截。

## 参考索引（渐进披露）

| 文件 | 内容 |
|------|------|
| `references/smoke-and-source-tests.md` | 只读冒烟、make check/check-world、TAP（t/*.pl）与 isolation/recovery 测试 |
| `references/sysbench-baselines.md` | 三负载准备与运行、预热、轮次与噪声判定、A/B 协议 |
| `references/observation-extensions.md` | pg_stat_statements/pg_buffercache/pg_buffer_stats 视图的判读与对账口径 |

## 输出契约

验证报告至少给出：

1. **身份**：版本 tag、configure 串、二进制 md5、GUC diff（对照组/实验组）；
2. **正确性结果**：冒烟逐项 PASS/FAIL、（若有源码变更）make check 结论；
3. **性能结果**：三负载 TPS/p95 err 的逐轮明细与均值、波动、判定（有效/噪声）；
4. **结论口径**：收益百分比 + 轮数 + 噪声阈值，同一系列内比较。

## 维护约定

- sysbench 参数与预热口径固定后不随轮次漂移；改口径=重新基线；
- 观测扩展的视图/函数名以安装版为准（自研扩展版本随源码）。

## 交接说明

姊妹 skill：`pgsql-architecture`、`pgsql-build-deployment`、
`pgsql-hotspot-analysis`、`pgsql-performance-optimization`。同属 pgsql-optimize
领域分组，由同一系列 PR 交付（源码位于
`Plugins/app-tuner/skills/pgsql-optimize/`，`install.sh` 会把五个成员平铺安装到
`<tool>/skills/<name>/`，安装后入口即可用——该安装行为随分组 DOMAIN.md 合入后
生效；此前 install.sh 对缺 DOMAIN.md 的领域分组按设计整组跳过，入口暂不可用属
预期窗口期）。找不到姊妹入口时不要凭空继续：把验证结论与证据包交回维护者。
