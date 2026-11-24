# 存储域（app-tuner 侧）

存储**引擎与服务层**的性能诊断与调优：LSM 引擎的读写路径、compaction、写入停顿与尾延迟。

与 `code-optimizer` 侧的同名域分工：那边负责**系统存储栈**（块层调度、文件系统挂载、NVMe 多队列与 IRQ 亲和、
页缓存回写、io_uring/libaio、分布式存储客户端），本侧负责**引擎自身机制**（LSM/compaction/block cache）。
两侧 `DOMAIN.md` 互相指路。

## 成员

| Skill | 职责 |
|---|---|
| [`rocksdb-perf-diagnostics`](rocksdb-perf-diagnostics/SKILL.md) | RocksDB 性能诊断与优化：读 LOG / properties / Statistics / perf 上下文，定位慢读慢写、p99 尾延迟、写停顿（write stall）、compaction 抖动与 backlog，给出 ColumnFamilyOptions / DBOptions、block cache、bloom filter 等参数动作 |
| [`rocksdb-deploy-baseline`](rocksdb-deploy-baseline/SKILL.md) | RocksDB 基线部署：安装依赖、构建 RocksDB jar 与 YCSB distribution，产出 `deploy.env` |
| [`rocksdb-test-baseline`](rocksdb-test-baseline/SKILL.md) | RocksDB 100W 数据量基线测试：load、warmup、workload a-f、报告和数据备份 |
| [`rocksdb-workflow-baseline`](rocksdb-workflow-baseline/SKILL.md) | 基线测试总工作流：串联 deploy 和 test，支持只部署、只测试和完整流程 |

## 域内路由

- 问题落在引擎参数与读写路径（stall、compaction、cache 命中）→ 本侧 `rocksdb-perf-diagnostics`
- 需要基线构建、部署产物或 `deploy.env` → `rocksdb-deploy-baseline`
- 需要执行 100W 基线测试、报告或数据备份 → `rocksdb-test-baseline`
- 需要完整基线流程或只部署/只测试调度 → `rocksdb-workflow-baseline`
- 问题落在系统栈（磁盘 IO 调度、挂载参数、页缓存、NVMe 队列、网络存储）→ `code-optimizer` 侧 `storage/`
- 问题落在业务代码的存储访问模式 → `code-optimizer` 现有 skill

## 准入判据

新增成员须满足：结论落在**存储引擎或存储服务自身机制**上（LSM/cache/compaction/副本与一致性），
且带可复现的基线口径（数据集规模、读写比例、SLA 目标）；落在系统栈或业务代码的，去对应域。

本组基线口径：100W 记录、YCSB 默认 value、固定 RocksDB 6.26.1 / YCSB commit `19e885f7`，报告包含分操作延迟和数据备份路径。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/storage/**`
