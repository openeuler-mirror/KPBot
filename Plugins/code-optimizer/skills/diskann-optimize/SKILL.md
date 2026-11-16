---
name: diskann-optimize
description: 编排 DiskANN 完整性能优化，连接源码与瓶颈分析、冻结基线、候选实现、UT/ST 和独立实验验收。用于从目标到可复现交付的优化任务；单独调查使用 diskann-source-analysis，单独跑分或采样使用 diskann-benchmark-baseline。
---

# DiskANN 优化工作流

## 输入

从请求和工作区确认源码/版本、目标、测试入口、数据/索引、资源范围和独立 run_dir；只询问无法查明且影响下一步的信息。

## 工作流程

1. 源码、环境或归因问题调用 diskann-source-analysis，必要时走 [源码调查流程](workflows/source-investigation-workflow.md)；基线、跑分或采样调用 diskann-benchmark-baseline；完整优化进入 [CP0–CP7](workflows/optimize-workflow.md)。
2. 先识别 Rust/C++、内存/SSD、量化、真实 CLI 和构建后端，不假设某个 io_uring、SIMD 或下游扩展存在。
3. 继承 pipeline_mode；默认 collaboration，沿用已有候选选择授权；auto 可自主排序候选，不增加提交或发布权限。
4. 当前主 Agent 加载本技能后担任协调者，维护 state 并遵循 [交接契约](workflows/references/handoff-contract.md)。按需使用 analyzer、profiler、candidate-planner、implementer、reviewer 五个子 Agent。工具无法提供独立角色时披露验收独立性的限制。
5. 基于实际热点一次实现一个主要机制，保留默认关闭的开关；按下表读取当前候选需要的参考资料，完成 UT/ST 和平衡性能对照。
6. 按 [实验验收](references/experiment-review.md) 与 [接受门禁](references/acceptance-gates.md) 交付结论、原始证据和复现方式。

| 候选方向 | 按需参考 |
| --- | --- |
| 读取提交/完成、buffer 生命周期与重叠 | [I/O](references/io-optimization.md) |
| 距离、PQ/LUT、NEON/SVE 与尾部 | [SIMD](references/simd-optimization.md) |
| visited、scratch、节点与物理布局 | [内存与布局](references/memory-layout-optimization.md) |

## 输出与约束

状态、源码证据、manifest、补丁、构建/UT-ST、原始性能样本和验收报告保存到 run_dir。通用技能目录不保存本次实验。

冻结条件保持可比，质量/速度权衡另建实验；历史记录为可选输入，不能替代当前证据。接受后仅按已有授权提交；推送、发布和外部写入按任务授权执行。Wiki 为可选 CP8，不阻断本地 CP7 交付。

## 共享依赖

复用 KPBot 的 `vector-experiment-methodology` 和 `vector-ut-st-validation`。

初始化、恢复和依赖检查见 [quickstart.md](quickstart.md)，状态细节见 [状态契约](workflows/references/state-template.md)。
