# 四类变换的选用与数组大小

## C2C

```c
kml_fft_plan kml_fft_plan_dft (int rank, const int *n, kml_fft_complex *in, kml_fft_complex *out, int sign, unsigned flags);
kml_fft_plan kml_fft_plan_dft_1d(int n, kml_fft_complex *in, kml_fft_complex *out, int sign, unsigned flags);
kml_fft_plan kml_fft_plan_dft_2d(int n0, int n1, ...);
```

- in/out 都是复数数组，元素数 = `n[0] * n[1] * ... * n[rank-1]`。
- `sign` 决定正/反变换：`-1` FORWARD、`+1` BACKWARD。
- 需要按维指定 stride 或多序列时，改用 guru/many 系列（04 章）。

## R2C（实数 → 复数）

```c
kml_fft_plan kml_fft_plan_dft_r2c(int rank, const int *n, double *in, kml_fft_complex *out, unsigned flags);
```

- **没有 sign 参数**（方向固定为正变换）。
- `in` 是 `double*` / `float*` / `__fp16*`，`out` 是复数类型。
- out 采用**半复数形式**：`n0 × n1` 的实数输入，复数结果只需 `n0 × [(n1/2)+1]` 个复数。
  3 维则是最后一维减半加一（`n0 × n1 × [(n2/2)+1]`）—— 按半复数定义外推，未在文档示例中逐字给出。
- 因此 out 缓冲元素数 = `n0 * ... * ((n_last/2) + 1)`，**不能按输入大小申请**。

## C2R（复数 → 实数）

```c
kml_fft_plan kml_fft_plan_dft_c2r(int rank, const int *n, kml_fft_complex *in, double *out, unsigned flags);
```

- 同样无 `sign`（反向）。
- `n` 填**实数侧**的完整尺寸（c2r 的结果是实数数组），输入复数数组按半复数尺寸准备。
- c2r 的结果**未归一化**是常见误解点：文档在此章节未声明归一化行为，做正反变换对时按实际比对结果决定是否除 N（待实测）。

## R2R（实数 → 实数，含 DCT/DST/DHT）

```c
kml_fft_plan kml_fft_plan_r2r(int rank, const int *n, double *in, double *out,
                              const kml_fft_r2r_kind *kind, unsigned flags);
```

- **rank 上限 3**（不同于 C2C 的 4）。
- `kind` 是长度 rank 的数组，**逐维指定**变换类型，可选值共 11 个：

| 组 | 取值 |
|---|---|
| 半复数 | `KML_FFT_R2HC`、`KML_FFT_HC2R` |
| DHT | `KML_FFT_DHT` |
| DCT | `KML_FFT_REDFT00`、`KML_FFT_REDFT01`、`KML_FFT_REDFT10`、`KML_FFT_REDFT11` |
| DST | `KML_FFT_RODFT00`、`KML_FFT_RODFT01`、`KML_FFT_RODFT10`、`KML_FFT_RODFT11` |

**一维长度约束**（不是「n ≥ 1 就合法」，建 plan 会返回空）：

| kind | 一维 n 的要求 |
|---|---|
| `REDFT11` / `RODFT11`（DCT-IV / DST-IV） | **4 的倍数**（n=6 建 plan 失败） |
| `REDFT01` / `REDFT10` / `RODFT01` / `RODFT10` | 偶数 |
| `REDFT00`（DCT-I） | n ≥ 2（logical N = 2(n−1)） |
| `RODFT00`、`R2HC`、`HC2R`、`DHT` | 文档未单列限制（n ≥ 1） |

- out 是**实数**数组，元素数 = 各维乘积（与 in 同尺寸）；`n/2+1` 那套半复数规则只适用于 R2C 的复数 out，套到 R2R 会越界写。

- 没有默认 kind：函数说明未定义缺省值；漏填/填错不会报错，只会算出别的变换（按接口无校验语义外推，待实测）。
- **矩阵转置技巧**：用 guru 类 r2r 接口的特殊用法可以直接做矩阵转置（文档单列一节），
  但具体参数组合本文档只给描述未给可复制代码，落地前须按现场文档核对。

## 2D 上采样

`kml_fft_plan_upsampling_2d` 提供「单个连续数据序列 2 维上采样变换」的 plan，
与 pad/半复数布局相关；本技能未展开其参数，需要时直接查函数页。

## 选型速查

| 输入 / 输出 | 用哪个 | 关键差别 |
|---|---|---|
| 复数 → 复数 | `plan_dft` | 有 sign，rank ≤ 4 |
| 实数 → 复数（频谱） | `plan_dft_r2c` | 无 sign，out 半复数 |
| 复数 → 实数（重建） | `plan_dft_c2r` | 无 sign，n 按实数侧长度 |
| 实数 → 实数（DCT/DST/DHT） | `plan_r2r` | kind 逐维指定，rank ≤ 3 |
| 多组同形状、带步长 | `plan_many_*` | 04 章 |
| 任意跨步布局 | `plan_guru_*` | 04 章 |
| 分布式多进程 | `mpi_plan_*` | 06 章，rank ≤ 3 |
