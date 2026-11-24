---
name: utf8-validation-neon
description: 把捆绑 utf8_range.c 中仅以 __SSE4_1__ 门控的 SIMD 校验改路由到库内自带 NEON 实现——AArch64 上 utf8_range_Validate/IsValid 全部落到 naive 标量逐字节循环时，通过平台分发复用 range-neon.c 的现成实现，不手写 SIMD。适用于鲲鹏/AArch64 上 protobuf/upb 解析路径的 UTF-8 校验热点、多字节内容占比显著的负载；含分发改写模板、极性/位置语义/符号存在性陷阱清单、fuzz 交叉验证与绑核 A/B 基准。x86_64（已有 SSE4.1 路径）、纯 ASCII 负载（SkipAscii 已短路无收益）、需要返回非法位置的 ValidPrefix 为主的负载不适用。
---

# UTF-8 校验 NEON 路由优化

你是一位鲲鹏平台 UTF-8 校验优化专家。你的任务是在不改变公开语义的前提下，把捆绑 utf8_range 在 AArch64 上落到 naive 标量路径的校验改为路由到库内自带的 NEON 实现，并用 fuzz 交叉验证与绑核 A/B 基准证明正确性与收益。

本 Skill 是 `utf8-validation-neon` 的唯一执行中心。触发对象由代码特征决定（`__SSE4_1__` 独占门控 + aarch64 无 SIMD 分支），不按库名路由：任何捆绑 utf8_range 的项目（protobuf、upb、独立 vendor 副本）同构适用。

## 职责边界

本 Skill 只在**分发结构缺失**（SIMD 路径被 x86 宏独占、目标平台无分支可走）且库内**已有现成 NEON 实现**时介入。判据是：热点成本来自逐字节标量循环，而同目录或同库中存在可复用的 NEON 校验入口。

以下情况不属于本 Skill，返回 `success=false` 并在 `error_message` 中说明原因与建议方向；不要在本 Skill 内改写这些方向，也不要假设环境中存在其它 skill：

| 情况 | 建议方向 |
|---|---|
| 库内没有现成 NEON/SIMD 实现，需手写向量化 | 向量化 |
| 热点是 ValidPrefix（return_position=1）且需要 NEON 加速 | 前缀定位算法（本 Skill 仅提供 naive 回退，不提供位置语义的 SIMD 实现） |
| 字符串以 ASCII 为主 | 无收益，拒绝（SkipAscii 短路两版同路径） |
| 需要服务级 protobuf 画像对齐、微基外推封顶或 SPE 硬件确证 | `protobuf-optimization`（本 Skill 提供其中"utf8 校验快路径"杠杆的代码级落地） |
| 热点不在 UTF-8 校验（校验占比低） | 先做热点分析，确认校验占比后再进入 |

边界判定只需要回答一个问题：把 `#ifdef __aarch64__` 路由块加进去之后，IsValid 是否能换成整块 16 字节处理的向量化实现？若库内无此实现，移交向量化；若有，属于本 Skill。

## 输入

```json
{
  "repo_root": "<仓库根路径>",
  "target_file": "<utf8_range.c 完整路径，通常在 third_party/utf8_range/>",
  "platform": "aarch64",
  "corpus_profile": {
    "ascii_share": "<0..1，ASCII 字节占比>",
    "multibyte_share": "<0..1，多字节序列占比>",
    "length_dist": "<典型字符串长度分布，如 8~128B 或 MB 级>"
  },
  "context": {
    "prepareProject": {},
    "analyzeHotspot": {}
  }
}
```

字段约束：

- `platform` 必须为 `aarch64`；`uname -m` 非 aarch64 时直接拒绝（x86_64 走既有 SSE4.1 路径）。
- `corpus_profile` 决定收益预估：`multibyte_share < 0.1` 时预期收益低于 10%，标注后仍可执行但必须在 `risk_notes` 中声明。
- `context.analyzeHotspot` 需包含 perf 热点证据；缺失时先执行热点分析再进入。

## 辅助脚本

- `scripts/checker.c` — fuzz 交叉验证器：对同一批确定性输入（973 组：空串/短 ASCII、1~4 字节全宽度码点、U+10FFFF/U+D7FF/U+0800 边界、overlong、代理对、非法首字节、逐偏移截断、16n±1 块边界、中点损坏、全随机字节、1B~100KB）分别链接基线版与优化版各编一个二进制，输出逐行 diff，必须**逐位一致**。
- `scripts/validate_bench.c` — A/B 基准：`-DIMPL='"<被测 utf8_range.c>"'` 切换实现，校准式计时（每 case 校准到 0.05s、5 样本取中位）+ 正确性内检；`utf8_range_Validate` 为 static inline，经同编译单元 `#include` 内联计时，与其在库内的真实使用形态一致。

## 执行步骤

1. **热点与现状确认**：perf 采样确认 `utf8_range_ValidateUTF8Naive`/`utf8_range_IsValid` 出现在解析路径热点；`objdump -d <binary> --disassemble=utf8_range_IsValid` 确认当前无 `bl <utf8_range>` 路由调用，证实 aarch64 走 naive。
2. **实现选型与语义审计**：Read 库内 `range-neon.c`，确认入口签名与返回值语义（`int utf8_range(const unsigned char *data, int len)`，0=合法；**非 0=非法**——SIMD 块内错误返回 -1，`<16` 字节尾部经 `utf8_naive` 返回 >0 的 1-based 错误下标；无位置语义，判非法必须用 `!= 0`，禁止用 `== -1` / `< 0`）；确认其 `<16` 字节尾部依赖 `naive.c` 的 `utf8_naive`，两者必须一起进构建；`len` 为 `int` 参数，评估 >2GB 输入的窄化风险。
3. **分发改写**：按 `references/router-patch-guide.md` 的模板改写 `utf8_range_Validate` 分发结构（头部声明 + `#ifndef __SSE4_1__` 内嵌 `#ifdef __aarch64__` 路由块），并把 `range-neon.c`、`naive.c` 加入同一构建目标。
4. **陷阱自查**：逐项核对补丁指南中的红线清单（极性、位置语义、返回值判定、符号存在性、尾部依赖、块边界）。
5. **fuzz 交叉验证**：用 `scripts/checker.c` 编出基线/优化两个二进制，输出 diff 必须为空。
6. **A/B 基准**：用 `scripts/validate_bench.c` 分别对基线与优化版各跑 ≥3 进程（绑核、5 样本中位），原始 CSV 归档。
7. **输出结果**：按下方契约输出 `utf8_route_result`。

## 证据到改写的映射

| 证据 | 判定 | 动作 |
|---|---|---|
| `grep -n "__SSE4_1__" <target_file>` 命中且无 `__aarch64__` 分支 | SIMD 路径被 x86 独占 | 进入步骤 2 |
| objdump 中 IsValid 无 `bl <utf8_range>` 调用 | aarch64 走 naive，路由收益存在 | 继续 |
| objdump 中已有 `bl <utf8_range>` 路由 | 已有 NEON 路径 | 拒绝（`success=false`） |
| perf 中校验热点占比 < 3% | 收益上限低 | 拒绝并建议先优化真正热点 |
| `ls range-neon.c naive.c` 存在 | 可复用实现齐备 | 继续；缺任一 → 移交向量化 |

## 拒绝条件

命中任一条即拒绝（`success=false` + `error_message`）：

1. 目标平台非 aarch64。
2. utf8_range.c 已有非 x86 的 SIMD 分支。
3. 库内无现成 NEON 实现（不手写 SIMD 是硬约束）。
4. 负载以 ASCII 为主（`ascii_share > 0.9`）且多字节热点占比不足。
5. 调用方主要消费 ValidPrefix（位置语义），且不接受 naive 回退。
6. 无 fuzz 与 A/B 运行条件（真机/编译链缺失）——没有证据不得宣称收益。

## 输出

```json
{
  "utf8_route_result": {
    "success": true,
    "router_patch": {
      "target_file": "<utf8_range.c>",
      "platform_gate": "__aarch64__",
      "neon_impl": ["range-neon.c", "naive.c"],
      "validprefix_mode": "naive_fallback",
      "build_files_added": ["<加入构建目标的文件>"]
    },
    "functional_test": {
      "fuzz_cases": 973,
      "bit_identical": true
    },
    "performance": {
      "rp0_multibyte_speedup": {"64B": 1.95, "256B": 3.25, "1KB": 3.40, "4KB": 3.47, "1MB": 3.51},
      "rp0_ascii_speedup_range": [0.92, 1.05],
      "rp1_speedup_range": [0.97, 1.03],
      "throughput_gibps": {"before": 1.5, "after": 5.3},
      "raw_runs": ["<归档的原始 CSV 路径>"]
    },
    "risk_notes": "ValidPrefix 未加速（naive 回退）；>2GB 输入存在 int 窄化需另行防护",
    "error_message": ""
  }
}
```

## 平台适用性

- 仅适用 AArch64（NEON 为基线指令集，无需额外 `-march`）；x86_64 已有 SSE4.1 路径，ARM32 未验证。
- 实测环境为鲲鹏 920（0xd01 微架构、384 核）；其他 AArch64 实现收益量级预期一致（向量化 vs 标量的比值主要由算法决定），但**必须按步骤 6 在目标机复测**后写入 `performance`。
- SVE 变宽实现不在本 Skill 范围；若目标机评估显示 NEON 128b 成为瓶颈，移交向量化深化。

## 实测收益

protobuf-27.3（AArch64 384 核，`-O3 -DNDEBUG`，绑核、3 进程 × 5 样本中位）下 `utf8_range_Validate(data, len, rp)` 直接计时：

| 输入（rp=0） | 尺寸 | 优化前 ns/call | 优化后 ns/call | 加速比 |
|---|---|---|---|---|
| 混合 UTF-8 | 64B | 41.4 | 21.2 | 1.95x |
| 混合 UTF-8 | 256B | 165.3 | 50.9 | 3.25x |
| 混合 UTF-8 | 1KB | 636.9 | 187.4 | 3.40x |
| 混合 UTF-8 | 4KB | 2518.2 | 724.8 | 3.47x |
| 混合 UTF-8 | 1MB | 643512.9 | 183335.8 | 3.51x |
| 尾部非法 | 64B~1MB | 41.1~649377 | 22.6~183590 | 1.82–3.54x |
| 纯 ASCII | 16B~1MB | 4.2~55897 | 4.6~53109 | 0.92–1.05x（无变化） |
| 全部输入（rp=1） | 16B~1MB | — | — | 0.97–1.03x（无变化，预期） |

多字节内容吞吐 1.5 → 5.3 GiB/s；API 级（utf8_range_IsValid）复测一致（1MB 混合 3.85x，1.47 → 5.66 GiB/s）。正确性：973 组 fuzz 用例优化前后逐位一致。完整方法学、原始数据与两次踩坑记录见 `references/case-study-protobuf-27.3.md` 与 `references/data/`。

## 参考资料

- `references/router-patch-guide.md` — 分发改写细则：完整补丁模板、构建集成、红线陷阱清单（极性反转、位置语义、未定义符号、尾部依赖）与错误形态对照。
- `references/case-study-protobuf-27.3.md` — protobuf-27.3 完整落地记录：环境与方法学声明、A/B 全量数据、正确性验证、反面教训（`#ifndef` 极性反转 = 指令级 no-op、引用未定义符号 = 链接失败）与结论局限。
- `protobuf-optimization` skill — 上游方法论：函数级收益外推到生产前，先按其"画像对齐 + 外推铁律"确认校验函数生产占比；其负结果档案记录过"utf8 校验快路径微基 +4%、生产≈0（占比 0.01%）"的标准反例，是本 Skill 热点占比门槛（< 3% 拒绝）的依据。

## Compatibility

- 目录自包含：`SKILL.md` + `scripts/` + `references/` + `cases/` 即全部依赖；脚本与用例路径均以技能目录为根，拷贝该目录到任意位置即可直接运行。
- 输出只有 `utf8_route_result` 一个顶层对象，不读写任何其他 skill 的结果对象。
- 失败统一走 `utf8_route_result.success=false` + `error_message`，由调用方决定如何呈现。
- 变更只允许发生在 `utf8_range.c` 与构建源列表；不改 `utf8_range.h` 公开 API，不改 SSE4.1 分支。

## 规则

1. **极性红线**：路由块必须 `#ifdef __aarch64__`。写成 `#ifndef` 时目标平台编译回 naive = 指令级 no-op（实测反汇编与基线 100% 一致、零收益），且非目标平台分支引用未声明符号。提交前必须 objdump 确认 IsValid 含 `bl <utf8_range>` 路由调用、`utf8_range` 符号内含 `tbl` 等 NEON 助记符（注意 intrinsic 名 vqtbl1q_u8/vld1q_u8 对应助记符 `tbl`/`ldr qN`）。
2. **位置语义红线**：`return_position=1` 严禁调 `utf8_range()`——非 0 返回值当"前缀长度"用即语义错误；只允许 naive 回退或另行实现前缀定位。
3. **返回值语义红线**：判非法必须用 `utf8_range(...) != 0`。SIMD 块内错误返回 -1，但 `<16` 字节尾部经 `utf8_naive` 返回 >0 的 1-based 错误下标；`== -1` / `< 0` 判定会放过尾部非法输入，形成静默的 UTF-8 校验绕过。
4. **符号先查后用**：补丁中每个被调函数先 `grep -rn` 到定义。实测教训：引用不存在的 `utf8_range_ValidateUTF8Native` 直接导致链接失败。
5. **不手写 SIMD**：库内有现成 NEON 实现必须复用；没有则拒绝并移交向量化。
6. **无证据不宣称**：没有 fuzz 逐位一致 + 没有绑核 A/B 原始数据（≥3 进程 × 5 样本中位），不得报告收益；原始数据归档到技能目录。
7. **负结果记账**：ASCII 无变化、rp=1 无变化是预期行为，写入报告但不视为回归；真正的回归是 rp=0 多字节 < 2x 或 fuzz 不一致。
8. **源码树零污染**：验证期所有改动在临时工作区副本上进行，确认后才回写用户源码树。
