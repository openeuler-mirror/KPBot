# 数学库域（BoostKit 数学库的使用与调优）

鲲鹏数学库 KML（HPCKit / BoostKit 数学库）各个子库的**接口用法、配置与调优判定**：
FFT、BLAS、SPARSE、SVML 等。只回答「这个库怎么用、参数怎么定、为什么慢」，
不判断跨架构结果一致性。

## 边界判据

- **属于本域**：结论只涉及某个 KML 子库自身的接口语义、参数约束、线程/MPI 配置与性能口径
  （例：`plan_with_nthreads` 必须在建 plan 之前调、R2C 的 out 按半复数尺寸开）。
- **不属于本域**：
  - 跨架构（x86 ↔ ARM）浮点结果是否一致、误差分层与验收 → **precision 域**（独立领域，负责一致性判定）。
  - 只在昇腾 NPU 侧成立的算子/CANN 数值话题 → 昇腾采集/算子侧。
  - 应用层热点定位与代码改写 → `code-optimizer` 各阶段技能。
  - 第三方库（FFTW / OpenBLAS / cuFFT）自身的选型与调优：不入本域，本域只写「迁移到 KML 要改什么」。

## 成员

| Skill | 职责 |
|---|---|
| [`kml-fft-tuning`](kml-fft-tuning/SKILL.md) | KML_FFT 的使用与调优：plan 建立/destroy 与 rank、n 约束，flags 只有 ESTIMATE/PATIENT 两档（无 MEASURE、本版无 wisdom 序列化），线程数在建 plan 时快照（init_threads → plan_with_nthreads → 建 plan → cleanup_threads），guru/guru_split/guru64/many 的 dims·stride·embed 语义，MPI local_size 家族与 SCRAMBLED/TRANSPOSED 标志，分布式转置，3D 扩展接口的 SCALFFT_* 环境变量，以及 FFTW→KML 迁移的五处必改 |

## 域内路由

```
KML_FFT 的 plan/flags/约束怎么用        → kml-fft-tuning（references/02）
线程数设了不生效 / 多线程               → kml-fft-tuning（references/05）
MPI 下本地缓冲、转置、scrambled         → kml-fft-tuning（references/06）
3D FFT 不满速，要调进程排布/转置算法    → kml-fft-tuning（references/07 + 08）
FFTW 代码要迁到 KML                     → kml-fft-tuning（references/09）
两边结果对不上、要判精度                → 不属本域：交 precision 域（跨架构一致性判定）
```

## 后续迁入 / 待议

- `KML_BLAS` / `KML_SPARSE` / `KML_SVML` 的使用与调优待沉淀，按同一体例（接口表 + 硬约束 + 判定表）并入本域。
- 数学库侧的「同一个库不同版本性能/精度行为」对比暂不入库：需要实测数据支撑，先留在 experiment 记录里。
