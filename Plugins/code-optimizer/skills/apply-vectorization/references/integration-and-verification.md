# 集成与验证说明

来源依据：

- `docs/specs/detailed-design/10-execution-state-machine.md`
- `docs/specs/detailed-design/11-git-version-management.md`
- `docs/specs/detailed-design/13-testing-verification.md`

## 上游交接

预期上游分析技能会提供：

- 目标函数身份
- 循环位置
- 依赖关系提示
- 诸如 `neon/sve/sme` 可用性之类的硬件适配提示

这些信息只能作为参考，生成改写前仍需本地重新做安全判断。

## 下游交接

本技能在产出 `vectorization_result` 后停止，不负责：

- 运行仓库级测试
- 创建 Git commit
- 做项目级基线与优化性能对比
- 执行回滚

这些动作属于后续 verification 与 helper skills。

## 开发期检查

使用 `scripts/detect_vector_capabilities.sh` 先探测本机向量能力：

- `--list` 用于展示本机支持的能力项
- `--require neon|sve|sme` 用于在当前优化项不受支持时提前退出

使用 `scripts/test_compile.sh [arch]` 做烟测编译：

- 有宿主编译器时，先编译标量夹具。
- 进入目标架构编译前，先确认本机支持对应能力。
- 只有在存在可用编译器时，才编译对应架构夹具。
- 如果 `generate/` 中存在 response 物化出的 `.S/.s/.asm` artifacts，也必须编译成对象并参与对应 driver 链接。
- 若工具链缺失，应返回 `skip`，而不是误报失败。

`scripts/benchmark.sh [arch]` 仅用于原生 ARM64 主机上的本地夹具基准，不代表目标项目的真实性能结论。

SME ZA inline asm 结果还必须额外完成：

- `query_arm_intrinsics.py validate-snippet --isa sme --json`
- 编译到汇编并扫描 `smstart za`、`zero {za}`、`fmopa`、`st1w`、`smstop za`
- 链接最小 driver 或实际 driver
- 用 `nm -u` 确认没有未验证的 `__arm_tpidr2_save`、`__arm_tpidr2_restore`、`__arm_za_disable` 依赖
- 不允许在生成源码中用弱符号 stub 伪造这些 SME ABI support routines

`codegen_style` 验证边界：

- C/C++ 标量源码的 `auto` 结果应是 `intrinsics`。
- C/C++ 中已有 `asm/__asm__` 的 `auto` 结果应是 `inline_asm`，并检查 `memory` clobber、寄存器 clobber 和尾处理。
- `.S/.s/.asm` 的 `auto` 结果应是 `assembly`，并检查 `.text/.globl`、函数 label、AAPCS64 调用约定、callee-saved 寄存器、目标 ISA 和链接边界。
- 显式 `codegen_style` 与源码形态冲突时必须拒绝，不能把纯汇编强行转成 intrinsics。

## 失败交接

当 skill 拒绝某个循环，或开发期编译检查失败时：

- 保持规范 JSON 包装不变
- 明确指出阻塞点
- 不要生成看似可用的半成品改写代码
- 不要触碰 Git 状态

## 后续技能的提交约定

后续带 Git 能力的层应使用如下向量化标签：

```text
[vectorization] <target_function> - <target_arch> <简短摘要>
```

本技能应明确说明使用了哪些 intrinsics、改动范围有多大，方便后续层生成精确的 commit message。
