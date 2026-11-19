# 压测方法与 fio_bench.sh 用法

> **何时读我**：要产生"可以写进台账的带宽数字"时读本文 —— 标准压测命令、口径纪律（哪些不一致会让结论作废）、`scripts/fio_bench.sh` 的三维度用法与 env 旋钮、输出解读、USRBIO 与 FUSE 路径怎么选。

节点角色与示例 IP 见 `SKILL.md`。命令中的占位符：`${FIO_BIN}`（默认 `/usr/local/bin/fio`）、`${USRBIO_SO}`（默认 `/usr/lib64/hf3fs_usrbio.so`）、`${MOUNT_POINT}`（示例 `/3fs/stage`）、`${FIO_DIR}`（压测目录，**换目录即换 chain table**，见 `topology-and-placement.md` §三）。

---

## 一、标准压测命令

### 1.1 顺序写（32 job，USRBIO 引擎，绕过 FUSE 内核）

```bash
${FIO_BIN} --name=seq-write-smoke --directory=${FIO_DIR} \
  --size=2g --bs=4M --rw=write \
  --ioengine=external:${USRBIO_SO} --mountpoint=${MOUNT_POINT} \
  --ior_depth=0 --iodepth_batch_submit=8 \
  --iodepth_batch_complete_min=8 --iodepth_batch_complete_max=8 \
  --iodepth=8 --numjobs=32 --direct=1 --fallocate=none --group_reporting \
  --filename_format='seq-write.$jobnum.0'
```

32×2g = 64 GiB。参考结果：基线软件查表 `bw=1462MiB/s (1533MB/s), io=64.0GiB, run=44825ms`；folly 硬件路径客户端 `bw=10.7GiB/s (11.5GB/s), run=6001ms`。

> 该 10.7 GiB/s 是 `enableChecksum=true` 下测的，说明客户端写路径 CRC 确实是 1462 MiB/s 那个天花板的成因。但当时用的是 ISA-L asm 路径，**后续判定其在鲲鹏 920 上对 >0x400 字节块段错误并已废弃**，现改为标量 `crc32cw`/`crc32cb` 路径（单核 5.19 GB/s vs 软件查表 0.32 GB/s）。复现时不要照搬旧二进制。代码侧细节归 skill `3fs-hotspot-code-optimization`（code-optimizer 插件）。

### 1.2 顺序读（32 job）

```bash
${FIO_BIN} --name=seq-read --directory=${FIO_DIR} \
  --size=128m --bs=4M --rw=read \
  --ioengine=external:${USRBIO_SO} --mountpoint=${MOUNT_POINT} \
  --ior_depth=0 --iodepth_batch_submit=8 \
  --iodepth_batch_complete_min=8 --iodepth_batch_complete_max=8 \
  --iodepth=8 --numjobs=32 --direct=1 --fallocate=none --group_reporting \
  --time_based --runtime=20 --filename_format='seq-read.$jobnum.0'
```

需先用相同 flags 以 `--rw=write` 预写入，**尺寸必须与读测试 `--size` 一致**。参考结果：`READ: bw=11.2GiB/s (12.0GB/s), io=224GiB, run=20057msec`。

### 1.3 单 job 顺序写 + ClickHouse 联动

```bash
taskset -c 12 ${FIO_BIN} --name=seq-write-smoke --directory=${FIO_DIR} \
  --size=4g --bs=4M --rw=write \
  --ioengine=external:${USRBIO_SO} --mountpoint=${MOUNT_POINT} \
  --numjobs=1 --direct=1 --group_reporting --filename_format='seq-write.$jobnum.0'
```

单 job 用于**延迟受限**区间的归因（并发区间的天花板是网卡，测不出 CPU 侧差异）。参考：基线 604~612 MiB/s / IOPS 151。绑核是为了让 CPU 侧对比可重复；`scripts/fio_bench.sh` 用 `TASKSET_CPU=12` 暴露。

---

## 二、口径纪律（不一致就让结论作废）

- `--size` 必须两腿一致（512m 瞬态 vs 2g 稳态差 23%）
- `--filename_format` 用**单引号**；被 shell 展开时 32 个 job 会写同一个文件，产出 556 MiB/s / io=4096MiB 的假结果
- 带宽数字取自**不带 profiling** 的跑次（三路并发 DWARF 采集能把 fio 从 1462 拖到 556 MiB/s）
- 以 fio **汇总行**（`WRITE: bw=…` / `READ: bw=…`）为准，不看 per-job `bw (…): avg=…` 行（USRBIO 下那是 artifact）
- 多客户端测试用 SSH 并行执行，**不要用 fio client-server 模式**（3FS 实测连接不稳定）
- 记录 fio 版本，两腿必须一致（3.42 与 3.34 的 fallocate/laying-out 行为不同，会污染"新文件 vs 覆盖写"对比）
- 读测试的预写入尺寸必须与读测试 `--size` 一致
- **单位换算**：fio 汇总行可能是 GiB/s、MiB/s 或 KiB/s，多节点相加前必须统一（1 GiB/s = 1024 MiB/s）；`scripts/fio_bench.sh` 内部统一折算成 MiB/s 再汇总，同时打印 GiB/s
- **多次重复**：平台期内的差异（如 1188/1190/1168/1185/1198 MiB/s）全在噪声带内，单次跑不足以判定收益；至少 3 次取中位，并记录离散度
- **预热**：首轮跑含建链、chunk 分配、XFS 分配开销；对比时两腿都丢弃首轮或都保留首轮，不要一腿预热一腿冷启
- **绑核**：CPU 侧归因用 `taskset`；带宽类对比两腿绑核策略一致
- 同时记录 **ConfigVersion** 与二进制 **md5**（见 `config-mechanics.md`、`lever-ranking.md` §2.5 ②）
- **压缩率 / 带宽 / CPU-秒 分开记录**，不要用一个数字代表"性能变好了"

---

## 三、`scripts/fio_bench.sh` 完整用法

一个脚本覆盖三个维度，替代 6 个分散脚本（单节点 USRBIO / 单节点 FUSE / 单节点 AI 负载 USRBIO / 单节点 AI 负载 FUSE / 多节点 USRBIO / 多节点 FUSE）。

```bash
bash scripts/fio_bench.sh [TEST|all|cleanup]
```

### 3.1 三个维度

| 维度 | 取值 | 默认 | 含义 |
|---|---|---|---|
| `ENGINE` | `usrbio` \| `fuse` | `usrbio` | `usrbio`：`--ioengine=external:${USRBIO_SO} --mountpoint=${MOUNT_POINT} --ior_depth= --iodepth_batch_submit= --iodepth_batch_complete_min/max=`，绕过 FUSE 内核；`fuse`：`--ioengine=libaio` 经 FUSE 内核，写测试自动加 `--create_on_open=1` |
| `MODE` | `single` \| `multi` | `single` | `single`：本节点执行；`multi`：按 `NODES`/`NODES_FILE` 清单 SSH 并行执行并汇总集群总带宽 |
| `WORKLOAD` | `micro` \| `ai` \| `mixed` | `micro` | 微基准 / AI 训练负载模拟 / 读写混合并行 |

### 3.2 TEST 清单与参数

`WORKLOAD=micro`（`iodepth=${IODEPTH}`、批次参数取 `IODEPTH_BATCH_*`、`ior_depth=${IOR_DEPTH}`）：

| TEST | rw | bs | 尺寸 | numjobs | time_based | filename_format | 备注 |
|---|---|---|---|---|---|---|---|
| `seq-write` | write | `${BS}`(4M) | `size=${WRITE_SIZE}`(512m) | `${NUMJOBS}`(32) | 否 | `seq-write.$jobnum.0` | 主力基线 |
| `seq-read` | read | `${BS}` | `size=${FILE_SIZE}`(128m) | 32 | 是，20s | `seq-read.$jobnum.0` | 自动预写入（已存在且尺寸够则跳过） |
| `rand-write` | randwrite | 4k | `size=${FILE_SIZE}` | 32 | 是，30s | `rand-write-4k.$jobnum.0` | |
| `rand-read` | randread | 4k | `size=${FILE_SIZE}` | 32 | 是，20s | `seq-read.$jobnum.0` | 复用 seq-read 预写入数据 |
| `seq-write-multi` | write | `${BS}` | `size=${WRITE_SIZE}` | 32 | 否 | `seq-write-multi.$jobnum.0` | 与 seq-write 同参不同文件，用于连续两轮写对比 |

`WORKLOAD=ai`（`iodepth=${AI_IODEPTH}`：usrbio 默认 128、fuse 默认 `${IODEPTH}`；批次参数 = iodepth；`ior_depth=0`；`time_based` + `runtime=${RUNTIME:-60}`）：

| TEST | rw | bs | 文件形状 | numjobs | openfiles | filename_format | 预填充 |
|---|---|---|---|---|---|---|---|
| `dataloader` | randread | 4k | `filesize=4k-256k`, `nrfiles=${DL_NRFILES}`(500) | `${DL_NUMJOBS}`(16) | 128 | `dl_sample.$jobnum.$filenum` | 是（同形状写入） |
| `parquet-read` | read | 4M | `filesize=16m-256m`, `nrfiles=${PARQUET_NRFILES}`(50) | `${PARQUET_NUMJOBS}`(8) | 16 | `pq_data.$jobnum.$filenum` | 是 |
| `checkpoint-write` | write | 16M | `size=${CKPT_SIZE}`(4G), `nrfiles=${CKPT_NRFILES}`(4) | `${CKPT_NUMJOBS}`(4) | 4 | `ckpt.$jobnum.$filenum` | 测前删旧文件 |
| `checkpoint-multi-write` | write | 16M | `filesize=${CKPT_MULTI_SIZE}`(512m), `nrfiles=${CKPT_MULTI_NRFILES}`(50) | 4 | 4 | `ckpt_multi.$jobnum.$filenum` | 测前删旧文件 |
| `data-prep-write` | write | 64k | `filesize=4k-1M`, `nrfiles=${PREP_NRFILES}`(200) | `${PREP_NUMJOBS}`(16) | 64 | `prep.$jobnum.$filenum` | 测前删旧文件 |

多文件参数语义：

- `--nrfiles=N`：每个 job 的工作集拆成 N 个文件
- `--filesize=4k-256k`：**每个文件**的大小在该区间内均匀随机（模拟海量样本文件尺寸不一）；与 `--size`（固定尺寸）互斥使用
- `--openfiles=N`：同时打开的文件数上限（模拟 DataLoader 的 fd 压力，会受 FUSE 侧 `iov_limit`/`max_background` 影响）
- `--filename_format` 在多文件场景必须含 `$filenum`，否则 N 个文件互相覆盖

`WORKLOAD=mixed`：

| MODE | 行为 |
|---|---|
| `single` | 三路 fio 并行：DataLoader 随机读 + Parquet 顺序读 + Checkpoint 顺序写，各自 `--output-format=json`，用 python3 解析出每路的 BW/IOPS 与 slat/clat 的 avg、P99 |
| `multi` | 各节点跑 `randrw`（`rwmixread=${RWMIXREAD}`，默认 70）读写混合，读的是预写入数据；汇总时分别累加读带宽与写带宽并给出合计 |

### 3.3 env 旋钮

| 组 | 变量（默认值） |
|---|---|
| 路径 | `FIO_BIN`(/usr/local/bin/fio) `USRBIO_SO`(/usr/lib64/hf3fs_usrbio.so) `MOUNT_POINT`(/3fs/stage) `FIO_DIR`(${MOUNT_POINT}/fio_bench) `AI_BASE_DIR`(${MOUNT_POINT}/fio_workload) `OUTPUT_DIR`(空=临时目录，用后删除) |
| micro | `NUMJOBS`(32) `BS`(4M) `IODEPTH`(8) `IOR_DEPTH`(0) `IODEPTH_BATCH_SUBMIT`(8) `IODEPTH_BATCH_COMPLETE_MIN`(8) `IODEPTH_BATCH_COMPLETE_MAX`(8) `WRITE_SIZE`(512m) `FILE_SIZE`(128m) `RWMIXREAD`(70) `TASKSET_CPU`(空=不绑核) `CLEAN_DIR`(0) |
| ai | `RUNTIME`(空=读 20s/随机写 30s/ai 60s) `AI_IODEPTH`(空=usrbio:128, fuse:${IODEPTH}) `PREFILL_IODEPTH`(32) `DL_NUMJOBS`(16) `DL_NRFILES`(500) `DL_OPENFILES`(128) `PARQUET_NUMJOBS`(8) `PARQUET_NRFILES`(50) `PARQUET_OPENFILES`(16) `CKPT_NUMJOBS`(4) `CKPT_NRFILES`(4) `CKPT_OPENFILES`(4) `CKPT_SIZE`(4G) `CKPT_MULTI_NRFILES`(50) `CKPT_MULTI_SIZE`(512m) `PREP_NUMJOBS`(16) `PREP_NRFILES`(200) `PREP_OPENFILES`(64) |
| multi | `NODES`("host1 host2 …") 或 `NODES_FILE`(inventory，每行一个 host，`#` 注释) `SSH_USER`(root) `LOCAL_NODE`(空；等于该值的节点本地执行不 SSH) `REMOTE_JOB_DIR`(/tmp/fio_bench_jobs) `SSH_OPTS` `SSHPASS`（只能 `read -rs SSHPASS && export SSHPASS` 注入，脚本用 `sshpass -e`，**绝无明文密码**） |
| 监控 | `RDMA_MON_LIB`(空=跳过 RDMA 监控) `REMOTE_NODES` `RDMA_MON_DEV` `RDMA_MON_PORT`（透传给 `rdma_monitor.sh`，见 `observability.md` §6.2） |

`IOR_DEPTH` 语义（USRBIO 引擎专有）：`0` = 立即处理；`>0` = 严格攒批到该深度；`<0` = 最多攒批 + 超时。

### 3.4 multi 模式做了什么

1. 装载节点清单（`NODES` 或 `NODES_FILE`），校验每个节点的 SSH 可达性、`fio`、USRBIO 引擎 so、挂载点，并建好 `${REMOTE_JOB_DIR}`
2. 按当前 TEST 与节点后缀**动态生成每节点 job 文件**（`filename_format` 里插入 `.node<后缀>`，保证共享文件系统上各节点文件名不冲突）
3. `scp` 分发 job 文件到各节点 `${REMOTE_JOB_DIR}`
4. 并行启动（本地节点直接起进程，远程走 `ssh`），`wait` 全部结束
5. 逐节点解析 fio 汇总行 → 统一折算 MiB/s → 打印每节点带宽 + **集群总带宽**（MiB/s 与 GiB/s）
6. 读测试前先在所有节点并行预写入（尺寸与读测试 `--size` 一致）
7. `trap … EXIT`：退出时 `pkill` 远程残留 fio、清理远程 job 目录与本地临时目录

认证：优先 SSH key；需要密码时 `read -rs SSHPASS && export SSHPASS`（脚本内不出现任何密码字面量）。

### 3.5 输出解读

```
[PASS]  seq-write-smoke: BW=1428MiB/s, IOPS=…      ← 单节点：取 fio 汇总行
  READ: bw=11.2GiB/s (12.0GB/s), io=224GiB, run=20057msec
[PASS]    198.51.100.21: 写 4077MiB/s              ← 多节点：每节点一行
  >>> 集群总写带宽: 12767 MiB/s (12.47 GiB/s)       ← 统一折算后相加
  测试结果汇总   通过: 3    失败: 0                  ← 任一节点失败则退出码 1
```

- 带宽只认汇总行/脚本打印的 `BW=`，不要手抄 per-job `avg=` 行
- 集群总带宽是**各节点汇总行折算后相加**，不是任何单节点数字
- 失败节点会打印其 fio 输出末 5~10 行，先据此定位（常见：挂载点残留、引擎 so 缺失、`Bad file descriptor` 说明并发窗口超限）
- 退出码：有 FAIL 则 1，可直接用于脚本化 A/B

### 3.6 常用组合

```bash
# 基线（改参前后各跑一次，同口径）
NUMJOBS=32 BS=4M IODEPTH=8 WRITE_SIZE=2g bash scripts/fio_bench.sh seq-write

# 单 job 延迟受限区间归因（配 ClickHouse 延迟拆分）
NUMJOBS=1 WRITE_SIZE=4g TASKSET_CPU=12 bash scripts/fio_bench.sh seq-write

# USRBIO vs FUSE 路径对照（同尺寸同并发）
ENGINE=usrbio WRITE_SIZE=2g bash scripts/fio_bench.sh seq-write
ENGINE=fuse   WRITE_SIZE=2g bash scripts/fio_bench.sh seq-write

# chain table / 副本数对照：只换目录，服务不重启
FIO_DIR=${MOUNT_POINT}/fio_2replica          bash scripts/fio_bench.sh seq-write
FIO_DIR=${MOUNT_POINT}/fio_2replica_balanced bash scripts/fio_bench.sh seq-write

# 多客户端集群总带宽（3 客户端）
read -rs SSHPASS; export SSHPASS
MODE=multi NODES="<fuse-client-1> <meta> <fuse-client-2>" \
  RDMA_MON_LIB=/path/to/rdma_monitor.sh REMOTE_NODES="<storage1> <storage2>" \
  bash scripts/fio_bench.sh seq-write

# AI 训练负载画像（读为主 + checkpoint 写）
WORKLOAD=ai RUNTIME=60 bash scripts/fio_bench.sh all
WORKLOAD=mixed bash scripts/fio_bench.sh mixed

# 清理测试数据
bash scripts/fio_bench.sh cleanup
```

---

## 四、USRBIO 与 FUSE 路径的差异与选型

| 维度 | USRBIO（`external:${USRBIO_SO}`） | FUSE（`libaio` 经内核） |
|---|---|---|
| 路径 | 用户态直连 3FS 客户端，绕过 FUSE 内核 | 应用 → 内核 VFS → FUSE → hf3fs_fuse_main → 3FS 客户端 |
| 专有参数 | `--mountpoint=`、`--ior_depth=`、`--iodepth_batch_submit=`、`--iodepth_batch_complete_min/max=` | 写测试需 `--create_on_open=1` |
| `iodepth` 语义 | **就是每 job 对 IOR ring 的硬窗口**：queue 满即返回 `FIO_Q_BUSY`，每 job 只有一个 batch in-flight | 只是进入内核/FUSE 的**入口窗口**，后面还有 daemon 的 `max_background`、`io_jobq_size`、worker/coro 队列与异步调度，能在 fio job 等待时继续供给 storage |
| 调参敏感度 | 对客户端 `traffic_control`、`num_bg_threads` 敏感；把 commit 改异步**无效（-4%）**，因为并发形态没变（见 `lever-ranking.md` §2.2） | 对 FUSE daemon 侧线程/队列参数更敏感 |
| 选型 | 测 **3FS 客户端与服务端**的能力上限、做存储侧参数 A/B | 测**应用真实可见**的性能（未改造成 USRBIO 的业务只能走这条路），以及 FUSE 侧参数 A/B |

**结论**：调 storage/客户端参数用 USRBIO 口径（噪声小、路径短）；评估业务收益或调 FUSE 侧参数时必须补一轮 FUSE 口径。两个口径的数字**不可直接互比**，台账里要分列记录。
