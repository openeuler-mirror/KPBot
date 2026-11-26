# 从 FFTW 迁到 KML_FFT

## 好消息：接口是同名同形的

KML_FFT 的函数名、参数顺序与 FFTW 基本一一对应（只换前缀），所以迁移主要是「换头文件 + 换前缀 + 删掉不存在的 option」。

| 用途 | FFTW | KML_FFT |
|---|---|---|
| 1 维 C2C plan | `fftw_plan_dft_1d` | `kml_fft_plan_dft_1d` |
| n 维 C2C plan | `fftw_plan_dft` | `kml_fft_plan_dft` |
| 跨步/多组 | `fftw_plan_guru_dft` | `kml_fft_plan_guru_dft` |
| split 复数 | `fftw_plan_guru_split_dft` | `kml_fft_plan_guru_split_dft` |
| 批量 | `fftw_plan_many_dft` | `kml_fft_plan_many_dft` |
| R2C / C2R | `fftw_plan_dft_r2c` / `_c2r` | `kml_fft_plan_dft_r2c` / `_c2r` |
| R2R | `fftw_plan_r2r` | `kml_fft_plan_r2r` |
| 执行 | `fftw_execute` / `fftw_execute_dft` | `kml_fft_execute` / `kml_fft_execute_dft` |
| 内存 | `fftw_malloc` / `fftw_free` | `kml_fft_malloc` / `kml_fft_free` |
| 销毁 | `fftw_destroy_plan` | `kml_fft_destroy_plan` |
| 线程 | `fftw_init_threads` / `fftw_plan_with_nthreads` / `fftw_cleanup_threads` | 同名去 `fftw` 前缀 |
| MPI | `fftw_mpi_plan_dft` / `fftw_mpi_local_size` | `kml_fft_mpi_plan_dft` / `kml_fft_mpi_local_size` |

## 必须改的五处（真实差异）

1. **flags 只有两档**：`FFTW_ESTIMATE`/`FFTW_PATIENT` 可映射；`FFTW_MEASURE`、`FFTW_WISDOM_ONLY`、
   `FFTW_UNALIGNED`、`FFTW_PRESERVE_INPUT` **没有对应值**，必须删掉或改写逻辑。
2. **本版没有 wisdom 类接口**：函数说明里没有 plan 导出/导入（也没有 `wisdom` 字样），所以跨进程/跨运行
   复用不可用。替代方案：同形状 plan 建一次、`execute_dft` 换数据指针，把复用限制在进程内；
   若后续版本补了序列化接口，按新文档重核本条。
3. **线程数在建 plan 时快照**：FFTW 侧「先建 plan 再开线程」的写法在 KML 侧静默失效（05 章）。
4. **rank 上限不同**：FFTW 的 C2C 可 4 维以上；KML C2C 只到 4 维、R2R 与 MPI 只到 3 维。
   超过上限的代码要改成分块或降维调用。
5. **R2R kind 的符号系统**：语义对应（R2HC/HC2R/DHT/REDFT00..11/RODFT00..11），但宏名不同，需要逐个替换。

## 迁移步骤

1. 先只换库与前缀，保留参数（这一步应当零逻辑改动）；能编译即说明接口面对齐。
2. 处理 flags：把 MEASURE 降级为 PATIENT 或 ESTIMATE，删掉 WISDOM_ONLY/UNALIGNED/PRESERVE_INPUT 分支。
3. 处理 wisdom：若有 wisdom 文件读写，改为进程内 plan 表，启动时预热。**缓存键不能只有形状**——至少
   `(变换类型, sign 或 kind 数组, rank + 各维长度, in/out 布局, 线程数)`：1024 点正变换与反变换形状完全相同、
   方向相反，只按形状查表会复用反向 plan，长度自查也发现不了（与 02 章的 plan 复用同一口径）。
4. 复核线程顺序：`init_threads` → `plan_with_nthreads` → 建 plan。
5. 复核维度上限与 R2R kind 映射。
6. 数值验收：与 FFTW 结果按精度阈值比对（08 章口径），确认归一化一致。

## 概念对照（写给读 FFTW 文档的人）

- **规划成本**：FFTW 侧「plan 很贵」的结论在 KML 同样成立，只是可选的严格度少一档（无 MEASURE）。
- **对齐**：FFTW 用 `FFTW_UNALIGNED` 显式放弃对齐优化；KML 没有该开关。`kml_fft_malloc` 的实际对齐粒度
  函数说明未写明（**待核实**：查实现或实测指针模 16/32/64），所以「分配器已对齐」不能当结论用。
- **线程**：FFTW 的 plan 也记线程数，但可以在建 plan 前后反复调 `plan_with_nthreads` 改变「后续」plan；
  KML 同样只影响后续 plan，且同样不可重入。
- **MPI**：FFTW 的 `*_transposed` 局部尺寸系列在 KML 有一一对应（06 章），迁移时按同一套布局逻辑改写即可。
