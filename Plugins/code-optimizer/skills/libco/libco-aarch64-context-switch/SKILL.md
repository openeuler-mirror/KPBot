---
name: libco-aarch64-context-switch
description: >
  用于 libco 在 AArch64 上的上下文切换移植与排障：处理协程切换后整数寄存器、
  栈指针或首次启动异常。适用于 libco 协程库 AArch64 移植、上下文切换汇编检查、
  16B 栈对齐与初始栈构造。触发词：libco 协程移植、AArch64 上下文切换、栈对齐、
  首次启动、协程切换排障。不适用：只缺浮点/SIMD 状态、普通函数 ABI 咨询。
---

# AArch64 上下文切换（整数/SP）

本 skill 只负责整数寄存器和栈指针。`d8-d15` 由
`libco-aarch64-callee-saved-simd` 负责；不要在这里顺手承诺 SIMD 状态。

## 适用条件

- 把 x86/x86_64 协程库移植到 AArch64，需要新增 `coctx_swap` 分支。
- 协程切换后出现整数局部变量、指针、栈帧错乱。
- 需要核对 `coctx_make` 的初始 SP、返回地址、参数寄存器。
- 需要确认 16B 栈对齐与 callee-saved 寄存器。

不适用：
- 只缺 d8-d15 保存（用 SIMD skill）。
- 普通线程上下文切换/内核调度。

## 方法

### 1. 保存/恢复整数寄存器

AArch64 需要保存 x19-x29（callee-saved）、x30（返回地址）、SP；x0-x18 是 caller-saved，
但派生版 libco 的 `coctx_t` 约定保存 x0-x30 + SP（32 个槽），用于首次启动参数与完整恢复。
**上游 Tencent/libco master 的非 i386 `coctx_t` 只有 14 个槽**；下面的 32 槽布局和
224/240 偏移只适用于带 AArch64 适配的派生版，应用到上游前必须先打派生补丁。

```c
#if defined(__aarch64__)
struct coctx_t {
    void *regs[32];    // 0..255
    size_t ss_size;    // 256
    char *ss_sp;       // 264
    uint64_t dregs[8]; // 272..335
};
static_assert(offsetof(coctx_t, regs) == 0, "regs offset");
static_assert(offsetof(coctx_t, ss_size) == 256, "ss_size offset");
static_assert(offsetof(coctx_t, ss_sp) == 264, "ss_sp offset");
static_assert(offsetof(coctx_t, dregs) == 272, "dregs offset");
static_assert(sizeof(coctx_t) == 336, "coctx_t size");
#endif
```
> 示例使用 C++11 `static_assert`；纯 C 构建改用 `_Static_assert` 或等价编译期断言。



用 `stp/ldp` 成对保存/恢复：

```asm
stp x0,  x1,  [x0]
stp x2,  x3,  [x0, #16]
...
stp x28, x29, [x0, #224]
mov x2, sp
stp x30, x2,  [x0, #240]
```

恢复时先读 pending 的 x2-x29、x30/SP，再 `mov sp, x0`，最后恢复 x0/x1 并 `ret`。

### 2. coctx_make 初始化

```c
char* sp = ctx->ss_sp + ctx->ss_size - sizeof(void*);
sp = (char*)((unsigned long)sp & -16LL);  // 16B 对齐
memset(ctx->regs, 0, sizeof(ctx->regs));
void** ret_addr = (void**)sp;
*ret_addr = (void*)pfn;
ctx->regs[kSP_ARM64] = sp;
ctx->regs[kRETAddr_ARM64] = (char*)pfn;
ctx->regs[kARG1] = (char*)s;
ctx->regs[kARG2] = (char*)s1;
```

### 3. get_sp

AArch64 上不要在 C++ 里靠局部变量地址猜 SP；提供汇编 helper：

```asm
.globl get_sp
get_sp:
    mov x1, sp
    str x1, [x0]
    ret
```

`co_swap` 在切换前调用 `get_sp(&curr->stack_sp)`，用于共享栈保存。

### 4. 栈对齐

- AAPCS64 要求 SP 16B 对齐；`coctx_make` 用 `& -16LL`。
- 在协程入口/汇编边界断言 `sp % 16 == 0`。
- 保存 d8-d15 时，上下文结构偏移也必须对齐（见 SIMD skill）。
- **x18 是平台寄存器**：Linux AArch64 通常保留不用，Android arm64 可能用于 ShadowCallStack。
  示例按 Linux AArch64 保存/恢复 x0-x30；移植到 Android 等平台时，不能盲目清零新上下文的
  x18，必须按目标 ABI 保留或初始化平台状态。

## 验证

- 多协程（≥3）交替 resume/yield，检查局部变量、调用顺序、结束行为。
- 在协程入口断言 16B 对齐。
- 切换前后比较 x19-x29（内联汇编/编译器屏障）；任何污染都可能导致随机错误。
- 用 `bench_switch` 做切换回归；保存 d8-d15 后必须重新确认寄存器与栈行为。
- 跑 `example_poll`、`example_cond`、`example_thread 4`、共享栈 ping-pong。

## 反例

- 不要为省指令删除 x19-x29 保存；会污染协程间寄存器。
- 不要把 `coctx_swap` 改成普通 C 函数或改参数顺序。
- 不要只测“能跑”，必须做寄存器/栈对齐专项测试。
- 不要在本 skill 里承诺 d8-d15；单独走 SIMD skill。

## 迁移到其他应用

- 在目标项目定位“上下文结构 + 切换入口 + 首次启动路径”，接口名可换，ABI 保存集合与 16B 对齐不变。
- 若目标使用 C++ 协程、OS 线程或 signal 栈，先确认是否真的由用户态汇编切换；不是则不要套本方法。
- 栈布局、入口参数和返回路径按目标 runtime 重写，并用多协程寄存器/栈对齐测试验收。

## 证据

- AAPCS64 规定 SP 16B 对齐与 callee-saved 寄存器；参考实现按此保存整数/SP。
- 本 skill 固化方法与门禁，不固化性能数字；目标应用需按自身负载复测。
- 共享参考：[references/libco-baseline-and-method-map.md](references/libco-baseline-and-method-map.md)、[references/other-applicability.md](references/other-applicability.md)。
