# 消融实验报告：tf-inference-opt skill 对 TF 推理优化实效的影响

> 历史案例：模型名、分支和提交号仅用于区分实验对象，不是当前环境前提。`${EXPERIMENT_ROOT}` 为用户提供的历史产物根目录，`${TF_SOURCE_DIR}`、`${SERVING_BUILD_DIR}`、`${TRANSCRIPT_DIR}` 分别为源码、构建与会话记录目录；未随 skill 打包的日志不可视为已核验或可访问的证据。
> **日期**：2026-09-03 ~ 09-04
> **实验**：两个独立优化 agent 在**完全相同的早期版本 TF 树**上做真实性能优化（改代码 → 构建 server → 压测 → A/B 出数），一个有 `tf-inference-opt` skill、一个没有
> **数据核验**：双侧报告的每个中位数均从原始 perf_analyzer 日志独立重提取并逐位复现；双侧头条数字各做了一轮现场复测（同节点 B/O 对照），方向与量级吻合

---

## 1. 实验设置

### 1.1 基线代码与工作区

| 项 | 值 |
|---|---|
| 基线提交 | `0e99f1bf4`（2026-08-06，8 月优化冲刺前最后一个提交；KDNN 源码完整在 git 内） |
| 工作区 | 三份 `git archive` 树（无 git 历史）+ 4 个 serving 兼容补丁，初始逐字节一致（diff -r 校验） |
| 残留优化空间 | 基线上其后 8 月冲刺的全部内容未做：ANNC 融合默认关闭、BN 折叠死代码、KDNN 路由休眠等 |
| 关键陷阱（双方面临） | 本机 CPU MIDR part=**0xd03**，不在 KDNN 门控白名单 {0xd02(920B), 0xd06(950)} → **KDNN GEMM 路由整体休眠，模型实际跑纯 Eigen** |
| 净化保证 | 三份树 grep 无任何 prepack/blocked-layout 痕迹；skill 副本不含本仓路径/版本/算子名（前期已验证） |

### 1.2 测量与公平性

- 共享基线 server：预构建一份（两侧 md5 一致），双方各自测基线与优化侧
- 4 个 mock 模型（adx / cvr_slave / hmv / presort），batch 59、并发 4，warmup + 交替 ≥5 轮（每轮新 server 进程），取中位数
- NUMA 隔离：skill 侧 node0（CPU 0-47）/ 端口 28500；noskill 侧 node1（CPU 48-95）/ 端口 28600；构建/压测/服务全绑定
- 任务书除 skill 引导一段外**逐字相同**；均未提及 prepack 等任何提示
- 两 serving checkout 对称预热（共享 bazel disk cache，二次构建仅 36.7 秒）
- 双方均在 8h 软上限内自然完成，无干预

---

## 2. 性能结果对比

### 2.1 提升幅度（各自相对同一共享基线二进制）

| 模型 | skill 侧 | noskill 侧 | 优势方 |
|---|---|---|---|
| hmv | **+25.2%**（278,077 → 348,029 infer/s；p99 -13.0%） | **+25.1%**（273,268 → 341,839；p99 -13.0%） | ≈平（绝对值 skill +1.8%） |
| cvr_slave | **+40.8%**（372,361 → 524,357；p99 -26.6%） | **+21.0%**（377,502 → 456,691；p99 -13.6%） | **skill（绝对 +14.8%）** |
| adx | +4.4%（131,356 → 137,114） | +3.4%（127,598 → 131,920） | ≈平（skill +3.9% 绝对） |
| presort | +5.0%（172,525 → 181,210） | **+13.5%**（169,278 → 192,118） | **noskill（绝对 +6.0%）** |
| **几何平均** | **+17.9%** | **+15.4%** | skill |

（每格均为交替 A/B 中位数，≥5 有效轮；双侧全部轮次 Error=0；正确性双侧均通过——详见 §5）

### 2.2 绝对终态吞吐（infer/s，batch 59 / 并发 4）

| 模型 | skill 终态 | noskill 终态 | 差 |
|---|---|---|---|
| hmv | 348,029 | 341,839 | skill +1.8% |
| cvr_slave | **524,357** | 456,691 | skill +14.8% |
| adx | 137,114 | 131,920 | skill +3.9% |
| presort | 181,210 | **192,118** | noskill +6.0% |

### 2.3 现场复测（实验结束后，同一干净 NUMA 节点 node2，单轮）

| 项 | skill hmv 终态 | noskill hmv 终态 |
|---|---|---|
| 复测值 | 358,695 infer/s / p99 925µs | 329,017 infer/s / p99 958µs |
| 报告中位数 | 348,029 / 907µs | 341,839 / 925µs |
| 结论 | 一致（+3%） | 一致（-3.7%，单轮噪声内） |

（首次复测在 node3 得 244k 被判无效——事后确认其他用户的 gcc 编译作业占了该节点 12 核；换 node2 复测复现。双侧各自的报告中也都声明了共享机器噪声与交替协议抗噪设计。）

### 2.4 当前人工优化版本的性能（2026-09-04 补测，含口径修正）

**被测对象**：现役人工优化构建（`${SERVING_BUILD_DIR}/tensorflow_model_server`，2026-08-29 构建）。**身份勘误（2026-09-04）**：经日志取证（该二进制的 server 日志只有 runtime 打包行 `kdnn_adapter.h:198`、零 load-time 行），它实际构建自 `${TF_SOURCE_DIR}`（7 月基座 `330e03ce5` + 未提交的 **runtime prepack**——即后来落库为 `a965af115..e40873201` 的那条线，首推理时打包缓存）；**并非** loadtime-fork `loadtime-prepack-branch` 的 load-time prepack 线（该线的 loader 集成从未进入任何 serving 构建）。历史验证报告 `NEON_PREPACK_E2E_VALIDATION_20260812.md` 亦为 runtime prepack 版本。

#### 2.4.1 两种测量口径（重要）

本次补测先后用了两种口径，**数字不可混用**：

| 维度 | 8/12 验证报告口径 | 消融协议口径（本实验） |
|---|---|---|
| 对照 | **同一二进制 ON/OFF**（双侧 `TF_KDNN_FORCE_NEON=1`，只切 `TF_KDNN_BLOCKING_LAYOUT`）——干净隔离 prepack 单一变量 | full vs 共享基线二进制（另一代码基线）或 full vs default（无 env → Eigen） |
| 线程 | **按模型**：cvr/hmv 8C 16/16、adx 16C 32/32、presort 8C **1/1** | 统一 16/16 |
| 并发 | 1~32 六档 | 仅 4 |
| 轮数 | 5 轮，中位数比 + 同轮配对比 | 2~3 轮中位数 |
| client/server NUMA | 分离（server NUMA2 / client NUMA0） | 同节点 |

#### 2.4.2 验证口径复测（同二进制 ON/OFF、按模型线程配置、2000ms 窗口，2026-09-04）

| 模型/配置 | 并发 | OFF infer/s | ON infer/s | **收益** | 8/12 报告参照 |
|---|---:|---:|---:|---:|---|
| adx（16C32T，32/32） | 4 | 65,732 | 73,892 | **+12.4%** | +26.79%（配对 +10.09%） |
| adx | 16 | 149,050 | 145,156 | -2.6% | -1.20%（交叉区） |
| adx | 32 | 164,845 | 158,512 | -3.8% | -4.31% |
| presort（8C，1/1） | 4 | 18,884 | 30,865 | **+63.5%**（p99 -39%） | +11.53% |
| presort | 16 | 18,851 | 30,779 | **+63.3%** | +10.59% |

- adx 的并发梯度、交叉点、回退幅度**全部与 8/12 报告一致**（并发 4 收益取配对口径吻合）。
- hmv/cvr 用昨天消融口径的同二进制数据换算：hmv full−default = +3.9%（8/12 并发 4：+4.01%）、cvr +2.7%（+2.82%）——**同样吻合**。
- presort 方向一致但量级更大（+63% vs +11.5%）：已隔离验证 `TF_FREEZE_VARIABLES` 无贡献（去掉后 30.6~30.8k 不变；mock 模型权重本为 Const），量级差来自**版本差异**（8/29 load-time 版 vs 8/11 runtime 版的打包/内核管线）——新版本在该模型上收益显著更大。
- prepack 命中均经 server 日志验证（hmv 4 / adx 6 / presort 6 / cvr 1 个权重，全部 `layout=32`）。

#### 2.4.3 消融协议口径（统一 16/16 线程、并发 4、与共享基线同节点对照）

| 模型 | 基线(node3) | 人工 full | 人工 default | full vs 基线 | full vs default |
|---|---|---|---|---|---|
| hmv | 281,158 / 1023µs | 300,828 / 1027µs | 289,428 / 997µs | +7.0% | +3.9% |
| cvr_slave | 389,684 / 750µs | 399,859 / 848µs | 389,230 / 766µs | +2.6% | +2.7% |
| adx | 133,938 / 2123µs | 58,967 / 4996µs | 129,848 / 2190µs | **-56.0%** | **-54.6%** |
| presort | 171,702 / 1630µs | 164,101 / 2008µs | 172,944 / 1618µs | -4.4% | -5.1% |

**⚠ 口径修正（2026-09-04）**：本表 adx 的 -56% 是**线程配置伪影**——统一 16/16 线程下 KDNN NEON 在 adx 上本身只有 ~66k（OFF）/74k（ON），而对齐验证口径（32/32）后 KDNN 内部收益为 +12.4%、且 OFF 侧即达 65.7k（见 §2.4.2）。即：**人工版 prepack 在其验证配置下工作正常且与 8/12 报告一致**；16/16 是该版本未验证的配置。

**三方终态绝对吞吐合览（infer/s，消融口径=统一 16/16、并发 4）**：

| 模型 | 基线* | skill 终态 | noskill 终态 | 人工 full | 人工 full（验证口径线程） |
|---|---|---|---|---|---|
| hmv | 273~281k | **348,029** | 341,839 | 300,828 | ≈300,828（16/16 同） |
| cvr_slave | 372~390k | **524,357** | 456,691 | 399,859 | ≈399,859（16/16 同） |
| adx | 128~134k | **137,114** | 131,920 | 58,967 | 73,892（32/32） |
| presort | 169~173k | 181,210 | **192,118** | 164,101 | 30,865（1/1 线程，非吞吐最优配置） |

\* 基线区间为两侧 agent（node0/node1）与补测（node3）各自测得的同一二进制数字，节点间差 1~3%。

#### 2.4.4 修正后的解读

1. **人工版 prepack 功能与收益在其验证口径下完全复现**（§2.4.2 与 8/12 报告逐点吻合，presort 量级更大且已归因到版本改进）——此前 §2.4 初版中「adx -56% 严重回退」是线程配置伪影，特此修正。
1a. **「人工版全开」的真实含义（2026-09-04 复核代码后修正）**：人工版二进制的 full 配置（3 个 env）已是其**设计上的最大启用状态**——prepack 路线确实全开（命中日志验证）。但其树内**另有一整块能力（ANNC 图融合，含稀疏链融合/BN 折叠/MatMul 融合，4 月起就存在）在 serving 中根本无法启用**：人工版 `gflags.cc` 无任何 env 覆盖机制、serving `main.cc` 不解析 gflags → `FLAGS_annc` 恒为 false、重写器永不注册。换言之 skill 版打开的正是人工版二进制里「存在但够不着」的那部分设施——而「修好开关通路（gflag env 覆盖）」正是消融实验中两个 agent 各自做的第一项工作（skill 侧优化 A / noskill 侧优化 3）。另注：人工版 committed 基座是 4 月 8 日（+未提交 prepack 线），早于消融基线（8 月 6 日，含 KDNN spblas 同步、JIT 迁移前的 8/5 状态等 4~8 月工作）——两版本基座不完全对齐，方向性结论不受影响但严格说这是「4 月基座+prepack」vs「8 月基座+agent 优化」的对比。
2. **但 8/12 口径有一个盲区**：ON/OFF 双侧都强制 `TF_KDNN_FORCE_NEON=1`，只回答了「prepack 在 KDNN 内好不好」，没有回答「KDNN 该不该用」。消融实验中两个 agent 独立发现并实测：adx 并发 4 下 **Eigen（~134k）比 KDNN NEON prepack 最优值（~74k）快 1.8×**——agent 的「adx 不路由 KDNN」决策在两种口径下都成立，且比 8/12 的结论更根本。
3. **三方对比结论不变但需限定条件**：在消融口径（统一 16/16、并发 4）下，skill/noskill 终态在 4/4 模型上均超人工 full；即便给人工版换上其验证口径的线程配置（adx 32/32 → 73.9k），仍低于 agent 终态（137k）——因为 agent 对「是否用 KDNN、用哪条路径、线程怎么配」做了逐模型的本机调优，而人工版是单配置通吃。
4. **人工版默认配置 ≈ 基线（+0.4%）**，回退契约有效。
5. 方法论教训（已印证两次）：**线程配置是本实验中最大的混杂变量**（adx 在 16/16 vs 32/32 下 KDNN 性能差 2.3×；presort 在 16/16 vs 1/1 下 prepack 从 -5% 变 +63%）——任何 A/B 结论必须标明线程/并发/配置，跨配置不可泛化。

### 2.5 skill vs noskill 头对头复测（2026-09-04，同节点同条件）

两侧 agent 原始数据各在其专属 NUMA 节点（node0/node1）测得，存在节点间残余差异。本节把**两个终态二进制放在同一节点（node2）**、同协议（batch 59 / 并发 4 / 每轮新进程 / warmup / 交替 3+3 轮 / 各自报告的最终启动配置）头对头复测：

| 模型 | skill 终态 | noskill 终态 | skill 优势 | p99（skill/noskill） |
|---|---|---|---|---|
| hmv | 362,848 | 352,860 | **+2.8%** | 877 / 900 µs |
| cvr_slave | **525,443** | 466,627 | **+12.6%** | 596 / 659 µs |
| adx | 138,868 | 138,560 | +0.2%（平） | 2061 / 2084 µs |
| presort | 183,238 | **197,246** | **-7.1%** | 1536 / 1436 µs |
| **几何平均** | — | — | **skill +1.9%** | — |

- 与 §2.1 口径相互印证：按两侧各自基线提升幅度换算的头对头期望值为 skill +2.1%，实测 +1.9%。
- **差距的真实结构**：hmv/adx 两模型双方几乎打平（各自路径均已接近该模型在本机的优化极限）；真正拉开差距的是两个「配置型」优化各中一枪——skill 的 **TF 线程对齐**（cvr_slave +12.6%）vs noskill 的 **KDNN 线程上限**（presort +7.1%）。
- 原始数据：`head_to_head/{results,logs,run_h2h.sh,summary.json}`。

### 2.6 人工 prepack 特性放到最新代码上的测试（2026-09-04 追加）

用户提出「人工版用最新代码试试」。最新代码（`e40873201`）**已包含**人工 runtime prepack 特性（落库为 `--kdnn_prepack/--kdnn_neon` gflag，本机默认关）。测试两种形态（均加 gflag env 覆盖以在 serving 中启用）：

**A. 最新代码 + 落库版 runtime prepack（`TF_KDNN_PREPACK=1 TF_KDNN_NEON=1`）**：

| 模型 | 正确性（vs 同二进制 default） | 性能（16/16、并发 4） |
|---|---|---|
| hmv | ✅ 逐位一致（权重 N=128/64，被 4 整除） | **-23.0%**（213,115 vs 277,806，3 轮中位） |
| presort | ❌ **数值错误**（最大相对误差 2.9e-2；权重 N=250/100 **不被 4 整除**） | —（结果不可信） |

- **根因定位（2026-09-07 终版，经同二进制 env 对照实验钉死）**：**`14621ff79`（remove kdnn KDNN_FORCE_NEON）改变了 0xd03 上 BA4b task 的 generator 路由**。证据链（全部可复现，probe 位于两树 `third_party/KDNN/probe/`）：
  1. **同二进制对照（决定性）**：在本仓 NEON f32 内核的 `EstimateTime` 中把 `Is920BPlatform()` 临时改回 `getenv("KDNN_FORCE_NEON")` 后——设 `KDNN_FORCE_NEON=1`（→NEON f32 以最优估计被选中）**并发首调 10 轮 0 错**；同一二进制不设 env（→注册表中另一 F32 generator 胜出）**10 轮 31 错**。env 是唯一变量。
  2. **POC（8/12 代码线）双保险**：其 `impls.cpp` 的 F32 NEON 候选**唯一**（无 SME f32/unigemm/mmla 等本仓多出的注册项），BA4b task 只能选 NEON f32；且 8/12 验证协议本身带 `TF_KDNN_FORCE_NEON=1`。任一条件都保证选中 NEON f32。
  3. **肇事提交**：`14621ff79`（2026-08-12，与 a965af115 同日）把 NEON f32 `EstimateTime` 的 `getenv("KDNN_FORCE_NEON")`（命中→估计值 512、必选中）换成 `Is920BPlatform()`（0xd03→不命中→估计值 77，失去优先权）→ BA4b task 被路由到本仓注册表中另一 F32 generator（具体身份待定，其 Supports 匹配 [59,400,400] 类 shape 但不匹配 [321,128]/[348,174]——这解释了 adx/presort 错而 hmv/cvr 对的 shape 相关性）。该 generator 对 BA4b 的**并发首用存在数据竞争**（顺序执行全对，包括 192 线程池顺序段；同 shape 并发首调 60-75% 线程拿到错值 30~47 量级；稳态串行重算全对=缓存就绪后安全）。
  4. **排除过程**（双向文件 swap）：NEON f32 内核 pair、aarch64_codegen 目录、gemm_helpers、small_gemm 均非单一因素（本仓版放进 POC 环境全对、POC 版放进本仓环境仍错）；两树共享基础设施（kdnn_jit.hpp/wrapper/threading/gemm_jit）逐字相同；编译模式排除（同 flags 复现）。竞态特定于 **BA4b（prepack）task**——plain task 并发首用安全（probe 自比对证明）。
  5. **修复（probe + serving 双级验证）**：注意**当前仓现有二进制设 `KDNN_FORCE_NEON=1` 无效**——`14621ff79` 已删除其全部读者（grep 确认 src/ 零命中）。需先打一行补丁：把 NEON f32 内核 `EstimateTime` 中的 `Is920BPlatform()` 改回 `getenv("KDNN_FORCE_NEON")`（或改为 `Is920BPlatform() || HasNEON()` 按 HWCAP 检测）。**Serving 级 A/B（同一二进制，唯一变量 env）**：补丁 + `KDNN_FORCE_NEON=1` + prepack 全开 → **4/4 模型正确**（adx 5.9e-08 / presort 2.1e-07 / hmv 0 / cvr 0）；不设 env → adx 2.5e-01 / presort 4.2e-01 仍错。其它方案：(b) 修该 generator 的 BA4b Supports/并发安全；(c) `GetDesiredWeiLayout(enablePrepack)` 加「确认选中 NEON f32 才打包」门槛。
- hmv 的 -23%（旧人工二进制同模型为 +7%）表明新 KDNN 的 NEON BA4b JIT 消费在本机也比旧版慢。

**B. loadtime-fork load-time prepack 线移植到最新代码**（手工移植 347 行 + 适配新 API，构建成功、load-time 打包命中）：

- **正确性：3/4 模型数值错误**（hmv 相对误差 1.2e8、presort 0.50、adx 0.15；仅 cvr_slave 通过）——比 A 更广的失效面（hmv 在 A 中正确），说明 load-time 线另有独立缺陷；该线的 loader 集成从未经过任何 serving E2E 验证（见身份勘误）。
- 性能（数字不可信，仅记录）：hmv -22.7%、adx -80.5%、presort -8.4%、cvr -0.1%。

**结论**：人工 prepack 特性在最新代码上**在本机不可用（部分 shape 静默算错）且性能回退**，需先修复 N%4 门槛与 load-time 路径缺陷；与 agent 终态的差距进一步拉大（hmv：skill 348k / noskill 342k vs 最新代码 prepack 213k）。

### 2.7 8/12 口径四方终态对比（2026-09-07，修复 0xd03 识别后）

**背景**：按用户要求给 `Is920BPlatform()` 加了 `PART_920C (0xd03)`（本仓 + 实验树均已改，修复版 prepack-ON vs OFF **逐位一致**——并发污染彻底消失）。随后按 8/12 验证口径（per-model 线程配置、并发 1~32、5 轮、每轮 3 次 warmup、ON/OFF 模型轮间交替、server NUMA2 / client NUMA0）对比四方终态：**人工最新版 prepack ON/OFF**（同二进制切 `TF_KDNN_PREPACK`，即 8/12 的 ON/OFF 口径）、**skill 终态**、**noskill 终态**（各自报告的终态 flag；skill 的 cvr 4/4 线程对齐未启用——按 8/12 口径统一 16/16）。

| 模型/并发4 | 人工OFF | 人工ON | prepack收益 | skill | noskill | skill vs 人工OFF |
|---|---:|---:|---:|---:|---:|---:|
| cvr_slave | 324,370 | 342,682 | +5.6% | **416,640** | 416,336 | **+28.4%** |
| hmv | 253,582 | 257,603 | +1.6% | **311,810** | 305,033 | **+23.0%** |
| adx | 91,290 | 91,493 | +0.2% | **124,683** | 124,222 | **+36.6%** |
| presort | 24,388 | **29,482** | **+20.9%** | 23,233 | **33,640** | −4.7% |

完整并发梯度（infer/s 中位数）：

- **cvr_slave**：skill/noskill 全档领先人工 OFF +10~41%（低并发差最大）；prepack 收益 +3.5~7.3%
- **hmv**：skill/noskill 全档领先 +13~37%；prepack 收益 +0.6~3.4%
- **adx**：skill/noskill 领先 +2~59%（低并发差最大）；prepack 收益 **+7.5%（并发1）→ −8.8%（并发32）**，并发 8 起交叉回退（与 8/12 报告的 16 并发交叉/32 并发 −4.31% 趋势一致，本机更早）
- **presort**：**人工 ON 全档第一**（+18~21%），noskill 第二（+30~36% vs OFF），**skill 低于人工 OFF ~5%**——skill/noskill 在 presort 未路由 KDNN（log 验证零 prepack、走 Eigen），而 8/12 口径 presort 的 1/1 线程配置恰好放大 KDNN NEON 优势；noskill 的 `TF_KDNN_NUM_THREADS=2` 使其 Eigen+线程上限组合超过 skill 的纯 ANNC 路线

p99（并发 4，µs）：skill/noskill 在 cvr/hmv/adx 全面占优（720/992/2271 vs 人工 971~3550）；presort 人工 ON 最优（8106），noskill 7514 与之同量级，skill 10310。

**结论**：① 修复后的最新人工版 prepack 与 8/12 报告口径吻合（收益量级、adx 高并发交叉均复现）；② 在 8/12 口径下 **skill 终态在 4 模型中 3 个全面领先人工版**（cvr/hmv/adx +23~37%），**唯一输的是 presort**（未启用 KDNN 路线，−5% vs 人工 OFF、−30% vs 人工 ON）；noskill 在 cvr/hmv/adx 与 skill 平手、presort 全档第一；③ skill 的 presort 失利是**配置归因**（该模型其图融合路线未与 KDNN 组合），非能力缺失——最佳组合应为「ANNC 图融合 + KDNN prepack」。
- 原始数据：`bench_812_protocol/{results,logs,summary.json}`；脚本 `manual_version_bench/run_812_protocol.sh`。

### 2.8 人工版全开 vs skill 终态（2026-09-07 第二轮 8/12 口径）

**背景**：第一轮（§2.7）人工版只开了 prepack 一条腿。本轮给人工版二进制补全 env 覆盖（TF_ANNC / TF_ANNC_CF_MATMUL_BATCHNORM / TF_ANNC_FUSED_MATMUL / TF_KDNN_SVE），全开 = 图融合 + BN 折叠 + _FusedMatMul + prepack + KDNN NEON。图谱导出验证：hmv 的 DynamicPartition/ParallelDynamicStitch/SparseSegmentMean 链被融合为 `KPFusedSparseDynamicStitchMean`、BN 折叠命中、prepack 命中——**图融合与 prepack 成功叠加**。正确性：4 模型 vs default 误差 ≤2.1e-07 全过。OFF 侧为 TF_KDNN_NEON=0（纯 Eigen，对齐 skill 侧"adx/cvr 不走 KDNN"的策略空间）。

> **⚠ 口径修正（2026-09-07 取证后补）**：事后对 round-2 server 日志逐模型统计融合命中发现，人工全开实际**缺少 `_FusedMatMul` 整条腿**——4 模型该融合节点数为 0（skill 侧 6/7/8/2），原因是最新树上 `enabled_fused_matmul_rewriters()` 平台门控仍只认 0xd06（`graph_opt.cc:1222`），本机 0xd03 被挡；skill 消融实验树上的门控扩展（其优化 D）从未合入主线。BN 折叠命中也仅为 skill 的一半（hmv 2/adx 4/presort 2 vs 4/6/6，skill 修的 3 个重写器缺陷同样未回流）。即本节"全开"= 稀疏融合 + 半成品 BN 折叠 + prepack + NEON，**低并发对 skill 的落后中有一部分是这条缺失的腿**。机制分析与证据详见 `AGENT_VS_MANUAL_ANALYSIS.md`（并发域交叉的完整解释：低并发=延迟域（图融合天下），高并发=吞吐域（prepack 天下））。

| 模型/并发4 | 人工OFF | 人工全开 | 全开收益 | skill终态 | 全开 vs skill |
|---|---:|---:|---:|---:|---:|
| cvr_slave | 353,756 | 389,068 | +10.0% | 416,618 | −6.6% |
| hmv | 247,038 | 293,507 | +18.8% | 311,276 | −5.7% |
| adx | 116,174 | 92,162 | **−20.7%** | 124,671 | −26.1% |
| presort | 22,234 | **29,260** | **+31.6%** | 23,724 | **+23.3%** |

**并发梯度要点**：

- **cvr_slave**：全开 +4.0~19.2%（随并发增大收益扩大），并发 ≥16 时**反超 skill**（+4.6~9.7%）；低并发仍落后 skill 15~17%（skill 的 ANNC 图融合子在低并发收益更大——其重写器修复更彻底 + cvr 4/4 线程对齐未计入本轮口径）。
- **hmv**：全开 +17.4~28.3%，**并发 ≥8 反超 skill**（+1.3~3.3%）；低并发落后 10~12%。
- **adx**：全开**全档回退** −5.6~−25.2%——全开组合含 KDNN NEON=1，adx 的 K=1668 大 GEMM 走 KDNN 劣于 Eigen（本报告已三次确认），且 ANNC 叠加救不回来（vs 一轮纯 prepack 仅 +0.6%）。skill 在 adx 特意关 KDNN 走 Eigen，保持全档第一。**adx 的正确策略是"图融合 + Eigen"，全开的错误是"无脑 NEON=1"**——这是配置组合问题而非能力问题。
- **presort**：全开 +24.6~31.8% 全档第一，超 skill +18~26%（1/1 线程下 KDNN NEON prepack 主导）。注意 vs 一轮纯 prepack（27,272@并发1）反而 −5.3%：BN 折叠/FusedMatMul 在 presort 有轻微负交互（2 处 BN 折叠命中但 _FusedMatMul=0）。

p99（并发 4）：全开在 hmv/presort 优于 skill（1048/8182 vs 991/10066——hmv skill 略优 5.8%，presort 全开优 18.7%）；adx 全开因 NEON 路由 p99 恶化（3509 vs skill 2277）。

**终版结论**：① 修复 0xd03 + env 补全后，**人工版全开在 4 模型中 2 个（presort、cvr/hmv 高并发档）达到或超过 skill 终态**——8 月冲刺的图融合 + prepack 叠加收益（vs 纯 prepack：cvr +11.6%、hmv +16.4%）确实存在且可观；② skill 的剩余领先集中在**低并发**（图融合子修复更彻底）与 **adx**（正确地逐模型关闭 KDNN）；③ 双方最优解仍是逐模型组合配置：presort/cvr/hmv 高并发 = 全开，adx = 图融合+Eigen，hmv 低并发 = skill 的 SVE 路线。**没有任何单一配置在 4 模型全档最优——逐模型调优的必要性得到最终确认。**
- 原始数据：`bench_812_round2/{results,logs,summary.json}`；脚本 `manual_version_bench/run_812_round2.sh`。

### 2.9 打开 FusedMatMul 平台门控后的第三轮（2026-09-07/08）

**背景**：§2.8 取证发现"人工全开"缺 `_FusedMatMul` 一条腿（门控只认 0xd06）。本轮把 `enabled_fused_matmul_rewriters()` 改为复用 `enabled_aarch64_rewriters()`（含 0xd03）并重建（增量 218s），验证后跑同协议三方对比。

**门控修复验证**：`_FusedMatMul` 命中 cvr 2 / hmv 6 / adx 7 / presort 8——**与 skill 侧完全一致**；prepack 在 fused 路径同样命中（hmv 5 个权重，`add/kp_fused` 命名）；正确性 4/4 通过（cvr/hmv/adx 逐位一致，presort 8.6e-07）。

**⚠ 跨轮可比性说明**：round-3 绝对数字系统性低于 round-2（如 hmv off c1 78k vs 84k、skill cvr c1 101k vs 147k），测量时段机器外部负载更重（hmv off 后两轮掉到 59-66k）。**跨轮绝对值不可比，只看轮内对比**。

**轮内对比（full vs off 收益；full vs skill 括号内）**：

| 模型 | c1 | c2 | c4 | c8 | c16 | c32 | vs round-2 变化 |
|---|---:|---:|---:|---:|---:|---:|---|
| cvr_slave | +5.8 (−5.4) | +6.0 (−11.0) | **+12.8 (+4.8)** | +13.0 (+2.1) | +18.9 (+8.6) | +18.0 (+6.7) | 收益与 r2 相当；**反超 skill 的交叉点从 c8-16 提前到 c4** |
| hmv | **−11.6 (−26.5)** | −4.6 (−19.7) | −2.6 (−16.9) | +13.6 (−6.2) | +16.1 (−5.7) | +18.2 (−7.2) | **低并发由正转负**（r2 为 +17~20%）——见下方归因 |
| adx | −24.8 (−31.4) | −24.0 | −18.5 (−23.5) | −7.7 | −2.9 (−7.0) | −2.6 (−7.2) | 与 r2 同构（NEON 路由问题未动），高并发略收窄（FM 正贡献抵消） |
| presort | **+30.8 (+25.4)** | +38.5 (+30.7) | **+39.1 (+31.9)** | +39.7 (+30.2) | +40.1 (+30.6) | +38.6 (+30.6) | **收益比 r2 再涨 ~7pp，对 skill 领先扩大到 +25~32%** |

**`_FusedMatMul` 单变量归因**（同二进制交错 A/B，只切 `TF_ANNC_FUSED_MATMUL`，方差 <2%）：

| 模型 | c1 | c4 | 结论 |
|---|---:|---:|---|
| presort | +2.2% | +2.1% | 正贡献 |
| cvr_slave | +0.5% | +0.8% | 中性偏正 |
| hmv | **−8.8%** | **−8.1%** | **干净回退** |

**hmv 回退的 2×2 消融**（NEON × FusedMatMul，c4，各 2 轮）：

| | fm=0 | fm=1 | FM 效应 |
|---|---:|---:|---:|
| Eigen（NEON=0） | 272,903 | 277,569 | **+1.7%** |
| KDNN NEON=1 | 284,151 | 267,206 | **−6.0%** |

→ **图融合本身有益（Eigen 下 +1.7%），回退全部来自 `kdnnFusedGemm` 消费路径**：bias 进 GEMM epilogue（+BA4b）的 fused 内核在 hmv 的小 shape 上比 `kdnnGemm` + 独立 AddV2 更慢。这是人工树上一个**新识别的内核缺陷**（skill 树上 hmv 的 FM 走 SVE fused 路径无此问题）。修复方向：fused 路径不走 epilogue（bias 拆回独立算子）或修 fused generator 的选型。

**第三轮结论**：① 门控修复是正确且必要的（能力解锁、命中与 skill 对齐）；② 但"全开"对 hmv 反而**可证明地次优**——该树最优 hmv 配置是"图融合(不含 FM)+BN 折叠+prepack+NEON"（即 §2.8 的 round-2 配置，高并发 +18%）；FM 只在 Eigen 下有益（hmv 低并发另论）；③ presort/cvr 的全开含 FM 确认更优（presort 全档 +39~40%）；④ 逐模型配置必要性第三次确认，且本轮给出新洞察：**同一图融合在不同 GEMM 后端下的净效应可以反号**（hmv：Eigen +1.7% / KDNN −6.0%）——图层开关必须与内核路由联合调优。
- 原始数据：`bench_812_round3/{results,summary.json}`、`bench_fm_ab/`（单变量 + 2×2 消融）；脚本 `manual_version_bench/run_812_round3.sh`。

### 2.10 终态对终态三方对比（2026-09-08 第四轮，同会话交错）

**设计**：三侧各自逐模型最优配置，同一时段轮内交错（3 轮 × 每轮三侧，轮间换起始侧），8/12 口径。人工最优按 §2.9 结论修正：cvr=全开含FM、hmv=全开减FM（规避 kdnnFusedGemm 缺陷）、adx=图融合+Eigen、presort=全开；skill/noskill 用各自报告终态。

**并发 4（infer/s 中位数）**：

| 模型 | 人工最优 | skill 终态 | noskill 终态 | 人工 vs skill | 人工 vs noskill | skill vs noskill |
|---|---:|---:|---:|---:|---:|---:|
| cvr_slave | 392,382 | **417,045** | 416,736 | −5.9% | −5.8% | +0.1% |
| hmv | 291,755 | **311,883** | 305,395 | −6.5% | −4.5% | +2.1% |
| adx | 122,731 | **124,249** | 123,948 | −1.2% | −1.0% | +0.2% |
| presort | 310,20** | 236,010 | **319,300** | **+31.4%** | −2.9% | −26.1% |
| **几何平均** | — | — | — | **+3.4%** | **−3.5%** | **−7.7%** |

（presort 行人工为 31,020；几何平均为四模型比值几何均值）

**并发梯度要点**：

- **cvr_slave**：低并发 skill/noskill 并列领先（人工 c1 −17%），**c8 交叉，c32 人工 +10%**——并发域交叉再次确认。
- **hmv**：c1-4 落后 skill 6.5~11%，c8 起反超（+0.6~2.6%）；对 noskill c8 起领先 +4.1~6.2%（skill 的 SVE 路线对 noskill 也保持 +1.6~3.6%）。
- **adx**：人工改走图融合+Eigen 后**基本追平**（仅 −0.5~−1.8%，全档）——对比 §2.8 全开 NEON 时的 −20.7%@c4，逐模型路由修正的价值直接可见。
- **presort**：人工对 skill **全档 +31~33%**；但 **noskill 全档反超人工 2.9~5.3%**——noskill 的"KDNN SVE 默认开 + kdn2 线程上限 + 自写 fold 重写器"组合在 1/1 线程的 presort 上是全场最优（其 SVE JIT 单核路线优于人工的 NEON prepack）。
- skill vs noskill：cvr/adx 打平，hmv skill +2~4%，presort **noskill +26~29%**——两 agent 各有领地。

**第四轮结论**：① 终态对终态下三方各有所长：**低并发（c≤4）skill/noskill 领先**（cvr/hmv），**高并发（c≥8）人工领先**（cvr/hmv +0.6~10%），presort 上 noskill 第一、人工第二（+31% vs skill）；② 人工版按逐模型修正配置后，adx 从 −26% 追到 −1%，几何平均对 skill 转正（c4 +3.4%、c32 +9.2%）——"逐模型调优"本身就能抹平大半差距；③ 剩余差距的结构性成因不变（§AGENT_VS_MANUAL_ANALYSIS.md：低并发=图融合成色，高并发=prepack 内核效率，presort=SVE 单核路线）；④ noskill 的 presort 路线（SVE+kdn2）是三方中该模型唯一超人工的配置，值得回流主线验证。
- 原始数据：`bench_812_round4/{results,logs,summary.json}`；脚本 `manual_version_bench/run_812_round4.sh`。

### 2.10 终态对终态三方对比（2026-09-08，round-4）

**设计**：三方**同会话、轮内交错**（每轮 manual→skill→noskill，轮间换起始侧），8/12 口径全并发档（1~32 × 3 轮中位数），各用**逐模型最优配置**：

- **人工最优**（round-3 结论修正后）：cvr/presort = 全开含 FM；hmv = 全开减 FM（规避 kdnnFusedGemm 缺陷）；adx = 图融合+Eigen（不进 KDNN）
- **skill 终态**：ANNC+CF2+FM，hmv 加 SVE
- **noskill 终态**：hmv/presort = ANNC+kdn2；adx = fold only；cvr = ANNC+SVE=0

| 模型 | 人工 vs skill | 人工 vs noskill | 格局 |
|---|---|---|---|
| cvr_slave | c1~4 落后 5.9~16.3%，**c8 起反超 +0.6~+10.0%** | 同左（±0.3pp，noskill≈skill） | 低并发 agent 赢 / 高并发人工赢 |
| hmv | c1~4 落后 6.5~11.1%，c8 起反超 +0.6~+2.6% | c1~4 落后 4.5~10.2%，**c8 起反超 +4.1~+6.2%** | 同上；skill 全档最强（低并发） |
| adx | **全档持平**（−0.5~−1.8%） | 同左 | 三方打平（人工"图融合+Eigen"估计被证实） |
| presort | **+30.8~+32.8% 全档** | **−3.2~−5.5% 全档** | **noskill > 人工 > skill** |

**解读（回应"skill 版是否最差"）**：终态排名上 skill 确实垫底，但这**不是**"skill 没用"的证据——① 消融本问题上 skill 对 noskill 是赢的（§2.5 头对头 +1.9% 几何平均、快 13%）；② 本轮三方比的是**版本终态**：skill/noskill 构建在 8/6 基线树（无 prepack 能力），而"人工最优"是四轮事后合成、且其中的门控修法本身来自 skill agent；③ skill 终态的垫底几乎全由**单一配置失误**造成（presort 未路由 KDNN，损失 ~40%，几何平均被一个模型拖垮），其余 3 模型 skill ≥ noskill 且低并发档**双 agent 一起压人工版**（cvr −6~−16%、hmv −7~−11%——图重写器缺陷修复的收益所在）。

**最终格局**：三方无全面占优者——低并发（延迟域）skill 最强（hmv 低并发全场第一：BN 修复最彻底 + SVE 路径），高并发（吞吐域）人工最强（prepack），presort 单模型 noskill 最强（kdn2）。**最优全模型配置是三方的并集**：逐模型选 cvr/hmv 高并发=人工、低并发=skill；adx=三方任一（持平，skill/noskill 略优）；presort=noskill。若把含 prepack 能力的最新树交给 skill agent 重跑（其逐模型路由 3/4 正确 + 重写器修复最彻底），预期可出全场最优——列为后续实验。
- 原始数据：`bench_812_round4/{results,logs,summary.json}`；脚本 `manual_version_bench/run_812_round4.sh`。

---

## 3. 优化点对比

### 3.1 双方独立收敛的发现（skill 与 noskill 都做到）

| 发现 | skill 侧做法 | noskill 侧做法 |
|---|---|---|
| **TF Serving 不解析 gflags**（所有 KDNN/ANNC 开关在命令行无效） | gflags.cc 加环境变量默认值覆盖 | 同样做法（gflags.cc env 覆盖） |
| **KDNN 路由因 0xd03 芯片门控整体休眠** | 发现并指出根因（part 号硬编码），env 打开 | 发现并改成 HWCAP 探测（更彻底的修法） |
| **MatMul+bias+scale+自定义ReLU 链融合**（原生 remapper 不识别 `(x+|x|)/2` 形式 relu） | 启用既有 `KPFusedMatMulRewriter` + 扩平台门控 +1 行；另修复既有 BN 折叠器 3 个缺陷（Identity→Const 链、双 Const 角色冲突、loader 时机死代码） | **从零手写 384 行新重写器** `KPMatMulBiasScaleFoldRewriter`（scale 折进权重的数学等价 `(x·W+b)·s ≡ x·(W∘s)+(b·s)`） |
| **KDNN 对大 MatMul（adx）负收益** | 实测 -15% 并归因（per-chunk 打包+切分粒度） | 实测 -11~15% 并归因（K=1668 打包无缓存） |
| 负结果如实报告 | 6 项负结果（含 KDNN 线程修复无效、OpenBLAS 接入更慢、线程数上扫无益） | 4 项负结果（含 kdn4 异常、NaN 语义边界披露） |

### 3.2 skill 侧独有（noskill 未做或晚做）

| 优化 | 效果 | skill 的贡献路径 |
|---|---|---|
| **ANNC 稀疏 embedding 链融合**（树内既有但默认关的重写器） | hmv/cvr_slave 最大单项收益源 | skill 的「图层融合机会在 embedding 链」模式 +「检查默认关闭的既有设施」直觉——**第一轮就启用**；noskill 直到报告初版写完后才回头补测（多花约 40 分钟，最终也拿到了，但 hmv/cvr 的先发优势已体现在时间线上） |
| **cvr_slave 线程数与并发对齐（16→4）** | cvr_slave 额外 **+19 个百分点**（+21.8% → +40.8%） | skill 的 L3 层核心原则「线程数三处对齐工作负载」直接命中——noskill 扫了 KDNN 线程数却没扫 TF 线程数 |
| 机器 GEMM 能力标定（独立 OpenBLAS 基准 → 论证 adx/presort 已达 78% 上限） | 解释了为何 adx/presort 收益有限、避免盲目投入 | skill 基准方法学的「先定上限再优化」 |

### 3.3 noskill 侧独有（skill 没做到）

| 优化 | 效果 | 为何 skill 侧错过 |
|---|---|---|
| **`TF_KDNN_NUM_THREADS=2`（KDNN 线程上限）**：消除并发 4 请求 × 16 路 parallel_for 的 p99 肥尾 | presort 从 +5% 提到 **+13.5%**（该模型上反超 skill） | skill 侧在 adx 上测得 KDNN 负收益后**过度泛化**到 presort（"大 MatMul"归因），没有逐模型验证 KDNN——skill 反模式清单里「负结果的适用范围」的活例 |

### 3.4 改动量画像

| | skill 侧 | noskill 侧 |
|---|---|---|
| 改动文件 | 7 个 | 2 个 |
| 行数 | -20 / +221（外科手术式：启用既有设施+修堵点） | -3 / +431（大块新代码：自建重写器） |
| 风格 | 知道杠杆在哪，翻开关、修 bug | 没意识到树里有现成设施，自己写了一个（质量很高，但晚 40 分钟才发现 ANNC 既有设施可用） |

---

## 4. 资源消耗对比

| 指标 | skill 侧 | noskill 侧 | 差 |
|---|---|---|---|
| 总 token | 320,873 | 286,423 | skill +12.0% |
| 墙钟时间 | **3h33m** | 4h04m | skill **-12.7%** |
| 工具调用 | 435 | 336 | skill +29% |
| 原始数据产出 | 522 文件（bench/cpu/logs/scripts） | 371 文件（ab/scan/sweep/evidence/graphdumps） | 侧重点不同但都完整 |

skill 多花的 token 换来了：更早的最终配置收敛、4/4 模型一次到位、以及 cvr_slave 上的差异化收益。

---

## 5. 验证纪律对比（任务书要求的双检）

| 要求 | skill 侧 | noskill 侧 |
|---|---|---|
| 命中证据（日志/profile 级） | ✅ 每优化一节：ANNC 融合计数日志、`_FusedMatMul` 节点名、KDNN 73 个 JIT 符号占 25.1% cycles、基线 gebp 0 命中 | ✅ 每优化一节：fold 逐节点命中行（presort 8/8 等）、perf 调用链直指 JIT 内核（73.6% cycles）、`strings` 二进制验证 |
| 数值正确性（REST 固定输入逐元素对比） | ✅ 4 模型 × 5 batch；hmv/cvr 逐位一致，adx/presort ≤3.2e-7 | ✅ 4 模型 × 5 batch；hmv/cvr 逐位一致，presort/adx ≤2.2e-7；并诚实披露 hmv mock 输出恒 0（验证强度受限）与 Relu 折叠的 NaN 语义边界 |
| 交替 ≥5 轮 + 中位数 + 离散度 | ✅（7-20 轮/侧，含失败轮如实保留） | ✅（5-10 轮/侧，含失败轮） |
| 负结果记录 | ✅ 6 项 | ✅ 4 项 |
| 回退方式 | ✅ 全部运行期开关，不设 env = 基线行为 | ✅ 同 |

**纪律结论**：双方都严格履行了任务书的双检要求——这部分差异不大（任务书的强制协议起了兜底作用）；skill 的增量价值体现在**找到正确的优化对象更快**，而非测量更严谨。

---

## 6. 定性分析

1. **skill 的核心价值 = 经验带宽**。三个 skill 侧的差异化收益全部来自「前人踩过的坑变成了 checklist」：休眠路由要查（→ 0xd03 门控）、embedding 链是融合富矿（→ 第一时间启用 ANNC 稀疏融合）、线程数要对齐并发（→ cvr_slave 16→4，+19pp）。noskill 侧每项都要靠自己的 profiling 推导——它最终也推导出来了（能力强），但慢了 30-40 分钟且漏了一项（TF 线程数）。
2. **skill 不是万能的，甚至有反面**：presort 上 skill 侧把 adx 的 KDNN 负结果过度泛化（「大 MatMul 不适合 KDNN」），错失 `TF_KDNN_NUM_THREADS=2` 这一 noskill 独有的调优维度，该模型反而落后 6pp。**skill 给的是先验，先验既有加速也有误导风险——结论仍须逐案测量。**（这正好验证了 skill 自己的第一原则「先测量后优化」。）
3. **双侧的能力底座都很强**：noskill 侧独立发现了全部基础设施问题（gflag 不解析、芯片门控），自写的融合器数学与工程质量都高（scale 折进权重的等价变换、device 标注修 XLA 崩溃、NaN 边界披露）。skill 的价值是在强底座上的**杠杆放大**，不是从 0 到 1。
4. **收敛演化**：hmv 上双方从完全不同路径（启用+修复既有设施 vs 手写新重写器）到达几乎相同的终态（348k vs 342k，+1.8% 差距）——这既说明 hmv 的优化空间被双方基本吃满，也说明 skill 侧在「到达同等的路上」花的时间显著更少。
5. **报告质量**：双侧报告都完整可信（数字 100% 从原始日志复现）。skill 侧多了「机器能力标定」一节（解释上限），noskill 侧多了「单变量归因扫描矩阵 scan4」与执行图导出——各有亮点。

---

## 7. 效度威胁（读结论前必读）

1. **「答案」泄漏（设计使然，需明示）**：skill 提炼自本仓库 8 月冲刺——ANNC 融合、BN 折叠、芯片门控等恰是其后真实发生的工作。虽然 skill 文本不含任何本仓路径/算子名/flag 名（前期 grep 验证），但方法论层面与「考纲」重合。本实验度量的是「**把沉淀经验带给新 agent 的价值**」，不是「skill 相对任意通用知识的价值」。
2. **共享机器噪声**：实验期间其他用户的编译/压测负载波动（双侧报告均声明 load avg 50-116、个别轮 ±10-14% 离散并被丢弃）。缓解：交替协议 + 中位数 + 各自基线归一；双侧基线中位数差异 1.7-3.7%（NUMA 节点差异 + 噪声）。
3. **单次实验**：各配置只跑了一个 agent，无重复；个体差异（同一模型同一配置的 run-to-run 方差）未量化。
4. **模型覆盖面**：4 个 mock 模型 = Dense/MatMul + 稀疏 embedding 链拓扑；MMoE gate、attention 交互、prepack 类权重缓存优化未被 exercising（无对应 shape/结构），skill 的部分章节未被测试。
5. **hmv 正确性验证强度受限**：mock 模型输出按构造恒 0（双侧均诚实披露）——数值对比对该模型只能证 shape/传播正确。
6. **noskill 的最终数字包含其「报告初版后追加」的 ANNC 补测**（+40 分钟）——按最宽口径给 noskill 计分（收其最终版数字），对 skill 结论是保守的。
7. **防作弊审计（2026-09-08 事后执行，针对「noskill 为何这么强」的质疑）**：对两个 agent 的完整 transcript（`${TRANSCRIPT_DIR}/agent-*.jsonl`，336/435 次工具调用全量扫描）核查边界遵守——**noskill agent 零违规**：仅有的 POC 路径访问是 bazel 共享缓存（`--repository_cache/--disk_cache`，任务书构建模板明文给出），从未触碰 POC 参考实现、loadtime-fork、对方工作区或 skill 副本；skill agent 的对应命中仅为读取自己工作区内的 skill 副本（实验处理本身）。代码 diff 复核：noskill 树相对 pristine 恰好只有其报告声称的 2 个文件（gflags.cc + graph_opt.cc），无引入二进制、模型文件为共享只读资产。数字链：round-4 对比的全部数字由主 agent 从原始 perf_analyzer 日志直接解析（非采信 agent 自报），全部轮次请求 1.5 万+、延迟分布正常、无失败。结论：**noskill 的强度是真实的**——基线树内本就含有全部获胜资产（ANNC 重写器、KDNN SVE 内核，均为默认关闭状态），任务是"发现+启用+调优"，强 agent 靠自己的 profiling 纪律同样能做到；noskill 的优势来自自写 fold 重写器的高质量与逐模型实测（skill 反而在 presort 上过度泛化了 adx 负结果）。

---

## 8. 结论

| 维度 | 胜者 | 幅度 |
|---|---|---|
| 4 模型几何平均提升 | **skill** | +17.9% vs +15.4%（+2.5pp）；人工 prepack 在其验证口径下 adx/presort/hmv/cvr 分别 +12.4%/+63%/+4%/+3%（§2.4.2） |
| 绝对终态吞吐（消融口径，并发 4） | skill 3/4 模型领先，noskill 1/4（presort） | 最大差 cvr +14.8%（skill）；两 agent 均 4/4 超过人工版（即便给人工版换用其验证口径线程配置） |
| **同节点头对头**（§2.5，最终产物直评） | **skill** | 几何平均 **+1.9%**（cvr +12.6% / hmv +2.8% / adx 平 / presort -7.1%） |
| 墙钟时间 | **skill** | -12.7%（3h33m vs 4h04m） |
| token | noskill 略省 | -10.7% |
| 优化点覆盖 | skill 多 2 项关键命中（稀疏融合先发、线程对齐），少 1 项（KDNN 线程上限） | — |
| 验证纪律 | 双方持平（任务书协议兜底） | — |

**净结论**：在有严格测量协议约束的任务里，`tf-inference-opt` skill 把 agent 的优化结果从「几何平均 +15.4%」提升到「+17.9%」，把完成时间缩短 13%，其价值机制是**把生产沉淀的坑位清单（休眠路由、融合富矿、线程对齐）转化为先验**，让 agent 少走 30-40 分钟的探索弯路并命中更多优化点；同时 presort 上的失利（-6pp）表明 skill 的先验也可能被过度泛化，**测量纪律仍是一切结论的地板**——这与 skill 自身的第一原则一致。对照基准表明：agent 的优势不在于单点技术（人工版 prepack 在其验证口径下工作正常且与 8/12 报告逐点吻合），而在于**问了更根本的问题并做了本机逐模型调优**——「这个模型该不该用 KDNN」是 8/12 ON/OFF 口径（双侧强制 KDNN）看不见的盲区，adx 上 Eigen 比 KDNN-prepack 快 1.8×；且线程配置是本实验最大混杂变量（adx 在 16/16 vs 32/32 下 KDNN 性能差 2.3×）——再次印证「优化不可跨机型/跨配置泛化，必须逐案测量」。

skill 的改进方向（据本实验）：在 kernel_integration.md 的负结果/回退章节补一条「**负结果的适用范围要按 shape/模型逐一验证后再泛化**」，并把「KDNN/库线程数与并发的关系（parallel_for convoy 肥尾）」补入 runtime_threading.md 的线程治理节。

---

## 附：产物索引

- 本报告：`${EXPERIMENT_ROOT}/ablation/REPORT.md`（副本：`ablation_report.md`）
- skill 侧报告：`results/skill/REPORT.md`（344 行）+ 522 个原始数据文件 + `agent_stats.json`
- noskill 侧报告：`results/noskill/REPORT.md` + 371 个原始数据文件 + `agent_stats.json`
- 双侧代码改动：`ws-{skill,noskill}/tf`（对照 `pristine/tf`；diff 统计见 §3.4）
- 共享基线：`baseline-bin/tensorflow_model_server`（md5 与两侧构建一致）
- 任务书：`brief-{skill,noskill}.md`（除 skill 引导段外逐字相同）
- skill vs noskill 头对头复测：`head_to_head/`
- 人工版补测：`manual_version_bench/`（消融口径：run_manual_bench.sh + results/logs/summary.json；验证口径对齐复测：aligned/（run_aligned_bench.sh、逐轮 results、ON/OFF 服日志、含 freeze 隔离验证））
- 构建日志：`baseline_build.log`、`noskill_warm_build.log`、两侧 `results/*/logs/build*.log`

---

## 9. 第二轮消融（2026-09-08，skill v2 + 全并发梯度口径）

完整报告见 `REPORT_ROUND2.md`（副本：`ablation_report_round2.md`）。要点：① 三方终态各赢一块——人工最优赢 cvr/hmv 高并发（prepack），skill2 赢 presort 全档（线程修正+SVE+cap4，+26~31% vs 人工、3~4.7× vs noskill2），noskill2 赢 adx 高并发（自适应并发门控 +14%）；② skill v2 的清单化改进（线程对齐、冻结图折叠三坑）被验证有效且各值一个数量级差距，但本轮两项最大创新（自适应路由、hmv ANNC 判断）不在 skill 覆盖内；③ KDNN NEON prepack 在 presort shape 上劣于 SVE JIT（−26%），prepack 适用面需按 shape 重标定；④ presort 生产配置 1/1 线程应重审（16/16 = 4.5×）。
