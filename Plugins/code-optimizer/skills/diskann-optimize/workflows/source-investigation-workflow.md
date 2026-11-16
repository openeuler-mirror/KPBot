# DiskANN 源码调查 Workflow

## Step 0：问题定界

明确 repo、commit、Rust/C++ 实现、问题列表、是否需要论文对比、是否允许运行诊断命令。初始化 state，workflow=`diskann-source-investigation`。

## Step 1：源码入口与版本

派发 Analyzer：确认 benchmark/API 入口、配置路径、目标符号和相关构建 feature。输出 `SOURCE_MAP.md` 初稿。

## Step 2：静态调用链

按用户问题逐条追踪：入口 → 对象所有权 → 循环/分支 → 数据结构 → 序列化或 syscall。每项给 `文件:行号`，标注事实/推断。

## Step 3：运行证据（按需）

只有结论涉及实际时间、阻塞、指令、I/O 次数、cache 或线程行为时才派发 Profiler。重型采集需要单独说明开销和运行范围。

## Step 4：交叉检查

派发 Reviewer 只做证据检查：

- 行号是否属于当前 commit；
- 结论是否超过源码能证明的范围；
- 论文与实现是否混淆；
- 图中节点/顺序是否与代码一致。

## Step 5：交付

输出一份结构化 Markdown：术语、总体流程、关键时序、数据布局、参数、证据索引、事实/推断/未知、待测项。不得把运行未知项写成确定结论。
