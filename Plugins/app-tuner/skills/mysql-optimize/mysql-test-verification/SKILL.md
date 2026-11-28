---
name: mysql-test-verification
description: >
  MySQL 8.0 的正确性与性能验证：构建产物核对、MTR（mysql-test-run.pl 全量/单例/suite、
  参数、失败定位、用例编写、debug sync、告警抑制、core 调试）、功能冒烟（DDL/DML/事务/
  隔离/字符集/回滚）、数据对账、sysbench point/只读/读写/只写四场景（参数、数据集、
  预热、并发、轮次）、指标口径（TPS/QPS、latency min/avg/max/P95、errors、CPU）、
  客户端/服务端瓶颈判别、A/B 同口径与噪声判定、结果归档。用于验证“功能是否正确、
  性能是否真的变好”；不适用编译部署（→ mysql-build-deployment）、热点归因
  （→ mysql-hotspot-analysis）、优化实施（→ mysql-performance-optimization）。
  触发词：MySQL 测试、MySQL 验证、MTR、mysql-test-run、sysbench、oltp_read_only、
  oltp_read_write、TPS、QPS、P95、A/B、正确性、回归。
license: MulanPSL-2.0
compatibility: MySQL 8.0.25/8.0.28；MTR 随源码；sysbench 1.0.20；鲲鹏 920/920G + openEuler 22.03/24.03 实测，x86 客户端通用
metadata:
  skill_type: tool_assisted
  category: testing
  domain: relational_database
  version: "1.0.0"
---

# MySQL 正确性与性能验证

## 简介

验证分两层，缺一不可：

1. **正确性**：构建可用、MTR/冒烟通过、事务与字符集语义正确、数据对账无缺失。
2. **性能**：在固定口径下测 TPS/QPS/延迟/CPU，做基线 vs 候选的 A/B，判断收益是否超出噪声。

原则：**没有正确性的性能数字无效；没有同口径基线的“提升”不可信。**

## 何时触发

- 编译/部署/初始化后，确认服务可用、参数生效、基本 SQL 正确。
- 改源码、打补丁、调参数、换二进制、升级依赖后，判断功能是否回归、性能是否变化。
- 需要建立或复现 point/RO/RW/WO 基线，或比较容器/虚机/物理机。
- 压测数字异常（TPS 上不去、CPU 不饱和、P95 长尾、错误率上升），判断服务端还是客户端。
- 需要可归档的验证报告（功能证据 + 性能证据 + 口径 + 结论）。

**不适用**：安装/编译/初始化本身 → `mysql-build-deployment`；perf/等待归因 →
`mysql-hotspot-analysis`；具体优化杠杆实施 → `mysql-performance-optimization`。

## 验证矩阵

| 层级 | 手段 | 依赖 | 能回答什么 |
| --- | --- | --- | --- |
| 构建 | 产物/版本/符号/库/md5 | 编译产物 | 编出来的东西是否可信 |
| 单元/回归 | MTR 全量或指定 suite | 源码 `mysql-test/` + 构建 | 功能/复制/崩溃是否回归 |
| 端到端功能 | 冒烟 SQL（DDL/DML/事务/字符集） | 运行实例 + 隔离库 | 基本语义是否正确 |
| 数据 | 计数、抽样、校验和 | 数据集 + schema | 数据是否完整/一致 |
| 性能 | sysbench point/RO/RW/WO | 实例 + 压测客户端 | 端到端 TPS/QPS/延迟 |
| 判定 | A/B 同口径、多轮、噪声阈值 | 基线 + 候选 | 收益是否真实、是否保留 |

## 工作流

```
① 构建验证 → ② MTR（按改动范围选 suite）→ ③ 功能冒烟 → ④ 数据对账/预热
  → ⑤ 系统基线（≥3 轮）→ ⑥ 候选复测（同口径）→ ⑦ 判定与归档
```

任何一步失败，停止性能测试；先修正确性。

## 功能验证（MTR）摘要

- `mysql-test-run.pl`（软链接 `mtr`）负责选例、启停 mysqld、调用 `mysqltest` 比对
  `t/*.test` 与 `r/*.result`；差异生成 `*.reject`（串行在 `var/log`，`--parallel>1` 在
  `var/<N>/log/`）。
- 以非 root 用户在 `${MYSQL_SRC}/mysql-test` 运行：全量 `--big-test --mem --parallel=8`，
  按改动范围用 `--suite=main,innodb`、`test1 test2` 或 `--do-test-list=file`。
- **`mtr | tee` 必须显式保留 MTR 退出码**：启用 `pipefail` 并核对 `${PIPESTATUS[0]}`，
  否则用例失败而 `tee` 成功会让自动化继续往下跑：

```bash
set -o pipefail
cd "${MYSQL_SRC}/mysql-test"
./mtr --force --timestamp --max-test-fail=0 --suite-timeout=6000 \
  --testcase-timeout=300 --retry=3 --parallel=8 --big-test --mem 2>&1 | tee mtr.log
mtr_rc=${PIPESTATUS[0]}
if [ "${mtr_rc}" -ne 0 ]; then echo "mtr failed: ${mtr_rc}" >&2; exit "${mtr_rc}"; fi
```

- 失败定位：串行看 `var/log/*.reject`、`var/log/mysqld.1.err`；`--parallel>1` 看
  `var/<N>/log/`（`N` 为报告中的 worker 号），`var/log/<suite>.<test>/` 只是失败现场存档。
  复现用 `./mtr --start --mysqld=--gdb` / `--manual-gdb`；core 用
  `gdb ${MYSQL_HOME}/bin/mysqld core.<pid>`；已知告警用 `call mtr.add_suppression("...")`；
  `--record` 只在确认语义正确后使用。
- 参数全集、`--error`/`--replace_regex` 等用例语法、`*.cnf`/`combinations` 配置、
  debug sync 与 core 调试细节 → `references/functional-and-mtr.md`。

## 功能冒烟（运行实例）摘要

`scripts/smoke_test.sh` 默认只读；`--write-test` 在隔离库做写入验证，退出前由 EXIT trap
删除临时库，清理失败也会让脚本非零退出。

```bash
MYSQL_SOCKET="${SOCKET}" MYSQL_USER=root \
  MYSQL_DEFAULTS_EXTRA_FILE="${CRED_FILE}" \
  bash scripts/smoke_test.sh            # 只读
MYSQL_SOCKET="${SOCKET}" MYSQL_USER=root \
  MYSQL_DEFAULTS_EXTRA_FILE="${CRED_FILE}" \
  bash scripts/smoke_test.sh --write-test   # 写入验证（隔离库，自动清理）
```

- 只读清单：连接与版本、基础视图（information_schema）、关键参数与状态
  （buffer pool/page size/sql_mode/charset/read_only）、Buffer Pool 命中率、
  `SHOW ENGINE INNODB STATUS` 可读、`SELECT 1`（同时核对退出码与实际返回值 1）。
- 写入清单（隔离库）：`CREATE DATABASE mtr_smoke_<pid>` → 主键/二级索引/utf8mb4 表 →
  多行 INSERT → 回读计数 → 事务 ROLLBACK → 唯一键冲突按预期失败 → `ORDER BY` 结果 →
  `DROP DATABASE` 清理。
- 写入验证不得在生产库执行；口令从 `--defaults-extra-file` 读取，且该选项必须排在其它
  客户端选项之前。清单与失败定位细节 → `references/functional-and-mtr.md` §7。

## 性能验证（sysbench）摘要

| 场景 | sysbench 命令 | 事务构成 | 主要瓶颈 |
| --- | --- | --- | --- |
| Point | `oltp_read_only --range_selects=0 --skip-trx=1` | 仅 point select | SQL/页查找/网络/IRQ |
| RO | `oltp_read_only` | point:simple:sum:order:distinct = 10:1:1:1:1 | 读路径 + collation/排序 |
| RW | `oltp_read_write` | RO + index/non-index update + delete_insert | 锁、redo、binlog、刷脏 |
| WO | `oltp_write_only` | index/non-index update + delete_insert | redo/binlog fsync、doublewrite |

- 数据集：64 表 × 1000 万行（`64t_1000w`）或 20 表 × 100 万（`20t_100w`）；`prepare`
  后**按测试库与实际表数逐表执行 `ANALYZE TABLE`**（MySQL 不接受库名/表名通配符）；
  正式测前只读预热约 600s，每轮之间等 CPU/IO 降下来。
- 指标口径：TPS/QPS 分开；延迟至少给 P95（sysbench 用 `--percentile=95`，写场景加
  P99/max）；读场景 errors 应为 0；
  同时记录服务端/客户端 CPU、IRQ/iowait。定义与报告模板 →
  `references/ab-protocol-and-metrics.md`。
- A/B 纪律：同一数据快照/配置/二进制/线程/时长，只改一个变量；每腿 ≥3 轮，轮间波动
  >5% 视为无效重测；收益 ≥5% 且多轮稳定才保留，负收益也要归档。细节 →
  `references/ab-protocol-and-metrics.md`。
- 口令不进 argv：sysbench 1.0.20 **没有** defaults-file 类客户端选项；本机用
  `--mysql-socket="${MYSQL_SOCKET}"` 指定实例，口令经受控环境变量（如 `MYSQL_PWD`，
  注意 `/proc` 可见性）注入并在报告中脱敏；其他版本确需配置文件时先用
  `sysbench --help` 核实选项。
- 客户端与服务端分机；多客户端时汇总 TPS，并分别记录客户端 CPU。

## 常见失效模式（速查）

| 现象 | 可能原因 | 处置 |
| --- | --- | --- |
| TPS 锁死、客户端 CPU 满 | 客户端先瓶颈（PS/解码/单机） | 加压测机/进程，降结果集 |
| 服务端 CPU 不满、网络低 | 并发不足/客户端慢/IRQ 集中 | 加并发、查 IRQ/客户端 |
| 写场景越跑越慢 | 脏页/checkpoint/IO 未归零 | 每轮前等 IO；调 io_capacity |
| 换二进制后结果不同 | 语义回归/数据格式不兼容 | 停性能测试，跑 MTR/冒烟 |

完整矩阵与噪声判定 → `references/ab-protocol-and-metrics.md`。

## 输出契约

一份可归档的验证报告至少包含：

1. **环境与身份**：硬件/NUMA、OS/编译器、MySQL 版本/commit、二进制 md5、部署形态
   （物理机/容器/虚机）、cpuset/绑核、my.cnf 关键项。
2. **功能证据**：MTR suite/结果、冒烟 SQL 与期望、数据对账、失败样本。
3. **性能口径**：场景、语句、数据集、线程、时长、轮数、预热、数据重置、客户端位置。
4. **结果**：每场景 TPS/QPS、errors、latency min/avg/max/P95（必要时 P99）、
   服务端/客户端 CPU、IRQ/iowait。
5. **判定**：有效/无效/劣化；是否保留；未达标的下一步（指向 hotspot/optimization）。
6. **可复现命令**：完整命令或脚本路径，不依赖未记录的 shell 状态。

## 参考资料与脚本

细节按下列文件按需读取，不在本文件复制全文：

| 文件 | 何时读 |
| --- | --- |
| `references/functional-and-mtr.md` | MTR 参数、用例语法、失败定位、告警抑制、core 调试、功能冒烟清单 |
| `references/sysbench-harness.md` | 场景脚本、数据集与 ANALYZE、预热、并发扫描、客户端瓶颈、容器/虚机注意事项 |
| `references/ab-protocol-and-metrics.md` | 指标定义、A/B 协议、噪声、报告模板、常见误判 |
| `scripts/smoke_test.sh` | 只读功能冒烟；`--write-test` 在隔离库做写入验证（含清理与失败退出） |
