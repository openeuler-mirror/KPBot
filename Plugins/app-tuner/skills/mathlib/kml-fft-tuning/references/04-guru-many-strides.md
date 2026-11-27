# guru / guru64 / split / many：跨步与批处理

连续单序列用 `plan_dft` 就够；一旦数据有跨步、要一次算多组、或要实虚分离存储，
就必须换到这一族。它们的语义与 FFTW 同名接口一致，参数名也基本对齐。

## guru：任意跨步的单组/多组

```c
kml_fft_plan kml_fft_plan_guru_dft(int rank, const kml_fft_iodim *dims,
                                   int howmany_rank, const kml_fft_iodim *howmany_dims,
                                   kml_fft_complex *in, kml_fft_complex *out,
                                   int sign, unsigned flags);
```

| 参数 | 约束 | 语义 |
|---|---|---|
| `rank` | 1 ≤ rank ≤ 4 | 单个 FFT 序列的维数 |
| `dims` | 长度 rank | `dims[i] = {n, is, os}`：第 i 维长度、输入相邻元素间隔、输出相邻元素间隔 |
| `howmany_rank` | **0 ≤ howmany_rank ≤ 4** | 多组 FFT 起始地址的访问模式维数；0 表示只有一组 |
| `howmany_dims` | 长度 howmany_rank | `{n, is, os}`：该维有几组、相邻组输入间隔、相邻组输出间隔 |

- `dims[i].n ≥ 1`。
- **is/os 单位是元素**（复数元素），不是字节。
- 单个 FFT 的序列不要求连续——这正是 guru 存在的理由（矩阵列变换、结构体内嵌数组、通道交织）。

## guru_split：实虚分离存储

`kml_fft_plan_guru_split_dft` 把实部与虚部分别放在两个连续数组里（split 布局），
与 `execute_split_dft` 配对。适用场景：上游库输出就是 split 复数（大量信号处理/求解器代码），
用普通 `plan_dft` 需要先交错成 `kml_fft_complex`，多一次搬运。

## guru64：64 位维度

`kml_fft_plan_guru64_dft` 与 guru 结构相同，但维度/步长用 64 位整型表达。
单维长度或元素总数可能超过 int 范围时（超大网格、长序列单机变换）用它，否则用 guru。

## many：固定形状的批处理

```c
kml_fft_plan kml_fft_plan_many_dft(int rank, const int *n, int howmany,
                                   kml_fft_complex *in,  const int *inembed,  int istride, int idist,
                                   kml_fft_complex *out, const int *onembed, int ostride, int odist,
                                   int sign, unsigned flags);
```

| 参数 | 语义 |
|---|---|
| `howmany` | 一次算多少组 |
| `inembed` / `onembed` | 输入 / 输出数组的逻辑尺寸（每维能放多少元素） |
| `istride` / `ostride` | 单组内部相邻元素的间隔 |
| `idist` / `odist` | 相邻两组之间的起始间隔 |

选型口径：

- 同形状、等间距的批量（多帧、多通道、多批次）→ `plan_many_*`，比循环调用 `plan_dft_1d` 少 N-1 次规划。
- 布局不规则（各维跨步不同、组间跨步不均匀）→ `plan_guru_*`。
- 既要批量又要 64 位 → guru64 族（`plan_many_*` 无 64 位变体）。

## 易错点

- `embed` 必须 ≥ 对应维长度；写 `NULL` 只有在「紧凑连续」时成立，跨步布局下传 NULL 会算错而不是报错。
- `istride`/`idist` 混用：单组内用 stride，组间用 dist，写反了结果只是「顺序不对」不会崩，排查很难。
- guru 的 `howmany_rank = 0` 时 `howmany_dims` 可以为 NULL；非 0 时必须给数组。
