---
name: kml-fft-tuning
description: >
  KML_FFT 使用与调优：plan/flags、线程数、MPI 本地尺寸、3D 扩展、FFTW 迁移。
  鲲鹏数学库（BoostKit）FFT 接口的只读知识库：plan 建立与 destroy 约束、flags 只有
  ESTIMATE / PATIENT 两档（没有 MEASURE、本版没有 wisdom 序列化）、线程数必须在建 plan 之前设置
  （init_threads → plan_with_nthreads，只影响后续 plan）、MPI 本地尺寸与 SCRAMBLED/TRANSPOSED 标志、
  guru / guru_split / guru64 / many 的跨步与批处理语义、3D 扩展的 SCALFFT_PROC_GRID /
  SCALFFT_DECOMPOSE_TYPE / SCALFFT_*_A2A_ALGO 环境变量，以及 FFTW → KML 时必须改的五处。
  当用户提到 KML_FFT、kml_fft_plan_dft、kml_fft_plan_with_nthreads、KML 的 FFT 线程数、
  kml_fft_mpi_local_size、SCALFFT_ 环境变量、把 FFTW 代码迁到鲲鹏数学库、r2c 的 out 要开多大时使用。
  不执行编译/压测/改码，也不做跨架构浮点结果一致性判定（归 precision 域负责）；
  只问 FFTW / cuFFT / numpy.fft 自身机制（wisdom、多 stream、链接错误、库选型）时不适用。
license: MulanPSL-2.0
---

# KML_FFT 使用与调优判定

鲲鹏数学库 KML_FFT（BoostKit / HPCKit 的一部分）的接口体系、硬约束与调优判定：plan 怎么建、
flags 只有两档怎么选、线程数为什么必须在建 plan 之前设置、MPI 下 local size 怎么算、3D 扩展接口用
哪几个环境变量。
**只做判定与口径**：本技能不执行编译、不跑 benchmark、不改用户代码。接口、参数、约束、flags 取值
逐条来自鲲鹏 HPC 开发文档的 KML_FFT 函数说明；凡属工程口径或按文档外推的结论，正文里都标注了
「按…外推 / 待实测」，未标注的不视为已验证。

**文档基线**：所核为鲲鹏 HPC 开发文档（Kunpeng HPCKit 开发指南）KML_FFT 章节
（`www.hikunpeng.com/document/detail/zh/kunpenghpcs/hpckit/devg/kunpengaccel_kml_*.html`），
按在线版逐页抓取于 2026-11-25；该站点不标版次与更新时间，故正文各处「本版」= 该次抓取快照。
现场版本以 `kfft.h` 的函数签名与 `KFFTGetVersion()` 返回值为准核对（文档示例输出为
HPCKit 24.0.RC1 / hpckit-kml-fft 2.3.0，文档注明「以实际运行结果为准」）；HPCKit 升级后按此基线重核。

## When to Use

- 「KML_FFT 的 plan 怎么建 / 用完要不要 destroy」
- 「ESTIMATE 和 PATIENT 选哪个，没有 MEASURE 吗」
- 「怎么设 FFT 的线程数 / 为什么开了线程没变快」
- 「MPI 下 FFT 要怎么分配数据 / local_size 返回的是什么」
- 「3D FFT 结果不对 / 进程排布怎么指定」
- 「从 FFTW 迁到 KML_FFT 要改什么」
- 「r2c 的 out 数组要开多大」

不在本技能范围：第三方 FFT 库（FFTW、cuFFT）自身的规划器/wisdom/对齐/stream 语义——KML 与它们接口
同名但库行为不同，别互相外推；跨架构浮点结果一致性判定；昇腾 NPU 侧算子与数值话题。

## Prerequisites

- 头文件：单进程/线程/内存 `kfft.h`；MPI 版本 `kfft-mpi.h`
- 链接方式与安装路径不在本技能覆盖范围（随 HPCKit 安装说明），别照搬 FFTW 的 `-lfftw3`
- `kml_ffth_*`（半精度/Taishan 特化）**仅适用 Taishan 200 1280 V2 与 2280 V2**

## How to Run

先读 `references/01-api-map.md` 定位到具体函数族，再按需 `skill_view` 载入对应章：

- 建 plan、选 flags → `references/02-plan-and-flags.md`
- 选变换类型、算数组大小 → `references/03-transforms-and-layout.md`
- 跨步/多维批处理 → `references/04-guru-many-strides.md`
- 多线程 → `references/05-threads.md`
- MPI 分布式 → `references/06-mpi.md`
- 3D 扩展接口环境变量 → `references/07-env-vars-3dfft.md`
- 调优决策与验收 → `references/08-tuning-playbook.md`
- 从 FFTW 迁移 → `references/09-fftw-migration.md`

## Quick Reference

| 项 | 值 |
|---|---|
| 双精度前缀 | `kml_fft_` |
| 单精度前缀 | `kml_fftf_` |
| Taishan 特化前缀 | `kml_ffth_`（仅 1280 V2 / 2280 V2） |
| 方向 sign | `-1`=`KML_FFT_FORWARD`，`+1`=`KML_FFT_BACKWARD` |
| flags | `KML_FFT_ESTIMATE` / `KML_FFT_PATIENT`（**只有两档**） |
| rank 上限 | C2C 1–4；R2R 1–3；MPI 1–3 |
| plan 成功判据 | 返回非空指针 |
| 内存 | `kml_fft_malloc` / `kml_fft_free` 必须成对，plan 用 `destroy_plan` |
| 线程数 | 全局设置：`init_threads` → `plan_with_nthreads` → 建 plan → `cleanup_threads` |

## Procedure

1. **定变换类型**：复数→C2C，实数输入→R2C，复数回实数→C2R，纯实数同类变换（DCT/DST/DHT）→R2R。
2. **算数组大小**：**只有 R2C 的复数 out** 走半复数布局（末维 `n/2+1` 个复数，见 03 章）；R2R 与 C2R 的实数 out 按**完整维度**分配（元素数 = 各维乘积：16 点 `REDFT10` 的 out 就是 16 个 double）。按 `n/2+1` 给 R2R 开缓冲会越界写。
3. **建 plan**：先定线程数（05 章），再 `plan_*`；非空=成功，失败直接查参数约束（rank、n[i]≥1、R2R kind 的长度约束）。
4. **执行**：`execute(plan)` 用建 plan 时的 in/out；换数据指针要**按 plan 类型选接口**——交错复数 C2C 用 `execute_dft(plan, kml_fft_complex *in, *out)`；R2C 用 `execute_dft_r2c(plan, double *in, kml_fft_complex *out)`；C2R 用 `execute_dft_c2r(plan, kml_fft_complex *in, double *out)`；R2R 用 `execute_r2r(plan, double *in, double *out)`（**没有 `execute_dft_r2r`**，文档 `kml_fft(f/h)_execute_r2r` 一节）；split 布局用 `execute_split_dft*`（04 章）。`execute_dft` 只接受交错复数，plan 可复用。
5. **分发与回收**：`destroy_plan` 释放 plan，`kml_fft_free` 释放自己申请的内存；多线程场景最后 `cleanup_threads`。
6. **MPI**：`mpi_init` → `mpi_local_size` 算本地缓冲 → `mpi_plan_*` → `mpi_execute_*` → `mpi_cleanup`（06 章）。

## 评测资产

- 触发路由矩阵：`evals/eval_queries.json`（`eval_type: trigger_rate`，20 条，10 正 10 负；
  负例用 `note` 写明「该由谁接 / 无对应技能」）。
  候选索引（5 个本仓 master 实存兄弟技能 + 本技能自身）：`kml-fft-tuning`、`model-precision-analysis`、
  `topdown-analysis-advisor`、`kptools-devkit`、`kptools-thread-affinity-analyzer`、`kpbot-app-tuner`。
  实测（2026-11-26，候选集 = 上述 6 个（5 兄弟 + 本技能），每 query ×3）：正例 **10/10**；
  负例 **5/10** —— 未通过的 5 条正是「只问 FFTW/cuFFT/numpy.fft 自身」的问题（wisdom 复用、
  FFTW_MEASURE 缓存、cuFFT 多 stream、numpy/scipy 选型、`undefined reference to fftw_execute`）。
  原因不是描述跑偏：本仓 master 没有第三方 FFT 技能，单域索引里任何 FFT 字样的问题都会落到本技能。
  description 已加排除句，但索引按 60 字符截断时该句不可见，故这 5 条记为**已知限制**（候选集缺口），
  要真正消掉需要仓内出现一条「第三方 FFT 库」技能，或按完整描述路由。
  过闸线：正例命中 ≥ 0.8（已达标 1.0）。
- 任务级用例：`evals/evals.json`（plan 参数、flags 二选一、线程顺序、MPI local size、迁移差异）。
- 跑法：按 `protocol` 字段执行——每条 query 跑 3 次，单条触发率 ≥ 0.5 时取「应触发/不应触发」判定，
  与 `should_trigger` 比对；候选索引须同时包含同域兄弟技能，否则测不出误选。

## Pitfalls

- **没有 MEASURE**：FFTW 的 `FFTW_MEASURE`/`FFTW_PATIENT`/`FFTW_WISDOM_ONLY` 三档在 KML 只有 ESTIMATE/PATIENT 两档，`FFTW_MEASURE` 传进来编译不过。
- **本版没有 wisdom 类接口**：当前函数说明里没有导出/导入 plan 的函数（也没有 `wisdom` 字样），
  跨运行复用只能自建 plan 表，**缓存键要含变换类型、方向（sign）或 kind 数组、rank 与各维长度、in/out 布局、线程数**——只按形状建键会把 1024 点正变换和反变换当成同一个 plan 复用（形状相同、方向相反，长度检查发现不了）。若后续 HPCKit 版本新增 plan 序列化接口，本条需重核。
- **线程数不是 plan 参数**：`plan_with_nthreads` 是全局设置，只影响其后新建的 plan；先建 plan 再设线程数无效。
- **`init_threads`/`plan_with_nthreads`/`cleanup_threads` 均不可重入**，且 cleanup 之后不能再使用多线程。
- **kml_fft_malloc 的内存只能 kml_fft_free**，混用 free() 会崩；plan 必须 `destroy_plan`。
- **rank 上限不一致**：C2C 允许 1–4 维，R2R 与 MPI 版本只到 3 维。
- **R2R 的 kind 是逐维数组**，四种变换类型（REDFT/RODFT/R2HC/HC2R/DHT）共 11 个取值，没有默认值。
- **R2R 一维长度不是「n≥1 就合法」**：`REDFT11`/`RODFT11`（DCT-IV/DST-IV）要求 n 是 **4 的倍数**（n=6 建 plan 会失败），`REDFT01`/`REDFT10`/`RODFT01`/`RODFT10` 要求偶数长度，`REDFT00` 需 n ≥ 2；`R2HC`/`HC2R`/`DHT` 文档未单列限制。建 plan 返回空时先核这条（02 章排查表第 5 条）。

## Verification

```bash
bash cases/skill-integrity/run.sh
```

检查项：frontmatter（name/description/license，且不带顶层 author/version/platforms）、references 索引
双向一致、KML 关键锚点、evals.json 与 eval_queries.json 结构、敏感信息（命中串脱敏回显）。
产物 `cases/skill-integrity/results/result.json`，退出码 0 = 通过。

`cases/` 属于仓库侧用例；同步到个人 profile 时不随附，那边按 Hermes 自己的技能校验（frontmatter
白名单、引用文件存在性）判定，口径不同、不要交叉套用。
