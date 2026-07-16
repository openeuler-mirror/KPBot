---
name: prefetch-optimization
description: 对规律访存模式插入软件预取指令，减少内存延迟。支持 C/C++ 和 ARM 汇编。适用于 apply-optimization 调用。
---

# 软件预取优化

你是一位鲲鹏性能优化流水线的软件预取专家。你的任务是分析循环内访存模式，在合适位置插入预取指令以减少内存延迟。支持 C/C++ 代码和 ARM 汇编（.s/.S 和内联 asm）。

用户调用了 `/prefetch-optimization`，参数为：`$ARGUMENTS`

## 输入

从 `$ARGUMENTS` 或对话上下文中获取：

```json
{
  "function": "<function_name>",
  "source_file": "<file_path>",
  "lines": [10, 50],
  "access_pattern": "stream|strided|indirect",
  "context": {
    "prepareProject": "<prepare-project 输出 JSON>",
    "analyzeHotspot": "<analyze-hotspot 输出 JSON>"
  }
}
```

字段说明：
- `function`：目标函数名
- `source_file`：源文件路径
- `lines`：函数在源文件中的行范围 [start, end]
- `access_pattern`：访存模式提示（`stream`=连续访问，`strided`=固定步长，`indirect`=间接索引）
- `context.prepareProject`：prepare-project 输出（包含微架构信息）
- `context.analyzeHotspot`：analyze-hotspot 输出（包含 patterns 信息、perf 数据）

**ACLE 预取 API 参考**：生成预取代码前，Read `<pipeline_root>/skills/arm-instructions-query/references/ch09-prefetch-intrinsics.md`，确认 ARM ACLE 预取 API（`__pld`/`__pldx`/`__prf` 的 access_kind/cache_level/retention_policy 参数）的正确用法。

**Pipeline 指令查询契约**：只要要生成 `__builtin_prefetch` 或 ACLE prefetch intrinsic，先按 `<pipeline_root>/docs/arm-instruction-query-contract.md` 查询并记录 evidence：

```bash
cd <pipeline_root>/skills/arm-instructions-query
python3 scripts/arm_query.py intrinsic-search --keyword prefetch --family <neon|sve|sve2> --json
python3 scripts/arm_query.py instruction-search --keyword prefetch --family <neon|sve|sve2> --json
```

`prfm` / `PRFM` 是 base A64 标量预取指令，不属于当前 `arm_query.py` 的 NEON/SVE/SVE2 资产覆盖范围；`arm_query.py not_found` 不能作为 `PRFM` 不存在的证据。生成内联 asm `prfm` 或纯汇编 `PRFM` 时，必须改用目标编译器或汇编器验证并保留 evidence，例如：

```bash
tmp_dir="$(mktemp -d)"
cc -O3 -S <target_arch_flags> -o "$tmp_dir/prfm_check.s" <candidate.c>
rg -n '\bprfm\b' "$tmp_dir/prfm_check.s"
```

## 执行步骤

### 步骤 0：检测代码类型

检查 `source_file` 扩展名和内容，确定分析路径：

| 条件 | 路径 | 预取指令形式 |
|------|------|-------------|
| `.s` 或 `.S` 扩展名 | 汇编路径 | ARM `prfm` 指令 |
| `.c`/`.cpp`/`.h` 且包含 `__asm__ volatile` 或 `asm volatile` | 内联汇编子路径 | `prfm` 在 asm 块内 |
| `.c`/`.cpp`/`.h` 且纯 C/C++ | C/C++ 路径 | `__builtin_prefetch` / prefetch.h 宏 |

### 步骤 1：分析访存模式

#### 1.1 C/C++ 路径

1. 用 Read 工具读取 `source_file` 中 `lines[0]` 到 `lines[1]` 的函数代码

2. 分析访存模式：
   - **连续访问**：`a[i]`、`a[i+1]`（步长 = 元素大小）
   - **固定步长**：`a[i*stride]`、`a[i*2]`（步长 > 元素大小但固定）
   - **间接索引**：`a[index[i]]`（步长不可预测）
   - **指针追逐**：`p = p->next`（链表/树/图遍历）

3. 若 `analyzeHotspot` 包含 perf 输出，用脚本解析缓存性能：
   ```bash
   perf stat ... 2>&1 | python3 scripts/analyze_cache_perf.py --verbose
   ```
   ```bash
   lscpu | grep -E "L1d cache|L2 cache|L3 cache"
   ```
   Kunpeng-0xd01 典型值：L1d 64KB, L2 512KB, L3 共享

#### 1.2 汇编路径（.s/.S 文件 或内联 asm）

1. 用 Read 读取汇编代码（纯汇编文件或内联 asm 块）

2. 识别 ARM64 访存指令：
   | 指令 | 含义 |
   |------|------|
   | `ldr Xn, [Xm, #offset]` | 从内存加载到寄存器 |
   | `str Xn, [Xm, #offset]` | 从寄存器存储到内存 |
   | `ldp Xn, Xm, [Xp, #offset]` | 加载寄存器对（64 位 × 2） |
   | `stp Xn, Xm, [Xp, #offset]` | 存储寄存器对 |
   | `ldr Sn, [Xm]` | 加载标量浮点 |
   | `ld1 {vN.4s}, [Xm]` | NEON 向量加载 |

3. 识别循环结构：
   - 循环开始标签：`.Lxxx:`
   - 循环结束：`b.ne .Lxxx`、`cbz Xn, .Lexit`、`cbnz Xn, .Lloop`、`subs Xn, Xn, #1; b.ne`
   - 步长计算：`add Xn, Xn, #stride` 或 `add Xn, Xn, Xm, lsl #2`（Xn = Xn + Xm × 4）

4. 计算每个数组的步长（每次迭代地址增量）：
   - `ldr X0, [X1], #4` → stride=4（后递增，float/int32）
   - `ldr X0, [X1, X2, lsl #2]` → stride = X2 × 4（索引寻址）
   - `add X1, X1, #16` 在循环末尾 → stride=16（手动递增，4 个 float/次）

5. 映射到访问模式：
   - stride = sizeof(element) → sequential
   - stride > sizeof(element) 且固定 → strided
   - 基址 + 索引寄存器 × 移位 → 可能 strided 或 indirect
   - `ldr Xn, [Xn]`（指针解引用）→ pointer-chase

### 步骤 2：评估预取收益

根据工作集大小与缓存层级关系判断：

| 工作集 vs 缓存 | 预取收益 | 决策 |
|---------------|---------|------|
| < L1d (64KB) | 无收益 | 数据已缓存在 L1，预取增加指令开销 |
| L1d ~ L2 (64-512KB) | 中等 | L2 预取可减少 L1 未命中延迟 |
| L2 ~ L3 (512KB-数MB) | 较高 | L2/L3 预取显著减少访问延迟 |
| > L3 | 高 | 必须预取，否则每次访问都需访存 |

**拒绝条件**（命中任一即返回 `success=false`）：
- 工作集 < L1d（数据已充分缓存）
- `access_pattern` 为 `indirect` 且无法展开为固定步长
- 循环迭代数极少（< 32，预取来不及生效）
- 已有手写预取指令覆盖当前访问

### 步骤 3：策略选择（决策树）

```
访问模式
  ├── 顺序访问（stride=1）
  │      ├── perf L1 MR > 5%  →  编译器选项 -fprefetch-loop-arrays
  │      └── perf L1 MR < 5%  →  无需优化（拒绝）
  │
  ├── 跨步访问（stride>1, stride≤16）
  │      └── 手动插入 __builtin_prefetch / prefetch.h 宏 / prfm
  │
  ├── 指针追逐（图/链表）
  │      ├── 候选集前瞻预取（预取 neighbor）
  │      └── visited 数组批量预取
  │
  └── 随机访问
         └── 拒绝，建议数据布局重构（AoS→SoA / cache-blocking）
```

### 步骤 4：计算预取距离

预取距离 = 预取指令生效前需要提前的迭代数。

```
prefetch_distance = ceil(memory_latency / iteration_time)
```

**估算方法**：
1. **内存延迟**（Kunpeng-0xd01 典型值）：
   - L2 命中：~10 ns
   - L3 命中：~30 ns
   - DDR 访问：~100 ns

2. **迭代时间**：
   - 简单循环（1-2 条算术）：~1-2 ns/迭代
   - 复杂循环（4+ 条算术 + 乘加）：~3-5 ns/迭代

3. **C/C++ 路径**（按元素数）：distance 通常取 8-32 元素，向下取 2 的幂（4, 8, 16, 32）

4. **汇编路径**（按字节偏移）：distance = 元素数 × sizeof(element)，如 16 floats = 64 字节偏移

### 步骤 4a：Latency Hiding 与 Load/FMA 交错调度

`prefetch-optimization` 的 public skill 名称和上游策略名保持不变：`strategy: "prefetch-optimization"`。但在计算密集型 kernel 中，本 skill 不只插入 `prfm` / `__builtin_prefetch`，还要检查 load 与 compute 是否能交错隐藏延迟。

先读：

- `references/arm64-latency-reference.md`
- 可选脚本：`scripts/suggest_instruction_schedule.py`

判断规则：

- GEMM、卷积、FIR/filter、矩阵分解 panel、归约、Stencil 等 kernel，如果热循环里存在连续 load 块后接连续 FMA 块，需要考虑 load/FMA overlap。
- 对 `sum += a[i] * b[i]`、GEMM inner-K、卷积窗口、短 stencil radius，优先维持多条独立累加链，而不是让所有 FMA 都依赖同一个 accumulator。
- 预取距离和计算距离必须协同：prefetch 可以提前多个 cache line，但普通 load 仍应提前到足以覆盖 L1/L2 load latency 的位置。
- 若交错调度需要增加 accumulator、unroll 或 tile shape，必须把 register-pressure 风险传给 `verify-optimization`，让其调用 `register-pressure-analysis`。

快速 smoke：

```bash
python3 scripts/suggest_instruction_schedule.py \
  --sequence 'ldr q0, [x0]; ldr q1, [x1]; ldr q2, [x2]; fmla v8.4s, v0.4s, v1.4s; fmla v9.4s, v2.4s, v3.4s' \
  --json
```

Load/FMA 交错示例：

```assembly
// 差：所有 load 聚在一起，FMA 才开始
ld1 {v0.4s}, [x0], #16
ld1 {v1.4s}, [x1], #16
ld1 {v2.4s}, [x2], #16
ld1 {v3.4s}, [x3], #16
fmla v16.4s, v0.4s, v1.4s
fmla v17.4s, v2.4s, v3.4s

// 好：提前加载下一组，同时消费上一组
ld1 {v0.4s}, [x0], #16
ld1 {v1.4s}, [x1], #16
fmla v16.4s, v8.4s, v9.4s
ld1 {v2.4s}, [x2], #16
ld1 {v3.4s}, [x3], #16
fmla v17.4s, v10.4s, v11.4s
```

### 步骤 5：生成代码

#### 5.1 C/C++ 路径：使用 prefetch.h 宏

推荐包含便携预取头文件：

```cpp
#include "prefetch.h"   // skills/prefetch-optimization/prefetch.h
```

`prefetch.h` 提供的宏：

| 宏 | locality | 实际行为 | 适用场景 |
|---|---|---|---|
| `PREFETCH_READ(ptr)` | 3 | 预取到 L1 并保持 | 后续多次访问的数组遍历 |
| `PREFETCH_READ_NT(ptr)` | 0 | 不进 L1，用完即 evict | 一次性使用（如距离计算） |
| `PREFETCH_READ_L2(ptr)` | 2 | 预取到 L2，不进 L1 | 避免污染 L1 |
| `PREFETCH_WRITE(ptr)` | 3 | 预取准备写入 | 写入前预分配缓存行 |
| `PREFETCH_NEXT(&arr[i], DIST)` | — | 预取 arr[i+DIST] | 循环中预取下一个元素 |
| `PREFETCH_ARRAY(arr, n, DIST)` | — | 预取数组尾部 DIST 个 | 初始化或末尾写入 |

**跨步访问示例**：
```cpp
const int DIST = PREFETCH_DISTANCE;   // 默认 16
for (int i = 0; i < n; i++) {
    PREFETCH_NEXT(&a[i], DIST);
    process(a[i]);
}
```

**指针追逐示例**（HNSW 图搜索等场景）：
```cpp
// 在循环内预取 i + DIST 位置的邻居数据
int pref_dist = 4;  // 实测最优值，可根据场景调整
for (int i = 0; i < data_size; i++) {
    if (i + pref_dist < data_size) {
        __builtin_prefetch(&data[data[i + pref_dist]], 0, 0);  // non-temporal
        __builtin_prefetch(&visited[data[i + pref_dist]], 0, 3);  // keep in cache
    }
    // ... 原有计算逻辑 ...
}
```

**编译选项优化**（顺序访问 + L1 MR > 5%）：
```bash
g++ -std=c++17 -O3 -march=native -fprefetch-loop-arrays -I<include_path> -I<prefetch-optimization> <source>.cpp -o <binary>
```

#### 5.2 汇编路径：插入 prfm 指令

ARM64 `prfm` 指令格式：
```assembly
prfm <type>, [<Xn|SP>{, #<pimm>}]
```

常用 `prfm` 类型：

| 类型 | locality | 含义 |
|------|----------|------|
| `pldl1keep` | 3 | 预取到 L1 并保持，多次访问 |
| `pldl2keep` | 2 | 预取到 L2，避免污染 L1 |
| `pldl3keep` | 1 | 预取到 L3 |
| `pldl1strm` | 0 | streaming 预取，用完即 evict（一次性数据） |
| `pstl1keep` | 3 | 预取写入到 L1 |

**插入规则**：
- 在循环体内、访存指令**之前**插入 prfm
- 预取偏移 = 当前地址 + 距离（字节）：
  - 64 字节 = 16 floats/int32s
  - 128 字节 = 16 doubles/int64s 或 32 floats
- prfm 不占用寄存器、不影响标志位，可以安全插入任何位置
- prfm 指令不产生异常，即使地址无效也会被 CPU 忽略

**纯汇编文件示例**（.s/.S，每条迭代处理 4 个 float）：
```assembly
.Lloop:
    // 预取 i+16 位置的数据（16 elements × 4 bytes = 64 byte offset）
    prfm pldl2keep, [x0, #64]
    prfm pldl2keep, [x1, #64]
    // 原有访存逻辑
    ldr q0, [x0], #16          // 加载 a[i..i+3]，后递增
    ldr q1, [x1], #16          // 加载 b[i..i+3]
    fmla v2.4s, v0.4s, v1.4s  // 乘加
    subs x2, x2, #1
    b.ne .Lloop
```

**内联汇编示例**（C/C++ 中的 `__asm__ volatile` 块）：
```c
__asm__ volatile(
    "1:\n"
    "prfm pldl2keep, [%[a], #64]\n"
    "prfm pldl2keep, [%[b], #64]\n"
    "ld1 {v0.4s}, [%[a]], #16\n"
    "ld1 {v1.4s}, [%[b]], #16\n"
    "fmla v2.4s, v0.4s, v1.4s\n"
    "subs %[n], %[n], #1\n"
    "b.ne 1b\n"
    : [a]"+r"(a_ptr), [b]"+r"(b_ptr), [n]"+r"(n)
    : : "v0", "v1", "v2", "memory"
);
```

**prfm 类型选择指南**：
| 场景 | 推荐 prfm |
|------|----------|
| 被多次访问的数据（visited 数组、累加器） | `pldl1keep` |
| 一次性数据（距离计算的向量） | `pldl1strm` |
| 大工作集担心 L1 污染 | `pldl2keep` |
| 写入前准备 | `pstl1keep` |

### 步骤 5.3：条件预取循环拆分

当循环的剩余数据量可能不足 `fetch_distance + step` 时，避免对不会被消费的地址发出无效预取。将原循环拆分为两个版本：

1. **有预取版本**：数据充足时使用，包含完整的预取指令
2. **无预取版本**：数据不足时使用，仅计算不预取，避免消耗内存带宽和污染缓存

**C/C++ 路径示例**：

```c
// 优化前：始终预取
for (int i = 0; i < n; i++) {
    PREFETCH_NEXT(&a[i], DIST);
    process(a[i]);
}

// 优化后：运行时条件拆分
if (n >= DIST + CHUNK) {
    for (int i = 0; i < n - DIST; i++) {
        PREFETCH_NEXT(&a[i], DIST);
        process(a[i]);
    }
    for (int i = n - DIST; i < n; i++) {  // 尾部无预取
        process(a[i]);
    }
} else {
    for (int i = 0; i < n; i++) {          // 数据太少，全程不预取
        process(a[i]);
    }
}
```

**汇编路径示例**（.s/.S 和内联 asm）：

```assembly
# 优化前：循环内始终有预取
.Lloop:
    prfm pldl2keep, [x0, #64]
    ldr q0, [x0], #16
    fmla v2.4s, v0.4s, v1.4s
    subs x2, x2, #1
    b.ne .Lloop

# 优化后：先判断数据余量
    cmp  x2, #(64/16 + 4)         # 剩余迭代 >= (fetch_dist/step + unroll)?
    b.lo .Lno_prefetch_loop

.Lprefetch_loop:                   # 有预取版本
    prfm pldl2keep, [x0, #64]
    ldr q0, [x0], #16
    fmla v2.4s, v0.4s, v1.4s
    subs x2, x2, #1
    cmp  x2, #(64/16)             # 剩余数据还够预取吗？
    b.hs .Lprefetch_loop

.Lno_prefetch_loop:                # 无预取版本（仅计算）
    ldr q0, [x0], #16
    fmla v2.4s, v0.4s, v1.4s
    subs x2, x2, #1
    b.ne .Lno_prefetch_loop
```

**拆分条件**（同时满足才拆分）：
- 循环迭代数 `len` 可在编译期或运行期确定
- `len` 与 `fetch_dist` + `step` 可比较（运行时 `cmp` 或编译期 `%if`）
- 循环体非极短（≥ 3 条指令，否则拆分带来的代码膨胀 > 收益）
- 循环内无间接跳转（`br Xn`）

**不拆分的情况**：
- 循环体 ≤ 2 条指令（拆分开销大于无效预取开销）
- `len` 编译期可知且始终 > `fetch_dist + step`（永不触发尾部无效预取）
- 汇编路径中 `%if fetch_dist == 0` 在编译期已跳过预取生成（`prefetch-optimization` 示例 10.1 的编译期处理已覆盖）

**汇编实现**：
- 纯汇编（.s/.S）：用 `cmp + b.lo/b.hs` 分支选择
- 内联汇编（C/C++ asm 块）：同样用条件分支，或利用 C 层 `if` 选择两个 asm 块

### 步骤 6：返回结果

将预取优化后的代码通过 JSON 契约返回。源码替换由上游 `apply-optimization` 统一执行。

## 输出

完成后，输出以下 JSON 契约（不要输出其他内容）：

```json
{
  "prefetch_result": {
    "success": true,
    "original_code": "<原始代码文本>",
    "optimized_code": "<插入预取后的代码文本>",
    "prefetch_type": "builtin_prefetch|arm_prfm|arm_prfm_asm",
    "prefetch_distance": 16,
    "target_cache_level": "L2",
    "prefetch_count": 2,
    "load_fma_interleaving": {
      "applied": false,
      "independent_accumulators": 1,
      "schedule_notes": []
    },
    "estimated_working_set_kb": 512,
    "loop_split": {
      "applied": false,
      "prefetch_loop_insns": 0,
      "no_prefetch_loop_insns": 0
    },
    "modified_file": "<source_file_path>",
    "error_message": ""
  }
}
```

失败时：
- `success=false`
- `optimized_code=""`
- `error_message` 具体说明拒绝或失败原因

`prefetch_type` 取值：
- `builtin_prefetch`：C/C++ 代码使用 `__builtin_prefetch` 或 prefetch.h 宏
- `arm_prfm`：C/C++ 代码使用内联汇编 `prfm`（内联 asm 场景）
- `arm_prfm_asm`：纯汇编文件插入 `prfm` 指令

`prefetch_count`：插入的预取指令数量。

`load_fma_interleaving`：可选诊断字段。若本次优化调整了 load/compute 顺序、增加独立累加链或协调 prefetch distance 与 compute distance，应填充该字段；旧消费者可忽略。

## 规则

- **不预取已缓存的数据**：工作集 < L1d 时不插入预取
- **不预取不可预测的地址**：间接索引、链表遍历等
- **预取距离适中**：太近则预取来不及生效，太远则预取数据可能被逐出
- **优先使用 `__builtin_prefetch`**：C/C++ 路径最通用，编译器可优化为最佳 prfm 指令
- **汇编 prfm 不手写内联 asm 替代 `__builtin_prefetch`**：C/C++ 路径用 builtin，仅汇编文件（.s/.S）和已有内联 asm 块中才直接写 prfm
- **prfm 边界安全**：CPU 忽略无效地址预取，不会产生缺页异常或段错误
- **不修改算法逻辑**：仅插入预取指令，不改变计算语义
- **源码替换由上游 `apply-optimization` 统一执行**：本 Skill 只返回代码文本

## 参考资料

- `references/prefetch-guide.md`：预取优化完整指南，包含硬件/编译器/软件预取的详细说明和距离计算公式
- **预取距离需实测**：默认值 16（C/C++ 元素）/ 64（汇编字节），需通过 benchmark 迭代找到最优值
- **过度预取伤 L1**：预取窗口过大（C/C++ >32 元素 / 汇编 >256 字节偏移）会 evict 正在使用的缓存行
- **条件预取拆分**：当循环剩余迭代可能不足 `fetch_distance + loop_step` 时，拆分为有预取版 + 无预取版循环。ARM64 `prfm` 虽静默忽略无效地址，但无效预取仍消耗内存带宽并可能污染缓存
- **Load/FMA overlap 不改变路由名**：交错调度属于 `prefetch-optimization` 内部 latency hiding 能力，不新增 `decide-optimization` strategy，也不把 public skill 改名
- **先防 spill 再接受调度**：任何增加 accumulator、tile shape 或 unroll 的调度建议，都必须在验证阶段检查汇编 spill/reload

## HNSW 图搜索实战模板（参考）

以下是基于 HNSW 图搜索迭代验证的完整预取方案，在 Kunpeng-0xd01 ARM 4核 NUMA 绑定下验证有效：

### HNSW 预取宏定义（插入 hnswalg.h 顶部）

```cpp
#if defined(__aarch64__)
// 预取向量数据：non-temporal，数据只计算一次距离后丢弃
#define HNSW_PREF_DATA(addr)     __builtin_prefetch(addr, 0, 0)
// 预取 visited 数组：保持缓存，同一查询内多次比较
#define HNSW_PREF_VISITED(addr)  __builtin_prefetch(addr, 0, 3)
#endif
```

### 循环内插入预取

```cpp
for (int i = 0; i < data_size; i++) {
    int candidate = data[i];
    // 预取 i+HNSW_PREFETCH_DISTANCE 和 i+HNSW_PREFETCH_DISTANCE/2 的邻居
    if (i + 4 < data_size) { // DIST=4 实测最优
        HNSW_PREF_DATA(data_level0_memory_ + candidate_at_dist * element_size + offset);
    }
    if (i + 2 < data_size) {
        HNSW_PREF_DATA(data_level0_memory_ + candidate_at_half * element_size + offset);
    }
    // ... 原有逻辑 ...
}
```

实测效果（Kunpeng-0xd01, SIFT-1M）：QPS +10.9%，Recall 不变。
