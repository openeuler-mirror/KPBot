# MPI 分布式 FFT：初始化、本地尺寸、转置

MPI 侧头文件是 `kfft-mpi.h`；`kml_ffth_*` 的 MPI 接口文档未列出，本节按 `kml_fft_` / `kml_fftf_` 说明。

## 生命周期

```c
int kml_fft_mpi_init(void);     /* MPI 环境下的 KML-FFT 资源初始化，成功返回 1，否则 0 */
int kml_fft_mpi_cleanup(void);  /* 释放 MPI 环境下的资源，成功返回 1，否则 0 */
```

顺序：`mpi_init` → `local_size` 算本地缓冲 → `mpi_plan_*` → `mpi_execute_*` → `destroy_plan` → `mpi_cleanup`。

## 分布式 plan

```c
kml_fft_plan kml_fft_mpi_plan_dft(int rank, const int *n, kml_fft_complex *in,
                                  kml_fft_complex *out, MPI_Comm comm, int sign, unsigned flags);
```

- **rank 约束 1 ≤ rank ≤ 3**（比单进程 C2C 少一维）。
- flags 在 ESTIMATE/PATIENT 之外多了 MPI 专用取值：

| 取值 | 含义 |
|---|---|
| `KML_FFT_MPI_SCRAMBLED_IN` / `_OUT` | 输入/输出为 scrambled 格式，**仅对一维变换生效** |
| `KML_FFT_MPI_TRANSPOSED_IN` / `_OUT` | 输入/输出由 `n0×n1×…×n(d-1)` 转为 `n1×n0×…×n(d-1)` |

这两组是「省掉一次显式转置」的开关：数据已经按对方布局排好时打开，可少一次全数组搬运。

## 本地尺寸与偏移

```c
ptrdiff_t kml_fft_mpi_local_size(int rank, const ptrdiff_t *n, MPI_Comm comm,
                                 ptrdiff_t *local_n0, ptrdiff_t *local_0_start);
```

- 返回值：**本进程需要分配的元素数量**（ptrdiff_t），拿它去 `kml_fft_malloc(sizeof(elem) * 返回值)`。
- `local_n0`：本进程负责的数据大小；`local_0_start`：本进程数据相对全局起点的偏移。
- 约束：`rank ≥ 1`，`n[i] ≥ 1`。

同一族还有（按需查函数页取参数）：

| 函数族 | 用途 |
|---|---|
| `_mpi_local_size_transposed` | 输入/输出转置过的布局 |
| `_mpi_local_size_1d` / `_many_1d` | 1 维、多维批量的 1 维 |
| `_mpi_local_size_2d` / `_2d_transposed` | 2 维 |
| `_mpi_local_size_3d` / `_3d_transposed` | 3 维 |
| `_mpi_local_size_many` / `_many_transposed` | howmany 批量 |
| `_mpi_local_size_ext` / `_3d_ext` / `_transposed_ext` / `_3d_transposed_ext` | 使能 box_order 后的扩展接口 |
| `kml_fft_split_world` | 描述**所有**进程数据的大小和位置 |

## 分布式转置

```c
kml_fft_plan kml_fft_mpi_plan_transpose(ptrdiff_t n0, ptrdiff_t n1, double *in, double *out,
                                        MPI_Comm comm, unsigned flags);
kml_fft_plan kml_fft_mpi_plan_many_transpose(...);
```

- 语义：`n0×n1` 数组，行主序、在 n0 维上块分布 → 转成在 n1 维上块分布的 `n1×n0` 数组。
- flags 表示输入和/或输出**已被局部转置**。
- 返回的是 plan，用 `execute` 执行；转置 plan 与 FFT plan 的差别只在内部调度。

## 简化调用接口

`kml_fft_mpi_forward_c2c` / `_backward_c2c` / `_forward_r2c` / `_backward_c2r`：
按正/反向直接调用的封装；`_mpi_execute_dft*` 与单进程的 execute 族对应。
`_mpi_plan_dft_3d_ext` / `_mpi_plan_create*` 是 3D 扩展接口（配套 07 章的环境变量）。

## 排查顺序（MPI 结果不对时）

1. 本地缓冲是否按 `mpi_local_size` 的**返回值**分配（不是 `n[0]*n[1]/nproc`）。
2. `local_0_start` 是否参与了全局索引换算（重排/输出时最常错）。
3. flags 的 TRANSPOSED/SCRAMBLED 是否与实际布局一致（多乘一次转置=静默错数）。
4. rank 是否超过 3。
