# 原生 DiskANN 性能用例

`cases/rust-memory-search` 是 case-spec tier B 用例，使用 Rust DiskANN 原生 graph-index-build/Load。适配依据为本地 0.54.0 的 diskann-benchmark/README.md、example/graph-index.json、src/backend/index/result.rs、src/utils/datafiles.rs 和 benchmark-runner/src/result.rs。其他版本先检查 CLI，输出结构不匹配时明确失败，不猜指标路径。C++、SSD、PQ 和动态缓存需另选项目适配器。

## 执行

先在目标 DiskANN 根目录使用项目要求的 Rust 工具链和依赖构建，公共编译 flags 按目标环境选择并记录：

```bash
cargo build --locked --release -p diskann-benchmark
```

从用例目录执行（Python 3.10+ 标准库；Linux 绑核额外需要 taskset）：

```bash
python3 run.py --source /path/to/DiskANN \
  --binary /path/to/DiskANN/target/release/diskann-benchmark \
  --output /path/to/runs/memory-search-001 --cpus 2
```

也可设置 SOURCE、BINARY、RESULTS 后运行 entrypoint.sh。Windows 使用 python 和 .exe 路径，省略 --cpus。输出目录必须全新且位于源码之外。没有工具链时可 `--prepare-only` 验证数据准备，但 result.status=PREPARED、success=false、performance_executed=false，不算通过性能用例。仅显式 --skip-if-unavailable 才把缺少二进制/命令转为 SKIP、退出码 77；其他失败退出 2，成功退出 0。

## 测量契约

- 默认 4096 base × 32 维、256 独立 query、k=10、L=100、R=32、build_L=100、1 查询线程、固定种子 1729。参数可调，但两次实验比较前需要全部冻结。
- Python 独立穷举 squared L2 计算 ground truth；向量先按可精确表示的二进制分数生成，写入 f32 bin；GT 写 u32 bin。小规模合成数据只提供运行和测量基础，不代表生产数据分布。
- 单次建图、保存索引；后续每次加载同一份索引，原生 reps=1 使每次都有独立 Recall。默认排除一次预热，再测 4 次；索引加载与构建不计入查询 QPS。这里测的是每次加载后查询，与持续驻留服务有差异。
- 输出 QPS 中位数、min/max/MAD、mean/p99 延迟（us）、Recall（0..1）、Comps/Hops。Recall 默认要求 >=0.95。无固定吞吐阈值、无候选 speedup，不将机器速度作为跨机器通过条件。
- 保留源码摘要清单、二进制/数据/保存索引 hash、配置、命令、退出码、原始 JSON 和 stdout/stderr、全部 samples。记录 CPU 亲和性与平台；编译器/flags 仍需保存真实构建日志，二进制路径和 hash 不能证明其构建选项。
- 内存图不采 SSD IOs，也不证明真实 SSD、PQ、动态缓存优化或目标鲲鹏性能验收通过。比较候选时另外使用冻结 manifest、UT/ST 与交错 A/B；不能直接将两次独立建图 case 的 QPS 相除宣称实现收益。

## 验证结果的含义

result.json 的 success=true 仅表示该合成内存图用例实际执行，参数/样本有效且 Recall 通过。脚本回归中的 mock 只验证错误处理和原生产物解析，不能作为 DiskANN 性能数据。
