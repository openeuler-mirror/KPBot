# pgvector-optimize 使用入口

## 任务信息

提供或从上下文确认源码、目标（HNSW/IVFFlat/距离内核/精确扫描）、性能目标、测试工具、数据/索引及独立产物目录。已有补丁和实验记录可选；能力与构建以实际检出为准。
简单 API 问题（如 `hnsw.ef_search` 取值）直接回答；源码调查使用 analyzer；完整性能优化进入 CP0–CP7。

## 构建与测试

pgvector 是 PostgreSQL 扩展，常用命令（在源码仓根目录）：

```sh
make                              # 编译（默认 OPTFLAGS=-march=native + 自动向量化标志）
make install                      # 安装到 pg_config 对应目录
make installcheck                 # 回归测试（REGRESS）
make prove_installcheck           # TAP 测试（test/t/*.pl）
make clean && PG_CFLAGS="-DUSE_ASSERT_CHECKING" make && make install   # 断言构建
make clean && PG_CFLAGS="-DHNSW_BENCH -DIVFFLAT_BENCH" make && make install   # 性能打点
make clean && PG_CFLAGS="-DHNSW_MEMORY -DIVFFLAT_MEMORY" make && make install # 内存统计
make clean && PG_CFLAGS="-DIVFFLAT_KMEANS_DEBUG" make && make install  # k-means 指标
```

Windows 使用 `nmake /F Makefile.win`（需 `PGROOT` 与 VS x64 工具链）。编译宏决定 SIMD 分派：
`VECTOR_TARGET_CLONES`（FMA target_clones）、`HALFVEC_DISPATCH`（F16C/AVX）、`BIT_DISPATCH`（AVX512 popcount）、`HNSW_USE_READ_STREAM`/`HNSW_USE_PREFETCH`（I/O 路径）。

## 初始化示例

```bash
python3 <skill-dir>/workflows/scripts/init_run.py \
  --algorithm hnsw --run-id hnsw-example-01 \
  --repo /path/to/pgvector --run-dir /path/to/experiment \
  --pgvector-version <detected-version> --baseline-id <source-build-id>
python3 <skill-dir>/workflows/scripts/validate_state.py /path/to/experiment/state.json
```

路径、版本、运行编号与配置均是任务输入，示例不绑定具体机器、后端或时间。

## 编排

通过 KPBot 入口调用时继承模式；直接调用默认 collaboration，可按用户意图设 auto。阶段执行见[工作流](../workflows/optimize-workflow.md)，角色定义位于 agents/。
产物保存到 run_dir，只有 primary 写 state。已有记录缺失不阻断启动；参数冻结、Recall 与实际结果验证、交错性能对照和独立验收共同决定结论。提交按授权执行。

非 Git 发布源码、空仓库或 Git 不可用时，初始化将 `source.baseline_commit` 记为 `unknown`，该状态可通过 CP0 校验。CP1 需核对实际版本及归档/源码摘要；CP2 仍要求明确的 `baseline_id` 和构建清单，不能把 `unknown` 当作完整基线身份。
