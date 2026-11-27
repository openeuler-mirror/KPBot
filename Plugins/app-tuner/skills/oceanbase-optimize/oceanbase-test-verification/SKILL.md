---
name: oceanbase-test-verification
description: >
  OceanBase 的正确性与性能验证：编译/部署验收、DBA 视图与 GV$ 指标核对、SQL 冒烟
  （计数/抽样/写回读/事务/主副本）、sysbench 1.0.20 五场景（write_only、read_only、
  point_select、insert、update_non_index）、交易创建/交易支付/账务业务模型、32U/64U/128U
  与单实例/多实例/三副本、obproxy 连接、指标口径（QPS/TPS、成功率、avg/P95/P99、CPU、
  客户端瓶颈）、A/B 同口径复测与噪声判定、结果归档。用于验证“功能是否正确、性能是否
  真的变好”；不适用编译部署（→ oceanbase-build-deployment）、热点归因
  （→ oceanbase-hotspot-analysis）、参数/源码优化实施
  （→ oceanbase-performance-optimization）。
  触发词：OceanBase 测试、OB 验证、OB 压测、OB benchmark、sysbench、obclient、
  write_only、read_only、point_select、update_non_index、QPS、TPS、P99、A/B 对比、
  正确性验证、数据对账。
license: MulanPSL-2.0
compatibility: OceanBase CE 4.3.5 源码树 4.3.5_CE_BP5_HF2；sysbench 1.0.20 + LuaJIT 2.1；obclient 2.2.x；鲲鹏 950/920B/7270Z、AMD 9T34 实测
metadata:
  skill_type: tool_assisted
  category: testing
  domain: relational_database
  version: "1.0.0"
---
# OceanBase 正确性与性能验证

## 简介

验证分两层：正确性（集群/租户/副本、查询与事务语义）和性能（固定口径的 TPS/QPS/延迟/CPU，做基线 vs 候选 A/B）。
原则：**没有正确性的性能数字无效；没有同口径基线的“提升”不可信。**

## 何时触发

- 编译/部署后确认节点、租户、Unit、副本与读写；参数/二进制/源码改动后判断收益或回退。
- 建立或复现 sysbench 五场景、业务模型基线（32U/64U/128U、单/多实例、直连/obproxy）。
- 压测数字异常——**只判数字是否可采信**（口径一致性/预热/轮数/噪声阈值），不做瓶颈归因
  （归因 → `oceanbase-hotspot-analysis`）。
- 需要可归档的验证报告（功能证据 + 性能证据 + 口径 + 结论）。

**不适用**：安装/编译/建租户 → `oceanbase-build-deployment`；火焰图与热点归因 →
`oceanbase-hotspot-analysis`；优化杠杆实施 → `oceanbase-performance-optimization`。

## 变量表（脚本与正文统一引用）

| 变量 | 含义 | 默认值 |
|------|------|--------|
| `${OB_HOST}` / `${OB_PORT}` | 连接参数（端口 1-65535 校验；社区 observer 的 MySQL 口默认 2881、RPC 口 2882，但端口以目标集群 `install.cfg` 为准；经 obproxy 填其监听端口） | `127.0.0.1` / `2881` |
| `${OB_USER}` / `${OB_DB}` | 系统租户用户 / 库 | `root@sys` / `oceanbase` |
| `${OB_PASSWORD}` | 系统租户口令（mktemp 0600 临时 defaults-file 注入，不进 argv/日志） | 空 |
| `${BIZ_USER}` / `${BIZ_DB}` / `${BIZ_PASSWORD}` | 业务租户账号/库/口令：直连 observer 写 `obtest@mysql_tenant`（带 `#集群名` 会连接报错），经 obproxy 写 `obtest@mysql_tenant#obcluster`；`SPACE_DB` 设置时 `BIZ_USER` 必填，口令走独立 defaults-file | 空 / 同 `${SPACE_DB}` / 空 |
| `${SPACE_DB}` / `${SAMPLE_TABLE}` / `${SAMPLE_COL}` | 业务库/抽样表/列（合法 SQL 标识符；`SAMPLE_COL` 须显式选列，默认只判可读、不输出原始行） | 空 |
| `${EXPECT_SERVERS}` / `${EXPECT_ZONES}` / `${EXPECT_TENANTS}` | 节点/Zone/租户数量期望（缺省只报不判） | 空 |
| `${EXPECT_TABLE_COUNT}` / `${EXPECT_ROWS}` | 表数/行数期望（缺省只报不判） | 空 |
| `${EXPECT_WRITABLE}` | 默认关；写验收显式 =1，用业务租户连接核 `read_only=OFF`，缺账号/口令 exit 2 | `0` |

## 验证矩阵与流程

逐层手段/依赖/能回答什么见 `references/functional-verification.md` §9。流程：①部署/元数据验收 →
②功能冒烟 → ③数据对账 → ④小规模微基准（可选）→ ⑤系统基线（多轮）→ ⑥候选复测
（同口径）→ ⑦判定归档；任一步失败即停止性能测试，先修正确性。

## 功能验证

### 部署与元数据

```
SELECT SVR_IP, SQL_PORT, SVR_PORT, WITH_ROOTSERVER, STATUS, BUILD_VERSION
  FROM oceanbase.DBA_OB_SERVERS;
SELECT * FROM oceanbase.DBA_OB_ZONES;
SELECT TENANT_ID, TENANT_NAME, PRIMARY_ZONE, LOCALITY, COMPATIBILITY_MODE, STATUS
  FROM oceanbase.DBA_OB_TENANTS;
SELECT u.UNIT_ID, u.TENANT_ID, c.NAME, c.MAX_CPU, c.MIN_CPU, c.MEMORY_SIZE
  FROM oceanbase.DBA_OB_UNITS u
  JOIN oceanbase.DBA_OB_UNIT_CONFIGS c ON u.UNIT_CONFIG_ID = c.UNIT_CONFIG_ID;
```

通过标准：节点全 `ACTIVE`、Zone 数与设计一致、业务租户 `NORMAL`、`primary_zone`/
`locality` 与 Unit 规格符合规划。副本数/主 Zone 是**手工核对项**（`DBA_OB_LS` + `LOCALITY`，
见 references §5/§6），不得把冒烟全 PASS 当作副本正确。

### SQL 冒烟

`scripts/smoke_test.sh` 是只读入口，覆盖版本/端口/节点/Zone/租户/Unit、关键参数
（限流 `writing_throttling_*` 不在脚本内，见 references §2 手工快照）、业务库表计数与抽样；
`EXPECT_WRITABLE=1` 时用**业务租户**连接核 `read_only`（缺账号/口令 exit 2），不覆盖副本/主 Zone。
系统租户 `root@sys` 与业务租户必须**分开连接**：`SPACE_DB` 只选库名、不切租户。
抽样默认只输出“可读”状态、不打印原始行；写回读在隔离库做，不在生产库跑。

`OB_PORT` 取目标集群 `install.cfg` 的 MySQL/SQL 端口（系列实测部署为 2882，官方默认 2881；
经 obproxy 时取其监听端口）：

```
OB_HOST='<observer_or_proxy>' OB_PORT=${MYSQL_PORT} OB_USER='root@sys' OB_DB=oceanbase \
  BIZ_USER='obtest@mysql_tenant' BIZ_DB=obtest SPACE_DB=obtest SAMPLE_TABLE='<table>' \
  bash scripts/smoke_test.sh
```

直连 observer 用 `obtest@mysql_tenant`；经 obproxy 才用 `obtest@mysql_tenant#obcluster`。
完整命令与期望值见 references §3/§8。

### 数据对账

导入/压测后按表统计行数并与期望比对；抽样核对类型、日期、字符串长度；事务核对总额守恒
与隔离级别行为。唯一键冲突（1062）在压测中属正常，需与“数据丢失/写失败”区分。
详细命令见 references §4/§5。

## 性能验证

场景（sysbench 五场景 + 交易创建/支付/账务业务模型）、工具链、指标口径与最小记录见
`references/benchmark-harness.md` §1–§5/§7/§9/§10。直连与 obproxy、单实例与多实例是不同
口径，不能混比。

A/B 纪律：同一数据快照/版本/并发/时长/绑核，只改一个变量；每腿 ≥3 轮，轮间波动 >5%
（P95/P99 >10%）重测；测运行参数两腿二进制摘要（md5/commit）必须一致，测编译选项/源码
补丁时分别记录各自摘要与编译参数；结果分列 TPS/QPS、成功率、avg/P95/P99、CPU
（observer/obproxy/client）、IO，不合并成一个“提升 X%”。完整判定、常见假收益与报告模板见
`references/ab-protocol.md` §1–§7。

## 常见失效模式

| 现象 | 处置 |
| --- | --- |
| TPS 锁死、sysbench CPU 打满 | 客户端先瓶颈：加客户端/减结果集/换场景 |
| 只读 QPS 低但 observer CPU 不满 | 查 Unit/worker/绑核/跨 NUMA |
| 写入 CPU 明显低于读场景但 TPS 不升 | off-CPU、线程 CPU、磁盘延迟（IO/ApplySrv/冻结） |
| 多实例性能差 10%+ / 128U 跑不满 | 网卡中断跨 NUMA / `net_thread_count` 不足 |
| 成功率下降、两轮差异大、新二进制结果不同 | 先查错误码/日志；重置数据、预热、固定环境；停性能测试并跑功能冒烟 |

完整表（现象/可能原因/处置）见 `references/ab-protocol.md` §8。

## 输出契约

报告至少包含：①环境与身份（硬件/NUMA、OS/内核、OB 版本与 commit、observer/obproxy md5、
编译参数、部署形态、Unit、`primary_zone`/`locality`、数据集）；②功能证据（节点/Zone/租户/
Unit、冒烟 SQL 与期望值、计数/抽样、事务回读、失败样本）；③性能口径（场景/lua、连接方式、
VU/线程/时长、绑核、轮数/预热、数据重置）；④每场景 TPS/QPS、成功率、avg/P95/P99、CPU/IO、
异常；⑤判定（有效/无效/劣化、是否保留、下一步）；⑥可复现命令（不依赖未记录的 shell 状态）。
模板见 `references/ab-protocol.md` §4。

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/benchmark-harness.md` | sysbench 安装/摘要冻结、五场景脚本、业务模型、连接串、线程与绑核、指标口径与最小记录 |
| `references/functional-verification.md` | DBA/GV$ 视图、参数快照、SQL 冒烟、计数/抽样/事务、副本与主 Zone 手工校验、错误码、验证矩阵 |
| `references/ab-protocol.md` | 基线/候选、多轮、噪声、假收益、报告模板、停止条件、失效模式表 |
| `scripts/smoke_test.sh` | 只读 SQL 冒烟（版本/节点/租户/Unit/参数/表/抽样、可选业务租户写可用性）；**不查副本/主 Zone** |
