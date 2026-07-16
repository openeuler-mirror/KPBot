# 0xd06 芯片架构

## 芯片概览

| 属性 | 值 |
|------|------|
| 型号 | Kunpeng-0xd06 |
| 架构 | ARMv8 (AArch64) |
| 系统模式 | 64-bit, Little Endian |
| Socket数量 | 1 |
| 物理核心数 | 192个 |
| 逻辑线程数 | 384个 |
| 线程/核心 | 2 (SMT2/多线程) |
| 核心/Socket | 192 |
| 频率范围 | 1.2GHz - 2.3GHz |
| Vendor | HiSilicon |
| NUMA节点 | 4个 |

## CPU核心架构

0xd03是0xd06的ARMv8-A处理器核心，采用超标量乱序执行流水线，支持SMT2同时多线程架构。

### 核心基础配置

| 属性 | 值 |
|------|------|
| 架构 | ARMv8-A (V9.2+ 兼容) |
| 执行状态 | 仅 AArch64 |
| 异常级别 | EL0, EL1, EL2, EL3 |
| 安全世界 | Secure + Non-Secure 支持 |
| 地址空间 | 48位PA (0xd06配置) |

### 流水线资源

0xd03采用9个执行流水线，支持超标量乱序执行。

| 流水线名称 | 符号 | 支持的uOPs |
|-----------|------|-------------|
| Branch 0/1 | B | 分支uOPs |
| Integer Single-Cycle (ALU0134) | ALU | 单周期整数ALU uOPs (6个简单单元) |
| Integer Single-Cycle (ALU14) | ALU | 单周期整数ALU (流水线1/4) |
| Integer Single/Multi-Cycle (ALU25) | ALU | 单/多周期整数ALU (2个复合单元，含1个除法) |
| Integer Single-Cycle (ALU1425) | ALU | 单周期(1/4)或多周期(2/5)ALU |
| FP/ASIMD/SVE 0/1/2/3 | V | ASIMD/FP/SVE运算 |
| FP/ASIMD/SVE 0/2 | V02 | ASIMD/FP/SVEuOPs (SVE128) |
| Load 0/1/3 | LD | 加载uOPs |
| Store 0/1 | ST | 存储地址uOPs |
| Store data 0/1 | STD | 存储数据uOPs |

### SMT资源配置

每核心支持2个SMT线程，硬件资源共享策略如下：

| 资源 | 共享策略 | 说明 |
|--------|---------|------|
| Fetch pipeline | Shared | 两个线程共享指令获取 |
| Instruction queue | Private | 每线程独立指令队列 |
| Decode | Shared | 两个线程共享指令译码 |
| Rename | Shared | 两个线程共享指令重命名 |
| Reorder Buffer | Partitioned | 每线程占用一半 |
| Issue Queue | Shared | 两个线程共享发射队列 |
| Execution Unit | Shared | 两个线程共享执行单元 |
| Mpq | Partitioned | 从队列分区 |
| Regfile | Shared | 两个线程共享寄存器文件 |
| L1 ICache/L1 DCache | Shared | 两个线程共享L1缓存 |
| TLB | Shared | 两个线程共享TLB |

### 前端流水线结构 (Front-End)

| 组件 | 配置/参数 |
|------|-----------|
| **指令获取 (Fetch)** |
| - Fetch Width (指令数) | 8指令/cycle |
| - Instruction Prefetch Engine | 支持 |
| - Mop Cache | 支持 |

**指令译码 (Decode)：**
- Decode width (译码宽度): 8指令

**分支预测单元：**
| 组件 | 配置 |
|------|------|
| Branch Prediction Width | 支持 |
| Nano BTB | 0周期 taken-branch bubble (零周期条件分支气泡) |
| 条件分支方向状态 | 支持 |
| Main BTB (主分支目标缓冲区) | 12K |
| Alt-Path Branch Prediction | 支持替代路径分支预测 |

### 后端流水线结构 (Backend)

| 组件 | 配置/参数 |
|------|-----------|
| **指令重命名 (Rename)** |
| - Rename width | 支持 |
| - Rename Checkpointing | 支持 |
| - ROB (重排序缓冲区) size | 192 entries |
| - Branch resolution | 支持 |
| - Overall Pipeline Depth | 支持 |

### 浮点和向量单元

**执行能力：**
- 浮点/SIMD：每周期并行4条指令 + 2条额外存储指令
- SVE256：每周期2条 + 2条额外存储指令
- SVE128：每周期4条 + 2条额外存储指令

**支持的指令集扩展：**
- ARMv8.0: AES, SHA1, SHA256, PMULL
- ARMv8.2: FP16, DotProd, FHM, SHA512, SHA3, SM3, SM4, RAS, SPE, SVE
- ARMv8.6: Bfloat16
- ARMv9.0: SVE2, SVE_BitPerm, SVE_AES/PMULL/SM4/SHA3, ETE, TRBE

## 硬件数据预取引擎

0xd03实现5种类型的硬件数据预取器，支持L1 DCache、L2 Cache和L3 Cache系统，通过模式预测提前发送内存访问请求以掩盖内存访问延迟。

| 预取器类型 | 说明 | 目标缓存层级 |
|-------------|------|-------------|
| BOF (Next Line) | Next Line预取器增强版 | 仅L2 |
| SEQ | 流式内存访问预取器 | L1, L2, L3 |
| SMS (Spatial Memory) | 空间内存预取器（捕获不规则区域访问） | 仅L2 |
| MOP (Multi-Offset) | 多偏移预取器（捕获规律性步长） | L1, L2, L3 |
| META | 链表结构预取器 | L1, L2 |

**预取器特性：**
- 所有预取器通过Load-Store Unit和L2C观察到的虚拟地址进行训练
- 支持自适应深度方案：自适应深度控制SEQ和MOP预取器的访问前向范围
- 支持自适应精度方案：根据预取准确性动态调整激进程度
- 支持自适应带宽方案：根据L3和DDR的带宽占用情况限制预取激进度

## 缓存层次结构

### Cache层级架构汇总

| 层级 | 总容量 | 实例数 | 单个容量 | 缓存类型 | 共享核心数 | 关联度 |
|------|--------|--------|---------|---------|-----------|--------|
| L1d | 12 MiB | 192 | 64 KB | Data | 2 | - |
| L1i | 24 MiB | 192 | 128 KB | Instruction | 2 | - |
| L2 | 192 MiB | 192 | 1,024 KB (1 MB) | Unified | 2 | - |
| L3 | 546 MiB | 24 | 23,296 KB (约23 MB) | Unified | 16物理核心 (32线程) | 19-way |

### L1数据缓存

| 属性 | 值 |
|------|------|
| 总容量 | 12 MiB |
| 实例数 | 192个 (每核心1个) |
| 单个容量 | 64 KB |
| 组织方式 | 4路组相联 |
| 缓存行大小 | 64 字节 |
| 标记方式 | VIPT (Virtual Index Physical Tag) |
| 替换策略 | LRU |
| ECC | SECDED保护 |
| 流水线宽度 | 3条加载 + 2条存储 (3×32B / 2×32B per cycle) |
| Load Inflight Queue | 48条目 |
| Load Hit Queue | 120条目 |
| Store Buffer | 48条目 |

### L1指令缓存

| 属性 | 值 |
|------|------|
| 总容量 | 24 MiB |
| 实例数 | 192个 (每核心1个) |
| 单个容量 | 128 KB |
| 组织方式 | 4路组相联 |
| 缓存行大小 | 64 字节 |
| 标记方式 | PIPT (Physically Indexed and Physically Tagged) |
| 替换策略 | LRU |
| ECC | Parity保护 |

### L2缓存

| 属性 | 值 |
|------|------|
| 总容量 | 192 MiB |
| 实例数 | 192个 (每核心1个) |
| 单个容量 | 1,024 KB (1 MB) |
| 组织方式 | 8路组相联 |
| 缓存行大小 | 64 字节 |
| 标记方式 | 物理索引和物理标记 |
| 替换策略 | 动态重参考插入策略 (DRRIP) |
| ECC | SECDED保护 |
| 互连性能 | 512位宽CHI-F接口 |

**L2缓存特性：**
- CPU私有缓存，紧耦合
- 与L1数据缓存严格包含
- 包含性数据缓存目录副本（用于数据一致性）
- L1指令缓存目录副本（用于指令一致性）

### L3缓存 (Last Level Cache)

| 属性 | 值 |
|------|------|
| 总容量 | 546 MiB |
| 实例数 | 24个 |
| 单个容量 | 23,296 KB (约23 MB) |
| 缓存行大小 | 64 字节 |
| 关联度 | 19-way associative |
| 缓存类型 | Unified (指令和数据共享) |
| 运行模式 | Write-Back (类) |
| 操作模式 | 随内存地址变化 |
| 位置 | Internal (CPU内部集成) |
| SRAM类型 | Synchronous (同步SRAM) |
| 速度 | 与CPU核心时钟同步 |
| 纠错类型 | Single-bit ECC (单比特纠错) |
| 配置状态 | Enabled, Not Socketed |
| 每实例共享 | 16个物理核心 (32线程) |
| 实例分组 | CPU 0-15, 16-31, ..., 分为24组 |

**L3缓存详细特性 (DMI报告)：**
- 关联度：19路自定义关联方式 (DMI: Other)
- 最大配置容量：546 MB (已装满)
- 高关联度设计：相比8/16路关联，显著降低cache conflict miss，提高命中率

### Cache层级设计分析

**分层设计特点：**
- **L1分离架构**: L1d (64KB) 和 L1i (128KB) 独立，减少数据和指令访存冲突
- **L2统一缓存**: 每核心独占1MB L2，平衡指令/数据共享需求
- **L3大容量共享**: 546MB超大容量LLC，24实例分组，每组16核心共享

## NUMA拓扑

### NUMA节点配置

| NUMA节点 | CPU编号 | 核心数 | 说明 |
|----------|--------|--------|------|
| Node 0 | 0-95 | 96 | 第一NUMA节点 |
| Node 1 | 96-191 | 96 | 第二NUMA节点 |
| Node 2 | 192-287 | 96 | 第三NUMA节点 |
| Node 3 | 288-383 | 96 | 第四NUMA节点 |

**跨NUMA访问特性：**
- Node0/Node1 可共享部分L3分组
- Node2/Node3 可共享部分L3分组
- 跨NUMA访问会穿透多个L3层级，增加延迟

### NUMA性能优化建议

- **本地NUMA节点优化**: 将高通信线程绑定在同一NUMA节点内
- **L3组内优化**: 更高性能场景下，将通信密集型任务绑定在同一L3分组内（16核心=32线程内）
- **避免跨NODE访问**: 尽量减少跨NUMA节点的内存访问，以最小化访问延迟

## ARM架构特性支持

| 架构版本 | 关键特性支持 |
|----------|-------------|
| ARMv8.1 | LSE, RDMA, LOR, HPD, TTHM, PAN, VMID16, VHE, PMUv1 |
| ARMv8.2 | FP16, DotProd, FHM, SHA512/SHA3/SM3/SM4, RAS, SPE, SVE |
| ARMv8.3 | CompNum, JSConv, RCpc, CCIDX, PAC |
| ARMv8.4 | DIT, CondM, LSE, RCpc增强, TLBI, TTL, S2FWB, TTST, TTRem, SecEL2, IDST, Debug, Trace, PMU, RAS, DFE, 活动监控, 内存分区监控 |
| ARMv8.5 | CondM, FRINT, GTG, CSV2/CSV3/SSBS/SB, 预测无效化, 分支目标识别, 随机数生成器, PMU扩展 |
| ARMv9.0 | SVE2, SVE_BitPerm, SVE_AES/PMULL/SM4/SHA3, ETE, TRBE |
| ARMv8.6/9.1 | ECV, FGT, DGH, TWED, AMUv1p1, PAuth2, BF16, MTPMU, FPAC, I8MM, ETEv1p1 |
| ARMv8.7/9.2 | XS, HCX, WFXT, PAN3, ETS, LS64, AFP, RPRES, PMUv3p7, SPEv1p2, ETEv1p2, BRBE, RME |
| ARMv8.8/9.3 | NMI, HBC, PACQARMA3/CONSTPACFIELD, RNG_TRAP, TIDCP1, CMOW, Debugv8p8, PMUv3p8/TH, HPMN0, SPEv1p3, BRBEv1p1, ETS2 |
| 高于8.8/9.3 | TCR2, HAFT, RPRFM, LRCPC3, HDBSS |

## 系统接口与互联

### 互连性能

| 属性 | 规格 |
|------|------|
| 互连协议 | AMBA 5 CHI-E |
| CHI数据宽度 | 512位 |
| 时钟比例 | 与处理器时钟1:1或N:1整数倍 |
| 流控方式 | C版本：valid-ready握手 / T版本：链层信用计数器 |
| DVM事务能力 | C版本：最多6个 / T版本：最多4个 |
| LPID分配 | 显示请求来源线程（SMT环境下识别线程） |

