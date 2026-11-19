# optimize 模式：ARM SIMD 逐函数优化

本文件是 optimize 模式的完整指令。你负责把一个 ARM 项目里值得向量化的函数逐个改写成 SVE2p1 代码，并且**用实测数字证明改完仍然正确、而且确实更快**。不是写完 intrinsics 就算完成。

入口的[模式选择](../../SKILL.md)已经完成；确认用户选择的是 optimize 后再执行本文件。文中的 `<skill>` 指技能根目录，所有相对路径都按技能根目录解析；方法库固定为 `<skill>/summary/`，由 [summary 模式](summary.md) 维护，本模式只读。

流程是“一问、二问、三问、逐个函数闭环”：先确认热点从哪来，再确认用哪种方式验证，再确认优化节奏，然后每个函数都要走完“读方法库 → 选手法 → 改写 → 正确性 → 性能 → 写回 → 小结”。入口的模式选择是第 0 问，本文件不再重复。

这个模式只做 ARM。目标向量长度固定 256 位（`-msve-vector-bits=256`），目标架构 `armv9.6-a+sve2p1+sme2p2`。在 x86 上不要使用本模式。

## 输入

用户可以用对话给出，也可以给 JSON：

```json
{
  "project_path": "<被优化的项目根目录>",
  "hotspot_file": "<热点函数文件路径，可为空>",
  "verify_method": "native | qemu",
  "mode": "auto | targeted",
  "targets": ["<指定优化的函数，targeted 模式用>"],
  "compile_cmd": "<项目自己的构建命令>",
  "sample_data": "<真实测试数据的路径，可为空>",
  "budget": "<时间/函数数量上限，可为空>"
}
```

- `project_path`：必需。没有就用 `ask_user_question` 问，不要拿当前工作目录猜。
- `hotspot_file`、`verify_method`、`mode`：走下面第一问、第二问、第三问，不要替用户决定。**每次执行 optimize 模式都要重新问这三个问题，并重新确认 `project_path`**，不沿用上一次调用留在 `state.json` 里的答案；只有同一次调用内部被中断时，才读本次账本接续（见阶段 4 末尾）。
- `compile_cmd`：项目自己的构建命令。**不要用自己拼的 gcc 命令替代项目构建**——验的是“改完项目还能编、还更快”，不是“这段代码在别的参数下更快”。
- `sample_data`：真实输入样本（如视频 YUV、图片、位流）。有就必须用，合成数据覆盖不到真实分支。

## 优化原则

### 先量“一次能连续读多少字节”，再决定用 SVE 还是 NEON

向量长度固定 256 位，一个 `z` 寄存器是 **32 字节**；NEON 的 `v` 寄存器是 **16 字节**。同一段数据用哪个方向更划算，取决于**最内层一次能连续读到多少字节**，记作 `R`：

- 行循环 `for (x = 0; x < lx; x++)` 访问 `T *p`：`R = lx * sizeof(T)`
- 固定块函数（模板 `log2Size`）：`R = blockSize * sizeof(pixel)`
- 跨步访问：`R` 是**连续那一段**的字节数；`R` 比单个元素还小，就无法在这个维度向量化

| R（字节） | 默认方向 | 理由 |
| --- | --- | --- |
| ≤ 8 | C_NEON，或先把多行打包再用量 | 128 位寄存器都填不满，SVE 浪费得更厉害 |
| 9–16 | **C_NEON** | 正好一个 128 位寄存器，不用付谓词的钱 |
| 17–31 | **C_SVE** | 一条谓词化向量覆盖整行，不必拆成两段 |
| ≥ 32 | **C_SVE** | 能填满 256 位，尾部交给 `svwhilelt` |

一句话：**一行能读超过 128 位就用 SVE，一次只能读 128 位就用 NEON。** 数据块越大，SVE 越占优；数据单位正好是 128 位时，NEON 是更紧的拟合。

这条原则同时决定先读哪个方法目录：`R > 16` 先读 `summary/auto/C_SVE/`，`R <= 16` 先读 `summary/auto/C_NEON/`。两个目录的方法都能用，只是顺序不同。

### 扩宽会改变拟合度

8 位输入扩宽到 16 位运算时，lane 数减半、每 lane 占 2 字节，16 个 `u16` 又正好填满 32 字节。所以 `R = 16`（8 位输入）这种“刚好 128 位”的情况，扩宽之后可能反而适合 SVE。这条只作提示，**以 A/B 实测为准**，不要据此直接选 SVE。

### 方向是默认起点，不是结论

上面的原则决定先写哪一版，`scripts/run_ab_bench.py` 决定留哪一版。首选方向没跑赢 1.05 时，换另一个方向再试一次——这正好用掉“每函数最多 2 种手法”的预算。不要在同一方向上反复微调参数，那是在错误的拟合度上做局部优化。

## 执行步骤

### 阶段 0：读方法库（硬门控，不可跳过）

在动手分析任何代码之前，先读本技能的方法库：

1. 方法库在 `<skill>/summary/`，由 summary 模式维护和增量融合；先确认它存在。
2. 先读人工维护区：`summary/manual/`（若同时存在 `summary/user/` 也读）。
3. 再读三个方向的索引：`summary/auto/C_NEON/summary.md`、`summary/auto/C_SVE/summary.md`、`summary/auto/NEON_SVE/summary.md`。
4. 按当前函数命中的场景，展开对应的 `patterns/<slug>.md`。**不要**把 `patterns/` 全读一遍。

优先级是 manual/user 先于 auto：人工维护的方法有人核对过，贴合度更高。

把读过什么、命中什么写进 `other/reference-digest.md`，每个函数的 `record.json` 也要带 `reference` 字段。最终报告的“方法库参考”一节就靠它。

**索引读不到就停下来告诉用户**（方法库缺失或被清空），不要凭记忆编造方法、也不要跳过这一步直接改代码。详细读取协议见 `references/modes/optimize/method-library.md`。

### 阶段 1：环境与工作区

```bash
python3 <skill>/scripts/check_env.py                          # 探测执行档与运行时 VL
python3 <skill>/scripts/init_workspace.py --project <项目根>  # 建 simd_optimization_result
```

`check_env.py` 会真编译并实际跑一个 VL 探针，不是只看版本号。

**它探测的是“这台机器能做什么”，不是“这次用哪种方式”**——后者由阶段 3 的用户选择决定。探测结果用来：给用户列出真正可选项、校验用户的选择是否可行、提前暴露“运行时 VL 不是 256 位”这类硬伤。

运行时 VL 不是 256 位时立即停下（`matches=false`）：编译期假设和运行时不一致会让每次向量访存少处理一半数据，而且不报错。

结果目录固定在**项目同级**的 `simd_optimization_result/`，结构见 `references/modes/optimize/workspace-layout.md`。项目里最终只保留验证过的 SIMD 代码。

### 阶段 2：第一问——有没有热点函数文件

**必须问用户**：

> 有没有已经采集好的热点函数文件（perf report、gprof、火焰图折叠栈，或者一份函数名+占比的清单）？有的话给我路径；没有的话我扫描整个项目找可向量化代码。

- **有**：解析文件，按占比降序处理。解析规则、失败处理、原始数据落盘见 `references/modes/optimize/hotspot-discovery.md`。解析不出来的条目要单列，**失败率过半就停下问用户**。
- **无**：跑扫描器拿候选，再逐个读代码确认。

  ```bash
  python3 <skill>/scripts/scan_candidates.py --project <项目根> --top 40
  ```

  扫描器只是按结构特征打分，不做数据流分析。它的输出是“阅读顺序”，不是结论。

两种来源都要写成 `other/hotspots/source.json`（含排序理由），这是报告里“为什么先改这个函数”的唯一依据。

### 阶段 3：第二问——用哪种方式验证

**必须问用户**，并先把两个选项的差异讲清楚：

> 正确性和性能用哪种方式验证？
> - **原生**：在你本机直接编译运行。收益最真实——真实微架构、真实内存层次、真实频率。前提是本机就是 AArch64 Linux，且 **SVE 向量长度必须是 256 位**。
> - **QEMU**：用 `qemu-system-aarch64 -cpu max` 跑裸机夹具。不需要目标硬件；计时口径是动态指令数，能证明语义正确和指令数收益，但不含微架构效应。

不要用默认值替用户决定，也不要因为某个选项更省事就引导用户。

**两种方法都必须保证运行时 VL = 256 位（32 字节）。** VL 不对时不会报错，只会让每次向量访存少处理一半数据——这是本模式最容易踩的静默错误。

#### 选原生

1. 确认本机是 AArch64 Linux（`uname -m` 返回 `aarch64`，`uname -s` 返回 `Linux`）。不是就停下来告诉用户原生验证不可行，**不要偷偷换成 QEMU**。
2. 确认并设置 VL：

   ```bash
   cat /proc/sys/abi/sve_default_vector_length          # 字节数，期望 32
   python3 -c "import ctypes; print(ctypes.CDLL('libc.so.6').prctl(50, 32, 0, 0, 0))"
   ```

   `PR_SVE_SET_VL`（`prctl` 的第 50 号）在“内核最大 VL 小于请求值”时**不报错，只返回更小的值**。必须读返回值核对，不能假设设置成功。
3. 进程内确认真实 VL：用内联汇编读 `cntb`。**不要用 `svcntb()`**——在 `-msve-vector-bits=256` 下它会被常量折叠成 32，永远“验证通过”，等于没验。
4. 本机最大 VL 只有 128 位时，二选一：改用 `-msve-vector-bits=128` 重新出方案，或放弃 SVE 方向改做 NEON。不要带着不匹配的假设继续。

#### 选 QEMU

1. 用 `qemu-system-aarch64 -M virt -cpu max`。`assets/bench/crt0.S` 会写 `ZCR_EL1.LEN = 1` 把 VL 钉在 256 位——复位值是 128 位，不写就少一半。
2. 夹具启动后读 `cntb` 自检，不等于 32 字节直接 `SYS_EXIT(2)`，不会带着错的 VL 往下测。
3. 用 `make run` 跑（内部是 `-icount shift=0`），计时为动态指令数，可完全复现。

#### 两种方法不能混用

换验证方法就要重跑性能门，两种口径的数字不可比：原生的“纳秒”包含微架构效应，QEMU 的“纳秒”实质是指令数。

把选择写进 `other/state.json` 的 `verify_method`，并写进每个 `record.json` 的 `verify_method` 与 `performance.harness`（取值同为 `native` / `qemu`，具体命令写进 `performance.harness_detail`）。阶段 5 的性能门据此选命令模板。

### 阶段 4：第三问——自动还是指定

**必须问用户**：

> 自动优化还是指定优化？

- **自动**：按顺序逐个优化，每个函数出小结后**立刻**开始下一个，中途不停。用户可以随时喊停。
- **指定**：默认从当前顺序的第一个开始，每个函数出小结后**停下等确认**；用户可以同意继续、跳过、或指定下一个函数。

两种模式的区别只有“要不要等确认”。**每个函数的优化总结都必须立刻产出**，不要攒到最后一起写——攒着写会丢细节，用户也没法及时纠偏。

本次调用的运行状态记在 `other/state.json`：`run_started_at`（本次调用开始时间，用来区分新旧调用）、`mode`（本问的答案）、`verify_method`（第二问的答案）、`hotspot_source`（第一问的答案）、`answers`（每个问题的原话摘要）、以及进度 `queue` / `cursor` / `completed` / `rejected`。开始本次调用时把 `run_started_at` 更新为当前时间；若它明显早于当前时间，说明 `state.json` 来自上一次调用，必须重新提问。重新提问后用本次答案覆盖 `answers`、`mode`、`verify_method`、`hotspot_source`，并按本次答案重建 `queue`；旧调用的 `queue` / `cursor` / `completed` 只作历史参考，不能当成本次已答或已做。用户明确要求接着上次队列时，先和用户确认再沿用。

同一次调用内部被中断或上下文被截断后，**先读本次账本里已经回答过的项，只补未完成的部分**。但**新的调用必须重新问第一问、第二问、第三问，并重新确认 `project_path`**：上一次的 `state.json` 可以作为背景信息展示给用户，不能当成本次答案直接执行。判断是不是新调用，看本次用户回合是否重新加载了本技能；重新加载就是新调用，必须重新提问。

### 阶段 5：逐函数闭环

对队列里的每个函数执行下面的 5.1～5.7。任何一步失败都要按规则处理，不能跳步。

**5.1 定位与冻结基线**

- 读函数完整体、所有调用点、它依赖的宏和静态辅助函数。
- 把原始实现复制到 `process/<fn>/ref/<file>.<fn>.ref.c`，函数改名为 `<fn>_ref`。这份副本是 A/B 的权威基线，**不进项目**。
- 记录 `why_hot`：占比、调用点数、或者结构判断（内层循环、固定小块）。

**5.2 判定可向量化性，以及“改它有没有用”**

先查一件事：**这个函数是不是 C 回退路径**。成熟项目里热点内核大多通过派发表注册（`p.pu[...].sad = sad<8,8>;`），同一操作往往已经有一份 NEON/SVE/汇编实现。如果 `aarch64/`、`arm/` 或对应架构目录里已经有同名变体被注册，那 C 版本只在没有 SIMD 支持时才会跑，改它不会改变任何运行性能。

```bash
grep -rn "<函数基名>" <项目>/source/*/asm-primitives* <项目>/source/*aarch64*/ 2>/dev/null
```

命中既存变体时，把结论记进 `record.json`（`status: rejected`，理由写清是哪一行注册的），换下一个目标。反过来，C 版本没有变体、又通过派发表注册的函数，通常就是最值得做的那一批。

然后按 `references/modes/optimize/arm-vectorization-rules.md` 第 2 节的八问逐条检查：跨迭代依赖、别名、副作用、访存连续性、溢出、浮点语义、尾部、数据布局。其中“访存连续性”这一问顺带给出下一步要用的 `R`（连续可读字节数），见第 0 节。

判定为不可向量化时，在 `record.json` 记 `status: rejected` 和理由，跳到 5.7 出小结，不要强行改写。

**5.3 定方向、选手法**

1. 先按「优化原则」算 `R` = 一行连续可读字节数，定下首选方向：`R > 16` 走 SVE，`R <= 16` 走 NEON。把 `R` 和选择写进 `record.json` 的 `analysis`，这是后面评审“为什么这么写”的依据。
2. 按方向读对应目录：SVE 先读 `summary/auto/C_SVE/`，NEON 先读 `summary/auto/C_NEON/`。人工方法（`summary/manual/`）无论哪个方向都先看。
3. 没有匹配时全网检索，**并把链接记进 `other/web-search/<fn>.md`**。
4. 仍然没有就自行设计。

任何一个 intrinsic，**先写 5 行 probe 编译一次**再写进实现。GCC 各版本对 SVE2.1 多向量 intrinsic 的支持差异很大，凭记忆写名字是最常见的返工来源。

**5.4 写实现并进项目**

新实现替换项目里原函数的函数体，签名不变。随后用**项目自己的构建命令**编译一次：

```bash
<compile_cmd>
```

编译不过就修到过，不要在“项目编不过”的状态下继续做测试——夹具能过但项目编不过，通常是头文件、宏或 `static` 作用域问题。

**5.5 正确性门控**

按 `references/modes/optimize/verification-gates.md` 建用例矩阵（`n=0/1/VL±1/大值/非对齐/极值`），逐位比较基线与新实现：

```bash
python3 <skill>/scripts/compare_outputs.py --ref <基线输出> --simd <新输出>
```

不一致时先排查夹具（运行时 VL、LTO 合并调用、输入未重置、`_x` 的未定义 lane），再怀疑算法。**不通过就不进性能测试**，更不写回。

**5.6 性能门控**

基线是“原始实现 + 项目实际编译参数”，不是 `-O0`。协议与阈值：

```bash
python3 <skill>/scripts/run_ab_bench.py --cmd "<运行命令>" --alt-cmd "<翻转顺序版本>" \
    --reps 5 --min-speedup 1.05 --label <fn>
```

- 通过线 1.05。低于 5% 的差异在真实负载里会被访存和调度淹没。
- 同一函数最多试 **2 种手法**；仍不达标就回滚，`status: rejected`，写清两次实测数字。
- 发现 GCC 已经自动向量化且手写版本没有优势时，正确答案是**放弃并记录**，而不是换一个更弱的基线把数字做漂亮。

**5.7 写回与小节**

- 只有 `correctness: pass` 且 `performance: faster` 才允许留在项目里。
- 写 `process/<fn>/record.json`（模板见 `assets/templates/function-record.json`）。
- 写 `process/<fn>/summary.md`，回答固定五个问题：为什么是热点、问题是什么、用了什么手法、正确性结论、性能结论与残留风险。
- 更新 `other/state.json`，然后按阶段 4 选定的模式继续或等确认。

### 阶段 6：收尾出报告

```bash
python3 <skill>/scripts/render_report.py --workspace <simd_optimization_result>
```

脚本生成机械部分（环境、逐函数表格、失败列表、复现命令）。手写内容写在 `other/report-notes.md`，按 `## 结论` / `## 补充风险` / `## 下一步建议` 分节，脚本会原样注入——**不要直接改生成的报告**，下一次重跑会把改动冲掉。

顶层只有一份 `simd_optimization_report.md`。不要放 HTML、PDF 或额外 JSON。

最后检查项目目录干净：

```bash
git -C <项目根> status --porcelain
```

输出里只应该出现被修改的源文件；出现 `.o`、`*.ref.c`、样本数据、`simd_optimization_result/` 都说明写回越界了。

### 阶段 7：反模式复核

交付前自查，命中任何一条都要修：

| 症状 | 原因 | 修法 |
| --- | --- | --- |
| 没读 `summary/` 就开工 | 跳过了阶段 0 | 回去读，并在 `reference-digest.md` 补记录 |
| 改完性能没变，但代码确实变向量化了 | 改的是 C 回退路径，项目里有既存 SIMD 变体 | 查派发表；确认后回滚并换目标 |
| 每个函数只跑了“典型长度” | 尾部逻辑没覆盖 | 补 `n=0/1/VL±1/非对齐/极值` |
| 加速比 5x 以上 | 基线是 `-O0` 或没开优化 | 换成项目实际编译参数重测 |
| 结果“差不多”就通过 | 用了容差 | 改回逐位比较，除非归约顺序必须变且已获批 |
| 项目里多了 `.o`/样本/夹具 | 写回越界 | 清理，只留源文件 |
| 函数小结最后一起写 | 攒批 | 改成每函数立即产出 |
| 自动模式停了 | 误用了指定模式的确认 | 自动模式只在用户喊停或队列空时停 |
| 一行只有 16 字节还硬写 SVE | 没先算 `R`，默认选了 256 位 | 按「优化原则」换 NEON，或先把多行打包再量 |
| 原生验证“应该没问题” | 以为 `-msve-vector-bits` 会改运行时 VL | `prctl` + `cntb` 实测；对不上就停，别往下测 |
| 两种验证方法的数字放在一张表里比 | 忘了两种口径不可比 | 换方法就重跑性能门，报告里分开写 |

## 输出

每个函数结束时返回一份结果；整轮结束时再给汇总。

```json
{
  "simd_optimize": {
    "success": true,
    "project": "<绝对路径>",
    "workspace": "<simd_optimization_result 绝对路径>",
    "profile": "native | cross-qemu | compile-only | none",
    "verify_method": "native | qemu",
    "verification_feasible": true,
    "mode": "auto | targeted",
    "hotspot_source": "user-file | scan | mixed",
    "reference_digest": {
      "manual_files": ["summary/manual/x.md"],
      "auto_indexes": ["summary/auto/C_SVE/summary.md"],
      "methods_used": ["<方法标题>"]
    },
    "functions": [
      {
        "function": "<fn>",
        "status": "verified | validated | correct_only | rejected | unverified",
        "correctness": "pass | fail | not_run",
        "speedup": 0.0,
        "summary_path": "process/<fn>/summary.md",
        "record_path": "process/<fn>/record.json"
      }
    ],
    "report_path": "<...>/simd_optimization_report.md",
    "next_action": "next_function | await_user | done | blocked",
    "blocked_reason": "",
    "error_message": ""
  }
}
```

字段说明：

- `profile`：机器**能力**探测结果，取值固定。`compile-only` / `none` 下不允许出现 `verified`。
- `verify_method`：用户**选择**的验证方式，取值固定 `native` / `qemu`。它是用户决定，不是从 `profile` 推出来的；两者不一致说明选择不可行，`verification_feasible` 应为 `false` 并在 `blocked_reason` 里写清缺什么（不是 aarch64 Linux、没有 qemu、运行时 VL 不是 256 位）。
- `verification_feasible`：所选方式在当前机器上是否真的能跑。为 `false` 时不得进入阶段 5 的两道门。
- `status`：固定五值（只允许追加，不改已有含义）。
  - `verified`：位精确通过、speedup ≥ 1.05、项目能编、已写回。
  - `validated`：两道门都通过，但按用户要求没有写回项目（验证性运行、只做评估、或项目是只读参考检出）。产物留在 `process/<fn>/proposed/`。
  - `correct_only`：位精确通过但性能未达标，已回滚。
  - `rejected`：试过、失败、已回滚，`rejected` 的具体原因写进 `reject_reason`。
  - `unverified`：没有可运行环境，只做了静态改写，未写回。
- `next_action`：固定四值，决定调用方下一步做什么。
  - `next_function`：自动模式，继续下一个。
  - `await_user`：指定模式，等用户确认或指定下一个。
  - `done`：队列已空。
  - `blocked`：门控失败且不可自动恢复，`blocked_reason` 必填。
- `success`：只有“整轮没有被阻塞、且每个函数都拿到了明确结论（含 rejected）”才为 `true`。有 `unverified` 或 `blocked` 时为 `false`。

失败路径：阶段 0 读不到方法库、阶段 1 环境不可用、阶段 3 用户选的验证方式在本机不可行（`verification_feasible=false`）、阶段 7 自查发现写回越界，都会让 `success=false` 并填 `error_message`；其余数组保持空。

## 规则

- **先读方法库再动手**。规范要求先查 `summary/manual/`，再查 `summary/auto/`，最后才是全网检索和自行设计。跳过这一步的优化不可复现，也无法解释为什么这样改。
- **先量 `R` 再选方向**。一行能连续读超过 128 位优先 SVE，一次只能读 128 位优先 NEON。方向选错不一定报错，但会稳定地少拿三分之一到一半的收益，而且后续无论怎么微调参数都补不回来。
- **只做 ARM**。x86 SSE/AVX、GPU、其它架构都不在范围内；遇到就说明不适用。
- **基线必须是项目真实编译参数下的原实现**。用 `-O0` 或关掉向量化的版本当基线，得到的加速比没有意义。
- **正确性用逐位比较**。容差会掩盖真正的语义破坏（溢出、饱和、舍入、符号位）。只有在归约顺序必须改变、且用户明确接受时才用容差，并在报告里标注误差界。
- **不达标就回滚**。性能门控不过的改动不能留在项目里，哪怕它“看起来更先进”。同一函数最多试两种手法，避免在一个函数上无限投入。
- **项目只留验证过的代码**。夹具、基线副本、样本、编译产物、临时 patch 全部放 `simd_optimization_result/`。写回越界会污染用户的仓库和 `.gitignore`。
- **每个函数立刻出小结**。攒批会让细节丢失，也让用户失去纠偏机会。
- **未验证就说未验证**。没有可用环境时，产物放 `process/<fn>/proposed/`，`status` 记 `unverified`，不要用“编译通过”冒充“验证通过”。
- **不编造数字**。`speedup`、`ref_ns`、`simd_ns` 必须来自真实跑过的命令，命令本身也要写进 `record.json`，否则结论不可复核。
- **不越权改项目构建系统**。本次任务是“在项目现有参数下让这几个函数更快”，不是“给项目换一套编译选项”。确实需要改编译参数时，单独提出来让用户决定。
- **不动无关代码**。只改目标函数的实现；顺手格式化、重命名、修 bug 会让 diff 无法评审，也让“性能变化是否来自本次改动”变得不可归因。

## 参考资料

- `references/modes/optimize/method-library.md`：`summary/` 方法库的读取优先级、场景匹配方法、全网检索协议、`reference` 字段写法。阶段 0 和 5.3 用。
- `references/modes/optimize/hotspot-discovery.md`：热点文件的格式识别与解析、无热点时的扫描与排序、没有 perf 时的兜底采集。阶段 2 用。
- `references/modes/optimize/arm-vectorization-rules.md`：可向量化判定八问、主循环骨架、扩宽/窄化/去交织/归约/查表的手法与已编译验证的代码片段、`_x`/`_z` 陷阱、SME 适用边界。阶段 5.2～5.4 用。
- `references/modes/optimize/verification-gates.md`：用例矩阵、位精确判定、性能测量协议与 1.05 阈值、回滚流程、未验证分级。阶段 5.5～5.7 用。
- `references/modes/optimize/toolchain-and-run.md`：三档执行档的完整命令、运行时 VL 确认、两种验证方式的完整步骤与 VL 校验、QEMU `-icount` 口径。阶段 1、阶段 3 用。
- `references/modes/optimize/workspace-layout.md`：`simd_optimization_result` 的完整结构与 `record.json` 契约。阶段 1 和 4.7 用。
- `assets/bench/`：bare-metal 基准夹具（`crt0.S`、`link.ld`、shim、runner、Makefile）。无目标板时用；`README.md` 记了四个会导致结论错误的坑。
- `assets/templates/`：`function-record.json`、`final-report.md` 骨架。
- `scripts/`：`check_env.py`、`init_workspace.py`、`scan_candidates.py`、`run_ab_bench.py`、`compare_outputs.py`、`render_report.py`。能被脚本判定的不要交给模型目测。
