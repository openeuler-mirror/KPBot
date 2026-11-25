# A/B 复测协议与判定

## 1. 目标

回答一个问题：**候选变更是否在统计噪声之外、可复现地改变了性能，且没有破坏正确性？**

A/B 不是“跑两次看哪个数字大”，而是控制变量、重复测量、分列指标、保留/回滚的闭环。

## 2. 变量控制

两腿（baseline / candidate）必须一致：

| 维度 | 要求 |
|------|------|
| 数据 | 同一份备份、同一 Space/分区/副本，每轮前重置 |
| 配置 | 除被测参数外完全一致；配置文件 md5 |
| 二进制 | 除被测编译选项/补丁外一致；记录 md5 与 commit |
| 工具链 | k6/importer/Bench 版本一致 |
| 环境 | 同机、同 NUMA/绑核、同 OS/内核、无其他租户 |
| 压测 | 同场景、同 VU、同 duration、同 k6 进程数/绑核、同是否 LIMIT |
| 测量 | 同预热、同轮数、同统计方式（中位数/均值） |

一次只改一个变量。若必须改多个（如新二进制同时换 third-party），结论只能归因到“整组”，
不能声称某个子项有效。

## 3. 基线身份

每轮基线记录：

- Nebula commit、三个二进制 md5；
- third-party / jemalloc / RocksDB 版本；
- 编译参数（CMake flags、`-march`、LTO、链接器）；
- 数据集版本、Space/分区/副本、导入时间与对账结果；
- 部署形态、进程数、线程参数、绑核布局；
- OS/内核/编译器、NUMA/磁盘。

没有这些，后续“变快了”无法复现，也无法解释回退。

## 4. 运行纪律

```
① 重置到同一备份（停服 → 成对恢复 → 删 cluster.id → 启动 → 验收）
② 预热（读场景至少 1 轮，丢弃）
③ 正式测量 ≥3 轮，每轮之间不重置（除非写场景需要）
④ 轮间记录环境（nproc/uptime/其他进程）
⑤ 换腿前重置/重启，避免上一腿的 cache/compaction 影响
```

噪声处理：

- 同腿轮间波动 >5%：先查环境（其他进程、频率、数据未重置、compaction），不要直接取平均。
- 两腿差异 <5%：视为无显著差异，不能宣称提升。
- 延迟看 p50/p99 与分布，不能只看均值。
- 写场景的成功率/超时率单独记录，不能因 QPS 高而忽略失败。

## 5. 微基准

### 5.1 C++ 微基准

用途：验证某个函数/模块的优化，不依赖运行中的集群。

```bash
cd "${BUILD_DIR}"
ninja -j"$(nproc)" expression_bm value_bm row_writer_bm
./bin/bench/expression_bm > baseline_expression_bm.txt 2>&1
./bin/bench/value_bm      > baseline_value_bm.txt 2>&1
./bin/bench/row_writer_bm > baseline_row_writer_bm.txt 2>&1
```

按改动选择：

| 改动 | 基准 |
|------|------|
| 表达式/函数调用 | `expression_bm`、`func_call_expression_bm` |
| Value/Edge | `value_bm`、`edge_bm` |
| 行编码 | `row_writer_bm` |
| 解析 | `parser_bm` |
| 迭代器 | `iterator_bm` |
| GetNeighbors | `get_neighbors_bm` |
| 内存分配 | `arena_bm` |
| ID 生成 | `segment_id_bm`、`snowflake_bm` |

注意：微基准使用 MockCluster/进程内数据，能证明函数变快，但不能替代端到端验证；
端到端可能被其他瓶颈掩盖。

### 5.2 Python 端到端基准

用途：验证 CRUD 路径无回退，单连接串行，不测并发图遍历。

```bash
cd "${NEBULA_HOME}/tests"
# 注意：上游 tests/conftest.py 只注册 address/user/password/partition_num/replica_factor/
# data_dir/stop_nebula/rm_dir/debug_log 九个选项，没有 --timeout（requirements.txt 也不含
# pytest-timeout，加 --timeout 会 unrecognized arguments 退出）；bench/insert.py 硬编码
# for i in range(20000) 批次，没有跳过 prepare 的环境变量——每次都会全量导入。
python3 -m pytest bench/insert.py bench/lookup.py \
  --address=127.0.0.1:9669 --user=root --password="<测试环境注入>" \
  --stop_nebula=false --rm_dir=false \
  --partition_num=10 --replica_factor=1 \
  -v -s \
  --benchmark-columns=min,max,mean,stddev,median,ops,rounds
```

| 用例 | 内容 | 注意 |
|------|------|------|
| `insert.py` | 批量插入顶点/边 | 百万级（20000 批 × 50/批 = 100 万顶点 + 100 万边），~分钟级 |
| `lookup.py` | 10 种索引查找 | 100 万顶点，~分钟级 |
| `delete.py` | lookup+delete 循环 | 很慢，按需 |

> 密码通过测试环境的配置/密钥注入替换 `<测试环境注入>`；不要把生产密码写进命令历史或报告。

局限：单线程/同步 RPC，不反映并发；没有 GO/MATCH/路径/子图。并发与图遍历必须用 k6。

### 5.3 k6 系统压测

见 `benchmark-harness.md`。每个场景单独 A/B，不要用“所有场景总和”掩盖某个场景的回退。

## 6. 判定规则

| 结果 | 判定 | 动作 |
|------|------|------|
| QPS 提升 ≥5% 且 p99 不劣化、成功率不降 | 有效 | 保留，记录收益与条件 |
| QPS 持平（±5%）且延迟/成功率不劣化 | 无效但可保留 | 仅当能降低资源/复杂度时保留，否则回滚 |
| QPS 持平但 p99 明显改善 | 有效（延迟收益） | 保留，报告延迟分位 |
| 任一场景 QPS 下降 >5% 或成功率下降 | 劣化 | 回滚，记录已证伪 |
| 结果波动 >5% 无法复现 | 不可判定 | 修环境后重测；不得据此放行 |
| 正确性失败 | 直接否决 | 回滚，修代码后重来 |

“有效”必须同时给出：

- 绝对数字（基线 → 候选）；
- 相对变化；
- 轮数与波动范围；
- 口径（场景/VU/时长/是否 LIMIT/绑核）；
- 正确性证据。

## 7. 回滚

回滚不是“失败了再说”，而是每轮前就准备好：

| 变更 | 回滚 |
|------|------|
| 二进制 | 恢复 `bin.bak.<ts>`，重启，复验 |
| 配置 | 恢复原配置文件，重启/热加载，复验 |
| 数据 | 从同一备份成对恢复，删 `cluster.id`，启动 |
| 编译参数 | 回退 CMake flags，重编，复验 md5 |
| third-party | 回旧 build/二进制；数据不兼容时恢复备份 |

回滚后必须复测到基线噪声范围内，才算回滚完成。

## 8. 报告模板

```
## A/B 报告

### 身份
- 基线：commit / md5 / third-party / jemalloc / 编译参数
- 候选：同上
- 数据集 / Space / 分区 / 副本
- 部署形态 / 进程数 / 绑核

### 口径
- 场景 / 语句 / 是否 LIMIT
- VU（每 k6）/ 总 VU / duration / k6 进程数
- 预热 / 轮数 / 数据重置方式
- 客户端绑核 / 服务端绑核

### 结果
| 场景 | 基线 QPS | 候选 QPS | 变化 | 基线 p99 | 候选 p99 | 成功率 | 结论 |
|------|---------:|---------:|-----:|---------:|---------:|-------:|------|

### 正确性
- 冒烟 / 对账 / 路径比对 / 写回读

### 判定
- 有效 / 无效 / 劣化
- 保留或回滚；回滚命令
- 未解释的异常与后续动作
```

## 9. 常见反模式

| 反模式 | 问题 |
|--------|------|
| 只跑一次就宣布提升 | 噪声/缓存/compaction 影响未排除 |
| 两腿数据/配置/绑核不同 | 无法归因 |
| 同时改参数和二进制 | 不知道收益来自哪 |
| 只看 QPS 不看成功率/延迟 | 可能用错误换吞吐 |
| 只看均值不看 p99 | 长尾被掩盖 |
| 用微基准替代端到端 | 函数变快但系统瓶颈在别处 |
| 失败后叠加第二个变量“补救” | 归因彻底失效 |
| 不记录环境/身份 | 无法复现，无法解释回退 |
