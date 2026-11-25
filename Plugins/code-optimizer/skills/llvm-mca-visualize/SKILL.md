---
name: llvm-mca-visualize
description: 当需要把 ARM64/AArch64 汇编或函数的 llvm-mca 流水仿真结果画成周期级视图（pipeline timeline / 流水时序 / 执行端口热力图 / 端口压力）时使用。交付自包含离线 HTML（KPI、瓶颈归因、关键路径、逐指令逐周期时序）以及可选的 JSON 数据契约。
---

# LLVM MCA 流水线可视化

你是一位鲲鹏性能分析专家。你的任务是调用 LLVM MCA 对 ARM64 指令序列做流水线级静态仿真，并把结果画成直观的**流水时序图**。核心重活由脚本 `scripts/mca_visualize.py` 完成：采集 `-timeline`、`--json`、`-bottleneck-analysis`，解析出每条指令跨周期的时序，并生成自包含离线 HTML。你的职责是正确调参、解读图上信息、把瓶颈讲清楚。

用户调用了 `/llvm-mca-visualize`，参数为：`$ARGUMENTS`

## 依赖与环境

- **平台**：aarch64 Linux（推荐）。非 aarch64 主机仍可交叉分析，但无法自动检测 uarch，必须显式 `--uarch`，且 LLVM 需带 HiSilicon 模型。
- **已验证**：openEuler 24.03 LTS-SP2（CPU part `0xd06` → hip12）；Python 3.11（仅标准库）；LLVM 17.0.6（openEuler `dev_17.0.6`，commit `382821b707e9`）。
- **构建依赖**（仅首次）：`git cmake ninja-build gcc gcc-c++ python3`；AArch64-only Release 构建，首次预留 ≥20 GiB 磁盘，`JOBS` 控制并行。
- **数据依赖**：patched llvm-mca（支持 `-issue-trace`）；没有它就没有真实端口数据，工具会拒绝出图（不做近似）。
- 构建细节见 `references/build-llvm-mca.md`。

## 输入

从 `$ARGUMENTS` 或对话上下文中确定以下参数（用户未明确时按默认或询问）：

```json
{
  "mca_bin": "<patched llvm-mca 路径，必填；或环境变量 MCA_BIN>",
  "uarch": "hip08",
  "input_mode": "asm|source|binary",
  "asm": "<汇编文本或 .s 文件路径>",
  "source_file": "<C/C++ 源码路径>",
  "binary_file": "<二进制/目标文件路径>",
  "function": "<函数名，source/binary 模式必填>",
  "iterations": 30,
  "timeline_max_iterations": 6,
  "start_iteration": 1,
  "show_iterations": 5,
  "view": "all|timeline|resource",
  "cflags": ["-O3"],
  "out": "<输出 .html 路径，可选；缺省 llvm-mca-visualize-demo-<uarch>-iter5.html>"
}
```

字段说明：
- `mca_bin`：**必填**（`--mca-bin` 或环境变量 `MCA_BIN`），指向打过 `IssueTraceView` 补丁、支持 `-issue-trace` 的 llvm-mca。工具只输出真实数据，**没有近似回退**；不支持时直接报错并给出构建步骤。
- `uarch`：目标微架构代号，按 `llvm-mca-analysis` 同表归一化（真源：`llvm-mca-analysis/scripts/mca_analyze.py`；`UARCH_MAP/MCPU_TO_UARCH/ALL_UARCHES/MTRIPLE` 一致性由 `scripts/test/test_uarch_table.py` 校验，兄弟 skill 缺失或单侧漂移时用例失败）（hip08→tsv110、hip09→hip09、hip10→hip10c、hip11→hip11、hip12→hip12）。**可省略**：缺省时脚本按 `/proc/cpuinfo` 的 **`CPU implementer` + `CPU part` 组合**检测（仅 HiSilicon `0x48`：0xd01→hip08、0xd02→hip09、0xd03→hip10、0xd22→hip11、0xd06→hip12），并在报告标注"auto-detected"；`0xd03` 同时被 ARM Cortex-A53（implementer `0x41`）使用，因此**缺失/混合/未知 implementer 会直接报错并要求显式 `--uarch`**，不会按 part 猜测。
- `input_mode`：与 `llvm-mca-analysis` 一致。`asm` 最干净，**分析循环时优先用此模式**（把循环体抽出来）；`source`/`binary` 会提取函数体（函数级近似）。
- `iterations`：MCA 仿真迭代次数，**默认 30（稳态口径）**——KPI 为稳态吞吐（`周期/迭代`、IPC、Block RThroughput、瓶颈）。只看单趟流水形态（含流水填充/排空）用 `--iterations 1`。
- `timeline_max_iterations`：透传 llvm-mca `--timeline-max-iterations=N`，限制 timeline 打印的迭代数（**默认 6**，配合默认 30 次仿真渲染 1..5；显式传值优先，> `--iterations` 会被钳制并告警）。
- `timeline_max_cycles`：透传 `--timeline-max-cycles=N`（0=不限）。MCA 默认 80 周期会截断；**若给了 `--timeline-max-iterations` 而未给本参数，脚本自动传 0（不限）**，避免多迭代渲染被截断。
- `start_iteration` / `show_iterations`：**默认自动**——解析到多次迭代（默认 30/6 即属此类）时，**跳过 iter0（热身）并渲染其余全部（1..5）**；只有一次迭代时渲染 iter0。显式传值则优先，超范围会被钳制并告警。
- 画单次迭代时，横轴默认**归零**（该迭代从 0 起）；加 `--keep-absolute-cycles` 保留原始绝对周期。
- 用法建议：**默认（30 次仿真 + timeline 6 次）就是稳态吞吐/瓶颈视图，画 1..5 次迭代**；只看单趟流水形态用 `--iterations 1`；要看更多迭代：`--iterations 100 --timeline-max-iterations 10`；显式 `--start-iteration 0 --show-iterations 2` 可看跨迭代重叠。
- `out`：HTML 输出路径。缺省时写 `llvm-mca-visualize-demo-<uarch>-iter5.html`（hip12 → `llvm-mca-visualize-demo-hip12-iter5.html`）；要 JSON 用 `--json`（打印到 stdout，不落盘）。**若 `--out` 与任一输入文件（同路径/符号链接/硬链接）相同会拒绝覆盖并退出 1**。
- `cflags`（`--source` 模式）：**白名单**，仅接受优化/代码生成开关：`-O0/-O1/-O2/-O3/-Os/-Oz/-Ofast/-Og`、`-march=`、`-mtune=`、`-mprefer-vector-width=`、`-msve-vector-bits=`、`-funroll-loops`、`-ffast-math` 系列、`-fopenmp[-simd]`、`-fstrict-aliasing`、`-finline*`、`-ffp-contract=`、`-fvect-cost-model=` 等。`-g`、`-D/-U/-I/-include/-std=`、`-W*`、`-mcpu=`（会覆盖脚本设定的目标模型）、`-flto`、`-fprofile-*`、`-fplugin`、`-mllvm`、`-Xclang`、`-save-temps`、`-o`、`-MD/-MF/-MT/-MQ/-MJ`、`-ftime-trace`、`-Wp,` 等一律拒绝；需要宏/头文件/语言标准时请先外部编译为 `.s` 再用 `--asm`。

## 微架构与预检

脚本内置 `uarch -> -mcpu` 映射与 hip 调度模型预检（同 `llvm-mca-analysis`）。**hip09/hip10c/hip11/hip12 仅在含 HiSilicon 调度模型的 LLVM 构建（openEuler `dev_17.0.6` 源码，见 `references/build-llvm-mca.md`）中可用**；标准 LLVM 一般只有 tsv110(hip08)。不支持的 `-mcpu` 会让 llvm-mca **静默回退到 generic 模型**（不报错但结果全错），预检就是防这个坑。可用 `--check-env` 查看当前系统支持哪些型号（以及是否支持 `-issue-trace`）；失败时把 `error_message` 原样转达用户，**不要**忽略继续。

## 安装与自检

一次性准备 patched llvm-mca（详细步骤与故障排查见 `references/build-llvm-mca.md`）：

```bash
bash <skill_dir>/scripts/bootstrap_llvm_mca.sh ~/llvm-project ~/llvm-project/build-mca
# 内存不足时：JOBS=8 bash <skill_dir>/scripts/bootstrap_llvm_mca.sh ...
```

成功判据（两条都满足）：
```bash
~/llvm-project/build-mca/bin/llvm-mca --help | grep issue-trace
python3 <skill_dir>/scripts/mca_visualize.py --check-env \
  --mca-bin ~/llvm-project/build-mca/bin/llvm-mca
# 期望 "issue_trace_supported": true，且目标 uarch supported: true
```

退出码约定：`0` 成功；`1` 预检/运行失败（含缺 `-issue-trace`、编译/反汇编失败、`--json`/IssueTraceView/瓶颈分析失败、`--parse` 文本解析失败）；`2` 命令行参数错误。实时模式下 timeline 解析失败但 `--json` 成功时**保留降级**：退出码 0，但 `degraded=true`、`meta.timeline_ok=false`，原因写入 `meta.warnings`。

## 权限与覆盖

- **只读输入**：`--asm/--source/--binary/--parse` 读取的文件不做任何修改；本 Skill 不修改用户代码。
- **写范围**：只写 `--out` 指定文件；未给 `--out` 时写当前目录的 `llvm-mca-visualize-demo-<uarch>-iter5.html`，**同名文件会被整份覆盖**。不写其它路径；`--out` 与任一输入文件相同（含符号链接/硬链接）时**拒绝覆盖并退出 1**。
- **权限**：用户级源码/构建目录即可，无需 root；构建会写 LLVM 源码树与 build 目录。
- **不接触凭据**：不读取/输出 token、密钥或客户数据；除可选的源码 `git clone` 外不联网。

## 降级与备选

| 情形 | 可交付 | 处理 |
|---|---|---|
| 没有 patched llvm-mca（缺 `-issue-trace`） | 无真实端口数据，不能出图 | 按 `references/build-llvm-mca.md` 构建后重试；若本轮无法构建，返回 `blocked` 并说明缺少 patched 二进制 |
| 只有已保存的 `-timeline` 文本 | `--parse <timeline.txt>` 可画 Tab A（流水+逐周期占用）及部分 KPI | 无端口热力图/压力条/trace；可回答依赖、重叠、占用问题，**不能**判断端口争用；文本为空/缺少 `Timeline view:`/数据行无效时以 `success=false` 退出 1，不出图 |
| 实时 timeline 解析失败（但 `--json` 成功） | 资源视图 + 部分 KPI | **保留降级**：退出码 0，但 `degraded=true`、`meta.timeline_ok=false`，原因写入 `meta.warnings`；完整时序需换兼容的 llvm-mca 或修正 `-timeline` 文本 |
| 非 aarch64 或源码缺 HiSilicon 模型 | 无有效结果（generic 回退结果不可用） | 停止并提示换 `dev_17.0.6` 源码或显式指定受支持的 `--uarch` |
| 输入无法解析 / 编译或反汇编失败 | 无 | 返回 `success=false`（退出 1），原样转达 `error_message`，不要出伪图 |
| 权限/路径不可用 | 无 | 返回 `blocked`，说明缺失的权限或路径 |

## 示例（assets/examples/）

可直接用来冒烟验证与看效果，详见 `assets/examples/README.md`：

- `assets/examples/loop.s`：SAXPY 风格 NEON 循环体（`y[i] = a*x[i] + y[i]`，10 条指令，2× 展开，只有循环体无分支）。
- `assets/examples/llvm-mca-visualize-demo-hip12-iter5.html`：用该 `loop.s` 在 hip12（`0xd06`）生成的默认输出，自包含离线可开。

复现（默认 30 次仿真 / timeline 6 次；输出名随 uarch 变化）：

```bash
python3 <skill_dir>/scripts/mca_visualize.py \
  --asm <skill_dir>/assets/examples/loop.s \
  --mca-bin ~/llvm-project/build-mca/bin/llvm-mca
```

示例口径：hip12；5 个迭代组（前 2 组默认折叠）×10 条指令；Tab A 流水+逐周期占用，Tab B 热力图+压力条；数据为真实 issue（`-issue-trace`）。注意 `loop.s` 仅循环体（MCA 不跟随控制流），整函数分析用 `--source/--binary`。

### 用例（cases/，可执行验收）

- `cases/visualize-saxpy-loop/`：成功路径。需 `MCA_BIN` 指向 patched llvm-mca；校验默认参数（30/6→5 组）、HTML 热力图与 JSON 逐单元/逐周期一致、无残留 trace 文案。
- `cases/missing-issue-trace/`：失败路径。用 mock 的未打补丁 llvm-mca，断言非零退出、`success=false`、提示 `-issue-trace`、且不产生 HTML。
- `cases/safety-guards/`：安全护栏（无需 patched mca）。覆盖 `--out` 与输入同文件拒绝且输入不变、危险 `--cflags` 拒绝、implementer+part 识别（Cortex-A53 不误判）、空/畸形 `--parse` 失败且不产 HTML、cycle 边界、uarch 表一致性（缺失/漂移即失败）、cflags 白名单与实际编译、热力图右边界裁剪。
- `cases/parse-timeline/`：解析回归（无需 llvm-mca）。`eor` 助记符完整保留，不再把首字母 `e` 当执行周期；未知活跃字符解析失败。

运行（在 skill 根目录，结果写 `cases/<case>/results/result.json`）：

```bash
MCA_BIN=/path/to/patched/llvm-mca bash cases/visualize-saxpy-loop/run.sh
bash cases/missing-issue-trace/run.sh
```

## 工作流程

### 步骤 0：环境与模型预检（脚本自动）

脚本分析前自动预检 llvm-mca 是否安装、目标微架构模型是否可用。失败会返回 `success=false` + 原因，转达用户即可。

### 步骤 1：确定参数

从请求提取 `uarch` 与输入。**uarch 不能猜**（不同 uarch 调度模型差异巨大，猜错结论全错）；用户没指定时必须询问，或按上下文默认。

### 步骤 2：运行可视化脚本

```bash
python3 <skill_dir>/scripts/mca_visualize.py \
  --mca-bin <patched-llvm-mca> \                # 必填（或环境变量 MCA_BIN）：支持 -issue-trace
  [--uarch hip08|hip09|hip10|hip11|hip12|0xd01|...] \   # 可省略，缺省自动检测
  --asm <file|-> \                              # 三选一
  # 或 --source <file> --function <name>
  # 或 --binary <file> --function <name>
  [--iterations 30] [--timeline-max-iterations 6] [--timeline-max-cycles N] \
  [--start-iteration N] [--show-iterations N] [--keep-absolute-cycles] \
  [--view all|timeline|resource] \             # 默认 all：指令时序 + 资源×周期 双视图
  [--out <result.html>]                        # 缺省写 llvm-mca-visualize-demo-<uarch>-iter5.html
```

`<skill_dir>` 是本 SKILL.md 所在目录。脚本会先预检，再采集 `-timeline`/`--json`/`-bottleneck-analysis` 三份数据，解析后写出 HTML。
- 不传 `--out` 时默认写 `llvm-mca-visualize-demo-<uarch>-iter5.html`（默认 30 次仿真、timeline 6 次）；`--out <file.html>` 覆盖路径。
- 输入**必须**用 `--asm <file|->`（或 `--source/--binary`+`--function`），**不支持**直接传位置参数路径。
- `--json` 打印 JSON 数据契约（含 `timeline` 结构化数据，供衔接 `llvm-mca-analysis` 或二次加工），不写默认 HTML。
- `--html` 直接把 HTML 打印到 stdout（不写文件）。
- `--check-env` 只查环境；`--parse <timeline.txt>` 只对已保存的 `-timeline` 文本做解析+画图（离线调试/与其它工具对接）；解析失败（空文件/缺少 `Timeline view:`/数据行无效）以 `success=false` 退出 1，不写 HTML。

### 步骤 3：检查结果并解读

- `success == false`：把 `error_message` 转达用户，停止（常见：预检失败、编译/反汇编失败、timeline 段格式无法解析）。
- 成功则打开生成的 HTML（自包含、离线可看）。页面结构：**顶部全局**（KPI 卡 + Dispatch 等待统计 + 瓶颈归因与关键路径）+ **双 Tab**（`--view all` 时默认 Tab A）：

1. **Tab A：指令流水时序图（按迭代分组）**——横轴周期，**每个迭代一个可折叠组**（`==== 第 N 次迭代 ====`，有 ≥3 次迭代时默认折叠前两组），组内纵轴=该迭代的 ISA 指令，行标签 `it4 #0`，行右侧标注 `uOps=N · LD.2×1 SM5×2`（端口占用来自该次迭代的真实 issue 记录）。
   - 色块：发射(D)蓝 / 执行(e/E)绿 / 退休(R)橙 / 退休等待(-)浅黄；**等待(=)统一灰色 = "等待（原因未知）"**，因为 **MCA 不区分等寄存器/等资源**；关键路径指令红色虚线框。
   - 迭代色（蓝/绿/黄/橙/红循环）只用于组标题与行标签，用于区分跨迭代。
   - 悬停任一方格：迭代号、`#index`、汇编、阶段、cycle、uOps、端口占用；wait 额外注明原因未知。
   - **不拆 uop 行**：MCA 不导出 uop 级时刻，拆行只能是编造；uOp 信息以行侧标注呈现。
   - 看图：等待段长 → 依赖/资源等待（结合关键路径与 Dispatch 统计判断）；相邻迭代组同一指令被推迟 → loop-carried 依赖。
2. **Tab A：逐周期执行占用**：每周期处于执行中(e/E)的指令数曲线。Plateau 接近指令行数 → 后端饱和；谷底 → 依赖等待/发射缺口。
3. **Tab B：端口资源热力图（辅助）**——纵轴=**有真实占用的端口单元**，横轴=周期；色块=端口类型色，hover 含**迭代号 + 指令 + issue cycle + cycles**；另含"逐资源端口压力"条形图（每迭代聚合）。
   - 每个色块表示“该周期被占用 1 次”，块长度=连续占用周期数，hover 显示“第 k/N 周期”。
   - **压力条口径**：`pressure` 单位是 **cycles/迭代**，不是百分比；单单元上限 = `cycles_per_iteration`。利用率 = `pressure / cycles_per_iteration`（示例 `loop.s`：12.3 cycles/iter，S1 3.00 → ≈24%）。条形长度按最大值归一化，读数请看数值。
4. **KPI 卡**：IPC、Block RThroughput、周期/迭代、Dispatch 宽度、瓶颈类型（资源压力/数据依赖/混合/低后端压力）。
5. **瓶颈归因与关键路径**：瓶颈类型 + MCA 挑出的关键序列（哪几条指令、什么依赖）。

### 步骤 4：落到优化建议

结合图回答"为什么慢、卡在哪"，给 1–3 条具体方向：
- 某单元 `pressure / cycles_per_iteration` 高 + 流水图里多条指令在同区间重叠于同一单元 → **减少该端口需求**（换更少 uOps/占用该端口的指令、融合、打断依赖让指令并行到其它端口）。
- 流水图里等待段(=)连续且长 + 关键路径是 `REGISTER/MEMORY dependency` → **打断依赖链**（拆累加器、寄存器重命名、循环展开提 ILP）。
- 逐周期执行占用有周期性谷底 → 检查 loop-carried 依赖（如 post-index 地址更新、循环携带累加）。

## 脚本工具

- `scripts/mca_visualize.py`：主入口。采集 `-timeline`/`--json`/`-bottleneck-analysis`，解析 issue trace，生成 HTML 或 JSON。
- `scripts/bootstrap_llvm_mca.sh`：一次性构建 patched llvm-mca（copy 补丁 → patch → cmake → ninja → 验收）。
- `scripts/llvm-mca-issue-trace/`：`IssueTraceView.{h,cpp}` 与 `patch_llvm_mca.py`（注册 `-issue-trace`、JSON/文本输出）。

## 参考资料

- `references/build-llvm-mca.md`：源码获取、构建、验收与常见问题。
- `references/timeline-guide.md`：页面结构、阶段字符、资源×周期视图的解读方法。
- `assets/examples/`：示例输入 `loop.s`、输出 HTML 与说明。
- `cases/`：tier A 可执行验收用例（成功/失败路径）。

## 输出

- 生成 HTML 文件（默认 `llvm-mca-visualize-demo-<uarch>-iter5.html`，可用 `--out` 指定），内联 SVG、无 CDN、离线可开。
- `--json` 时改为打印 JSON 数据契约（见下），不写 HTML。
- 你向用户呈现：HTML 文件路径 + 一段结合图的解读（一句话点明主要瓶颈与根因 + 2–3 条优化方向）。不要只甩 IPC 数字，重点讲"为什么"。
- 人读示例（`assets/examples` 的 hip12 结果）：IPC 0.813、12.3 cycles/迭代；S1 3.00 cycles/迭代（≈24%）、SM2 2.97、S0 2.03，压力集中在整数端口 → 结合 Tab A 看这些单元上是否同一区间被多条指令重叠。

### AI 可读输出（`--json`）

成功时顶层含 `"success": true`，主要字段（单位：cycle / cycles 每迭代）：

```json
{
  "success": true,
  "degraded": false,
  "view": "all",
  "meta": {
    "uarch": "hip12", "mcpu": "hip12", "mode": "asm",
    "iterations": 30, "mca_bin": "/path/build-mca/bin/llvm-mca",
    "issue_trace": true, "timeline_ok": true, "warnings": []
  },
  "summary": {"iterations": 30, "ipc": 0.813, "block_rthroughput": 2.3333,
              "cycles_per_iteration": 12.3},
  "bottleneck": {"primary_type": "data_dependency"},
  "issue_trace": {
    "instructions": 10,
    "records": [{"Iteration": 1, "Index": 0, "Cycle": 0,
                 "Text": "ldr q0, [x0], #16",
                 "Resources": [{"Kind": "HIP12UnitLD.0", "Cycles": 1}]}],
    "reserved": []
  },
  "resource_pressure_per_iteration": [{"resource": "HIP12UnitS1", "pressure": 3.0}]
}
```

- `issue_trace.records` / `timeline.instructions` 只保留**展示窗口**的迭代（默认 1..5），且 `Cycle` 已归零到该窗口；跨右边界（`Cycle + round(Cycles) > cycles_total`）的占用已按窗口**裁剪**，`Cycles` 与热力图格数一致；完整 30 次统计在 `summary` 与 `resource_pressure_per_iteration` 中。
- `resources[].Kind` 带单元后缀（如 `HIP12UnitS1`），`resource_pressure_per_iteration[].pressure` 为 cycles/迭代。
- `degraded` / `meta.timeline_ok`：实时模式 timeline 解析失败（JSON 成功）时为 `true` / `false`，原因见 `meta.warnings`；此时仅资源视图与部分 KPI 可用。`--parse` 解析失败则直接 exit 1。
- 失败时 stdout 为 `{"llvm_mca_visualize_result": {"success": false, "error_message": "..."}}`，退出码 1；参数错误退出 2。

## 输出一致性

`cases/visualize-saxpy-loop/verify.py` 对同一次运行做交叉校验（可复跑）：

- HTML 实心格数 == JSON 中 `Σ max(1, round(Resources[].Cycles))`（与渲染取整一致），且每条记录满足 `0 <= Cycle < cycles_total`、`Cycle + round(Cycles) <= cycles_total`（跨边界已裁剪）；
- 热力图行名集合 == JSON 中单元（含 reserved 成员）集合；
- `issue_trace.records` 数 == `timeline.instructions` 数，且每条 record 的 `(Iteration, Index)` 在 timeline 中存在；
- 压力条行数 == `resource_pressure_per_iteration` 中 `pressure > 0` 的资源数；
- 单迭代时逐 `(指令, 单元)` cycles 与 MCA `ResourcePressureInfo` 完全一致。

复跑：`MCA_BIN=<patched> bash cases/visualize-saxpy-loop/run.sh`，查看 `cases/visualize-saxpy-loop/results/result.json`。

## 规则

- **MCA 不建模 cache/内存层级**：load/store 在 MCA 里只有执行延迟，没有 cache miss。访存密集代码 MCA 的 IPC 偏乐观。若图上显示充分流水但实测仍慢，提示用户用 perf/SPE 查内存瓶颈。
- **MCA 不跟随控制流**：分析循环时优先用 `asm` 模式把循环体单独抽出；`source`/`binary` 提取的是整个函数（含分支与尾部），作函数级近似，并在解读里说明。
- **不改变用户代码**：本 Skill 只分析+画图，不修改任何源文件。
- **uarch 不能猜**：用户不指定时必须询问。
- **失败如实报告**：`success=false` 时把 `error_message` 原样转达，不要编造结果。
- LLVM 17 的 `-timeline` **不输出 Legend 段**；脚本使用内置字符集（`D/e/E/R/-/=/./空格`，若文本里存在 Legend 段也会读取覆盖）。出现**未覆盖的活跃字符**时判定解析失败：`--parse` 以 `success=false` 退出 1；实时模式降级并在 `meta.warnings` 写明字符（`degraded=true`），不会静默丢段出错图。
