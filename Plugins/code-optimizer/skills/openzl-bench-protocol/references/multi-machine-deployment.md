# OpenZL 多机部署复测协议

> 目标：把基准口径（数据、代码、构建、绑核）复制到多台异构机器，产出可横向对比的吞吐表。以下以"ARM 基准机 + x86 复测机"为典型场景；机器地址、账号、凭据使用本地 ssh config 或环境变量，不写入仓库。

## 1. 拓扑探测与统一绑核

不同机器 SMT 兄弟线程编号规律不同（实测两种：相邻编号 `cpu0-1, cpu4-5…`；间隔编号 `0,192 / 4,196…`），必须逐机探测：

```sh
lscpu | grep -E "Architecture|CPU\(s\)|Model name|Thread|Core|Socket|NUMA"
cat /sys/devices/system/cpu/cpu0/topology/thread_siblings_list
```

统一口径：取各机**物理核数的最小值 N**，每机绑定 N 个"不同物理核的线程 0"：

```sh
# 相邻编号机器（偶数逻辑核 = 物理核线程0）
CPUS=$(seq -s, 0 2 $((2*N-2)))
# 间隔编号机器（前 N 个逻辑核 = 物理核线程0）
CPUS="0-$((N-1))"

# 验证绑核数量（应输出 N）
taskset -c "$CPUS" bash -c 'python3 -c "import os; print(len(os.sched_getaffinity(0)))"'

# 绑定运行
taskset -c "$CPUS" ./unitBench -i=2 <scenario> <file>
```

注意：unitBench 场景本身单线程，taskset 只统一口径（消除 SMT 争抢与频率干扰），不提供并行度；绑核与非绑核的单线程结果应几乎一致（<1%），可作口径自检。

## 2. 代码分发（rsync）

```sh
rsync -az -e "ssh" \
    --exclude='build-bench/' --exclude='obj/' --exclude='cachedObjs/' \
    --exclude='.git/' --exclude='.gitmodules' \
    --exclude='*.o' --exclude='*.a' --exclude='*.so*' \
    --exclude='/unitBench' \
    <本地 openzl>/ <user>@<host>:<远程>/openzl/
```

两个必踩坑：

1. **排除根级可执行文件用 `--exclude='/unitBench'`（带前导斜杠）**。写成 `--exclude='unitBench'` 会把 `benchmark/unitBench/` 源码目录一并排除，远程构建报 `undefined reference to main`。
2. **必须排除预编译依赖产物（`*.a`/`*.so*`）**。本地构建在 `deps/*/lib/` 残留本架构库，传到异构架构机器直接链接失败（`file in wrong format`）。排除后远程 `make` 从源码重编依赖。已误传时清理：

```sh
rm -rf deps/zstd/lib/*.a deps/zstd/lib/*.o deps/lz4/lib/*.a deps/lz4/lib/*.o
```

替代方式：`tar` 打包（同样排除编译产物）+ 密文通道传输，适合受限环境。

## 3. 远程环境与构建验证

```sh
# 传输前确认工具链
ssh <user>@<host> 'which rsync gcc g++ cmake make python3 curl'

# 传输后验证关键文件在位
ssh <user>@<host> 'cd <远程>/openzl && \
  ls benchmark/unitBench/benchList.h examples/getting_started/sample_inputs/era5_ints.bin'

# 构建与冒烟
ssh <user>@<host> 'cd <远程>/openzl && make unitBench -j$(nproc) && \
  ./unitBench --list | grep <scenario>'
```

注意：ssh 非交互会话工作目录是 `$HOME`，相对路径须配合 `cd`。

## 4. 数据分发

真实 KV 数据（数百 MB 级）单独传输；文件名与 `prepare_kvdata.py` 产出约定对齐，各机放置于统一目录（`KVDATA_DIR`）：

```sh
rsync -az -e "ssh" <kvdata>/*.bin <user>@<host>:<远程>/kvdata/
```

压缩率是跨机一致性自检：同一数据同一代码，各机压缩率必须完全一致；不一致说明存在非确定路径（如未初始化内存、时间戳），先修实现再谈性能。

## 5. 执行与结果回收

```sh
# 远程按同一口径执行（示例）
ssh <user>@<host> 'cd <远程>/openzl && taskset -c 0-191 ./unitBench -i=2 <scenario> <kvdata>/<file>'

# 回收
scp <user>@<host>:<远程>/result.txt .
```

长跑稳定性：全量 benchmark 单次跑可能因宿主机硬件错误/IO 异常静默退出——按名称分段 filter 执行、每段失败自动重试、按 benchmark 名合并去重：

```sh
F='MCR .*Float|MCR .*Varint'
taskset -c "$CPUS" ./build-bench/benchmark/openzl_benchmark \
    --benchmark_filter="$F" --benchmark_min_time=0.2 > seg_A.txt 2>/dev/null
# 合并去重后与各机对比
```

## 6. 横向对比表做法

- 表维度：场景 × 指标（吞吐/压缩率）× 机器；
- 压缩率一行呈现（应全机一致），吞吐按机器分列；
- 同批优化做 A/B 时，控制变量：同源码基线、同工具链与构建 flags、同数据与绑核，仅改目标机制；单机先做反序复测，跨机结论注明每机是独立构建；
- 共享负载机器的个别异常慢用例标注"待低负载复测"，不直接进结论。
