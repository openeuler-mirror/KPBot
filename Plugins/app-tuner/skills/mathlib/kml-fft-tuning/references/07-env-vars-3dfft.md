# 3D FFT 扩展接口的环境变量

这些环境变量只作用于**扩展接口计算 3D FFT** 的路径（对应 `_mpi_plan_*_ext` / `_plan_create*` 系列），
普通 `mpi_plan_dft` 不受它们影响。设置方式按文档示例是 mpirun 透传：`-x NAME=VALUE`。

## 进程与分解

| 变量 | 作用 | 取值 |
|---|---|---|
| `SCALFFT_PROC_GRID` | 三维进程排布 | 形如 `1,3,5`（15 进程 = 1×3×5） |
| `SCALFFT_DECOMPOSE_TYPE` | 三维矩阵分解方式 | `0`=Slab，`1`=Pencil，`2`=Brick（**暂不支持**） |
| `SCALFFT_TILE_COUNT` | 多线程计算 3D FFT 时矩阵的最大切分数 | 正整数 |

## 转置算法（三段可分别设）

`SCALFFT_PRE_A2A_ALGO` / `SCALFFT_MID_A2A_ALGO` / `SCALFFT_POST_A2A_ALGO`
—— 分别对应 3D FFT 的 pre / mid / post 阶段。MID/POST 取值与 PRE 相同：

| 值 | 算法 | 备注 |
|---|---|---|
| 0 | `AUTO_TUNING` | 库按进程数与矩阵大小自选 |
| 1 | `COLL_TASK_ALLTOALLV` | MPI A2A 的 task 化转置 |
| 2 | `P2P_TASK_LINEAR` | linear A2A 的 task 化 |
| 3 | `P2P_TASK_PAIRWISE` | pairwise A2A 的 task 化 |
| 4 | `P2P_PIPELINE_LINEAR` | pipeline 转置 |
| 5 | `P2P_LAMMPS` | Lammps 原生集合通信转置 |
| 6 | `P2P_PAIRWISE_WO_PACK` | 无 pack，**仅 slab 分解** |
| 7 | `P2P_LINEAR_WO_PACK` | 无 pack，**仅 slab 分解** |
| 8 | `P2P_PIPELINE_INTRA_NODE` | 单节点内 pack/unpack 的 pipeline |

## 使用口径

- **先 0 后手动**：默认 `AUTO_TUNING` 已按进程数与矩阵大小选，只有在明确知道瓶颈（某阶段转置占比高）时才逐段指定。
- 6/7 只在 slab 分解下合法；用 pencil 分解又设 6/7 = 配置冲突。
- `SCALFFT_PROC_GRID` 必须与 `mpirun -np` 的进程数乘积一致，否则进程排布与实际不符（典型表现：结果错、或部分进程空转）。
- 这些变量的收益是**机器/规模相关**的，文档未给量化对比，必须现场扫参数；扫的时候一次只动一个变量。

## 版本确认

`KFFTGetVersion` 取 FFT 产品版本信息——报障、对比不同 HPCKit 版本行为前先记录版本号。
