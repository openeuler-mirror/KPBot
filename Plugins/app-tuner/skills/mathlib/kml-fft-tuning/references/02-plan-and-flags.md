# plan 建立、flags 选择与执行

## 核心 plan：kml_fft_plan_dft

```c
kml_fft_plan kml_fft_plan_dft(int rank, const int *n, kml_fft_complex *in,
                              kml_fft_complex *out, int sign, unsigned flags);
```

| 参数 | 类型 | 约束 / 取值 |
|---|---|---|
| `rank` | int | 变换维数，**1 ≤ rank ≤ 4** |
| `n` | const int* | 长度 rank 的数组，每维大小，`n[i] ≥ 1` |
| `in` / `out` | complex* | 输入 / 输出缓冲（可同一指针 = in-place） |
| `sign` | int | `-1` = `KML_FFT_FORWARD`，`+1` = `KML_FFT_BACKWARD` |
| `flags` | unsigned | `KML_FFT_ESTIMATE` 或 `KML_FFT_PATIENT` |

返回值：`kml_fft_plan` 结构体指针。**非空 = 建 plan 成功；空 = 失败**——函数说明未定义扩展错误码，
所以失败原因只能靠参数自查（见文末四条），不要指望 errno。

## flags 只有两档

| 取值 | 行为（文档原文语义） | 代价 |
|---|---|---|
| `KML_FFT_ESTIMATE` | 不对不同算法做实际计算评估，快速选一个方案 | 可能不是最优 |
| `KML_FFT_PATIENT` | 实际评估多个方案并测量执行时间，期望更优 | **plan 函数耗时增加** |

FFTW 的 `FFTW_MEASURE`、`FFTW_WISDOM_ONLY`、`FFTW_UNALIGNED`、`FFTW_PRESERVE_INPUT` 在 KML_FFT 的
函数说明里**没有对应取值**——迁移时代码里的这些宏必须换掉，不能照传。

选择口径：

- 变换**只做一次** → `KML_FFT_ESTIMATE`（PATIENT 的规划时间摊不回来）。
- 同形状变换**反复执行**（迭代求解、逐帧、逐时次）→ 用 `KML_FFT_PATIENT` 建一次 plan，之后只 `execute`。
- 形状在运行期会变（每步不同长度）→ 别在热路径里建 plan：把形状分桶，每桶建一次 plan 复用。

## 执行：execute 与 execute_dft

```c
void kml_fft_execute(const kml_fft_plan p);                            /* 用建 plan 时的 in/out */
void kml_fft_execute_dft(const kml_fft_plan p, kml_fft_complex *in, kml_fft_complex *out);  /* 仅 C2C：换交错复数指针 */
```

- in/out **指针固定** → 用 `execute(p)`。
- 数据在池子里换块（多缓冲、流水线、乒乓缓冲）→ **按 plan 类型选换指针的接口**，签名不同不能混用：
  - C2C（plan 来自 `plan_dft*`，交错复数）：`kml_fft_execute_dft(p, kml_fft_complex *in, kml_fft_complex *out)`
  - R2C（`plan_dft_r2c`）：`kml_fft_execute_dft_r2c(p, double *in, kml_fft_complex *out)`——输入是实数 `double*`，交给 `execute_dft` 编译期就类型不符
  - C2R（`plan_dft_c2r`）：`kml_fft_execute_dft_c2r(p, kml_fft_complex *in, double *out)`
  - R2R（`plan_r2r`）：`kml_fft_execute_r2r(p, double *in, double *out)`
  - split 布局（`plan_guru_split_*`）：`kml_fft_execute_split_dft` / `_split_dft_r2c` / `_split_dft_c2r`（实部、虚部分开传）

  同形状、同类型的 plan 可复用——这是 KML 侧替代 FFTW wisdom 的做法：本版没有 plan 序列化接口，只能在**进程内**把规划成本摊到后续执行上。

`execute_dft` 家族（含 `_execute_dft_r2c` / `_execute_dft_c2r`）与 R2R 的 `_execute_r2r`——**没有 `_execute_dft_r2r`**：文档 `kml_fft(f/h)_execute_r2r` 一节的签名是 `void kml_fft_execute_r2r(const kml_fft_plan p, double *in, double *out)`——以及 `guru_split` 族对应的 `_execute_split_dft` / `_execute_split_dft_r2c` / `_execute_split_dft_c2r` 是同一套「换指针执行」接口，**按 plan 类型配对**，不要用 C2C 的入口去跑实数 plan。

## 释放

```c
void kml_fft_destroy_plan(kml_fft_plan p);   /* 调用后 plan 不再可用 */
```

调用约束（文档明写）：plan 使用完毕后**必须**通过 `destroy_plan` 释放；
`kml_fft_malloc` 申请的内存**只能**由 `kml_fft_free` 释放。

## 最小骨架（双精度 1 维，单精度把前缀换成 kml_fftf_）

```c
int N = 16;
kml_fft_complex *in  = (kml_fft_complex*)kml_fft_malloc(sizeof(kml_fft_complex) * N);
kml_fft_complex *out = (kml_fft_complex*)kml_fft_malloc(sizeof(kml_fft_complex) * N);

kml_fft_plan plan = kml_fft_plan_dft_1d(N, in, out, KML_FFT_FORWARD, KML_FFT_ESTIMATE);
if (!plan) {
    /* 建 plan 失败：查 rank 范围、n[i] ≥ 1、R2R kind 的长度约束、缓冲是否 kml_fft_malloc 分配 */
    kml_fft_free(in);
    kml_fft_free(out);
    return -1;                      /* 空 plan 不可执行，先释放再返回 */
}

kml_fft_execute(plan);              /* plan 有效才执行；换指针用 execute_dft(p, in, out) */

kml_fft_destroy_plan(plan);
kml_fft_free(in);
kml_fft_free(out);
```

## 常见建 plan 失败原因（按排查顺序）

1. `rank` 越界（C2C > 4、R2R/MPI > 3）。
2. `n[i] < 1` 或传了未初始化的 `n` 数组。
3. 缓冲不是 `kml_fft_malloc` 分配（用 malloc/new 分配后传给 plan，文档要求配套分配器）。
4. 维度乘积与缓冲大小不匹配（R2C 的 out 必须按半复数尺寸；R2R 的 out 是实数、按完整维度）。
5. R2R 的 `n` 不满足 kind 的长度约束：`REDFT11`/`RODFT11` 要求 **4 的倍数**（一维 n=6 就会建失败），`REDFT01`/`REDFT10`/`RODFT01`/`RODFT10` 要求偶数长度，`REDFT00` 需 n ≥ 2。
