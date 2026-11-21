---
name: simdjson-optimization
description: >
  simdjson 解析器在 Kunpeng/AArch64 上的热点优化：以 stage1（json_structural_indexer::index<N>）
  的 cycles/byte 与 GB/s 为核心指标，执行「基线建立 → perf/Top-down/SPE 取证 → 依赖链归因
  → step 加宽/寄存器复用/缓冲区对齐/数据依赖四类落地 → ctest 功能与 perfdiff 性能回归」的标准化流程。
  触发场景：simdjson stage1 慢、parse 基准 c/b 偏高、stage1 GB/s 低、is_ascii 出现
  umaxv→fmov→tbnz 串行归约链、NEON 寄存器 spill、LD_CANCEL_MISALIGN、store-to-load forwarding
  failure、step<64>/<128>/<256> 选择、AArch64 NEON/SVE 解析热点优化。
  不适用：非 simdjson 项目、GPU/NPU 调优、只允许配置调参或只要求理论建议的任务。
---

# simdjson AArch64 stage1 性能优化

你是 simdjson 解析器的 AArch64 性能优化专家。你的任务是对 simdjson 的解析热点执行标准化优化流程：**基线 → 取证 → 归因 → 落地 → 验证**，产出带完整证据链、可合入上游的优化补丁与报告。

本 Skill 的知识提炼自 simdjson v3.8/v3.9 在鲲鹏 920B（Kunpeng-0xd03）上的实测。核心信条：

1. **采样最高频的指令不一定是真瓶颈。** ARM64 非精确采样会向前滑移 2~5 条指令。实测 stage1 有 **52.5%** 采样堆在 `orr v21,v21,v15`（`error |= prev_incomplete`），但该 OR 是 1 周期、且隔了整轮循环（~50 周期）才被消费，完全被掩盖；真凶是其上游 `reduce_or → umaxv → fmov → tbnz` 的 **~12 周期串行归约链**。归因必须看依赖链，不能看采样排名。
2. **AArch64 没有 x86 `movemask` 的等价单指令。** `is_ascii` 只能走 `umaxv`（水平归约 ~5c）+ `fmov`（NEON→GPR 跨域 ~3c）；实测替换成 `vshrq+vget`（-2.3%）或 `lteq+to_bitmask`（-5.0%）都更慢——**跨域延迟不可消除，替代方案只会增加指令数**。突破方向是 ILP 掩盖，不是换单条指令。
3. **寄存器溢出是 NEON step 加宽的第一约束。** `step<128>` 非交错实测 **-4.7%**：栈帧 288→464B、NEON spill/reload 125/110→261/265，头号热点变成 `ldp q2,q4,[sp,#160]`（29%）。加宽本身不产生收益。
4. **寄存器复用与数据并行必须同时做。** 同一 `step<128>`，把 `scanner.next(in_2)` 挪到 `this->next(in_1)` 之后交错执行，spill 降到 42/45、栈帧 368B，性能从 -4.7% 转为 **+0.7%**。**先缩短寄存器生命周期，再谈加宽**。
5. **编译器自动向量化可能制造数据依赖故障。** GCC 会把连续的标量数组写批量成 NEON 16B store，随后从同一栈区读回 16B 到 q 寄存器时触发 **store→load forwarding failure**——性能上表现为一条高延迟 `ldr q`，源码上完全看不出来，只能靠 SPE/反汇编定位。
6. **未对齐访存会让指令无法发射。** Top-down/PMU 上表现为 `LD_CANCEL_MISALIGN`（Kunpeng 920 事件 `r5091`）抬升、内存子项 stall；输入缓冲区应做 **64B 对齐**（`posix_memalign`/`aligned_alloc`，ARM64 缓存行 64B，`ldp q,q` 不跨行）。
7. **"编过了"不等于"跑的是这份实现"。** simdjson 是运行时多实现分派，必须用 `SIMDJSON_FORCE_IMPLEMENTATION=arm64` + `parse -a arm64` 固定，并用 `nm`/`objdump` 确认 stage1 里真有 `umaxv`/`eor` 等 NEON 指令，否则 A/B 可能落在 fallback 或别的实现上。
8. **三层数字口径不可混用。** `stage1 c/b`（机制指标）、`stage1 GB/s`（阶段吞吐）、`all GB/s`（端到端）三者结论可能不同；本 Skill 的优化目标固定在 stage1，报告必须同时给三层。
9. **负载形态决定收益。** twitter.json 含 **15.11%** 非 ASCII 字节、31,808 个多字节 UTF-8 字符，因此非 ASCII 路径（`prev_input_block` 栈溢出）被频繁触发；纯 ASCII 输入的收益结构完全不同，换语料必须重测。
10. **每一次加宽/重排都要用 spill 计数与栈帧大小复核。** 只看源码判断"编译器会复用寄存器"是不可靠的；`objdump` 统计 `ldr q ...,[sp]`/`str q ...,[sp]` 是廉价而可靠的证据。

## 证据状态（引用数字前必读）

| 层次 | 内容 | 可信度 |
|---|---|---|
| **A. 项目实测（数字与命令见 `references/`）** | Kunpeng 920B 7260Y / openEuler 24.03 SP3 / Release -O3 / `FORCE_IMPLEMENTATION=arm64` 上的基线、`step<128>` 交错与非交错、两种 `is_ascii` 替换实验；twitter.json 语料 | 高，可按脚本复现 |
| **B. 设计方向（未附实测）** | `step<256>` 四块展开、`classify` 查表重写、`PMULL` 版 `prefix_xor`、64B 对齐分配 | 中，必须在目标机用本 Skill 的 A/B 流程重新测量后才写入结论 |
| **C. 仅来自上游注释/常识** | `prefix_xor` 注释"PMULL is apparently slow"（针对旧核 A72/N1） | 低，不得作为选型依据 |

A 层数字与具体负载强绑定；换 CPU 型号、编译器版本、语料后必须重测，不得直接复用收益百分比。

## 何时触发

- **性能类**：simdjson `parse` 基准 stage1 c/b 偏高、stage1 GB/s 低、端到端 GB/s 不达预期
- **归因类**：`perf annotate` 出现 `umaxv`/`fmov`/`tbnz` 串行链，或栈加载 `ldr q,[sp]` 成为头号热点；Top-down 出现 `LD_CANCEL_MISALIGN`、`memstall_anyload`；SPE 出现高 `total_lat` 的 LD
- **实现类**：评估 `step<64>`/`<128>`/`<256>`、`scanner.next` 与 `this->next` 交错、缓冲区对齐、`classify`/`is_ascii`/`bit_indexer` 重写
- **验证类**：优化后功能回归、perfdiff 性能对比、确认 arm64 实现真正生效
- **不触发 / 移交**：非 AArch64 平台的 SIMD 问题；纯编译选项/PGO 调优（移交 `compiler-flag-tuning`）；通用向量化改写（移交 `apply-vectorization`）；SPE 环境采集细节（移交 `arm-spe-analysis`）

## 输入

| 字段 | 必填 | 说明 |
|------|------|------|
| `repo` | 是 | simdjson 源码树（可构建），默认取当前目录或 `$SIMDJSON_ROOT` |
| `build_type` | 建议 | `Release` 用于性能，`RelWithDebInfo`/`-g` 用于 perf 符号化，默认两者都建 |
| `impl` | 是 | 目标 SIMD 实现，AArch64 固定 `arm64`；SVE 实验另起实现目录 |
| `bench_json` | 是 | 基准语料，默认 `jsonexamples/twitter.json`；需记录非 ASCII 占比 |
| `baseline` | 建议 | 已有基线（c/b 与 GB/s）；缺省时由阶段 1 建立并落盘 |
| `goal` | 是 | `stage1 c/b` / `stage1 GB/s` / `all GB/s` 三者之一，**先定后测，不得事后换判据** |
| `platform` | 是 | CPU 型号、向量宽度（NEON 128 / SVE VL）、编译器与版本、OS |

`bench_json` 的非 ASCII 占比必须测量（`LC_ALL=C grep -o` 或 Python 统计 `>=0x80` 字节），否则无法解释非 ASCII 路径的权重。

## 执行步骤

### 任务初始化

用任务列表工具建立六个阶段任务（基线、取证、归因、落地、功能验证、性能验证），每阶段完成即更新状态。**阶段 3 是门控**：归因不成立（如误判采样伪影）不得进入落地。

### 阶段 1：环境冻结与基线建立

1. **构建**（开发者模式才能拿到 `benchmark/dom/parse` 与测试目标）：

```bash
scripts/build.sh -t Release            # 性能基线
scripts/build.sh -t RelWithDebInfo     # perf 符号化与反汇编
```

2. **冻结运行环境**：固定绑核与 NUMA（`numactl -N <node> -m <node>`）、固定迭代次数、关闭其它负载；记录 CPU/编译器/OS/构建类型指纹。

3. **建立三组基线**：同一命令跑 3 次，取均值，记录 7 列输出的后 6 列：

```bash
scripts/bench.sh -a arm64 -j twitter.json -n 200 -R 3
```

基线必须落盘（建议 `perf_data/baseline_<date>.tsv`），后续所有 A/B 都与它同口径比较。实测参考值（A 层，twitter.json）：

| 指标 | 均值 |
|---|---|
| stage1 c/b | 0.577829 |
| stage1 GB/s | 3.794422 |
| stage2 GB/s | 4.283889 |
| all GB/s | 1.996804 |

4. **perf 硬件计数器**（可选，用于判断 CPU 利用率与缓存压力）：`scripts/bench.sh --perf-stat`，关注 IPC、L1-dcache miss、branch-miss。

### 阶段 2：取证（perf / Top-down / SPE）

目标是把"stage1 慢"变成**带源码位置与依赖链的证据**。三条取证路径按问题类型选用，不追求全覆盖。

2a. **perf 采样与指令级归因**：

```bash
cd build-RelWithDebInfo
perf record -e cycles:u -g -o perf.data -- numactl -N 1 -m 1 \
  ./benchmark/dom/parse -a arm64 -n 200 -t ../jsonexamples/twitter.json
perf report --stdio -i perf.data | head -40
perf annotate -i perf.data --stdio -s <stage1 符号> | head -80
```

- 必须用带调试信息的构建；`-a arm64` 与 `SIMDJSON_FORCE_IMPLEMENTATION=arm64` 同时固定。
- 若 `Release` 无符号，按历史做法用 `-g` 重建 `parse`（stage1 函数地址稳定，codegen 不变），再用 `addr2line -f -C -i` 把热点地址映射回源码行。
- **采样滑移判据**：热点指令若是分支目标后的首条指令（如 `orr v21`），且其依赖在上一轮已就绪，应按 `perf annotate` 往上游找 2~5 条指令，确认真正的停顿点（见信条 1）。

2b. **Top-down / PMU 归因**（用于对齐、访存与 store 压力）：

```bash
scripts/topdown.sh -p <parse_pid> -t 10        # 或 -c "numactl ... ./parse ..."
```

关注事件（Kunpeng 920 原始编码，见 `scripts/topdown.sh`）：

| 事件 | 含义 | 关联优化点 |
|---|---|---|
| `LD_CANCEL_MISALIGN` (`r5091`) | load 因未对齐被取消发射 | 缓冲区 64B 对齐 |
| `LD_CANCEL_FWD_HZD` (`r5094`) | load 因 store→load 前递 hazard 被取消 | 数据依赖/写回结构 |
| `memstall_anyload` (`r7005`) / `memstall_L1miss` (`r7007`) | load 相关 stall | 访存路径 |
| `ST_ORDER_FAIL` (`r50a3`) / `ST_SCA_FULL` (`r50a0`) | store 顺序/缓冲区压力 | 批量写与写回 |
| `vfp_ptag_stall` (`r2008`) / `vpd_ptag_stall` (`r200a`) | 向量寄存器重命名/端口 stall | 寄存器压力 |

2c. **ARM SPE 指令级归因**（用于高延迟 LD 与 store→load 依赖）：

```bash
perf record -e arm_spe/branch_filter=0,load_filter=1,store_filter=1/ -o spe.data -- <parse_cmd>
perf script -F cpu,op,pc,event,issue_lat,total_lat,vaddr,paddr,data_source -i spe.data > spe-ldst.csv
scripts/spe_ldst.py spe-ldst.csv --bin ./benchmark/dom/parse --op LD --top 20
```

判读：`total_lat` 显著高于同函数其它 load 的 PC，就是阶段 3d 的候选；再 `objdump -d` 对照该 PC 附近是否有 16B NEON store 后跟 16B load。

### 阶段 3：依赖链归因与门控

把阶段 2 的证据归入下表四类之一，**并给出"消掉它能换来多少"的上界**。归因不成立就回到阶段 2。

| 瓶颈 | 典型证据 | 机制 | 优化方向 |
|---|---|---|---|
| **A. `is_ascii` 串行归约链** | `umaxv→fmov→tbnz` 约 12c；采样滑移到下游 `orr` | NEON 水平归约 + 跨域传输，无法与后续指令重叠 | 加宽 step 提供 ILP、交错两条流（3a/3b） |
| **B. NEON 寄存器溢出** | 头号热点 `ldr/ldp q,[sp,#N]`；栈帧变大；spill/reload 计数上升 | 同时存活的输入块/中间结果超过 32 个 v 寄存器 | 缩短寄存器生命周期、交错消费（3b） |
| **C. 缓冲区未对齐** | `LD_CANCEL_MISALIGN` 抬升；`ldp q,q` 跨 64B 行 | 起始地址未按 64B 对齐 | 对齐分配与配套释放（3c） |
| **D. 数据依赖 / store→load** | SPE 高 `total_lat` 的 16B `ldr q`；附近有批量 `str q` | GCC 向量化连续数组写，读回时前递失败 | 避免回读、拆分/改结构（3d） |

**门控判据**：先估算理论上界——该瓶颈占 stage1 cycles 的比例 × stage1 在 all 中的占比。若 stage1 本身只占 all 的约一半（基线 0.578 / 1.086），则 stage1 提速 10% 仅带来约 5% 端到端提升，报告必须如实换算，不能把 stage1 收益写成端到端收益。

### 阶段 4：代码落地（四类优化点）

#### 4a. 数据并行：step 由 64B 提高到 128B/256B

目标：用多条独立的 `is_ascii` 归约链互相掩盖串行延迟。

- `src/arm64.cpp` 中把 `index<64>`/`minify<64>` 改为 `index<128>` 或 `index<256>`。
- 在 `src/generic/stage1/json_structural_indexer.h` 与 `json_minifier.h` 为新的 `STEP_SIZE` 增加 `step<N>` 特化，按 64B 为单位展开为 `in_1..in_4`。
- template 特化不能只改入口：**`buf_block_reader<N>` 与 `index<N>` 的其余逻辑需能处理新步长**；末尾残块仍走原有 padding 路径。

**硬约束**：加宽必须与 4b 的交错同时实施，否则寄存器压力会吞掉全部收益（实测 `step<128>` 非交错 -4.7%）。

#### 4b. 寄存器复用：`scanner.next` 与 `this->next` 交错

原顺序（问题形态）：

```cpp
json_block block_1 = scanner.next(in_1);
json_block block_2 = scanner.next(in_2);      // in_1 与 block_1 仍活跃，寄存器需求翻倍
this->next(in_1, block_1, idx);
this->next(in_2, block_2, idx + 64);
```

交错顺序（推荐形态）：

```cpp
json_block block_1 = scanner.next(in_1);
this->next(in_1, block_1, idx);               // 消费 in_1，寄存器可复用
json_block block_2 = scanner.next(in_2);
this->next(in_2, block_2, idx + 64);
```

原理：`scanner.next()` 与 `this->next()` 操作不相交的状态集，唯一耦合是值传递的 `block`，因此重排安全。实测 spill/reload 由 261/265 降到 42/45。同时可评估 `json_structural_indexer.h` 的 `STEP_UNTIL`（默认 24，实测改 12 可让尾部循环更早接管，属 B 层方向，需实测）。

#### 4c. 缓冲区地址对齐

- `include/simdjson/padded_string-inl.h`：把 `allocate_padded_buffer` 改为 64B 对齐分配（`posix_memalign` / `aligned_alloc`，Visual Studio 用 `_aligned_malloc`），并新增 `free_padded_buffer`。
- 所有释放点必须同步改为 `free_padded_buffer`（`padded_string` 析构/移动赋值、`dom::parser::loaded_bytes` 的 `unique_ptr` deleter）——**用 `delete[]` 释放对齐内存是未定义行为**。
- 目标：与 `SIMDJSON_PADDING`（64）一致，保证 `ldp q,q` 不跨 64B 行。
- 验证：Top-down 的 `LD_CANCEL_MISALIGN` 应显著下降或归零；功能测试必须覆盖空串、短串、超长串与非对齐起点。

#### 4d. 数据依赖：消除 store→load forwarding failure

症状：SPE 显示某条从栈读回 16B 到 `q` 寄存器的 `ldr` 的 `total_lat` 异常高；反汇编在其前面能看到 GCC 把连续数组写合并成 `str q`（如 `bit_indexer::write` 的 `this->tail[i] = ...` 被批量成 `str q2,[x23,#N]`）。

处置顺序：
1. **确认可回读来源**：用 `addr2line` + 源码确认该 load 读的是哪个数组/局部变量（常见是索引数组、`prev_structurals`、`prev_input_block`）。
2. **优先消除回读**：把"先写数组再读回"改为在寄存器中继续使用同一个值；或把标量写与后续读取分开到不同的生命周期。
3. **退而改变形态**：把批量 16B 写拆回标量写 / 调整数组布局与对齐 / 把回读改为标量读，选实测收益最高且不引入别处 spill 的方案。
4. **禁止无证据改写**：没有 SPE 高延迟证据时，不要仅凭"看起来会 forward fail"就改代码。

#### 落地纪律

- 一处机制一个补丁，便于单变量 A/B 与回滚。
- 不修改 `singleheader/`（由 `amalgamate.py` 生成）；如需一致性，构建后执行 `cmake --build . --target amalgamate`。
- 不引入 `-mavx2`/`-march=native` 这类会破坏运行时分派的编译选项（AArch64 上同样不得用 `-mcpu=native` 强制私有 ISA）。
- 改动前先 `git diff`/`git status` 确认工作区干净，避免把无关文件带进补丁。

### 阶段 5：功能验证

功能正确性是性能结论的前提，**先绿再谈快**：

```bash
scripts/func_test.sh -b build-Release -i arm64
```

覆盖：
- `ctest -L dom`、`ctest -L ondemand`：API 与行为回归。
- `ctest -L per_implementation`：按实现的专项测试（50 项），必须带 `SIMDJSON_FORCE_IMPLEMENTATION=arm64`。
- 边界输入：空串、1 字节、无结构字符的长串、跨 64B/256B 边界、非法 UTF-8、多字节 UTF-8 被块边界截断、超长数字/字符串。
- 对齐改动额外覆盖：非对齐起点、`padded_string` 移动/析构、`parser.load/load_many` 的缓冲区复用。

任何一项失败即回到阶段 4，不得带病测性能。

### 阶段 6：性能验证与回归

6a. **A/B 口径**：优化前后二进制分别构建，同语料、同迭代、同绑核；**5 轮 AB/BA 交错**取中位，而不是先跑完 A 再跑 B（慢漂移会伪装成收益）。

```bash
scripts/bench.sh -a arm64 -R 5                # 优化后
# 与 perf_data/baseline_*.tsv 按同口径对比
```

6b. **回归判定**：`perfdiff` 交错运行新旧 `parse` 7 次并比较吞吐区间，判定是否退化：

```bash
build-Release/benchmark/dom/perfdiff build-Release/benchmark/dom/parse_new build-Release/benchmark/dom/parse_old
```

6c. **寄存器证据复核**（针对 4a/4b）：统计新二进制的 NEON spill/reload 与栈帧，确认加宽没有把瓶颈从归约链搬到栈加载。

```bash
objdump -d build-Release/benchmark/dom/parse | grep -c 'ldr[[:space:]]*q.*\[sp'   # reload
objdump -d build-Release/benchmark/dom/parse | grep -c 'str[[:space:]]*q.*\[sp'   # spill
```

6d. **归因复核**：同参数重采 perf，确认目标指令占比下降、替代路径占比符合预期；注意采样滑移，按依赖链而非单条指令下结论。

6e. **无收益即回滚记账**：端到端收益落在轮间波动内（<2~3%）且 `goal` 不是 stage1 c/b 时，回滚并写入「反证台账」，不要用"理论上应该更快"包装成结论。

## 反证台账（实施前逐条对照）

| 尝试 | 结果 | 根因 | 启示 |
|---|---|---|---|
| `step<128>` 非交错（只加宽） | **-4.7%**（3.617 GB/s） | 栈帧 288→464B，spill/reload 261/265，头号热点变 `ldp q2,q4,[sp,#160]` 29% | 加宽必须先解决寄存器生命周期 |
| `is_ascii` 改 `vshrq + vget_low/high + vorr + vget_lane` | **-2.3%**（0.5910 c/b） | 指令数 +4，NEON→GPR 跨域延迟不变 | 换单条指令不解决跨域开销 |
| `is_ascii` 改 `lteq(0x7F) + to_bitmask（vpaddq×3）` | **-5.0%**（0.6065 c/b） | 指令数 +7，额外竞争 NEON 端口 | 同上 |
| 把 `error` 累加器当作 loop-carried 瓶颈去优化 | 无效方向 | 1 周期 OR 有约 50 周期松弛，早被掩盖 | 先判断依赖是否真在临界路径上 |
| 只信 `perf annotate` 最高占比指令 | 会误判 | 非精确采样向前滑移 2~5 条 | 按依赖链往上找真停顿点 |

**B 层待验证项**（未附实测，不得直接宣称收益）：`step<256>` 四块展开、`classify` 查表重写、`PMULL` 版 `prefix_xor`、`STEP_UNTIL` 调参、64B 对齐分配。每项落地前都要走阶段 5/6。

## 测量纪律

- **判据先定**：`goal` 在阶段 1 固定，事后不得换指标。
- **固定实现**：`SIMDJSON_FORCE_IMPLEMENTATION=arm64` 与 `parse -a arm64` 同时使用；用 `scripts/verify_impl.sh` 核验二进制里确实有 NEON stage1 指令。
- **单变量**：A/B 两腿只差目标机制；构建类型、语料、迭代数、绑核、NUMA 全同。
- **交错多轮取中位**：AB/BA 交替，绝对值整体漂移不影响相对结论。
- **profiling 与测速分离**：采集 perf/SPE 的跑次不用于性能结论。
- **口径分层**：stage1 c/b、stage1 GB/s、all GB/s 同时报告，并用 stage1 占 all 的比例换算端到端上界。
- **语料形态必记录**：非 ASCII 占比、结构密度决定非 ASCII 路径与 `bit_indexer` 的权重。
- **证据可复现**：每次结论附带构建指纹、命令、原始输出路径。

## 降级处理

| 场景 | 降级动作 |
|---|---|
| `perf record` 无符号/无法 annotate | 用 `RelWithDebInfo` 或 `-g` 重建；仍不行则只做函数级 `perf report`，标注"未到指令级" |
| 无 SPE 权限 / `arm_spe` 不可用 | 用 Top-down 的 `LD_CANCEL_FWD_HZD`/`ST_ORDER_FAIL` + `objdump` 静态判断；标注低置信 |
| 非 Kunpeng 平台（无 `rXXXX` 事件） | `topdown.sh` 自动跳过不可用事件，改用通用 `perf stat` 事件（cycles/instructions/L1 miss），并说明平台差异 |
| 无法改缓冲区分配（外部托管内存） | 只做 4a/4b/4d；对齐项标注"不适用" |
| `step<256>` 实测退化 | 回退到 `step<128>` 交错形态，记录 spill 与栈帧数据 |
| 无 `perfdiff`（未构建） | 用 `scripts/bench.sh` 5 轮交错替代，标注"非官方回归工具" |

## 输出契约

优化报告（Markdown）固定结构：

```
# <项目> simdjson stage1 优化报告
## 1. 目标与环境
   goal、语料（非 ASCII 占比）、CPU/编译器/OS、构建类型、二进制 sha256
## 2. 基线
   3 组 c/b 与 GB/s（stage1/stage2/all）+ perf stat 关键计数器
## 3. 取证
   热点函数/指令 → 源码行；依赖链（含采样滑移排除）；Top-down/SPE 证据
## 4. 归因与门控
   四类瓶颈归类 + 理论上界换算（stage1 占 all 比例）+ 是否继续的理由
## 5. 落地
   每项改动：机制、文件:行、diff 摘要、单变量 A/B 数据、spill/栈帧复核
## 6. 功能验证
   ctest dom/ondemand/per_implementation 通过数、边界输入、对齐/生命周期专项
## 7. 性能验证
   5 轮交错中位、perfdiff 结论、三层口径对照、端到端换算
## 8. 反证台账
   尝试/结果/根因/启示（含本轮新增）
## 9. 剩余热点与下一步
   可优化 vs 架构下限（如 NEON→GPR 跨域是架构下限）
```

每个数字标注证据层级（A/B/C）；B 层设计方向不得写成已实现收益。

## 脚本工具

| 脚本 | 用途与用法 |
|---|---|
| `scripts/build.sh` | 开发者模式构建。`build.sh [-t Release\|RelWithDebInfo\|Debug] [-b build_dir] [-r repo] [--clean]`；默认 `-D SIMDJSON_DEVELOPER_MODE=ON`，`-j$(nproc)` |
| `scripts/bench.sh` | 运行 `benchmark/dom/parse` 多轮并聚合。`bench.sh [-a arm64] [-j twitter.json] [-n 200] [-R 5] [-b build_dir] [--perf-stat] [--numa N]`；输出每轮与中位的 stage1/stage2/all c/b 与 GB/s，落盘 `perf_data/bench_<ts>.tsv` |
| `scripts/func_test.sh` | 功能验证。`func_test.sh [-b build_dir] [-i arm64] [-L label] [-R regex]`；默认依次跑 `dom`、`ondemand`、`per_implementation`，任一失败退出非零 |
| `scripts/verify_impl.sh` | 核验 arm64 实现确实编入并被使用。`verify_impl.sh <binary>`；检查 `simdjson::arm64::` 符号、`umaxv`/`eor` 等 NEON 指令、`SIMDJSON_FORCE_IMPLEMENTATION` 生效 |
| `scripts/topdown.sh` | Kunpeng 920 Top-down/PMU 采集（分组避免 multiplexing），覆盖 `LD_CANCEL_MISALIGN`/`LD_CANCEL_FWD_HZD`/`memstall_*`/`ST_*`/向量 ptag stall。`topdown.sh -t 10 -p PID` 或 `-c "<cmd>"` |
| `scripts/spe_ldst.py` | SPE LD/ST CSV 聚合：按 PC 统计 count/mean/p99/max `total_lat`，可 `--bin` 用 `addr2line` 映射符号与源码行。`spe_ldst.py <csv> --bin parse --op LD --top 20` |

## 协作

- SPE 环境检查、采集参数与通用解析：`arm-spe-analysis` skill
- Kunpeng 微架构参数（流水线、缓存、指令延迟）：`kunpeng_microarch` skill
- 通用 NEON/SVE 向量化改写：`apply-vectorization` skill
- 编译选项、LTO/PGO：`compiler-flag-tuning` skill
- 优化后的正确性/性能门控与提交：`verify-optimization` skill

## 参考

- `references/stage1-bottleneck-cases.md`：四类瓶颈的完整取证、汇编片段、实测数据与源码位置
- `references/measurement-notes.md`：perf/Top-down/SPE 采集参数、口径陷阱与可复现命令
