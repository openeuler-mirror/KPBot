# Troubleshooting

## 内存不足

preflight 或 drop pagecache 后看到:

```text
NUMA node 0 free 32000MB < required 65536MB
```

退出码为 `3`。停止测试,保留日志,等待人工释放内存或调整 NUMA node;不要自动降低 recordcount 或换 node。

## CPU 列表跨 NUMA

如果 `CPU_LIST` 跨多个 node,preflight 会失败。把 CPU 列表限制在同一个 node,例如:

```bash
numactl -H
lscpu -e=CPU,NODE
```

## warmup 达到最大轮数

当前 `LOG` 仍有 `compaction_started`,退出码 `4`。检查:

- `$DATA_DIR/LOG` 当前文件,不要读 `LOG.old.*`
- compaction 是否被反复触发
- 配置是否被意外覆盖

达到上限不应当作成功;保留日志,人工确认后再决定是否继续。

## 当前 LOG 不存在

warmup 退出码 `5` 表示 YCSB 没有在 `$DATA_DIR/LOG` 写日志。检查 `rocksdb.dir`、数据目录权限和 YCSB 启动目录;不要把它当作 compaction 已完成。

## 当前 LOG 与旧 LOG

每次 `ycsb run` 会新建当前 `LOG`,旧文件变成 `LOG.old.<timestamp>`。判断本轮 compaction 只看当前 `LOG`。

## Java 版本不对

默认使用 Java 8。若 `JAVA_HOME` 指向 11/17,构建或运行可能偏离基线;设置 `JAVA_HOME` 到 JDK 8,或让 `install_deps.sh` 安装 `java-1.8.0-openjdk-devel`。

## 磁盘不足

preflight 要求 `/data` 至少 `50GB`。测试结束或中途失败后，脚本会把数据目录重命名为带时间后缀的备份目录，不会自动删除。备份路径写入 `env/cleanup.env` 的 `BACKUP_DIR`，请人工确认后手动清理。

```bash
bash scripts/cleanup_data.sh --data-dir /data/workload_data --dry-run
```

`--dry-run` 只显示将使用的备份路径，不会移动数据。不要指向 `/`、`/data` 或挂载点。

## workload 中途失败

脚本会停止后续 workload,报告标记失败,并默认备份数据。不要在已修改的 DB 上从下一个 workload 继续;重新执行完整工作流。

## 结果报告缺少指标

检查:

- 六个 `baseline_workload*.log` 是否存在
- 每个日志是否包含 `[OVERALL]` 的 throughput 和 latency
- `env/bench.env` 的 `WORKLOADS_DONE` 是否为 `a b c d e f`
- `env/warmup.env` 的 `FINAL_STARTED` 是否为 `0`
