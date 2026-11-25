---
name: nebula-test-verification
description: >
  NebulaGraph 3.8.0 的正确性与性能验证：构建验证、ctest 单元测试、TCK/pytest-bdd、
  nGQL 冒烟（GO/MATCH/LOOKUP/FETCH/GET SUBGRAPH/INSERT 回读/最短路径逐对比对）、
  数据导入对账、LDBC 五场景压测（Go1/Go2/Go3/FindShortestPath/Insert）、双 k6 直连、
  C++ 微基准与 Python 端到端基准、指标口径（QPS/成功率/latency/responseTime/CPU）、
  A/B 同口径复测与噪声判定、结果归档。用于验证“功能是否正确、性能是否真的变好”；
  不适用编译部署（→ nebula-build-deployment）、热点归因（→ nebula-hotspot-analysis）、
  参数/源码优化实施（→ nebula-performance-optimization）。
  触发词：Nebula 测试、Nebula 验证、Nebula 对账、Nebula 压测、Nebula benchmark、
  k6、NebulaGraph-Bench、TCK、ctest、QPS、P99、A/B 对比、正确性验证。
license: MulanPSL-2.0
compatibility: NebulaGraph 3.8.0；NebulaGraph-Bench v1.2.0、k6 v0.33.0 + xk6-nebula v1.0.0、importer v3.1.0；鲲鹏 950 / aarch64、openEuler 24.03 实测
metadata:
  skill_type: tool_assisted
  category: testing
  domain: graph_database
  version: "1.0.0"
---

# NebulaGraph 正确性与性能验证

## 简介

验证分两层，缺一不可：

1. **正确性**：构建可用、单元/TCK 通过、查询结果与基线一致、导入数据对账无缺失。
2. **性能**：在固定口径下测 QPS/延迟/CPU，做基线 vs 候选的 A/B，判断收益是否超出噪声。

原则：**没有正确性的性能数字无效；没有同口径基线的“提升”不可信。**

## 何时触发

- 编译/部署后要确认服务可用、数据完整、查询结果正确。
- 改源码、调参数、换二进制、升级依赖后，要判断是否有性能收益/回退。
- 需要建立或复现 LDBC 五场景基线，或对比不同部署拓扑。
- 压测数字异常（QPS 上不去、CPU 不饱和、成功率下降、延迟长尾），需要判断是服务端还是客户端。
- 需要给出可归档的验证报告（功能证据 + 性能证据 + 口径 + 结论）。

**不适用**：安装/编译/导数据本身 → `nebula-build-deployment`；火焰图与热点归因 →
`nebula-hotspot-analysis`；具体优化杠杆实施 → `nebula-performance-optimization`。

## 验证矩阵

| 层级 | 手段 | 依赖 | 能回答什么 |
|------|------|------|-----------|
| 构建 | 产物、符号、库版本、md5 | 编译产物 | 编出来的东西是否可信 |
| 单测 | `ctest -jN --output-on-failure` | `ENABLE_TESTING=ON` | 函数/模块是否回归 |
| 端到端功能 | TCK（pytest-bdd）/ nGQL 冒烟 | 运行中的集群 + 测试数据 | 查询语义是否正确 |
| 数据 | 导入对账、计数、抽样属性 | 数据集 + schema | 数据是否完整/一致 |
| 微基准 | C++ `_bm` / Python bench | 构建产物/运行集群 | 单函数或 CRUD 是否变慢 |
| 系统压测 | k6 + NebulaGraph-Bench 五场景 | 集群 + 压测客户端 | 端到端 QPS/延迟/扩展性 |
| 判定 | A/B 同口径、多轮、噪声阈值 | 基线 + 候选 | 收益是否真实、是否保留 |

## 工作流

```
① 构建验证 → ② 功能冒烟 → ③ 数据对账 → ④ 微基准（可选）
  → ⑤ 系统基线（多轮）→ ⑥ 候选复测（同口径）→ ⑦ 判定与归档
```

任何一步失败，停止后续性能测试；先修正确性。

## 功能验证

### 构建与单测

```bash
# 构建产物/库版本/符号
ls -lh "${BUILD_DIR}/bin/nebula-"{metad,graphd,storaged}
strings "${BUILD_DIR}/bin/nebula-graphd" | grep -m1 -E 'jemalloc|[0-9]+\.[0-9]+\.[0-9]+-[0-9]+-g[0-9a-f]+'
# 注意：这是版本串核对，不是下载物完整性校验；sha256/GPG/commit 校验应在编译前完成
md5sum "${BUILD_DIR}/bin/nebula-"{metad,graphd,storaged}

# 单测（需 ENABLE_TESTING=ON）
cd "${BUILD_DIR}" && ctest -j"$(nproc)" --output-on-failure
```

### nGQL 冒烟

`scripts/smoke_test.sh` 是只读冒烟入口；覆盖：

- `SHOW HOSTS` / `SHOW SPACES` / `DESCRIBE SPACE`
- Tag/Edge 计数与抽样属性
- `GO 1 STEP` / `GO 2 STEP`
- `MATCH` 单跳/变长路径
- `FETCH PROP` / `LOOKUP`（等值/范围）
- `FIND SHORTEST PATH`（双向、已知点对、不可达点对）
- `GET SUBGRAPH`

```bash
NEBULA_ADDR=127.0.0.1:9670 NEBULA_USER=root \
  NEBULA_CONSOLE_WRAPPER=/path/to/console-auth-wrapper \
  SPACE=ldbc_bench NEBULA_VID_TYPE=int EXPECT_PERSON=933 SAMPLE_PROP=firstName bash scripts/smoke_test.sh   # int64 Space：VID 不加引号
```

写入类验证（INSERT 回读、DELETE 后校验）单独在隔离 Space 做，不能在生产 Space 上跑。

### 数据对账

- 导入后按 Tag/Edge 统计计数，与数据集期望值比对。
- 抽样点/边读取属性，确认类型、日期、字符串长度正确。
- 索引建立后抽查 LOOKUP，确认索引命中且结果与全表扫描一致。
- 记录差异：LDBC 的 VID 区间重叠可能造成少量合并/覆盖，属于数据固有现象，需与“导入失败”
  区分。

详细命令与期望值见 `references/functional-verification.md`。

## 性能验证

### 场景

| 场景 | 语句 | 负载特征 | 常见瓶颈 |
|------|------|---------|---------|
| Go1Step | `GO 1 STEP FROM "{}" OVER KNOWS YIELD ...` | 轻读，1 次 RPC/查询 | graphd 队列 futex、RPC 序列化 |
| Go2Step | `GO 2 STEP ...` | 中读，多跳串行 RPC | storaged RocksDB + 结果组装 |
| Go3Step | `GO 3 STEP ...` | 重读，大扇出 | storaged 扫描/内存/队列 |
| FindShortestPath | `FIND SHORTEST PATH FROM "{}" TO "{}" OVER *` | BFS | graphd BFS 容器/Value 字符串 |
| InsertPerson | `INSERT VERTEX Person(...) VALUES ...` | 写，batch=100 | nGQL 解析、WAL fsync、compaction |

### 工具链

- NebulaGraph-Bench v1.2.0：场景脚本与编排。
- k6 v0.33.0（xk6 + k6-plugin v1.0.0）：压测引擎。
- nebula-importer v3.1.0：数据导入。
- 客户端与服务端分核/分机；多 graphd 时用多个 k6 进程各连一个 graphd。

### 指标口径

| 指标 | 含义 | 注意 |
|------|------|------|
| `iterations/s` | QPS | 多 k6 要相加；同 VU/时长才可比 |
| `checks` | 成功率 | 读场景要求 100%；写场景允许少量固有冲突 |
| `latency` | 服务端处理延迟 | graphd 收到→返回 |
| `responseTime` | 客户端端到端 | 含网络与客户端解码 |
| `p(50/90/95/99)` | 延迟分位 | 均值会掩盖长尾 |
| CPU% | 进程/整机 | 结合 `mpstat`、`pidstat`、`ss` 判断是否打满 |
| 客户端 CPU | k6 进程 | 接近满载说明客户端先瓶颈 |

### A/B 纪律

1. 同一数据快照、同一配置（除被测变量）、同一编译参数（除被测变量）、同一 VU/时长。
2. 先预热（读场景），每轮 ≥3 次，取中位数/均值；波动 >5% 视为无效重测。
3. 只改一个变量；失败/无收益必须回滚并记录。
4. 两腿二进制记录 md5/commit；数据引擎版本必须匹配。
5. 结果分列 QPS、成功率、p50/p99、CPU、客户端状态，不合并成一个“提升 X%”。

详细口径与失效模式见 `references/benchmark-harness.md` 和 `references/ab-protocol.md`。

## 常见失效模式

| 现象 | 可能原因 | 处置 |
|------|---------|------|
| QPS 锁死在低位，客户端 CPU 打满 | 客户端先瓶颈（CSV 解码/JS 构造） | 加 k6 进程/核，或减小结果集 |
| Go2/Go3 QPS 远低于预期，k6 CPU 很高 | 返回行数巨大，客户端解码成本超过服务端 | 加 `LIMIT`（保持服务端遍历扇出）或换成聚合查询 |
| 多 graphd 但流量只进一个 | xk6 `nebulaPool.init()` 全局单例 / LB 单点 | 多 k6 进程各连一个 graphd，不用单 k6 分地址 |
| 成功率下降 | 写入冲突、超时、数据未恢复 | 先查日志和失败样本，不要只看 QPS |
| 两轮差异很大 | 数据未重置、compaction/预热、其他租户干扰 | 重置到同一备份、预热、固定核、记录环境 |
| 改完参数“提升”但不可复现 | 多变量同时改/噪声 | 回滚，单变量重测；波动 >5% 不采信 |
| 新二进制启动后查询结果不同 | 语义回归/数据格式不兼容 | 立即停止性能测试，跑功能冒烟与路径比对 |

## 输出契约

一份可归档的验证报告至少包含：

1. **环境与身份**：硬件/NUMA、OS/编译器、Nebula commit、二进制 md5、third-party/jemalloc/
   RocksDB 版本、数据集与 Space/分区/副本、部署形态与绑核。
2. **功能证据**：构建/单测结果、冒烟查询与期望值、对账表、失败样本（如有）。
3. **性能口径**：场景、语句、VU/时长、k6 进程数/绑核、轮数、预热、数据重置方式。
4. **结果**：每场景 QPS、成功率、p50/p99、CPU（服务端/客户端）、异常。
5. **判定**：有效/无效/劣化；是否保留；未达标的下一步（指向 hotspot/optimization）。
6. **可复现命令**：完整命令或脚本路径，不依赖未记录的 shell 状态。

## 参考资料与脚本

| 文件 | 何时读 |
|------|--------|
| `references/functional-verification.md` | 构建验证、ctest/TCK、nGQL 冒烟、数据对账、路径比对、错误签名 |
| `references/benchmark-harness.md` | k6/NebulaGraph-Bench 场景、脚本生成、双 k6、绑核、指标解析、客户端瓶颈 |
| `references/ab-protocol.md` | 基线/候选、多轮、噪声、保留/回滚、C++/Python 微基准与报告模板 |
| `scripts/smoke_test.sh` | 只读 nGQL 冒烟（主机/计数/GO/MATCH/LOOKUP/FETCH/FSP/子图） |
