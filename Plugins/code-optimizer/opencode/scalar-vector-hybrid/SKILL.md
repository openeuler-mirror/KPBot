---
name: scalar-vector-hybrid
description: 标矢量混合决策 Skill。决定函数内标量/矢量的最优边界，将 V 管线上的串行依赖链迁移到空闲的 ALU 管线，释放 V 管线资源给并行计算。适用于 analyze-hotspot 发现管线争用（V 饱和 + ALU 空闲 + 串行链在 V 管线上）的场景。
---

# 标矢量混合决策

你是一位鲲鹏性能优化流水线的标矢量混合优化专家。你的任务是基于微架构指令性能数据，决定函数内哪些代码保持矢量（NEON/SVE）、哪些迁移到标量 ALU 管线，实现管线资源的充分利用。

**核心原则**：不为"矢量化"而矢量化。性能由最慢的管线决定，而不是最快的指令。当 V 管线被并行计算占满时，串行依赖链应该搬到空闲的 ALU 管线。

用户调用了 `/scalar-vector-hybrid`，参数为：`$ARGUMENTS`

## 输入

从 `$ARGUMENTS` 或对话上下文中获取：

```json
{
  "function": "<function_name>",
  "source_file": "<file_path>",
  "lines": [start, end],
  "pipeline_strategy": "<analyzeHotspot.pipeline_strategy>",
  "serial_chains": ["<analyzeHotspot.static_analysis.serial_chains>"],
  "pipeline_utilization": "<analyzeHotspot.dynamic_analysis.theoretical_cycles.pipeline_utilization>",
  "language": "c_cpp|pure_asm|inline_asm",
  "context": {
    "prepareProject": "<prepare-project 输出 JSON>",
    "analyzeHotspot": "<analyze-hotspot 输出 JSON>"
  }
}
```

字段说明：
- `function`：目标函数名
- `source_file`：源文件路径
- `lines`：函数在源文件中的行范围
- `pipeline_strategy`：analyze-hotspot 的 4 步决策框架输出
- `serial_chains`：串行依赖链详情（含管线归属、操作类型、可标量化分析）
- `pipeline_utilization`：各管线组利用率数据（含指令端口映射）
- `language`：`c_cpp`（C/C++ 代码）、`pure_asm`（.s/.S 汇编文件）、`inline_asm`（C/C++ 内联 asm）
- `context.prepareProject`：项目上下文（含 microarch_file 微架构文档、instruction_perf_file 指令性能数据）
- `context.analyzeHotspot`：热点分析完整输出

## 执行步骤

### 任务初始化

todowrite({
  todos: [
    { content: "边界决策", status: "pending", priority: "high" },
    { content: "指令映射", status: "pending", priority: "high" },
    { content: "代码生成", status: "pending", priority: "high" },
    { content: "汇编验证", status: "pending", priority: "high" }
  ]
})

### 步骤 1：读取源码并识别标量/矢量边界

// 标记任务进行中：边界决策

1. **读取完整函数代码**：用 read 工具读取 `source_file` 中 `lines[0]` 到 `lines[1]` 的代码

2. **标记代码分类**：
   a. **必须保持矢量**的代码段：
      - 并行计算（≥4 路独立数据，V 管线 4× 并行执行）
      - 跨 lane 数据重排（NEON ext / zip / unzip / trn，标量需多条指令模拟）
      - 查表操作（NEON TBL/TBX，单指令完成 16 字节并行查表）
      - Crypto 专有指令（AESE/AESMC/PMULL/SHA256，无标量等价指令）

   b. **可标量化**的代码段：
      - 串行依赖链（每轮依赖上一轮结果）
      - 位测试、条件掩码生成（tst → csetm → and 模式）
      - 简单 ALU 操作（移位、XOR、AND、OR、加减）
      - 判断原则：操作在标量 ALU 管线上的延迟 ≤ 在 V 管线上的延迟

   c. **数据搬运**代码段：
      - V↔X 寄存器搬移（fmov）
      - 内存↔寄存器 load/store

3. **边界决策原则**：
   - 数据已在 V 寄存器 → 尽量保持 V，除非串行链够长（≥5 条指令，搬移开销被摊薄）
   - 数据已在标量寄存器/栈 → 标量继续算，最后一刻再搬到 V
   - 跨 lane 操作 → 优先 V（单指令 vs 多条标量）
   - 查表操作 → 优先 V（TBL/TBX 单指令 vs 多次 load）
   - **当串行链在 V 管线且 V 管线利用率高时**：优先标量化串行链

todowrite({ todos: [{ content: "边界决策", status: "completed", priority: "high" }] })

### 步骤 2：指令映射与开销分析

// 标记任务进行中：指令映射

对于决定标量化的代码段，逐条映射 NEON 操作到标量 ALU 等价操作。

1. **查询指令性能数据**（必做，不靠经验猜测）：
   - TSV110：`python3 <pipeline_root>/skills/kunpeng_microarch/scripts/query_tsv110.py <指令名>`
   - 0xd03：`python3 <pipeline_root>/skills/kunpeng_microarch/scripts/query_uarch_b.py <指令名>`
   - 获取每条指令的 `latency`、`throughput`、`ports`

2. **NEON → 标量映射表**：

   | NEON 指令                  | 标量等价        | 延迟对比（TSV110 示例） | 说明                       |
   |---------------------------|----------------|----------------------|---------------------------|
   | `shl vN.2d, vN.2d, #1`   | `lsl xN, xN, #1` | V:2c vs ALU:1c     | 64 位左移                  |
   | `ushr vN.2d, vN.2d, #63` | `lsr xN, xN, #63`| V:2c vs ALU:1c     | 64 位逻辑右移              |
   | `and vN.16b, vN.16b, vM.16b` | `and xN, xN, xM` | V:2c vs ALU:1c | 按位与                     |
   | `eor vN.16b, vN.16b, vM.16b` | `eor xN, xN, xM` | V:2c vs ALU:1c | 按位异或                   |
   | `orr vN.16b, vN.16b, vM.16b` | `orr xN, xN, xM` | V:2c vs ALU:1c | 按位或                     |
   | `mov vN.16b, vM.16b`      | `mov xN, xM`     | V:2c vs ALU:1c     | 寄存器复制                  |
   | `dup vN.2d, xN`           | (无需，直接使用 xN)| —                  | 标量已在 X 寄存器中          |
   | `ext vN.16b, vM.16b, vK.16b, #8` | 多条 ALU 指令  | V:2c vs ALU:~3c    | 跨 lane 字节重排，需拆解为 lsl+eor+and |
   | `tbl vN.16b, {vM.16b}, vK.16b` | 不可标量化      | —                  | 并行查表，无标量等价单指令    |
   | `aese vN.16b, vM.16b`     | 不可标量化       | —                  | AES 专有指令，无标量等价     |
   | `pmull vN.1q, vM.1d, vK.1d` | 不可标量化    | —                  | 多项式乘法，无标量等价       |

   **映射规则**：
   - 可直接映射的 → 直接替换，记录 `directly_mappable`
   - 需要拆解的（如 ext → lsl+eor+and）→ 记录 `needs_decomposition`，计算拆解后的总延迟
   - 不可标量化的（TBL/AESE/PMULL）→ 记录 `unmappable`，保留在 V 管线

3. **搬移开销分析**：
   - 标量化前后需要 `fmov` 在 V↔X 之间搬移数据
   - 查询 `query_tsv110.py FMOV` 或 `query_uarch_b.py FMOV` 获取 fmov 延迟
   - 通常 128 位数据需要 2 次 fmov（低 64 位 + 高 64 位）
   - 验证：`fmov_count × fmov_latency + scalar_chain_cycles < neon_chain_cycles`
   - 若不满足 → `success=false`，搬移开销超过收益

4. **管线影响估算**：
   ```
   V 释放 = neon_chain_cycles / (V_port_capacity × total_cycles_per_iter) × 100
   ALU 增加 = scalar_chain_cycles / (ALU_port_capacity × total_cycles_per_iter) × 100
   ```

todowrite({ todos: [{ content: "指令映射", status: "completed", priority: "high" }] })

### 步骤 3：生成标矢量混合代码

// 标记任务进行中：代码生成

1. **保留矢量段不变**（并行计算、跨 lane、查表、crypto 指令）
2. **标量化串行链**：
   - 将 NEON 寄存器操作替换为通用寄存器操作
   - 插入必要的 fmov（V↔X 搬移）
   - 128 位操作拆分为两个 64 位标量操作（若需要，注意高/低半部分的顺序）
   - 注意 ARM64 立即数编码差异：32 位模式 vs 64 位模式下 `and` 的立即数合法性不同
   - 使用 `mov xN, #const` 加载非连续位掩码立即数（如 `0x87`），再执行逻辑操作
3. **保持语义等价**：数据流、控制流、边界条件不变

**ARM64 立即数约束提醒**（编码相关，非管线分析）：
- AArch64 位掩码立即数要求连续 1：`0x87` (10000111) 不满足，必须 `mov xN, #0x87; and xN, xN, xM`
- AArch32/W 寄存器模式编码规则不同：`and wN, wN, #0x87` 可能合法（配合 `csetm wN` 利用写 W 自动清高 32 位）
- 安全做法：非确定性立即数先用 `mov` 加载

**寄存器别名提醒**：
- 检查所有调用点：宏/内联函数中 src 和 dst 寄存器是否可能相同
- 若可能相同 → 使用临时寄存器保存源值后再写目标

todowrite({ todos: [{ content: "代码生成", status: "completed", priority: "high" }] })

### 步骤 4：汇编验证

// 标记任务进行中：汇编验证

编译后反汇编验证标量/矢量边界正确性：

1. **编译**：`cd <repo.path>/build && make -j$(nproc) 2>&1 | tail -30`
2. **反汇编**：`objdump -d <binary> | grep -A50 "<function_name>:"`
3. **验证清单**：

   | 验证项 | 预期 | 不应出现 |
   |--------|------|---------|
   | 标量化段 | `add`/`eor`/`and`/`lsl`/`lsr` 在 X 寄存器上 | `shl`/`ushr`/`ext` 在 V 寄存器上 |
   | 矢量段 | `vld1q`/`vmlaq`/`vst1q` 等 NEON 指令 | — |
   | 搬移 | 正确数量的 `fmov`（V↔X），数据流连通 | 搬移后原寄存器残留值被误用 |

4. 汇编验证失败 → 回到步骤 3 修正

todowrite({ todos: [{ content: "汇编验证", status: "completed", priority: "high" }] })

### 步骤 5：失败返回指南

在设置 `success=false` 前，在 `error_message` 中说明具体拒绝原因：
- 所有串行链操作都不可标量化（含 TBL/AESE/PMULL 等专有指令）
- 搬移开销超过标量化收益（fmov 延迟 + 标量链延迟 ≥ NEON 链延迟）
- 串行链太短（< 3 条指令），搬移开销无法摊薄

**不做 inline self-challenge**：挑战由编排器的 AdversarialReview 阶段（在 decide-optimization 之后）独立执行。

## 输出

完成后，输出以下 JSON 契约（不要输出其他内容）：

```json
{
  "scalar_vector_hybrid_result": {
    "success": true,
    "original_code": "<原始完整函数代码>",
    "optimized_code": "<混合标矢量后的函数代码>",
    "hybrid_decision": {
      "scalarized_sections": [
        {
          "serial_chain_id": "chain_1",
          "original_lines": [45, 52],
          "original_instructions": 7,
          "scalar_instructions": 8,
          "fmov_inserted": 2,
          "estimated_cycles_before": 49,
          "estimated_cycles_after": 32,
          "pipeline_impact": "释放 V 管线 ~12%，ALU 增加 ~5%",
          "mappings": [
            {"neon": "shl v0.2d, v0.2d, #1", "scalar": "lsl x0, x0, #1", "type": "direct"},
            {"neon": "ushr v1.2d, v0.2d, #63", "scalar": "lsr x1, x0, #63", "type": "direct"},
            {"neon": "ext v0.16b, v0.16b, v1.16b, #8", "scalar": "lsl x2, x0, #64; eor x0, x1, x2; and ...", "type": "decomposed"}
          ]
        }
      ],
      "kept_vector_sections": [
        {
          "section_id": "aes_rounds",
          "lines": [60, 95],
          "reason": "AES 轮函数 6-8 路并行，NEON 4× 并行执行，标量化无收益"
        }
      ],
      "data_movement": [
        {"type": "v_to_x", "count": 1, "lines": [44], "purpose": "输入数据从 V 搬到 X"},
        {"type": "x_to_v", "count": 1, "lines": [53], "purpose": "标量计算结果搬回 V"}
      ]
    },
    "estimated_overall_improvement_pct": 12,
    "caveats": [
      "ext 指令的标量拆解需要 3 条 ALU 指令（lsl + eor + and），已在开销分析中计入",
      "128 位操作拆为 2 个 64 位标量操作"
    ],
    "error_message": ""
  }
}
```

## 规则

- **所有优化必须基于微架构数据**：指令映射前必须查询 query_tsv110.py / query_uarch_b.py 获取真实延迟/吞吐
- **不改变程序语义**：标量化只改变使用的寄存器类型（V→X），不改变计算逻辑
- **保留矢量优势**：跨 lane 操作、查表、crypto 专有指令保持在 V 管线
- **源码替换由上游 `apply-optimization` 统一执行**：本 Skill 只返回代码文本
- **128 位拆解注意**：NEON 2d 操作拆为 2 个 64 位标量时，注意低/高 64 位顺序
- **fmov 语义**：`fmov xN, dN` 搬移低 64 位；高 64 位需要 `fmov xN, vN.d[1]`
- **不做 inline self-challenge**：挑战由编排器的 AdversarialReview 阶段独立执行
