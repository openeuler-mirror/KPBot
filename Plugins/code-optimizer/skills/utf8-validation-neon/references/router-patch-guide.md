# 分发改写细则（router-patch-guide）

> 本文件是 `utf8-validation-neon` 的改写细则：完整补丁模板、构建集成、红线陷阱清单与错误形态对照。按需加载，改写前必须通读。

## 1. 前置确认

改写前逐项确认（任一不满足即停止）：

| 检查 | 命令 | 期望 |
|---|---|---|
| SIMD 门控独占 | `grep -n "__SSE4_1__\|__aarch64__" <target_file>` | 只有 `__SSE4_1__`，无 `__aarch64__` 分支 |
| 当前走 naive | `objdump -d <bin> --disassemble=utf8_range_IsValid` | 无 `bl <utf8_range>` 路由调用 |
| NEON 实现存在 | `ls range-neon.c naive.c` | 两者齐备 |
| 尾部依赖 | `grep -n "utf8_naive" range-neon.c` | 命中（<16 字节尾巴走 utf8_naive） |
| 返回值语义 | `grep -n "return" range-neon.c \| tail` | 0=合法；非 0=非法（SIMD 块内错误返回 -1，尾部经 `utf8_naive` 返回 >0 的 1-based 错误下标），判非法必须用 `!= 0` |

## 2. 完整补丁模板

### 2.1 头部声明（include 与 FORCE_INLINE 定义块之后）

```c
#ifdef __aarch64__
int utf8_range(const unsigned char *data, int len);  /* 0=合法, 非0=非法（尾部错误为>0下标） */
#endif
```

### 2.2 分发结构改写（utf8_range_Validate 内）

改写前：

```c
#ifndef __SSE4_1__
  return (return_position ? (data - (end - len)) : 0) +
         utf8_range_ValidateUTF8Naive(data, end, return_position);
#else
  /* SSE4.1 代码，不动 */
```

改写后：

```c
#ifndef __SSE4_1__
#ifdef __aarch64__
  if (return_position) {
    /* NEON 实现无位置语义：rp=1 走 naive 回退 */
    return (data - (end - len)) +
           utf8_range_ValidateUTF8Naive(data, end, return_position);
  } else {
    return utf8_range(data, end - data) == 0 ? 1 : 0;
  }
#else
  return (return_position ? (data - (end - len)) : 0) +
         utf8_range_ValidateUTF8Naive(data, end, return_position);
#endif
#else
  /* SSE4.1 代码，不动 */
```

要点：

- 调用点在 `utf8_range_SkipAscii` 与 `<16` 早退判断之后，因此传入 `utf8_range` 的 `end - data` 恒 ≥ 16，NEON 主循环必然被触发；纯 ASCII 已被 SkipAscii 短路，两版同路径。
- `end - data` 是 size_t，`utf8_range` 的 len 是 int：输入 > 2GB 时窄化，超大 buffer 场景必须先加长度断言或分段处理（写进 risk_notes）。
- 只动 `#ifndef __SSE4_1__` 分支；SSE4.1 分支与 `utf8_range.h` 公开 API 不动。

## 3. 构建集成

- `range-neon.c` 与 `naive.c` 必须加入与 utf8_range.c 相同的构建目标（protobuf：libutf8_range / libutf8_validity 对应的 CMake 源列表）。缺 `utf8_range` → 链接失败；缺 `utf8_naive` → 链接失败。
- range-neon.c 自身由 `#ifdef __aarch64__` 全文包裹，可安全加入跨平台源列表；非 aarch64 平台它编译为空。
- 编译参数与基线保持一致（如 protobuf Release：`-O3 -DNDEBUG`），A/B 双方必须同参数。

## 4. 红线陷阱清单（逐项打勾后才能进入验证）

| # | 红线 | 违反形态（实测案例） | 自查命令 |
|---|---|---|---|
| 1 | 路由块极性必须 `#ifdef __aarch64__` | 写成 `#ifndef`：aarch64 编译回 naive，反汇编与基线 **100% 一致**，零收益；非 aarch64 分支引用未声明符号 | `grep -A1 "__SSE4_1__" <target_file>` 下一行必须是 `#ifdef __aarch64__` |
| 2 | `return_position=1` 不得调 `utf8_range()` | 非 0 返回值被当作前缀长度消费，结果语义错 | code review |
| 3 | 补丁引用的每个符号先查到定义 | 引用不存在的 `utf8_range_ValidateUTF8Native` → **链接失败**（undefined reference） | `grep -rn "<符号>" <repo>` 必须命中定义 |
| 4 | naive.c 与 range-neon.c 同进构建 | `utf8_naive` 未定义 → 链接失败 | `nm <产物> \| grep utf8_naive` |
| 5 | 提交前反汇编确认（注意 intrinsic 名 ≠ 反汇编助记符：vqtbl1q_u8→`tbl`、vld1q_u8→`ldr qN`） | 路由未生效 = 改动无效 | `objdump -d <bin> --disassemble=utf8_range_IsValid` 含 `bl <utf8_range>`；且 `--disassemble=utf8_range` 符号内含 `tbl` 等 NEON 助记符 |
| 6 | 判非法必须用 `utf8_range(...) != 0` 语义 | 尾部（<16 字节）错误经 `utf8_naive` 返回 >0 的 1-based 错误下标而非 -1；写成 `== -1` / `< 0` 会放过尾部非法输入，形成静默校验绕过 | code review；模板 `== 0 ? 1 : 0` 为安全形态 |

## 5. 错误形态对照

| 现象 | 根因 | 处置 |
|---|---|---|
| 链接失败：undefined reference to `utf8_range` / `utf8_naive` | 构建源列表未加 range-neon.c / naive.c | 补构建集成（§3） |
| 链接失败：undefined reference to `utf8_range_ValidateUTF8Native` 或类似符号 | 引用了不存在的函数（想当然的"Native"实现） | rp=1 改为 naive 回退；不造符号 |
| A/B 数据完全相同（speedup≈1.0 全表） | 极性写反，no-op | §4 红线 1；objdump 复查 |
| fuzz 不一致（块边界 16n±1、截断用例失败） | NEON 块尾/naive 尾巴衔接或尾处理 lookahead 有改动 | 不得修改 range-neon.c 本体；回退改写重新路由 |
| ASCII 场景 ~10% 波动 | 代码布局噪声/新增分支 | 噪声级，可接受，写入报告 |
