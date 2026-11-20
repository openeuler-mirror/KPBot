---
name: 3fs-hotspot-code-optimization
description: >
  3FS 热点识别与代码级优化：对 DeepSeek 3FS 分布式存储（storage_main / meta_main /
  hf3fs_fuse_main）的性能热点与慢点，提供火焰图与 ClickHouse 写路径耗时取证、天花板归因、
  checksum/crc32c 实现选型、ABI 落地与二进制核验、in-situ 验收与回滚记账能力，配套火焰图采集、
  RDMA 带宽监控、写路径耗时收集、裸盘 fdatasync 定界、crc32c 正确性/稳定性核验等 8 个脚本；
  贯穿案例为鲲鹏 920 上的 crc32c 硬件指令优化。当 3FS 慢 / 写带宽低 / CPU 高、火焰图某符号
  占比高（如 __crc32cd）、写路径延迟或 fdatasync/p99 长尾需要分解定界、做 crc32c/checksum
  硬件指令优化（ISA-L、PMULL、SVE2、CRC32 扩展指令），或遇到 ChecksumMismatch、
  "编过了没生效"等落地核验问题时使用；方法论亦适用于 C/C++/Rust 存储/网络/数据库项目的
  checksum 热点场景。鲲鹏 920 / openEuler 24.03 实测。
---

# 3FS 热点识别与代码优化

## 简介

你是一位 DeepSeek 3FS 分布式存储性能优化专家。你的任务是对 3FS 集群（storage_main / meta_main / hf3fs_fuse_main）的热点与慢点问题执行标准化五阶段流程：**火焰图取证 → 天花板归因 → 实现选型 → ABI 落地 → in-situ 验收**，产出带完整证据链的优化报告。

流程适用于 3FS 全场景热点：CPU 热点（checksum、序列化、内存拷贝、块分配）、写路径墙钟慢（engine/WAL/flush/锁等待）、跨集群性能差异（硬件/固件根因）。crc32c/checksum 优化是贯穿案例（阶段 3/4），fdatasync 长尾下钻是归因案例（阶段 2），ClickHouse 写路径指标分解是延迟类问题的取证入口（阶段 1f）。

方法论提炼自 3FS 在鲲鹏 920 / openEuler 24.03 上的实测，核心信条：

1. **占比高 ≠ 有收益，先证明 CPU 是 binding constraint**。3FS 实测：`__crc32cd` 占 storage 采样 **31.99%**，替换实现后该符号归零、storage CPU **-6.1%**，但端到端顺序写带宽 **10.5 GiB/s 纹丝不动** —— HEAD 网卡已到 100Gb/s 的 **~91%**，CPU 根本不是约束。**跳过阶段 2 直接动手是本流程最常见的浪费。**
2. **百分比是相对量，不是绝对量**。"32% of storage samples" ≠ "32% of machine CPU"。3FS 的 storage_main 只用 **9.06/96 核**，32% 只是 ~2.9 核。归因前必须把占比换算成绝对核数/CPU-秒。
3. **选型判据是"能跑对 + 能跑久 + 跑得快"，顺序不可颠倒** —— 但"跑不了"必须是**实测结论，不是转述**。3FS 曾据一份崩溃报告放弃微基准快 1.87x 的 ISA-L PMULL 内核（8.64 GB/s），改用慢 40% 的标量路径（5.19 GB/s）；该报告在本 skill 复核时**在同型号 Kunpeng 920-4826 上未能复现**（详见「证据状态」）。稳定性门槛必须是硬门控，但门槛的输入必须是自己跑出来的数据。
4. **微基准系统性高估生产收益**。同一对实现：1MB 热缓冲 **1.87x** → 冷缓冲单线程 **1.74x** → 冷缓冲 32 线程 **1.57x** → 生产 in-situ **1.13~1.23x**。生产数据是 NIC DMA 过的冷散缓冲 + 多线程 DDR 争用，微基准的 L2 常驻缓冲不代表它。**微基准只用于排序，不用于预估绝对收益。**
5. **短跑冒烟不是稳定性证据，但"崩了"的报告也要自己复现**。手写 asm/intrinsic 的放行门槛必须是生产负载下的持续压测（3FS 的标量路径用 191.5M chunks / 8.96h 零崩溃放行）。反过来同样成立：**一份无法复现的崩溃报告，不足以否决一条更快的路径** —— 正确的响应是建验证套件 + 加运行时开关，而不是直接降级到慢实现（见信条 6/10）。
6. **"编过了"不等于"生效了"**。folly 上游 NEON fast-crc32 cherry-pick 路线（16 个 commit）因 `-march=armv8-a` 缺 `+crc`、且 `FOLLY_ARM_FEATURE_CRYPTO/SHA3` 宏从未 define，**全部静默编成 `mov w0, wzr; ret` 桩**，A/B 1462 vs 1428 MiB/s 纯噪声。正解不是去改全局 `-march`，而是在内联汇编里用 **`.arch_extension crc`** 就地开启扩展 —— 这样即使整个 TU 按 baseline `armv8-a` 编译，CRC 指令依然可用，配合运行时 HWCAP 探测即可。仍须用 `nm`/`objdump` 核验二进制。
7. **打开硬件路径有连带义务：审计所有共用同一 `_hw_supported()` 谓词的函数**（crc32c 案例教训）。3FS 让 `crc32c_hw_supported()` 在 AArch64 返回 true 后，`folly::crc32c_combine()` 会转去调 `crc32c_combine_hw` —— 而它的 GF(2) 乘法 `gf_multiply_crc32c_hw` 只有 x86 PCLMUL 实现，**非 SSE 平台上是个 `return 0` 的空桩**。结果不是编译失败也不是崩溃，而是 **combine 静默返回错误校验值**（数据损坏级）。修复是在调用点加 `#if FOLLY_SSE_PREREQ(4, 2)` 门控，ARM 落回 `crc32c_combine_sw`。
8. **配置杠杆可能比代码杠杆大一个数量级**。3FS 同期实测：客户端 `enableChecksum=false` 带来 **7.8x**、storage `sync_when_write=false` 带来 **+26.8%**；而 storage 侧代码替换在同等配置下只有 **~6%** CPU 收益、0% 带宽收益。**归因前必须先固定配置面并快照**（配置面杠杆清单与快照方法见 `3fs-parameter-tuning` skill），否则配置差异会淹没代码优化的 A/B 信号。
9. **跨语言/跨进程的 checksum 必须 bit-exact**（crc32c 案例教训）。客户端 C++ `folly::crc32c(data, len, ~0U)` 与服务端 Rust `crc32c::crc32c(data)` 只要约定差一个取反，每次写都会 `ChecksumMismatch`。落地前必须先验约定，再谈性能。对外暴露的 C ABI 应当**自己消化取反**（init 与 xorout 都固定 0xFFFFFFFF），不要让每个调用方去记约定。
10. **分不清哪一档在跑，就加一个运行时访问器**。3FS 反复出现「以为优化生效了其实没有」（16 个 commit 静默编成桩、部署的二进制 md5 与本地构建不一致、ISA-L 符号数为 0）。`nm`/`objdump` 能证明符号存在，**证明不了运行时分派真的走到那里**。最终补的 `h3fs_crc32c_impl_name()` 返回 `"sve2"/"pmull"/"scalar"/"none"/"software"`，一次调用即可确认当前活跃档位 —— 这比事后从火焰图反推可靠得多，成本也低得多。
11. **慢的不一定是软件：跨集群/跨机器的性能差异，先做 5 秒硬件前置检查**。3FS A/B 集群顺序写 8 GB/s vs 3 GB/s，火焰图初判 CPU/CRC 算法问题并据此重建了 B 集群全部 target —— 无效。真因是 **NVMe 固件 FLUSH 长尾**（`fr=1059` vs `fr=8312`，p50 相同、p99 差 10~15 倍）。`nvme id-ctrl /dev/nvmeXn1 | grep -E 'mn |fr |sn '` 是 5 秒钟的检查，应排在所有软件分析之前。完整下钻过程见阶段 2c 案例。

## 证据状态（引用本 skill 数字前必读）

本 skill 的数据来自三个层次，可信度不同，**引用时必须区分**：

| 层次 | 内容 | 可信度 |
|---|---|---|
| **A. 本 skill 作者实测** | `scripts/` 下四个 crc32c 核验程序的运行结果；Kunpeng 920-4826 / openEuler 24.03 SP2（内核 `6.6.0-111`）上的 HWCAP 探测、x6 压测（8625 次 × 2 工具链）、分级分派矩阵（7 例） | 高，可复现 |
| **B. 3FS 会话/构建产物中记录的历史实测** | 火焰图占比、CPU-秒 A/B、fio 带宽、ClickHouse 写路径指标、bpftrace fdatasync/pwrite64 延迟分布、裸盘 append+fdatasync 分位数、微基准三档数据 | 中高，原始数据在会话记录里，但**未由本 skill 作者复跑** |
| **C. 仅来自 commit message 的声称** | 标量路径 5.19 GB/s / 查表 0.32 GB/s / e2e 2209 MiB/s / 191.5M chunks 8.96h / 7204 项检查；以及 **x6 对 >0x400 字节块段错误**、**HWCAP2 SVE2 探测误报** | **低 —— 未独立复测，且后两条已被 A 层实测反驳（见下）** |

**C 层两条声称的复核结果**（folly commit `64b1a2a2a` 据此把 `crc32c_hw()` 改成无条件走标量路径）：

| 声称 | A 层复核（SP2 内核 `6.6.0-111`） |
|---|---|
| `crc32c_isa_l_pmull` 对 **>0x400 字节块**段错误 | **未复现**：8625 次调用 × 2 工具链（clang 17.0.6 + BiSheng 4.2.0.B009），34 档长度精确跨越 `1023/1024/1025/1026`、17 种对齐（含跨页）、5 个种子，装 SIGSEGV/SIGILL/SIGBUS handler → **0 崩溃 0 错误** |
| HWCAP2 SVE2 位 `#ifndef` 回退常量取值错误、**误报 supported** | **未复现**：8 个常量的回退**全部未触发**（系统头文件提供了定义且值相同），`getauxval(AT_HWCAP2)` = **0**，谓词正确返回 false |

**未解决的关键变量**：复核环境是 SP2，集群 storage 节点是 SP3（`6.6.0-145`）；CPU 型号两边一致（920-4826）。HWCAP2 声称本身与内核 ABI 相关，**内核差异是可能的解释，必须在 SP3 节点上重跑 `scripts/probe_hwcap.c` / `scripts/stress_x6.c` 才能定论**。因此本 skill 把这两条标注为「已报告、未复现、待定论」，**不当作事实，也不当作已证伪**。

**定论悬而未决时不要二选一**（信它 → 降级到慢实现；不信它 → 照常上线）。正确形态是三件事一起做：① 可移植验证套件（scp 到目标环境一键复跑，自带内核/CPU/编译器环境指纹）；② 运行时开关（`FOLLY_CRC32C_SCALAR_ONLY=1` 强制标量档，现场无需重编译即可规避）；③ 运行时档位访问器（`h3fs_crc32c_impl_name()` 直接确认当前走哪条路）。无论报告真假，代价都可控。

## 何时触发

- **场景入口**：3FS 慢 / 写带宽低 / CPU 占用高；storage_main / meta_main / hf3fs_fuse_main 热点归因；chunk_engine / RocksDB WAL 同步开销大；跨集群 / 跨机器性能差异定界
- **取证类**：火焰图中某符号占比高（`__crc32cd`、`Checksum::calcSerde` 等）；ClickHouse 写路径某环节延迟高（`storage.engine_update` 等）；fdatasync / p99 长尾飙高；怀疑 HEAD 网卡打满
- **实现类**：checksum/crc32c 查表改硬件指令、ISA-L / PMULL folding / SVE2 选型、CRC32 扩展指令、`.arch_extension crc`
- **落地类**：`ChecksumMismatch`、crc32c 结果不一致、符号重复定义、`objcopy redefine-sym`、Rust 调用 C 汇编、`build.rs` 汇编 `.S`、"编过了没生效"的二进制核验
- **不触发 / 移交**：配置面杠杆（enableChecksum / sync_when_write / chain table 等）→ skill `3fs-parameter-tuning`；编译部署与二进制上集群 → skill `3fs-deployment-installation`；非 3FS 项目可复用五阶段方法论与 crc32c 案例，但写路径指标地图与指标脚本为 3FS 专有

## 输入

从用户请求或对话上下文获取：

| 字段 | 必填 | 说明 |
|------|------|------|
| `cluster_env` | 是 | 3FS 集群访问方式：节点与角色（storage / meta / mgmtd / FUSE 客户端）、挂载点、ClickHouse 监控库可达性 |
| `hotspot_site` | 建议 | 已知的热点函数/符号/指标（如 `__crc32cd`、`storage.engine_update` 延迟）；缺省时由阶段 1 从火焰图与写路径指标自寻并说明 |
| `workload_cmd` | 是 | 可复现的负载命令（含参数口径），用于采集与 A/B |
| `optimization_goal` | 是 | 端到端带宽/IOPS、CPU-秒、单请求延迟 —— **三者结论可能完全相反**，必须先明确 |
| `target_platform` | 是 | CPU 型号 + ISA 能力（是否有 CRC32 扩展 / PMULL / SVE2）+ 盘/网卡型号与固件 |
| `constraints` | 建议 | 是否允许改 ABI/构建、是否有跨语言一致性要求、正确性红线 |

`optimization_goal` 缺省时必须向用户确认，不得默认按"降 CPU"验收 —— 3FS 案例中 CPU 降了 6.1% 而业务指标（带宽）零收益，若未事先约定判据，这次优化会被错误地记为成功或失败。

## 执行步骤

### 任务初始化

用任务列表工具建立五个阶段任务，每阶段完成即更新状态。阶段 2 是**硬门控**：未通过则终止流程并输出"不建议优化"结论。

### 阶段 1：火焰图取证与写路径分解

目标：把"3FS 好像很慢"变成**带绝对量纲的函数级/环节级证据**。CPU 型问题走 1a~1e（火焰图），延迟型问题走 1f（ClickHouse 写路径分解），网络型怀疑走 1g（RDMA 带宽）。

1a. **采集参数**：用 `scripts/get_flame.sh`（`PROC` 默认 `storage_main`，可设 `meta_main`/`hf3fs_fuse_main`）或手工执行：

```bash
PIDS=$(ps aux | grep 'storage_main' | grep -v grep | awk '{print $2}' | tr '\n' ',' | sed 's/,$//')
perf record --call-graph dwarf,8192 -F 99 -m 2048 -p ${PIDS} -o perf.data -- sleep 15
perf script -i perf.data 2>/dev/null | stackcollapse-perf.pl > perf.folded
flamegraph.pl --title "<workload> Flame Graph" perf.folded > flamegraph.svg
# 同时起 1 秒粒度 CPU 基线采集，事后核对 profiling 是否压到了负载
while true; do top -bn1 | head -20 >> cpu_stat.txt; sleep 1; done &
```

参数依据与三个已踩过的坑：
- **必须 DWARF 展开**：`perf record -g`（帧指针）穿不过 Rust release 代码 → 3FS 实测 **51.4%** 样本丢失中间帧，`__crc32cd` 直接挂在线程根下；
- **栈 dump 与采样率要压住**：`--call-graph dwarf,65528`（默认上限）7 秒产出 **14.4 GB** perf.data、`lost 32.43%` 样本（占比数字本身不可信）；`dwarf,8192 -F 99 -m 2048` 同场景 3 秒 **2.2 MB / 0 丢失**；
- **lbr 不可用**：`--call-graph lbr` 在鲲鹏 920 PMU 上直接报 `doesn't support branch stack sampling`（LBR 是 x86 习惯，ARM 平台先探测再用）；
- `perf script` 的 `addr2line: could not read first record` 是无害 stderr 噪声，用 `2>/dev/null` 抑制；交叉核对符号用 GNU addr2line（eu-addr2line 可能给错符号）。

前提：二进制带 debug_info、未 strip（`file <bin>` 确认；`RelWithDebInfo` 构建）。Rust chunk_engine 静态链进 C++ 时栈里出现 `hf3fs$chunk_engine$cxxbridge1$...` 桥接符号是正常的，不要当成解析失败。

**profiling 会压负载**：3FS 三路并发 DWARF 采集期间，fio 顺序写从 1462 MiB/s 掉到 556 MiB/s。**采集火焰图的跑次不得用于带宽结论。**

1b. **聚合口径（数字可信的前提）**：
- **同符号必须求和，不能取单帧最大值**：3FS 曾把 `__crc32cd` 按最大值读成 **0.81%**，按求和才是 **24.56%**（差 30 倍，足以误判"不是热点"）。folded 文件按叶子聚合：

```bash
awk '{n=$NF; $NF=""; split($0,a,";"); k=a[length(a)]; s[k]+=n}
     END{for(x in s) print s[x], x}' perf.folded | sort -rn | head -20
# 按关键词族求和（含栈中任意位置；防止样本落在局部标签上漏计）
for k in __crc32cd crc32_iscsi .align_finish folly::crc32c crc32c_combine; do
  printf '%-28s %s\n' "$k" "$(grep -F "$k" perf.folded | awk '{s+=$NF} END{print s+0}')"
done
```

- **口径差异要标注**：叶子聚合（权威，反映自身耗时）/ 任意位置 grep 求和 / SVG 帧求和（不同采集批次不可直接比）/ 非加权栈计数（**DWARF 模式下无意义**，栈唯一化稀释计数：236/76801=0.31% 的假数字）。报告里写清用的哪一种。
- **跨图对比用子树归一化**：两张图总样本量不同时不得直接比全图百分比，各自除以**同名线程根**的样本数（3FS：A 图 `__crc32cd` 3029/Update2 6720=45.07%，B 图 382/935=40.86%）。小样本子树要给近似置信区间；父帧 inclusive 与子帧 inclusive 不可相加（父含子）；**结构性展开失败与采样噪声要区分**。
- folded 权重是 perf period（cycles）不是样本数：两次同为 15 秒 `-F 99` 的采集总权重可差 **43%**，只能比相对占比，绝对量用 CPU-秒（1c）。

1c. **换算绝对量纲**：占比 × 进程实际占用核数 = 该热点的绝对核数。进程核数用 `/proc/<PID>/stat` 字段 14+15（utime+stime，100 tick/秒）差分：

```bash
a=$(awk '{print $14+$15}' /proc/$PID/stat); sleep 5
b=$(awk '{print $14+$15}' /proc/$PID/stat)
echo "scale=2; ($b-$a)/100/5" | bc    # 核数
```

**不要按线程名聚合 `/proc/*/stat`**（重名线程重复计数：3FS 曾把客户端读成 143.78 核，真值 3.77 核）。示例换算：`__crc32cd` 占 storage 采样 31.99% × storage_main 实际 9.06 核 ≈ **2.9 核**（不是 0.32×96=30.7 核）。CPU-秒（负载前后差分 ÷100，再除以处理数据量得 CPU-秒/GiB）是 A/B 首选判据，比火焰图占比可靠；测量窗口必须干净（见「测量纪律」）。

1d. **证伪假热点与符号误读**：
- **局部标签吃掉整个函数的样本**：静态汇编的局部标签会成为 perf 的"最近符号"。3FS 替换实现后全局符号 `hf3fs_ce_crc32_iscsi_x6` 显示 **0.00%**，样本全落在函数内局部标签 `.align_finish`（**28.36%**）上。看到"优化后热点消失"先按关键词族求和确认不是符号解析问题 —— 反过来，`.align_finish` 占 28.36% 也是 x6 **确实执行过**的一手证据。
- **符号形态不同 ≠ 实现不同**：`core::core_arch::arm_shared::crc::__crc32cd` 与 `crc32c::hw_aarch64::crc32c → neon::generated::__crc32cd` 是同一个 ARMv8 CRC32C 硬件 intrinsic，只是 rustc 版本不同（stdarch 重构）。**要证明实现不同必须看 build-id 或反汇编，不能看符号名。**
- **负载口径核对**：3FS 一次标称"顺序读"的采集里**一个读帧都没有**（`handleRead` 缺席，Update+UpdatePool 占 88%），实际是顺序写。采集前查关键业务符号、线程名分布、监控里的请求类型计数。
- **空闲等待帧不是业务开销**：`UnboundedBlockingQueue::try_take_for`、`MemoryIdler::futexWaitPreIdle` 是消费者空闲时的 futex 等待；且看火焰图猜队列实现会猜错（3FS 更新路径实际用的是 BoundedQueue/MPMCQueue），**必须回源码确认**。

1e. **区分 on-CPU 与阻塞**：火焰图宽度只反映 on-CPU 时间，`fdatasync` 帧宽 ≠ 墙钟等待。计算热点与 IO 等待在同一线程交替出现时，用 bpftrace 打 syscall 延迟直方图定界：

```bash
bpftrace -e '
tracepoint:syscalls:sys_enter_fdatasync { @start[tid] = nsecs; }
tracepoint:syscalls:sys_exit_fdatasync /@start[tid]/ {
  $dur = nsecs - @start[tid];
  @fdatasync_ns = hist($dur); @fdatasync_stats = stats($dur);
  delete(@start[tid]);
}
tracepoint:syscalls:sys_enter_pwrite64 { @pstart[tid] = nsecs; }
tracepoint:syscalls:sys_exit_pwrite64 /@pstart[tid]/ {
  @pwrite64_ns = hist(nsecs - @pstart[tid]); delete(@pstart[tid]);
}' -p $(pgrep -o storage_main) -o bpftrace_$(date +%Y%m%d_%H%M%S).txt
```

3FS 实测：fdatasync 均值 A 集群 **264 µs** vs B 集群 **1210 µs（4.6x）**，直方图峰值 [64K,128K) ns vs [1M,2M) ns 几乎不重叠 —— **这种长尾差异在 on-CPU 火焰图上完全不可见**。无 bpftrace 时降级 `perf trace --duration 0.001 --summary`（只有均值，长尾不可见，结论标注低置信）。

1f. **ClickHouse 写路径指标分解（延迟型问题的取证入口）**：火焰图回答"CPU 花在哪"，回答不了"墙钟花在哪"。`scripts/run_fio_seq_write_ck.sh` 跑一次顺序写并自动拉取全链路耗时（五段查询，tee 落盘）。指标地图：

**FUSE 侧（客户端节点，查询须按 `mount_name` 过滤）**：

| 指标 | 含义 |
|---|---|
| `fuse.iov.latency.ib_reg` | iov 共享内存缓冲的 IB 注册/注销延迟。**不是每笔写的往返延迟**：usrbio 缓冲在 job 启动时只注册一次，窗口内样本稀疏属正常，确认测试窗口改用 `fuse.piov.submit.write` 或 storage 侧 `req_write` 的 count 时间线 |
| `fuse.iov.latency.map` | IoVector 共享内存映射/分配与释放延迟 |
| `fuse.write.latency` | FUSE 内核 write 入口（缓冲写路径）单次调用总延迟 |
| `fuse.piov.{overall,prepare,submit,complete}.write` | `flushBuf` 一批写的总耗时 / 批次构建（切 chunk、地址翻译）/ 提交（**客户端视角的存储侧往返**）/ 完成处理。**USRBIO 数据面对应 `usrbio.piov.*`**（IoRing 路径），两族不会同时有量：查询为空就把 IN 列表换成另一族。判读前先 `GROUP BY metricName` 确认哪族有样本 |
| `fuse.iov.bytes` / `bytes.ib_reg` / `times` | iov 字节量 / 注册块大小分布 / 次数（`sum(mean*count)` ≈ 窗口总字节） |

**Storage 侧 22 项（按 host 分组，流水线顺序；嵌套：`req_write ⊇ reliable_update ⊇ do_update ⊇ engine_update ⊇ chunk_engine.{pwrite,copy_on_write,allocate}`，`reliable_update` 内并行含 `reliable_forward ⊇ update_remote`）**：

| # | 指标（`storage.` 前缀省略） | 含义 |
|---|---|---|
| 1 | `req_write.succ_latency` | 客户端写请求 RPC 总延迟（HEAD 入口） |
| 2 | `req_update.succ_latency` | update RPC 入口总延迟（链上每个副本都收到） |
| 3 | `reliable_update.succ_latency` | channel 锁 + 幂等去重 + handleUpdate |
| 4 | `update_remote.succ_latency` | update 转发给下一跳副本的延迟 |
| 5 | `reliable_forward.succ_latency` | 链转发整体延迟（含等下游 ack；HEAD 上最能反映"下游+网络"） |
| 6 | `do_update.succ_latency` | 本地 update（RDMA 拉数据 + chunk 锁 + engine update） |
| 7 | `do_commit.succ_latency` | update 成功后的元数据提交 |
| 8 | `engine_update.succ_latency` | chunk_engine（Rust）update：crc32c 校验、COW、pwrite、WAL 写（**crc32c 消融的 -41% 就体现在这项**） |
| 9 | `engine_commit.succ_latency` | engine commit：WAL/元数据落盘（sync 由 `sync_when_write` 传入） |
| 10 | `chunk_engine.pwrite_latency` | 引擎内数据 pwrite 均值（CheckWorker 每 1s 收集累计均值，**与逐请求口径的 8/9 只能看趋势不能精确相减**） |
| 11 | `chunk_engine.copy_on_write_latency` | COW 均值（覆盖写已有 chunk 时触发） |
| 12 | `chunk_engine.allocate_latency` | 新 chunk 分配均值（`allocate_size` 大块预分配 → XFS fallocate） |
| 13-15 | `pwrite` / `chunk_update` / `chunk_commit` `.succ_latency` | 旧 ChunkReplica 后端（`only_chunk_engine=false` 路径）对应项 |
| 16 | `wait_chunk_lock.succ_latency` | 等 chunk 锁（同 chunk 并发写串行化在这里） |
| 17 | `wait_channel_lock.succ_latency` | 等 update channel 锁（抢锁失败返回 `kChannelIsLocked` 让客户端重试） |
| 18-19 | `wait_post` / `wait_sem` `.succ_latency` | **读路径（batchRead）打点**；写路径对应项是 `write_wait_post` / `write_wait_sem`（do_update 内 storage 侧 RDMA read 拉数据），分析写路径 RDMA 受限时把这两项加进 IN 列表 |
| 20 | `update.queue_latency` | RPC 处理队列排队时间（高 = UpdateWorker 能力不足或下游反压） |
| 21-22 | `sync_start` / `sync_done` `.succ_latency` | target resync 开始/完成。**压测窗口内有量 = 窗口污染**（chain 在 SYNCING，与前台写抢盘抢网络），等全部 SERVING 后重测 |

计数类（counters 表 12 项：`req_write.total`、`req_update.total/bytes`、`reliable_update.total`、`do_update.total`、`do_commit.total`、`engine_update.total`、`engine_commit.total`、`chunk_update.total`、`chunk_commit.total`、`reliable_forward.total`、`forward.write_bytes`）用于：排除写放大（`engine_update.total`÷写入 GiB，A/B 实测 1024.00 vs 1024.32 相同→排除）、排除请求粒度差异（`req_update.bytes`÷`total`）、核对转发量与 HEAD 网卡 TX（配合 1g）、排除重试放大（各环节 total 应成比例）。

**统计口径**（`distributions` 表，时间原始单位 ns）：首选 **count 加权均值** `sum(mean*count)/greatest(sum(count),1)/1000`（µs）；百分位取 **区间口径** `max(p50/p90/p95/p99)`（窗口内最差区间，作长尾报警；要精确分布用 bpftrace 直方图）。**均值会掩盖长尾**，任何延迟结论同时看加权均值与 interval_p99/max。

**时钟偏移校正（必做，不做 storage 查询直接为空）**：storage 指标时间戳跟随 storage/META 时钟，压测机时钟可能偏差分钟级（3FS 实测 storage 快 ~113s、某 FUSE 节点 -136s）。脚本自动计算 `CK_OFFSET = CK now - 本机 now`：FUSE 指标用本机窗口 `[T0-30s, T1+60s]`，storage 指标用平移后的 `BEG_CK/END_CK`，**不得混用**；报告写明 CK_OFFSET（分钟级偏移本身就是环境问题，时钟漂移会连带触发 mgmtd `HeartbeatFail(5002)`）。百分位出现 `7.47e12` 类异常值 → 该节点该指标整列排除。

**使用前提**：`monitor_collector_main` 必须存活（3FS 曾查询全空，根因是采集服务已死 472 分钟）；测试结束后等 `CK_SETTLE_SECS`（默认 10s）落库。

手工查询 SQL 模板（延迟类；计数类把表换成 `counters`、聚合换 `sum(val)/avg(val)/max(val)`）：

```sql
WITH '<BEG_CK>' AS begin_time, '<END_CK>' AS end_time
SELECT host, metricName, sum(count) AS total_count,
  round(sum(mean * count) / greatest(sum(count), 1) / 1000, 1) AS weighted_mean_us,
  round(max(p99) / 1000, 1) AS interval_p99_us, round(max(max) / 1000, 1) AS max_seen_us
FROM `3fs`.distributions
WHERE TIMESTAMP BETWEEN begin_time AND end_time AND metricName IN (<清单>)
GROUP BY host, metricName
ORDER BY host, indexOf(<与 IN 相同清单>, metricName) FORMAT PrettyCompact
```

**判读：哪一层耗时占比高说明什么**（单机纵向看嵌套占比；A/B 横向先对齐计数类再比延迟类）：

| 现象 | 指向 |
|---|---|
| `engine_update` 占 `do_update` 大头，消融 checksum 后大幅下降 | 引擎内计算开销（crc32c/COW 重算）。消融实测 526 → 311 µs（-41%），端到端 604 → 799 MiB/s（+32.3%，单 job 延迟受限区间） |
| `engine_commit`/`do_commit` 高，或 A/B 倍数远大于 `engine_update` | WAL/RocksDB 同步慢 → fdatasync 长尾，下钻 syscall 与裸盘（2c 案例：`engine_commit` 5.42x 最终定界 NVMe 固件） |
| `chunk_engine.pwrite/copy_on_write/allocate_latency` 高 | 落盘/COW/块分配层 → bpftrace 打 `pwrite64` 直方图 + 裸盘对照；`allocate` 高查 `xfs_file_fallocate` 热点（`allocate_size` 256MB→1~2GB 曾提议） |
| `reliable_forward`/`update_remote` 高而本地 `do_update` 正常 | 下游副本慢或副本间网络受限 → 对照 1g 每节点 TX/RX，按 host 分组逐跳定位 |
| `wait_chunk_lock` 高 | 同 chunk 并发写热点（负载 key 分布或 numjobs 打同一片区） |
| `wait_channel_lock` 高 / 日志 `kChannelIsLocked` | 同客户端 channel 串行化，客户端在重试 |
| `update.queue_latency` 高 | UpdateWorker 队列积压：线程/协程配置不足或下游反压 |
| `write_wait_sem`/`write_wait_post` 高 | 写路径 RDMA 拉数据受限 → 网络轴，对照线速（100Gb/s = 12500 MB/s） |
| FUSE `piov.submit.write` ≈ storage `req_write` | 时间花在存储侧，客户端无额外开销 |
| FUSE `piov.submit.write` ≫ storage `req_write` | 差值在客户端排队/网络往返/客户端 CPU → 查客户端进程核数与 `traffic_control` 配置（(32,8)→(256,64) 曾带来 785→1198 MiB/s） |

1g. **RDMA 带宽监控（网络轴取证）**：`scripts/rdma_monitor.sh` 提供 `start_rdma_monitor` / `stop_rdma_monitor`（本地 + 远程节点并发采样，`perfquery` 的 `PortXmitData`/`PortRcvData` 差分 ×4 换算 MB/s，输出每节点 TX/RX 峰值与均值）。压测脚本 source 后即可在测试结束时得到网络利用率数据。**峰值判"是否触顶"，均值判"稳态约束"，两者必须分开报**（3FS 第一次用 TX 峰值 9299 得出"74%"，改均值 4825 后节点角色解读随之改变）。

**阶段输出**：函数级调用链 + 各热点符号的**求和占比**与**绝对核数** + 假热点排除记录 + 负载口径确认；延迟型问题另附写路径指标分解表（哪一层吃掉了墙钟）与 RDMA 峰值/均值。

### 阶段 2：天花板归因（硬门控）

目标：回答"把这个热点优化到 0，端到端指标能涨多少"。**答不出来就不许进阶段 3。**

2a. **三轴排查**（成本远低于改代码，先做）：

**网络轴**：RDMA 端口计数器换算实测带宽，对照线速：

```bash
perfquery -x -C mlx5_0 -P 1 | grep -E 'PortXmitData|PortRcvData'
# 计数器单位是 4 字节字：MB/s = (delta × 4) / 1048576
# 无 perfquery 时读 /sys/class/infiniband/<dev>/ports/<n>/counters/{port_xmit_data,port_rcv_data}
```

| 线速 100Gb/s = 12500 MB/s | 判定 |
|---|---|
| 实测 ~11360 MB/s（**~91%**） | 网络已是约束，CPU 优化不会涨带宽 |
| 实测 ~8400 MB/s（67%） | 有余量，继续查其它轴 |
| TX+RX 合计 ~9660（77%） | 临界，看是否单网卡双工共享 |

**链式复制的网卡账**：写路径 Client→HEAD→MIDDLE→TAIL，HEAD 单张网卡同时承载 RX（收客户端数据）与 TX（转发下一跳）。3FS 实测：3 副本（3 跳）HEAD 负载 TX 4825 + RX 4835 = 77%；2 副本（2 跳）RX 8384 = 67%（`--size=2g`）/ ~96%（`--size=512m`）。减少一跳直接提升总带宽：3 副本 9681 MiB/s → 2 副本 **10068 MiB/s（2g 稳态）/ 12380 MiB/s（512m 瞬态）**。

**存储 flush 轴**：on-CPU 火焰图看不见阻塞，用 1e 的 bpftrace 直方图 + 2c 案例的下钻方法。

**CPU 轴**：把占比换算成绝对核数（1c），并看 off-CPU：3FS storage_main 只用 9.06/96 核、Update 线程约 25% 忙（75% off-CPU）→ CPU 侧有 ~4x 余量，砍 CRC 不可能涨吞吐。**off-CPU 占比高时，on-CPU 优化的端到端收益天然受限** —— 这是比"网卡打满"更普适的解释。

2b. **硬件前置检查（跨集群/跨机器对比的第一件事，5 秒钟）**：

```bash
nvme id-ctrl /dev/nvmeXn1 | grep -E 'mn |fr |sn '   # 盘型号 / 固件 / 序列号
nvme get-feature /dev/nvme0n1 -f 0x06 -H            # Volatile Write Cache
uname -r; xfs_info <数据盘>; findmnt -no OPTIONS <数据盘>
```

2c. **fdatasync 长尾案例（"软件指标 → syscall → 裸盘 → 固件"逐级下钻的范本）**：

现象：A/B 两集群配置、部署形态、fio 语句、内核、XFS 参数、chain 拓扑完全相同，顺序写 **A = 8 GB/s，B = 3 GB/s**。**第一次归因（错误，代价最大）**：火焰图上 B 的 `crc32c::hw_aarch64` 占比高 + COW 有全 chunk CRC 重算 → 判为 CPU/算法问题 → 重建 B 集群全部 target → **仍是 3 GB/s，白做**。教训：**火焰图占比高 ≠ 它是原因** —— B 的 CRC 占比高是因为其它环节更慢导致占比相对上升。

源码背景：chunk_engine **每次 update 都硬编码 `sync=true` 写 RocksDB WAL**（`src/storage/chunk_engine/src/meta/meta_store.rs` 的 `persist_writing_chunk()` 写死 `rocksdb.put(..., true)`，字面量不受任何配置控制；`sync_when_write` 只作用于 commit 阶段）。盘固件 FLUSH 慢，update 就被直接阻塞。

逐级下钻与关键证据（B 层）：

| 层 | 手段 | 关键数字 | 结论 |
|---|---|---|---|
| 软件指标 | ClickHouse A/B 同窗口同 SQL | 计数类全同（请求大小 1048576 vs 1045268 B、updates/GiB 1024.00 vs 1024.32、coroutines 65 vs 65、`checksum_recalculate` 均 0、COW 比例 B 反而更低 31.88% vs 100%）；延迟类全劣化且 **`engine_commit` 倍数最大（5.42x）**、`engine_update` 3.23x、pwrite 1.8~1.9x | 排除写放大/请求粒度/COW/配置；差异在延迟不在次数，嫌疑收敛到落盘/同步层 |
| syscall | bpftrace（1e 命令） | fdatasync 均值 **264 vs 1210 µs（4.6x）**，直方图峰值 [64K,128K) vs [1M,2M) ns；次数 7214/s vs 1792/s（4.0x，与带宽 2.7x 吻合）；pwrite64 仅 1.54x | B 是磁盘 flush 受限，不是 CPU/CRC 受限 |
| 文件系统 | `xfs_info` / `findmnt` / `uname -r` | 逐字段一致 | 排除 XFS/挂载/内核 |
| 裸盘 | `scripts/append_sync.py`（4K 追加写+fdatasync，精确复现 WAL 模式，绕开 3FS 与 fio 版本差异，**最可信**） | **B 的 p50 与 A 几乎相同（66-69 vs 58-60 µs），p99 是 A 的 10-15 倍（0.7-1.2 ms vs 76-81 µs）** | 系统性长尾，不是均匀变慢；**均值会掩盖这一点，必须看分位数** |
| 块分配路径 | fio `--fallocate=posix` 预分配后 randwrite+fdatasync | B 仍 548.5 µs / p99 1631（A 对照 24.5 µs / p99 64） | 排除 XFS 扩展/块分配，矛头指向 FLUSH 命令本身 |
| 固件 | `nvme id-ctrl` | A `fr=1059`，B `fr=8312`；B 换成与 A 相同盘型号后问题依旧 | **唯一剩余差异** |

因果链：固件 8312 的 FLUSH 长尾（裸盘 p99 0.7~1.6 ms）→ WAL fdatasync 毫秒级 → 高并发下放大为均值 1.2 ms → 每次 update 多阻塞 ~1 ms → updates/s ÷4 → 8→3 GB/s。**软件侧无化解空间**（`sync=true` 硬编码）：出路是硬件侧统一固件；`sync_when_write=false`（+26.8%）只作用于 commit 阶段，消不掉 update 阶段的 WAL 同步（配置杠杆见 `3fs-parameter-tuning` skill）。

裸盘判读标准表：

| 裸盘测试结果 | 结论 |
|---|---|
| append avg ~60 µs（与对照集群相同） | 磁盘/文件系统追加路径正常，瓶颈在软件负载侧（并发、队列、脏页量） |
| append avg 100 µs+ / p99 ms 级，覆盖写仍快 | XFS 元数据路径或固件 flush 慢，用预分配覆盖写实验区分 |
| 预分配覆盖写也慢 | 固件基本盘 flush 慢，升级固件验证（本案例 B 集群即此形态） |

注意事项：fio 3.34 与 3.42 对 fallocate/laying-out 行为不同，会污染"新文件 vs 覆盖写"对比 —— 跨集群用同一版本同一构建的 fio，做不到时**只有两边同款的 python append 测试可信**；同固件个体差异大（B 两节点 548 vs 376 µs），每节点至少跑 3 次。**本案例不能证明的事**：决定性验证（B 盘升 1059 固件重测，预期 p99 回到 ~80 µs）在记录时点尚未执行，当前是排除法结论；direct=1 对照只在 A 测过。

2d. **消融实验求上界**：临时把嫌疑开销整体跳过（如 checksum 输出假校验值、去掉校验比对），测端到端变化 —— 这是"该开销归零能换来多少"的**唯一可靠上界**，任何实现的加速比都不可能超过它。3FS `remove_crc32c` 分支改了 7 处（覆盖清单模板）：`MessageHeader.h` 的 `Checksum::calcSerde`（返回 serde 魔数）、`Processor.h` 的接收端比对、`Common.h` 的 `ChecksumInfo::create/combine`、`StorageClientImpl.cc` 读路径校验、Rust `engine.rs` 的 `update_chunk` 入站校验、`chunk.rs` 的 `copy_on_write/safe_write` 5 处。保留不动：非数据路径的 CRC32（token 校验）与 `#[cfg(test)]` 代码。一致性保障：HEAD/TAIL 都沿用请求携带的相同 checksum，forward/local 对比仍成立。

3FS 消融结果（单 job 4G/4M 顺序写）：带宽 **604 → 799 MiB/s（+32.3%）**、`do_update` -33%、**`engine_update` 526 → 311 µs（-41%，crc32c 净成本）**、pwrite/COW 不变（瓶颈转移到数据落地本身）。**上界的适用范围要标注**：+32.3% 只在单 job 延迟受限区间成立；32 job 并发区间天花板是网卡（~91%），消融也涨不动。

两条纪律：**仅供归因，不是可交付代码**（结论记录后必须还原，走临时分支，绝不进正式提交/生产）；**必须全集群同分支部署**（3FS 只部署到部分服务 → 校验协议不匹配 → FUSE 客户端崩溃 `RoutingInfoNotReady`，mgmtd/meta/collector/admin_cli 全部同分支重建才恢复；替换前备份所有二进制并记录 md5）。

2e. **对照配置面（归因前固定并快照）**：确认当前配置下嫌疑路径是否真的在跑（如客户端 `WriteOptions::enableChecksum` 默认 true，被改成 false 后写路径 CRC 根本不算）。快照命令与 A/B 纪律：

```bash
ADMIN_CLI="${INSTALL_PREFIX:-/opt/3fs}/bin/admin_cli -cfg ${INSTALL_PREFIX:-/opt/3fs}/etc/admin_cli.toml"
$ADMIN_CLI "get-config --node-type STORAGE -o /tmp/storage_current.toml"
$ADMIN_CLI "get-config --node-type FUSE    -o /tmp/fuse_enable_checksum.current.toml"
grep -nE 'enableChecksum|chunk_checksum_type|sync_when_write|force_use_tcp' /tmp/*_current.toml
```

记录 `ConfigVersion`，A/B 两腿必须同配置版本、同二进制 md5。配置面杠杆清单与热更新机制见 `3fs-parameter-tuning` skill。注意 `chunk_checksum_type='NONE'` **不是可用的关闭开关**（会导致写全部失败：服务端仍重算 CRC32C、chunk meta 无类型字段、HEAD 比对 forward/local checksum → `kChecksumMismatch`）—— 要跳过只能走 2d 的消融分支。

2f. **门控判定**：

| 消融上界 | 网络/存储余量 | 判定 |
|---|---|---|
| 端到端 < 3% | 任一轴已 >85% | **终止**：输出"不建议优化"，改推网络/存储/固件/配置侧杠杆 |
| 端到端 < 3% | 有余量 | **降级**：只在 `optimization_goal` 是 CPU-秒/成本/延迟时继续，报告须显式声明带宽无收益 |
| 端到端 ≥ 3% | — | 继续阶段 3 |
| 未测（不允许改代码） | — | **低置信继续**：用解析上界（占比 × 进程核数 ÷ 总核数）替代，报告标注"上界未经实验验证" |

3FS crc32c 案例落在第二行：CPU-秒 -6.1% 是真实收益（降本、给其它线程让出 CPU），但带宽零收益必须在报告里写明，不能包装成性能提升。其 commit message 表述可作范本：`bandwidth unchanged at 10.5 GiB/s; the HEAD NIC is already at ~91% of 100Gb/s, so CPU is not the binding constraint`。

2g. **比 checksum 更大的杠杆（归因结论若是"不是约束"，转向这张表）**：

| 杠杆 | 3FS 实测收益 | 类型 |
|---|---|---|
| 客户端 `enableChecksum=false` | **7.8x** | 配置（牺牲端到端校验） |
| storage `sync_when_write=false` | **+26.8%**（10068 → 12767 MiB/s，3 客户端） | 配置（牺牲掉电安全性） |
| 副本数 3 → 2（写路径 3 跳 → 2 跳） | **+28%**（512m 口径） | 拓扑 |
| 客户端 `traffic_control.write` (32,8) → (256,64) | 785 → **1198 MiB/s** | 配置 |
| 客户端 `num_bg_threads` 2 → 6 | 461 → **815 MiB/s** | 配置 |
| FUSE 启动缺 `--cfg` 参数修正 | 写性能 **4x** | 部署缺陷 |
| NVMe 固件 8312 → 1059 | 预期消除 4.6x fdatasync 差距（待验证） | 硬件 |
| **代码侧 folly crc32c 换标量硬件路径** | 单核 **0.32 → 5.19 GB/s（~16x）**；e2e 写 **2209 MiB/s** | 代码 |
| 代码侧 Rust chunk_engine 换 ISA-L PMULL | CPU-秒 **-6.1%**，带宽 **0%** → **已停用** | 代码 |

**代码侧 checksum 优化在这张表里排后，但不是没价值** —— 它降 CPU-秒、降单请求延迟（`engine_update` -41% 的消融上界说明延迟收益是真的）、给同机其它线程让出 CPU —— 而是**必须先跟用户对齐判据**，并在报告里把它排在正确的位置。**注意最后两行的对比**：同样是代码侧 checksum 优化，打在 folly/C++ 客户端上（`enableChecksum=true` 时的写路径 CRC，串行前置开销，直接决定单客户端能推多快 —— 1462 MiB/s 的天花板就是它）拿到 ~16x 单核加速；打在 Rust storage 上只拿到 -6.1% CPU、0% 带宽（CRC 只占 9.06/96 核里的 ~2.9 核，链路已被 HEAD 网卡 ~91% 卡住）。**差别不在技术手段，在覆盖面与约束位置** —— 这就是门控要回答的："消掉它能推动哪个端到端指标"。

**阶段输出**：三轴余量表（网络利用率 / flush 延迟分布 / 进程核数与占比换算）+ 消融上界数据 + 配置面快照 + 硬件/固件前置检查记录 + 门控判定与理由。

### 阶段 3：实现选型（crc32c 为贯穿案例）

目标：在目标平台**能跑对、能跑久**的实现里选最快的那个 —— 判据顺序不可颠倒。但「跑不了」必须是**自己实测出来的结论，不是转述**。非 checksum 热点同样适用本判据顺序，候选集换成算法/数据结构/指令级变体。

3FS 的正反两面教材：微基准最快的 ISA-L PMULL 6 路（8.64 GB/s，B 层）被一份崩溃报告否决，改上慢 40% 的标量路径（5.19 GB/s，C 层）；而**那份报告在 A 层复核中未能复现**（见「证据状态」）。

3a. **五类实现对比**（鲲鹏 920 实测；方法论适用于鲲鹏平台，其它鲲鹏机型按 3g 决策树重新探测与压测 —— 如 950 类平台有 SVE2 档）：

| # | 实现 | 核心指令 | 并行度 | 单核吞吐 | 层级 | 920 可用性 |
|---|---|---|---|---|---|---|
| 1 | 软件查表（slice-by-8/16） | `ldr`+`eor` | 无 | **0.32 GB/s** | C | 基线 |
| 2 | 标量 CRC32C 指令串行 | `crc32cw`×2 / `crc32cb` | 1 路（延迟链） | **5.19 GB/s（~16x）** | C | 可用 —— 上线方案 |
| 3a | CRC32 指令 3 路并行（`crc32c` crate） | `crc32cx`（经 `bl` 调用） | 3 | 4.61~4.95 GB/s（1MB） | B | 可用但退化（3e） |
| 3b | CRC32 6 路并行 + PMULL 合并（ISA-L `crc32_iscsi_x6`） | `crc32cx`×6 + `pmull` | 6 | **8.64~9.42 GB/s（1MB）** | B | **最快**；曾被判">0x400 字节段错误"（C 层），**A 层实测未复现** |
| 4 | SVE2 融合（ISA-L `crc32_iscsi_p8c10`） | `eor3` + SVE `pmull` | 8×32B SVE + 10×16B 标量 | 无数据 | — | 920 无 SVE2 硬件，执行即 SIGILL；950 类平台适用但从未实测 |

**基线口径**：`~16x` 是「标量 vs folly 软件查表」，`1.87x` 是「x6 vs Rust crate」—— 两组不同基线，不可混用或相乘。3b 与 2 的取舍是 40% 吞吐差，那份崩溃报告值得查清楚再定。CRC32C 多项式：Castagnoli `0x1EDC6F41`，反射形式 `0x82F63B78`。

3b. **能力探测先行（不可跳过）**：

```bash
lscpu | grep -o 'Flags:.*' | tr ' ' '\n' | grep -E '^(crc32|pmull|sha2|sve2?|svepmull|svesha3|asimd)$'
```

鲲鹏 920（ARMv8.2-A，implementer 0x48，2×48 核无 SMT）实测只有 `asimd aes pmull sha1 sha2 crc32 asimdhp asimdrdm asimddp asimdfhm` —— **没有 SVE/SVE2**（这条是硬事实：`getauxval(AT_HWCAP2)` 返回 0，执行 SVE2 指令即 SIGILL）。C 侧 HWCAP 位：`AT_HWCAP=16`/`AT_HWCAP2=26`；`HWCAP_ASIMD=1<<1`、`PMULL=1<<4`、`CRC32=1<<7`；`HWCAP2_SVE2=1<<1`、`SVEPMULL=1<<3`、`SVESHA3=1<<5`。

探测纪律：
- **探测结论必须交叉验证**：`lscpu` Flags 与 `getauxval` 同源，不能互相验证；独立证据是**直接跑一条该 ISA 的指令并捕获 SIGILL**。高档扩展（SVE2/SVEPMULL/SVESHA3）必须实测。用 `scripts/probe_hwcap.c` 逐常量报告 `#ifndef` 回退是否触发。
- **`#ifndef` 回退常量是风险点，但风险是否兑现取决于构建环境头文件** —— 不能断言"回退不可信"，要实测回退有没有被触发（A 层实测：全部未触发，系统头文件提供了定义且值相同）。
- **优先用架构不变量而不是探针**：SVE2 ⊆ SVE 是架构保证，给 SVE2 探测加一个 `HWCAP_SVE` 位测试就能防住"HWCAP2 不可信"整类问题，比 SIGILL 探针更便宜安全（探针要在库初始化期装信号处理器，干扰调试器和 sanitizer）。A 层用 `-Wl,--wrap=getauxval` 伪造「HWCAP2 声称 SVE2 但 HWCAP 无 SVE」验证过：分派正确落到 pmull。
- **探测面越小，误报面越小**：标量档只需 `HWCAP_CRC32` 一位（标量 CRC 指令不需要 ASIMD/PMULL）。但"收窄探测面"不是"取消分档"—— 错的是用单 bit 谓词无条件短路掉所有更快档位。
- 探测结果用 `static const bool` + lambda（C++）或 `AtomicU8` 三态（Rust `is_aarch64_feature_detected!`）缓存，不要每次调用都 `getauxval`。
- **每一档都要在真实硬件上验证「会命中」且「命中后不崩」**。"有 fallback 所以安全"是静默失效的温床：3FS 最初的 `crc32c_hw` 只实现 SVE2 档，在 920 上谓词恒 false，`folly::crc32c()` **静默退回软件查表**，优化完全没生效且无任何报错。
- **选型前确认对手实际在哪一档**：`crc32c` crate 0.6.8 的硬件路径是编译期（`armsimd` cfg，要求 rustc ≥ 1.80）+ 运行期双重门控，rustc < 1.80 时静默走软件查表。用 `nm`/`objdump` 查二进制里有没有 `crc32cx` 指令，不要信 Cargo.toml。

3c. **稳定性门槛（先于任何测速）**：任何候选实现先跑 `scripts/stress_x6.c`（改被测符号即可复用）：

| 检查 | 方法 | 证据层级 |
|---|---|---|
| 大输入不崩 | 覆盖生产最大尺寸 + 超出尺寸，跨越每个已知长度门槛，多种对齐（含跨页），装 SIGSEGV/SIGILL/SIGBUS handler 报告是哪个 len/off/seed | A 层：x6 在 920/SP2 上 8625 次 × 2 工具链，0 崩溃 0 错误 |
| 长时间不崩 | 生产负载持续压测 ≥ 数小时 | C 层：标量路径 191.5M chunks / 8.96h 零崩溃（仅 commit message） |
| 差分正确 | 见阶段 4 | A 层：321 组长度×对齐 + 27 档长度经 x6 内核，0 mismatch |

**两条纪律，方向相反但同等重要**：
- **短跑冒烟不足以放行**：x6 曾通过 1MB 微基准**和** 64 GiB fio 冒烟（`err=0`、16 条 chain 正常 SYNCING→SERVING、无 `ChecksumMismatch`）—— 八项验证全绿。若崩溃报告为真，这些全都不构成放行证据。段错误不会体现在性能数字里，**性能 A/B 与稳定性压测是两条独立的门**。
- **但"崩了"的报告也必须自己复现**：未复现的崩溃报告不足以否决一条快 40% 的路径；正确响应是验证套件 + 运行时开关 + 档位访问器，而不是直接降级。**微基准直接调汇编符号、生产经包装层调用，两条路径的输入分布可能不同 —— 压测必须打包装层，不能只打裸符号。**

3d. **微基准测选型，但只用于排序**：同一缓冲尺寸下对比通过稳定性门槛的候选，取排序不取绝对值，且按**生产实际缓冲尺寸**取数（3FS chunk 是 1MB，所以相关数字是 1.87x 不是 64KB 的 2.74x）。衰减链（同一对实现）：

```
1MB 热缓冲（L2 常驻）  1.87x
1MB 冷缓冲 单线程      1.74x    ← 生产数据是 NIC DMA 过的冷散缓冲
1MB 冷缓冲 32 线程     1.57x    ← DDR 争用，聚合吞吐 27~42 GB/s 饱和
生产 in-situ          1.13 ~ 1.23x
```

报告必须同时给微基准与 in-situ 数字并解释差距，只报微基准属于夸大。**未解释的离群值要标注并追根因，不要平滑掉**（3FS 8 线程冷缓冲 1.09x 离群值当时被当噪声记录，事后看是 x6 路径出问题的唯一征兆）。

3e. **查清现有实现为什么慢**（决定选型是否有意义，不看反汇编就换实现可能换完发现瓶颈不在这）：
- `crc32c` crate 0.6.8 的 aarch64 硬件路径**每 8 字节一次 `bl __crc32cd` 函数调用**：`__crc32cd` 带 `#[target_feature(enable="crc")]` 而调用者 `crc_u64_append/crc_u64_parallel3` 没带，Rust 禁止把 target_feature 函数内联进无属性调用者，三路并行退化成跨函数调用（对照组 `crc_u8` 带属性，是内联紧循环）。**这类退化只能靠反汇编发现，看源码看不出来。** rustc 1.90 把 intrinsic 移到 `neon::generated` 但属性相同，退化依旧。
- ISA-L x6 为什么快 —— 不是"更高级的指令"（主循环同一条 `crc32cx`），是工程实现：`.macro` 内联展开消除每 8 字节的 `bl`、并行度 3→6（stride 168×N）、`.rept 4` 手工调度交错 `ldr` 与 CRC 隐藏加载延迟、每 1024B 用 5 路 `pmull` 折叠合并（常量 `x^k mod P`）+ `eor` 树；≤1024 字节走尾块路径（`.loop_64B → .loop_16B → .less_16B/8B/4B/2B` 逐档）。
- C++ 侧的同类问题：全局 `-march=armv8-a` 缺 `+crc` 导致编译期宏门控的 intrinsic 路径整体编成桩（见信条 6），正解是 `.arch_extension crc`。

3f. **上线形态：分级分派 + 两项加固**。反例演进：(a) 只有 ISA-L 两档无标量兜底（无硬件平台直接 throw）→ (b) 无条件标量（ISA-L 变不可达死代码：920 丢掉 x6 的 40% 吞吐，950 永远到不了 SVE2 档，16533 行 SVE2 汇编编进每个构建却不可达，链接器剥不掉）→ (c) **推荐的分级分派**：

```cpp
switch (crc32cTier()) {          // Sve2 > Pmull > Scalar > None，解析一次并缓存
  case Crc32cTier::Sve2:   return crc32c_isa_l_sve2(...);    // 鲲鹏 950 类
  case Crc32cTier::Pmull:  return crc32c_isa_l_pmull(...);   // 鲲鹏 920 类
  case Crc32cTier::Scalar: return arm_hw_crc32c(...);        // 其它 ARMv8-A + CRC32
  case Crc32cTier::None:   break;
}
throw ...;
```

两项配套加固缺一不可：① SVE2 探测加 `HWCAP_SVE` 交叉校验（架构不变量，防 HWCAP2 不可信整类问题；`scripts/probe_hwcap.c` 已内建该校验，`scripts/probe_hwcap_fake_sve2_test.c` 用 `--wrap=getauxval` 伪造"HWCAP2 谎报 SVE2"做机器断言回归）；② 运行时开关 `FOLLY_CRC32C_SCALAR_ONLY=1` + 档位访问器 `h3fs_crc32c_impl_name()`。**`_supported()` 门必须与分派档位一致**（门只查 CRC32 位而分派会走进 SVE2 档，门就形同虚设）；公开 API 前必须有 `_supported()` 门（让 throw 从公开路径不可达）。

上线的标量内核四个设计要点（`arm_hw_crc32c`，~128 行）：**`.arch_extension crc` 写在内联汇编里**（免疫全局 `-march` 缺 `+crc`，A 层已用生产 compile_commands.json 的原始 flags 对照验证：同一套 flags 下 intrinsic 路径编成桩、该路径正常落地且 helper 完全内联）；`std::memcpy` 对齐安全加载（数据来自 NIC DMA，起点不保证对齐）；`__AARCH64EB__` 下 bswap；串行依赖链不做多路并行（简单到不会崩，代价是比 x6 慢 40%）。

SVE2 档补充事实：920 上不可用是**硬件事实**（无 SVE2，SIGILL）；950 上从未实测，**不得计入已验证收益**，报告必须写"未在目标平台执行"；已知隐患 —— `max_blk_num=1260`（常量表覆盖 ≈524 KB/次调用），3FS 1MB chunk = 2520+ 块，每次调用都走 `xnmodp_cal` 运行时常量计算慢路径（影响有界，可扩表消除）。**死代码的判定取决于分派逻辑，不取决于代码是否被编译**：同一份 `.S` 在无条件标量分派下是死代码，在分级分派下是 950 档；判断方法是 grep 调用点 + 读分派顺序 + 运行时档位访问器确认，不要读 commit message 下结论。

3g. **选型决策树与 ROI**：

```
第 0 步：探测 HWCAP（SIGILL 探针交叉验证，回退常量不可全信）
├── 无 CRC32 扩展 → 终止本子流程 → 转 apply-vectorization（查表 SIMD 化）
│   或算法侧（增量校验 / 校验值复用 / 降低校验粒度）
└── 有 CRC32 扩展
    第 1 步：稳定性门槛（先于测速）：生产最大尺寸+超出尺寸，各 ≥1e6 次
    │   段错误/SIGILL → 淘汰并记入反证台账；全绿 → 第 2 步
    第 2 步：持续压测：生产负载 ≥ 数小时；崩 → 淘汰；零崩 → 第 3 步
    第 3 步：存活者中测速排序（920：x6 8.64~9.42 > 标量 5.19 > crate 4.61~4.95 > 查表 0.32 GB/s）
    │   现有实现已内联且并行度足够 → 收益空间小，回阶段 2 重估
    第 4 步：定论悬而未决时（如崩溃报告未复现）→ 不二选一：
        保留快路径 + 探测交叉校验 + 运行时开关 + 档位访问器 + 可移植验证套件
```

**决策树的顺序就是判据的顺序**：能跑对 → 能跑久 → 跑得快。3FS 的错误正是先按第 3 步选了最快的，再回头发现它过不了第 1 步（而第 1 步的否决依据至今未复现）。

ROI = 预期收益 × 覆盖面 ÷ 风险：收益取**与生产缓冲尺寸匹配**的微基准档位再按衰减链打折（经验上 in-situ 约为 1MB 热缓冲数字的 60~65%），会崩的实现收益记 0；覆盖面 = 该调用点占总 checksum 计算的比例 —— **动手前枚举所有调用点并标注各自占比与所在进程，只打占比大的**（3FS 教训：folly 侧优化只覆盖 C++ 调用点，而 `only_chunk_engine=true` 时目标 payload 的 CRC 是 Rust 侧算的（12.5% CPU），folly 优化完全没碰到；serde 头部 CRC 只占 0.25%，加速 4~8x 也只有 <0.3% 总收益；客户端写路径 CRC 在客户端进程里算，**永远不会出现在 storage_main 的火焰图上**）；风险 = 是否触及跨语言约定（高）/改构建系统（中）/只改单侧实现（高，校验失败）/**有其它函数共用同一 `_hw_supported()` 谓词**（高，见 4c）。逐条对照「反证台账速查」，确认待实施项与已证伪项**机制不同**，而不是换个写法重试同一个方向。

**阶段输出**：平台能力探测结果（含交叉验证）+ 候选实现稳定性门槛结论 + 微基准排序表 + 现有实现退化根因（反汇编证据）+ 选定实现与理由 + 未选用项的封闭声明。

### 阶段 4：ABI 落地（crc32c 为贯穿案例）

目标：把选定实现接进构建，并**先证明结果 bit-exact、再证明没有连带破坏，最后才谈性能**。任何代码级优化在本阶段的通用义务是：正确性验证 + 证明改动真的进了部署二进制（md5/sha256 核对 + 符号/反汇编核验）。

4a. **约定核验（第一优先级）**：ISA-L 的 `crc32_iscsi_x6(data, len, crc)`（AAPCS64：BUF=x0, LEN=x1/w1, wCRC=w2；`len <= 0` 走零长度返回）收发的是**未取反的内部状态**，与常见 API 差一个前后取反：

```
crc32c_append(seed, data) == !crc32_iscsi_x6(data, len, !seed)
```

KAT：`CRC32C("123456789") = 0xE3069283`。直接 `x6(data, 9, 0)` 得 `0x58E3FA20`（**错**）；`!x6(data, 9, !0)` 得 `0xE3069283`（对）。**KAT 不过就不要往下走 —— 后面所有性能数字都建立在错误结果上。**

4b. **对外 ABI 应当自己消化约定**：`uint32_t h3fs_crc32c(const uint8_t* data, uint64_t nbytes)` + `bool h3fs_crc32c_hw_supported(void)`，init 与 xorout 都固定在内部（`~0` 进、`~` 出）、`uint64_t` 长度免除 int 上限与调用方分块义务、内部自动回退软件实现（调用方无需先做 feature check）、`nullptr`+0 字节合法。**每多一个需要记住约定的调用方，就多一个 `ChecksumMismatch` 的机会。**

4c. **combine 连带审计（不可跳过）**：grep 出所有以同一个 `_hw_supported()` 谓词分派的函数，逐个确认其 `_hw` 变体在目标架构上**真的有实现**：

```bash
grep -rn "crc32c_hw_supported\|crc32_hw_supported" --include=*.cpp --include=*.h <folly>/
grep -rn "FOLLY_SSE_PREREQ\|__ARM_NEON\|return 0" <folly>/folly/hash/detail/Crc32*Detail.cpp
```

3FS 的实际故障：`crc32c_combine_hw` 依赖的 `gf_multiply_crc32c_hw` 只有 x86 PCLMUL 实现，非 SSE 平台上是 `return 0` 的空桩 —— 谓词在 ARM 返回 true 后，`folly::crc32c_combine()` **静默返回错误校验值**。这类故障不崩不报错、符号正常存在（`nm` 查得到、`verify_hw_path.sh` 不报警），**只有差分测试能抓出来**。架构事实：ARM 的 CRC32C 指令只提供多项式**除法**（`crc32cx`），不提供**乘法**，而 combine 本质是 `crc1 * x^len mod P` —— **ARM 上 combine 没有硬件实现，只能软件算**；替换实现后 `crc32c_combine` 仍占火焰图 7.56%→8.10%，那是**架构下限，不是遗漏的优化机会**。差分测试必须单独覆盖 combine：`combine(crc32c(buf1), crc32c(buf2), len(buf2)) == crc32c(buf1||buf2)`（3FS 记录 7204 项 C ABI/combine 检查通过，且原始 combine 实现在同组检查下失败 —— 测试有区分力，不是恒真）。**这条审计要写进报告**：它是一次性的、容易被后人重构掉的隐性约束。

4d. **差分验证**：KAT 通过后做交叉差分 —— 4 个种子 `{0x00000000, 0xFFFFFFFF, 0xDEADBEEF, 0x01020304}`（1MB LCG 填充缓冲）× 覆盖 `0..1MB+7` 的长度集合，逐一对比新旧实现，**不一致条数必须为 0**。长度集合必须覆盖各代码路径：空（0）、尾块各档（1/2/3/4/7/8/15/16 → `.less_2B/4B/8B/16B`）、主循环门槛两侧（1024/1025）、64B 循环两侧（63/64/65）、生产尺寸（1048576）、门槛外零头（1MB+7）。用 `scripts/verify_crc32c_convention.c`（含分块串联等价与非对齐起点）。

4e. **跨语言/跨进程一致性**：若 checksum 由一端计算、另一端校验，两端必须同时改或约定 bit-exact。3FS 的三处约定：客户端 C++ `ChecksumInfo::create/combine` = `folly::crc32c(data, len, ~0U)`；服务端 C++ `ChunkEngine.cc` 存 `~checksum`；服务端 Rust `engine.rs` 用 `crc32c::crc32c(data)` 存原始值比对 —— 中间隔一次按位取反，任何一处搞错都会每次写 `ChecksumMismatch`。**bit-exact 验证的直接价值：把改造范围从"全部调用点"缩小到"最热的一个调用点"**（bit-exact 时新旧实现可混用）。副本链一致性：HEAD 比对 forward 与 local checksum，**灰度期间新旧二进制混跑要求 bit-exact**；不 bit-exact 就必须全量同版本部署。

4f. **长度与降级**：汇编签名收 C `int` 长度时，超过 `i32::MAX` 必须有降级路径 —— C++ 侧按 `kMaxChunk` 分块循环（x6 收发内部状态，可直接串联无需额外取反）；Rust 侧选**回退 crate**而不是分块（生产上限 64MB 远在限内，分块代码没有收益只增加风险 —— **按实际数据尺寸上限选策略，并把上限写进注释**）。非目标平台用 `#[cfg]` 直通原实现。

4g. **符号集成与构建**：把同一份汇编链进两个归档（CMake 的 `libfolly.a` + cargo 的 `libchunk_engine.a`）会撞符号，且 cargo 的 bin 目标不链 folly 会 `undefined reference`。解法：crate 的 `build.rs` 自己汇编 `.S` + `objcopy --redefine-sym crc32_iscsi_x6=hf3fs_ce_crc32_iscsi_x6` 重命名 + 打独立静态库（`find_tool` 逐候选试 `--version`，LLVM 优先 GNU 兜底；只在 `aarch64 && linux` 启用）。集成前自检：

```bash
clang -c crc32_iscsi_x6.S -o /tmp/x.o
nm -u /tmp/x.o     # 必须为空 —— .S 自包含（常量表是局部的）
nm -g /tmp/x.o     # 必须只有一个全局符号：T crc32_iscsi_x6
llvm-objcopy --redefine-sym crc32_iscsi_x6=hf3fs_ce_crc32_iscsi_x6 /tmp/x.o && nm -g /tmp/x.o
```

**技巧的适用边界**：只适用于自包含的 `.S`/`.c`（`nm -u` 为空）；依赖库头文件的 C++ TU 搬不动（此时先把核心算法抽成自包含 TU，或让 cargo bin 链上该库）。**先确认搬运的目标实现已通过稳定性门槛，再设计搬运机制** —— 否则得到一套精巧的构建机制包裹着一个未经核实的内核。CMake 侧要点：`.S` 自带 `.arch` 指令（x6 是 `armv8-a+crc+crypto`，p8c10 是 `armv8.5-a+crc+sha3+sve2-aes`），**免疫全局 `-march` 缺 `+crc`**；声明为带预处理的汇编（`set_source_files_properties(... PROPERTIES COMPILE_OPTIONS "-x;assembler-with-cpp")`，因为 `.S` `#include "../include/aarch64_label.h"` 且用宏 —— 目录布局约束：`asm/*.S` 与 `include/aarch64_label.h` 必须保持相对位置）；`RelWithDebInfo` 是火焰图能符号化的前提。

4h. **二进制核验（不可跳过）**："构建成功"不等于"优化生效"：

```bash
bash scripts/verify_hw_path.sh <binary> [symbol_regex] [detect_fn_regex]
```

| 核验项 | 命令 | 通过判据 |
|---|---|---|
| 加速符号存在 | `nm -C <bin> \| grep -E 'crc32_iscsi\|Crc32cIsaL'` | 出现预期符号（3FS ISA-L 版 storage_main 12 个、fuse 3 个；基线与 NEON cherry-pick 版均 **0 个**） |
| 探测函数未被编成常量桩 | `objdump -d --disassemble-symbols=<探测函数> <bin>` | 不是 `mov w0, wzr; ret`（NEON 路线 16 个 commit 全靠这一步发现空转） |
| 热点函数真的用了硬件指令 | `objdump -d <bin> \| grep -c 'crc32c[xwhb]\|pmull'` | 计数 > 0 且落在目标函数内（脚本做定向反汇编，数百 MB 带 debug_info 二进制同样可用） |

**反向核验空桩**：`return 0` 型架构兜底桩在 `nm` 里是正常符号，脚本查不出来，必须靠读源码 + 差分测试（4c）。**部署前核对部署二进制与本地构建的 md5/sha256 并记录构建目录** —— 拿错构建会让整轮 A/B 归因失效（部署流程见 `3fs-deployment-installation` skill）。

4i. **生产验证与部署纪律**：代码级验证通过后还有一次生产验证（3FS：ISA-L 版部署到两个 storage 节点，观察 16 条 chain SYNCING→SERVING ~40s、fio `err=0`、无 `ChecksumMismatch` 日志、与 crate checksum 混用安全）。部署纪律：替换前备份所有被替换二进制（文件名带日期如 `*.bak.20260826`）；**全集群同分支部署**，不要只换部分服务（校验协议不匹配会导致客户端崩溃）；记录每个二进制的 md5 与构建目录，A/B 归因时靠它排除"拿错构建"。

**阶段输出**：约定映射式 + KAT 结果 + 差分验证 0/N（含 combine 专项）+ combine 连带审计清单 + 跨语言一致性说明 + 长度降级路径 + `verify_hw_path.sh` 输出 + 部署二进制 sha256。

### 阶段 5：in-situ 验收

目标：在真实负载上量化收益，证明长时间不崩，并诚实标注微基准与生产的差距。

5a. **持续压测（手写 asm/intrinsic 的硬门槛）**：阶段 3c 的稳定性门槛要在**生产负载**上重跑一遍。3FS 标量路径的放行证据是 **191.5M chunks / 8.96 小时零崩溃**，配套 e2e 顺序写 **2209 MiB/s 稳定 60s**（C 层）。**"跑过一次没崩"不是放行证据；"有人报告崩了"同样不是否决证据** —— 两边都拿不出时，用「保留快路径 + 运行时开关 + 档位访问器 + 可移植验证套件」把决定权交给目标环境。

5b. **CPU-秒归一化**（比火焰图占比可靠）：负载前后取 `/proc/<PID>/stat` 字段 14+15 差分 ÷100 得 CPU-秒，再除以处理数据量得 CPU-秒/GiB。3FS 实测 **108.69 → 102.09 CPU-s（-6.1%）**，1.6983 → 1.5952 CPU-s/GiB。**测量窗口必须干净**：第一次测出 -3.5%，原因是测试脚本在窗口内 `rm -rf` 了上一轮 64 GiB 数据（删除/punch-hole/RocksDB 后台的 CPU 被算进两腿）；修正为先删 → `sleep 30` 等落定 → 再取快照。

5c. **火焰图前后对比**：同口径重采（`scripts/get_flame.sh` 同参数），确认目标符号占比归零/下降、替代符号占比符合预期（注意局部标签吃样本，按关键词族求和）。folded 权重是 perf period 不是样本数，**只能比相对占比，不能把占比差换算成绝对时间**。

5d. **A/B 交错多轮取中位**，除目标变量外全同（同二进制构建配置、同 `--size`、同 numjobs、同 chain/副本拓扑）。两条已踩过的坑：`--size=512m` 是瞬态峰值、`--size=2g` 是稳态（同配置 12380 vs 10068 MiB/s，**对比时尺寸必须一致**）；profiling 本身会压负载（**带宽数字必须用不带 profiling 的干净跑**）。fio 的 `$jobnum` 必须用单引号（双引号被 shell 展开，32 个 job 全写同一个文件，产出 556 MiB/s / io=4096MiB 的假结果）；USRBIO per-job 行的 `bw avg` 可能是报表 artifact（~2046），**以汇总行 `WRITE: bw=` 为准**。

5e. **写路径指标前后对比**：用 `scripts/run_fio_seq_write_ck.sh` 同口径重跑，对照 1f 的指标地图确认目标环节（如 `storage.engine_update`）延迟下降、其它环节无劣化。3FS 消融实验的 `engine_update` 526 → 311 µs（-41%）就是这一层证据。

5f. **微基准 vs in-situ 差距必须写进报告**：3FS 微基准 1.57~1.90x，in-situ 只有 **1.13~1.23x**（差距来自冷散缓冲与多线程 DDR 争用；两种推算：火焰图占比 31.99%/28.36%=1.13x，CPU-秒 34.8→28.2=1.23x）。只报微基准数字属于夸大。**引用历史测量数据时核对它描述的实现是否仍然有效。**

5g. **无收益即回滚记账**：端到端收益在噪声带内（<2~3% 或中位差小于轮间波动）且 `optimization_goal` 不是 CPU-秒 → 回滚，写入「反证台账速查」（手段/结果/根因/启示）。

5h. **剩余热点复测**：每轮落地后重跑阶段 1 的关键采集确认新瓶颈，但**要先判断剩余热点是"可优化"还是"架构下限"**（如 `crc32c_combine` 的 7.56%→8.10%，见 4c）。

**阶段输出**：持续压测结论（chunk 数 / 时长 / 崩溃数）+ CPU-秒 A/B 表（含干净窗口声明）+ 火焰图前后占比对照 + 写路径指标前后对照 + 端到端指标 A/B + 微基准/in-situ 差距说明 + 采纳或回滚结论 + 剩余热点（含"架构下限"判定）。

## 测量纪律

- **判据先定**：`optimization_goal` 在阶段 0 就固定，事后不得换判据。CPU-秒、端到端带宽、单请求延迟三者结论可能相反。
- **硬件前置检查**：跨集群/跨机器对比先比盘型号、固件（`nvme id-ctrl`）、内核版本、网卡型号，再谈软件（见 2b/2c）。
- **稳定性先于速度**：候选实现未过大输入 + 长时间压测，不得进入性能对比。
- **占比必换算**：任何火焰图百分比都要乘以进程实际核数才有意义；进程核数以 `/proc/<PID>/stat` 14+15 差分为准。
- **同符号求和**：多线程下同一符号出现在多条栈里，聚合必须求和，取最大值会低估一个数量级。
- **窗口干净**：测量窗口内不得混入数据清理、compaction、预热；删除后留落定时间。ClickHouse 查询窗口按 1f 做时钟偏移校正。
- **profiling 与测带宽分离**：采集火焰图的跑次不用于带宽结论。
- **单变量**：A/B 两腿只差目标变量；二进制要 md5/sha256 核对防止拿错构建；配置面要同 ConfigVersion 快照，防止配置差异混入代码收益。
- **交错多轮取中位**，绝对值可能整体漂移。
- **正确性优先**：性能异常好先怀疑正确性（校验被跳过、数据被缓存、负载没真跑）。**结果异常"正确"也要怀疑** —— combine 空桩返回 0 时不崩不报错，只有差分测试能抓出来。
- **均值会掩盖长尾**：延迟类结论必须看分位数与直方图（fdatasync 案例 p50 相同、p99 差 10~15 倍）。

## 反证台账速查（实施前逐条对照）

已证伪的方向、静默失效与归因错误。**确认待做项与已证伪项机制不同，而不是换个写法重试同一个方向。**

**静默失效与归因类**：

| 手段/现象 | 真相与防护 |
|---|---|
| cherry-pick 上游 16 个 NEON fast-crc32 commit，A/B 纯噪声（1462 vs 1428 MiB/s） | 编译期宏门控的 intrinsic 路径对全局 `-march` 敏感（648 个文件拿到 `+crc`、308 个 folly 文件没有），且 `FOLLY_ARM_FEATURE_CRYPTO/SHA3` 宏从未 define → 全部编成 `mov w0, wzr; ret` 桩。防护：`.arch_extension crc` 就地开启 + `verify_hw_path.sh` 核验。"加了硬件加速但没测出收益"的第一反应是核验二进制，不是怀疑测量方法 |
| cherry-pick 按性能标题挑选 commit | 漏了纯重构 commit（头文件搬迁 `AuxVector.h`）→ 编译失败。**头文件搬迁/重构类 commit 必须一起带上** |
| 优化分支基于上游 tag 开 | 缺项目本地 patch（ConcurrentHashMap/xlog 修复）→ 构建失败。**从"当前生产在用的 commit"开分支** |
| 只实现最高档 ISA（SVE2）| 920 上谓词恒 false → **静默退回软件查表，无任何报错**。每一档都必须验证"会命中且命中后不崩" |
| 启用硬件路径后 combine 结果错 | 共用谓词的 `crc32c_combine_hw` 依赖 x86 PCLMUL，非 SSE 平台是 `return 0` 空桩 → **数据损坏级静默故障**。防护：4c 连带审计 + combine 专项差分 |
| A/B 两腿二进制不一致 | 拿错构建使归因整体失效。防护：两腿核对 md5/sha256 并记录构建目录 |
| 配置面未固定就做代码 A/B | 配置差异（如 enableChecksum 开关）会淹没或伪装代码收益。防护：归因前 `get-config` 快照，两腿同 ConfigVersion |
| 优化面与热点面不重合 | folly 侧优化碰不到 Rust 侧算的 payload CRC（12.5% CPU）；serde 头部 CRC 只占 0.25%；客户端 CRC 不出现在 storage 火焰图上。**动手前枚举所有调用点、标注进程与占比** |
| 把硬件问题当算法问题 | B 集群 CRC 占比高是"其它环节更慢"的相对效应；重建全部 target 白做。防护：2b 硬件前置检查 + CK 计数类对齐 + 裸盘对照 |
| 部署"部分服务"做消融 | 校验协议不匹配 → FUSE 客户端崩溃 `RoutingInfoNotReady`。消融必须全集群同分支 |

**测量与口径类**：

| 错误 | 后果 → 正确做法 |
|---|---|
| 同符号取单帧最大值 | 0.81% vs 真值 24.56%（差 30 倍）→ 按叶子聚合求和 |
| 用非加权栈计数 | DWARF 下 0.31% 完全无意义 → 用 perf period 加权值 |
| 两次采集占比差换算绝对时间 | 总权重可差 43% → 只比相对占比，绝对量用 CPU-秒 |
| 按线程名聚合 `/proc/*/stat` | 143.78 核 vs 真值 3.77 核 → 按 PID 读字段 14+15 差分 |
| 用整机 `top` idle 做 A/B | 含 perf 自身开销 → 用目标进程 CPU-秒 |
| 跨图直接比全图百分比 | 样本量基准不同 → 子树归一化（除以同名线程根） |
| 父帧 inclusive + 子帧 inclusive 相加 | 重复计数（父含子）→ 不可加 |
| 只看符号名判断实现 | rustc 版本重构符号形态 → 看 build-id/反汇编 |
| 只看函数名查"热点消失" | 样本落在局部标签 `.align_finish` → 按关键词族求和 |
| 测量窗口内 `rm -rf` 上轮数据 | -6.1% 测成 -3.5% → 先删、sleep 30 落定、再取快照 |
| fio `--filename_format` 用双引号 | `$jobnum` 被 shell 展开，32 job 写同一文件出假结果 → 单引号 |
| 标称负载与实际不符 | "顺序读"采集里一个读帧都没有 → 采集前核对业务符号/线程名/请求计数 |
| `--size` 口径不一致 | 512m 瞬态 12380 vs 2g 稳态 10068 MiB/s → 对比时尺寸一致 |
| fio 版本不一致（3.34 vs 3.42） | fallocate/laying-out 行为不同污染对比 → 同版本同构建，否则只信 python 对照 |
| 读测试预写入尺寸不匹配 | 结果无效 → 预写入大小与读测试 `--size` 一致 |

**配置/拓扑/环境类速查**：

| 项 | 结论 |
|---|---|
| `chunk_checksum_type='NONE'` | **写全部失败**，不是可用开关（服务端仍重算、meta 无类型字段、HEAD 比对 → `kChecksumMismatch`）；跳过校验只能走消融分支 |
| `traffic_control.write` `max_concurrent_requests=512` | FAIL `Bad file descriptor`；(256,64) 是上限 |
| 单纯增大 fio `numjobs` / `numjobs ≥ 128` | 无收益 / 劣化（瓶颈在 `batchWrite` 内部信号量：`waiting_time` 572 ms vs `server_latency` 4 ms） |
| USRBIO `commit()` 改异步 | **-4%**（等待只是平移，每 job 仍一个 batch in-flight，CQE 映射成纯开销） |
| `iodepth_batch_complete_min/max` 8→1、`num_event_loop`/`max_connections` 增大、`rdma_listen_ethernet` 翻转 | 无可测影响 |
| 2 storage 下 HEAD 均匀分散（双向 RDMA 转发） | **灾难：41 MiB/s**（`forward timeout`、epoll 报错）；3 节点单向链式无此问题 |
| cargo 网络超时 | `cargo_build_all` 整体失败 → `--offline` 或配代理 |
| ClickHouse 查询全空 | 先查 `monitor_collector_main` 存活（曾死 472 分钟） |
| ClickHouse 时间窗口错位 / 百分位 `7.47e12` | 按节点算 `CK_OFFSET` 校正或窗口加容差；异常时基整列排除 |
| 节点时钟慢 34s | 超 `heartbeat_timestamp_valid_window='30s'` → `HeartbeatFail(5002)` → target 全标 OFFLINE → rotate 时 `RoutingVersionMismatch(7014)`。先 `chronyc makestep` 再重做 |
| FUSE 挂载点残留 `Transport endpoint is not connected` | 重启前必须先 umount |
| FUSE 配置热更新无效（`Update config ignored ... not hotupdatable`） | `set-config --type FUSE` 后必须 `systemctl restart hf3fs_fuse_main` |
| 旧文档硬件描述与实测不符 | 以 `ibv_devinfo`/`lspci`/`nvme id-ctrl` 实测为准，不要信旧文档 |
| 核对编译产物 flags 时用前缀白名单重拼命令 | 漏掉 `-O2` 编出 `-O0` 产物，反汇编结论全错且"看起来像真实缺陷" → **直接复用 `compile_commands.json` 完整 argv，只替换需要改的部分** |

## 降级处理

| 场景 | 降级动作 |
|------|---------|
| `perf` DWARF 不可用 / perf.data 过大 | 降到 `dwarf,4096 -F 49`，或改用 `perf record -e cycles:u` 只采用户态；标注栈完整性风险 |
| 无 bpftrace | `perf trace -p <PID> -e fdatasync,pwrite64 --duration 0.001 --summary` 取均值（无直方图，长尾不可见，结论标注低置信） |
| 无 `perfquery`（非 IB / 未装 infiniband-diags） | 读 `/sys/class/infiniband/<dev>/ports/<n>/counters/{port_xmit_data,port_rcv_data}` 差分，同样 ×4 换算字节；或以 ethtool -S 计数替代 |
| ClickHouse 监控库不可达（monitor_collector 挂了 / 无网络） | 阶段 1f 退化为 bpftrace syscall 直方图 + fio 汇总行；先查 `monitor_collector_main` 存活 |
| 不允许改代码做消融 | 阶段 2 退化为解析上界估算（占比 × 进程核数 ÷ 总核数），门控判定标注"上界未经实验验证" |
| ISA-L 类多路 asm 在目标平台确认崩溃 | 退回**标量 CRC32C 指令路径**（`crc32cw`/`crc32cb` 串行 + `.arch_extension crc`）。3FS 记录 5.19 GB/s，仍比软件查表快 ~16x（C 层证据）。**但先用验证套件在自己环境复现崩溃** —— 未复现就降级等于白丢 40% 吞吐。同时加运行时开关，让现场可回退而无需重新编译 |
| 目标平台无 CRC32 扩展 | checksum 子流程终止，转 `apply-vectorization` 或算法侧（增量校验/校验复用/降低校验粒度） |
| 无法做长时间压测 | 阶段 5a 降级为"大输入 × 高次数"循环 + ASAN/valgrind；报告须标注"未完成持续压测，asm 路径崩溃风险未排除" |
| 跨语言一致性无法验证 | 阶段 4 停止，不得上线；先补差分测试 |
| fdatasync 长尾确认在固件层 | 软件侧无化解空间（chunk_engine 硬编码 `sync=true`）→ 转硬件侧（统一固件）或配置侧（`sync_when_write`，牺牲掉电安全性，见 `3fs-parameter-tuning`），见 2c |

## 错误处理

工具与脚本层的常见故障及处置（证据质量类降级见上表，两者不混用）：

| 症状 | 处置 |
|------|------|
| `get_flame.sh` 报 `perf 未安装` | `yum install perf` 后重跑；离线环境按「降级处理」表降档采集并标注 |
| `get_flame.sh` 报 `未找到 ${PROC} 进程` | `ps aux | grep -E 'storage_main\|meta_main\|hf3fs_fuse_main'` 确认进程名与存活，`PROC=` 重跑 |
| FlameGraph 工具自动 clone 失败 | 按脚本提示手动 `git clone https://github.com/brendangregg/FlameGraph` 后以 `FLAMEGRAPH_DIR=` 指向重跑 |
| `perf record` 报 `doesn't support branch stack sampling` | lbr 在鲲鹏 920 PMU 不可用，改 `--call-graph dwarf,8192 -F 99 -m 2048`（1a） |
| perf.data 异常巨大 / `lost` 样本率高 | 压小栈 dump 与采样率（dwarf,65528 曾 7 秒产出 14.4GB、lost 32.43%），参数见 1a |
| `run_fio_seq_write_ck.sh` 报 `CK_PASS 未设置` / 远程模式缺 `CK_SSH_HOST` | `read -rs CK_PASS; export CK_PASS` 注入；远程模式指定可 SSH 的 CK 节点，SSH 优先 key 认证 |
| ClickHouse 预检 `SELECT 1` 失败 | 查端口 / 网络 / 密码注入方式；先确认 `monitor_collector_main` 存活 |
| 写路径查询结果为空或数字对不上 | 核对时钟偏移校正窗口与指标名版本（1f）；查 monitor_collector 的 `filter_list` 网卡名是否为本机实际名；FUSE 指标为空时把 IN 列表换成 `usrbio.piov.*` 族再查（1f 口径辨析） |
| `rdma_monitor.sh` 报缺 sshpass | `yum install -y sshpass`，或清空 `SSH_PASS` 改走 SSH key 认证 |
| ssh/scp 报主机密钥校验失败（accept-new 拒绝变更的密钥） | **不得降级 `StrictHostKeyChecking=no` 或 `UserKnownHostsFile=/dev/null` 绕过**：先查明原因（节点重装？伪造节点？），带外核对指纹后 `ssh-keygen -R <host>` 清旧键重连；期间该节点网络轴数据缺失必须写进归因结论 |
| `append_sync.py` 报"目标文件已存在" | O_EXCL 设计使然（防误清空/误删既有 WAL/数据文件）：换一个不存在的新测试文件名重跑 |
| `perfquery -x` 失败 | 脚本已自动回退普通计数器模式；非 IB 环境按「降级处理」表改读 `/sys/class/infiniband/*/counters` 或 `ethtool -S` |
| crc32c 核验程序 KAT 失败 / 档位不符 / 压测崩溃 | 不得放行发布：先按 4a/4c 定界取反约定与分派档位；x6 崩溃可临时 `FOLLY_CRC32C_SCALAR_ONLY=1` 兜底，但必须按「证据状态」在自己环境复现定论 |
| 火焰图符号占比明显偏低（差一个数量级） | 按 1b/「反证台账速查」的聚合口径错误排查（同符号取 max、丢中间帧、符号误读、局部标签吃样本） |

## 输出契约

优化报告（Markdown）固定结构：

```
# <集群/项目名> 3FS 热点优化报告
## 1. 目标与口径
   workload_cmd 回显、optimization_goal、平台能力探测结果（CPU/ISA/盘固件）、二进制 sha256
## 2. 取证
   函数级调用链 + 求和占比 + 绝对核数换算 + 假热点/符号误读排除记录
   延迟型问题：写路径指标分解表（FUSE 侧 + Storage 侧，含时钟偏移校正声明）
## 3. 天花板归因（门控）
   三轴余量表（网络利用率 / flush 延迟分布 / CPU 核数）
   消融上界数据 + 配置面快照 + 硬件/固件前置检查 + 门控判定与理由
## 4. 选型
   能力探测（含交叉验证）+ 稳定性门槛结论 + 微基准排序表
   + 现有实现退化根因（反汇编证据）+ 封闭轴声明
## 5. 落地
   约定映射式 + KAT + 差分验证 0/N（含 combine 专项）+ combine 连带审计清单
   + 跨语言一致性 + 长度降级 + verify_hw_path.sh 输出
## 6. 验收
   持续压测结论（chunk 数/时长/崩溃数）+ CPU-秒 A/B 表 + 火焰图前后占比对照
   + 写路径指标前后对照 + 端到端指标 A/B + 微基准/in-situ 差距
## 7. 反证台账
   手段 / 结果 / 根因 / 启示（含本次新增条目）
## 8. 剩余热点与下一步
   含"可优化"vs"架构下限"判定
```

所有数字标注证据层级（A/B/C）；争议声称（如未复现的崩溃报告）必须带「已报告、未复现、待定论」标注，不得写成事实。

## 脚本工具

| 脚本 | 用途与用法 |
|---|---|
| `scripts/get_flame.sh` | 火焰图一键采集（perf dwarf,8192 -F 99 -m 2048 + 每秒 top 基线 + stackcollapse + flamegraph SVG）。`bash get_flame.sh [duration] [subdir]`；`subdir` 仅允许单层目录名 `[A-Za-z0-9._-]+`（拒绝 `/`、`.`、`..`，rm -rf 前另做输出根包含性校验，防路径穿越）；env：`PROC`（默认 storage_main，可设 meta_main/hf3fs_fuse_main）、`FLAMEGRAPH_DIR`（缺失自动 clone）、`OUTPUT_DIR`。产物：`perf_*.data`、`*.folded`、`flamegraph_*.svg`、`cpu_stat_*.txt` |
| `scripts/rdma_monitor.sh` | RDMA 带宽监控**共享库**（真源：本 skill；`3fs-parameter-tuning` 的 fio_bench.sh 可选 source）。`source rdma_monitor.sh` 后 `start_rdma_monitor` / `stop_rdma_monitor <label>`（停止时打印每节点 TX/RX 峰值与均值 MB/s）。env：`REMOTE_NODES`（空格分隔 IP，可多节点并发）、`SSH_USER`（默认 root）、`SSH_PASS`（**无默认值**；留空走 SSH key 并强制 `PreferredAuthentications=publickey`，非空经 SSHPASS 走 `sshpass -e` 且打改用 key 告警）、`SSH_OPTS`（默认 `StrictHostKeyChecking=accept-new`，主机密钥变更拒连，勿改为 no）、`KNOWN_HOSTS`（可选，带外核对指纹后的预置 known_hosts）、`RDMA_MON_DEV`（默认 mlx5_0）、`RDMA_MON_PORT`（默认 1）。远端采样脚本用 mktemp 唯一路径（0700，防 /tmp 预置劫持）；perfquery 扩展计数器差分 ×4 换算，`-x` 失败自动回退普通模式；节点不可达/密钥校验失败时告警并计入"网络轴数据缺失"，全部失败返回非零 |
| `scripts/run_fio_seq_write_ck.sh` | 单 job 顺序写 + ClickHouse 写路径全链路耗时五段查询（FUSE 延迟/字节、Storage 22 项延迟按流水线排序、counters、每秒时间线），自动时钟偏移校正，结果 tee 落盘 `${OUT_DIR}/`。`read -rs CK_PASS; export CK_PASS; ./run_fio_seq_write_ck.sh`；env：`MOUNT_POINT`、`CPU`（taskset 绑核）、`SIZE`（默认 4g）、`BS`（默认 4M）、`FIO_BIN`、`USRBIO_SO`、`CK_PORT`（默认 9123）、`CK_PASS`（**无默认值**，`read -rs` 注入；本地经 `CLICKHOUSE_PASSWORD` 环境变量、远程经 ssh stdin 首行传递，**不进本地与远端 argv**）、`CK_SSH_HOST`/`CK_SSH_PASS`（本机无 clickhouse-client 时的远程模式；留空走 SSH key 并强制 publickey）、`CK_SSH_OPTS`（默认 `StrictHostKeyChecking=accept-new`）、`KNOWN_HOSTS`、`CK_SETTLE_SECS`（默认 10）。指标地图与判读见阶段 1f |
| `scripts/append_sync.py` | 裸盘 4K 追加写 + 每次 fdatasync（精确复现 RocksDB WAL 模式，绕开 3FS 与 fio 版本差异），输出 count/avg/p50/p90/p95/p99/p99.9/max（µs）。`python3 append_sync.py <file> [seconds]`（默认 30s；文件放在与 chunk_engine meta 相同的文件系统上）。**`<file>` 必须是不存在的新文件名**：O_EXCL 独占创建，已存在即报错退出（防误清空/误删既有 WAL/数据文件），结束只删除本次自建文件。纪律：每节点至少 3 次（个体波动大）、A/B 两边同一脚本、**判读看 p99/p99.9 比值不要只看 avg**（见 2c 判读标准表） |
| `scripts/verify_hw_path.sh` | 二进制硬件路径核验（4h 三项：加速符号存在 / 探测函数非常量桩 / 硬件指令落地），定向反汇编，数百 MB 带 debug_info 二进制可用。`bash verify_hw_path.sh <binary> [symbol_regex] [detect_fn_regex]`（默认 `'crc32_iscsi\|Crc32cIsaL\|crc32c_hw'` / `'supported\|_detect\|hwcap'`），任一 FAIL 即判定加速路径未生效 |
| `scripts/verify_crc32c_convention.c` | CRC32C 约定核验与差分验证（KAT 0xE3069283 + 4 种子 × 多长度差分 + 分块串联等价 + 65 个非对齐起点）。构建：`clang -O2 -Wall -o verify_crc32c_convention verify_crc32c_convention.c crc32_iscsi_x6.S -I<path/to/folly/external/include>`（`.S` 需能按 `../include/aarch64_label.h` 相对位置找到头文件）。验证其它实现改 `CANDIDATE_SYM`/`candidate_raw()`；约定不明时 `EXPECT_INVERT=0/1` 两组都跑即可确定实际约定。退出码：0 全过 / 1 有不一致 / 2 参数环境错误 |
| `scripts/probe_hwcap.c` | HWCAP 探测核验：逐常量报告 `#ifndef` 回退是否触发（fallback=0/1，含加固用的 `HWCAP_SVE`）+ 复现 folly 三个 `_supported()` 谓词的分派结果。SVE2 判定同时输出 as-written（folly 原文，无 SVE 交叉校验）与 hardened（加 `HWCAP_SVE` 交叉校验，SKILL.md 3f 加固①）两档，tiered 分派按 hardened 判定 —— "HWCAP2 谎报 SVE2 而 HWCAP 无 SVE"时显式打印 FALSE POSITIVE 警告。构建：`clang -O2 -o probe_hwcap probe_hwcap.c`（**刻意不加 `-march`**，复现 baseline armv8-a 构建场景）；`PROBE_HWCAP_EXPECT_SVE2=0/1` 可机器断言（不符退出 2）。用于「证据状态」中 SP3 内核复核 |
| `scripts/probe_hwcap_fake_sve2_test.c` | probe_hwcap 的伪造 CPU 回归用例：`-Wl,--wrap=getauxval` 链接期拦截，伪造「HWCAP2 声称 SVE2+SVEPMULL+SVESHA3、HWCAP 有 ASIMD/PMULL/CRC32 但无 SVE」的安全关键组合，断言加固谓词否决 SVE2、分派落 PMULL。构建：`clang -O2 -o probe_hwcap_fake_sve2_test probe_hwcap_fake_sve2_test.c -Wl,--wrap=getauxval`；运行：`PROBE_HWCAP_EXPECT_SVE2=0 ./probe_hwcap_fake_sve2_test`（退出 0 = 通过，2 = 谓词假阳性回归失败）。可在任意架构编译运行（getauxval 已被整体拦截） |
| `scripts/stress_x6.c` | ISA-L PMULL 内核稳定性压测：直压 `crc32_iscsi_x6`，装 SIGSEGV/SIGILL/SIGBUS handler + setjmp 恢复，崩溃时打印 len/off/seed；34 档长度（精确跨越 1023/1024/1025/1026、63/64/65、1MB±、4MB± 门槛）× 17 种对齐（含跨页 4095/4096/4097）× 5 种子，每次对软件查表参考实现校验。构建：`clang -O2 -o stress_x6 stress_x6.c crc32_iscsi_x6.S -I<include dir>`；运行：`./stress_x6 [rounds]`（默认 20 轮 = 57500 次调用），判读 `crashes: 0` 且 `mismatches: 0` |

## 协作

- 编译部署、二进制上集群、替换备份与 md5 记录、服务停启顺序：`3fs-deployment-installation` skill（app-tuner 插件）
- 配置面杠杆（enableChecksum / sync_when_write / traffic_control 等）、配置快照与热更新、标准压测 harness：`3fs-parameter-tuning` skill（app-tuner 插件）—— **归因前先固定配置面并快照**
- 鲲鹏微架构参数（IPC 校准/指令延迟）：`kunpeng_microarch` skill
- 计算侧静态流水线仿真：`llvm-mca-analysis` skill
- 无 CRC32 扩展时的向量化替代：`apply-vectorization` skill
