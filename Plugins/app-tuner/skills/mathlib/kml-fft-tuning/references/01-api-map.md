# KML_FFT 函数总览与数据布局基础

## 六大类函数

| 类别 | 作用 | 代表函数 |
|---|---|---|
| plan 函数 | 建立 FFT 变换 plan，用完须 `destroy_plan` 释放 | `kml_fft_plan_dft` / `_r2c` / `_c2r` / `_r2r` 及其 `_1d/_2d/_3d`、`guru`、`guru_split`、`guru64`、`many` 变体 |
| 变换执行 | 按 plan 执行变换 | `kml_fft_execute`、`kml_fft_execute_dft`、`_dft_r2c`、`_dft_c2r`、`_r2r`、`_split_dft*` |
| 内存函数 | 分配/释放/销毁 | `kml_fft_malloc`、`kml_fft_free`、`kml_fft_destroy_plan` |
| 线程函数 | 多线程框架初始化与释放 | `kml_fft_init_threads`、`kml_fft_plan_with_nthreads`、`kml_fft_cleanup_threads` |
| MPI Local Size | 进程间划分数据、算本地缓冲 | `kml_fft_mpi_local_size`（及 transposed / 1d / many / 3d / ext 一族）、`kml_fft_split_world` |
| MPI transposition | 分布式转置 | `kml_fft_mpi_plan_transpose`、`kml_fft_mpi_plan_many_transpose` |

另有：`KFFTGetVersion`（取 FFT 产品版本）、`kml_fft_plan_upsampling_2d`（二维上采样 plan）。

## 精度前缀（三档）

| 前缀 | 精度 | 复数类型 | 头文件 |
|---|---|---|---|
| `kml_fft_` | 双精度 | `kml_fft_complex` = `double[2]` | `kfft.h` |
| `kml_fftf_` | 单精度 | `kml_fftf_complex` = `float[2]` | `kfft.h` |
| `kml_ffth_` | 半精度/特化 | `kml_ffth_complex` | `kfft.h`（**仅 Taishan 200 1280 V2 / 2280 V2**） |

MPI 版本另需 `kfft-mpi.h`；`kml_ffth_*` 的 MPI 侧接口文档未列出。

## 数据结构

```c
typedef double kml_fft_complex[2];   /* 双精度复数 */
typedef float  kml_fftf_complex[2];  /* 单精度复数 */

struct kml_fft_plan_s { problem_s problem; solver_s solver; KML_UINT num_threads; };
typedef struct kml_fft_plan_s *kml_fft_plan;
```

- `problem_s`：待变换问题的描述（变换类型、输入输出等）
- `solver_s`：求解器参数（所用函数、拆成的子问题 plan 等）
- `num_threads`：该 plan 使用的线程数 —— **plan 建好即固定**，见 05 章

跨步描述用 `kml_fft_iodim`（guru/many 系列）：

```c
int n;   /* 该维长度 */
int is;  /* 该维输入相邻元素的间隔（stride） */
int os;  /* 该维输出相邻元素的间隔 */
```

## 四类变换与 rank 上限（有差异，别一律按 4 维写）

| 变换 | plan 函数 | rank 约束 | sign 参数 |
|---|---|---|---|
| C2C（复数→复数） | `kml_fft_plan_dft` | 1 ≤ rank ≤ 4 | 有 |
| R2C（实数→复数） | `kml_fft_plan_dft_r2c` | 文档未单列（同族按 C2C 理解） | 无 |
| C2R（复数→实数） | `kml_fft_plan_dft_c2r` | 同上 | 无 |
| R2R（实数→实数） | `kml_fft_plan_r2r` | 1 ≤ rank ≤ 3 | 无（方向由 kind 表决定） |
| MPI C2C | `kml_fft_mpi_plan_dft` | 1 ≤ rank ≤ 3 | 有 |

## 数据布局

- 数组为**行主序**；guru 转置接口的描述即为「以行为主顺序，在 n0 维上分布块」。
- 支持 **in-place**（`in == out` 同一指针）与 out-of-place 两种用法。
- **半复数形式**（仅 R2C 的复数 out）：`n0 × n1` 的数组 A，若 `A(n0,j)=A(0,j)`、`A(i,n1)=A(i,0)`，
  则用 `n0 × [(n1/2)+1]` 的数组 B 保存 —— 这就是 `plan_dft_r2c` 的 out 要比 in 小的原因。
  R2R（DCT/DST/DHT/R2HC）的 out 是**实数**数组，按完整维度分配（元素数 = 各维乘积），不走这条规则。
- in-place 时若要 `plan_dft_r2c` 复用同一块内存，缓冲必须按**半复数后的较大尺寸**申请；
  文档未给出官方 padding 公式，按实际 out 元素数申请（待实测确认）。

## 本技能未展开的部分

- `kml_fft_plan_dft_scale` / `kml_ffth_execute_dft_scale*`：带缩放的 h 版接口，文档页存在但本技能未逐参数展开。
- `kml_fft_plan_upsampling_2d`：二维上采样 plan，同上。
- 具体链接库名与编译选项：KML_FFT 函数说明章节未给出，须按 HPCKit 安装指导，不要照搬 FFTW 的 `-lfftw3`。
