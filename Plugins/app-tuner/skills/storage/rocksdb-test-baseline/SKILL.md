---
name: rocksdb-test-baseline
description: 使用已部署的 RocksDB/YCSB 产物执行 RocksDB 单实例基线性能测试。Use this skill whenever the user mentions RocksDB baseline testing, YCSB load/warmup/workload a-f, 100W 数据量, default value size, NUMA 绑定, pagecache 清理, or asks to run a baseline test with prebuilt artifacts, even if they do not say "test" explicitly. Consumes deploy.env from rocksdb-deploy-baseline.
license: MulanPSL-2.0
compatibility: Requires bash, python3, root, openEuler aarch64, numactl, and a deploy.env plus a YCSB distribution produced by rocksdb-deploy-baseline.
metadata:
  skill_type: benchmark
  category: testing
  domain: rocksdb
  activation_mode: active
  side_effect_free: "false"
  version: "1.0.0"
---

# RocksDB Baseline Test

只负责测试执行：load、drop pagecache、NUMA 绑定的 warmup、workload a-f、报告和数据备份。不安装依赖、不构建 RocksDB/YCSB。

## 快速开始

```bash
bash scripts/run_test.sh \
  --deploy-env /data/rocksdb-baseline/deploy/<timestamp>/env/deploy.env \
  --data-dir /data/workload_data
```

`deploy.env` 必须包含：

```text
YCSB_BIN
YCSB_WORKLOADS
YCSB_OPTIONS
```

load、warmup、benchmark 共用同一份 YCSB distribution 和同一份默认配置文件。

## 输入与产物

必需输入:

- `deploy.env`，包含 `YCSB_BIN`、`YCSB_WORKLOADS`、`YCSB_OPTIONS`
- 可写的 `DATA_DIR` 和 `RESULT_DIR`
- root 权限用于 drop pagecache

预期产物:

- `summary.json`、`summary.csv`、`REPORT.md`
- 六个 workload 日志、load/warmup 日志
- `cleanup.env`:数据备份路径和状态

## 受阻时交回

内存不足、磁盘不足、NUMA 跨节点、warmup 超限或 LOG 缺失时，保留日志和 `cleanup.env`，返回失败阶段、退出码、备份路径和待处理项，等待人工处理。

## 基线默认参数

| 项 | 值 |
|---|---|
| recordcount | `1000000` |
| operationcount | `1000000` |
| warmup 每轮 operationcount | `1000000` |
| value 大小 | YCSB 默认值，不传 `fieldlength` / `fieldcount` |
| load / warmup / bench threads | `5` / `3` / `5` |
| CPU list | `0-15` |
| workloads | `a b c d e f` |

参数覆盖会自动把 `profile` 标为 `custom`。

## 执行流程

1. preflight：检查 aarch64、root、NUMA、单 node 可用内存 >=64GB、磁盘 >=50GB。
2. load：不绑核，清空数据目录后加载 100W 记录。
3. drop pagecache：`sync` + 写 `/proc/sys/vm/drop_caches`，随后复检同一 NUMA node 内存。
4. warmup：`numactl --physcpubind=$CPU_LIST --membind=$NUMA_NODE`，每轮 `workloadc` 100W 次操作，读取当前 `LOG`，有 `compaction_started` 则继续，最多 10 轮。
5. bench：同样绑定，依次跑 workload a-f，每个 workload 100W 次操作。
6. report：生成 `summary.json`、`summary.csv`、`REPORT.md`。
7. cleanup：成功或失败后默认把数据目录重命名为带时间后缀的备份目录；不自动删除，提示用户手动清理。`--keep-data` 可保留原目录。

每轮非 dry-run 开始时，旧的 `load.log`、warmup/benchmark 日志、`summary.json`、`env/*.env` 会先移到 `previous.<run_id>/`，避免旧结果被本轮报告误用。`--dry-run` 不写 `test_state.env` 或 `command.txt`。

退出码：

- 内存不足：`3`
- warmup 达到最大轮数：`4`
- 当前 LOG 缺失：`5`
- 其他阶段失败：对应脚本退出码或 `1`

## 参数

```bash
--deploy-env FILE
--work-root DIR
--data-dir DIR
--result-dir DIR
--cpu-list LIST
--numa-node N
--keep-data
--force-wipe
--dry-run
```

load 之后不要复用已修改的数据继续跑；六个 workload 中仅 workload c 只读，a/b/d/e/f 均含写操作，任何 workload 失败后都必须重新 load。

## 报告

报告包含 CPU/OS/磁盘/内存、RocksDB/YCSB 版本、gcc/Java/Maven、load 参数、pagecache、warmup、数据备份路径和六个 workload 的吞吐。延迟按 READ/UPDATE/INSERT/SCAN 等操作类型分别报告；`CLEANUP` 不计入业务 operations。`Return=ERROR`、`NOT_FOUND` 等失败请求会使报告失败。详见 `references/report-template.md`。

## 验证

测试完成后检查 `REPORT.md`、`summary.json` 和六个 workload 日志。开发期测试脚本不随 skill 提交。
