# DiskANN 环境检查

## 工作流程

1. 从上下文确认源码路径、Rust/C++、任务类型和目标平台。
2. 在 `diskann-source-analysis` 技能目录运行 `python3 scripts/collect_env.py --repo /path/to/DiskANN --output /path/to/run/env.json`。如需记录索引文件信息，追加 `--index /path/to/index`。
3. 按 [检查表](kunpeng-checklist.md) 标记 pass/warning/fail/unknown。只在当前步骤实际依赖缺失时阻断；不把 Linux/AArch64、io_uring 或 Git 作为一般源码调查前提。
4. 记录逻辑核/物理核、CPU 与内存 NUMA 绑定、实际构建 flags 和工具来源。非 Git 源码使用版本与摘要；不补猜机器信息。

## 输出

env.json、环境结论、缺失依赖及对本次任务的影响。采集器只读系统信息，仅写指定输出。

## 参考资料

- [平台检查项](kunpeng-checklist.md)
