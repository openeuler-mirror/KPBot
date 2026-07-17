---
name: performance-library-selection
description: 根据热点函数、perf 与 BPF 证据推荐高性能库或替代实现，例如 malloc、memcpy、压缩或加密库，作为 kpbot-app-tuner 的统一高性能库选型入口使用。在 aarch64 场景下可条件引用外部 library-replacement skill，否则回退到内部通用路径。
---

# Performance Library Selection

当热点函数表明标准库或当前依赖库存在性能瓶颈时，使用本子 skill。

本子 skill 是统一入口：

- 通用高性能库替换分析入口
- 外部 `library-replacement` 的 ARM64 适配层
- 平台判断、依赖检查和回退决策中心

## External Integration

当前支持按条件引用外部 skill：

- 默认优先路径：`ref-skills/library-replacement`
- 入口文件：`ref-skills/library-replacement/SKILL.md`
- 外部兼容 fallback：已移除硬编码绝对路径；仓库内路径不可用时回退到内部通用路径，无需用户手动指定

外部 skill 当前约束：

- 仅支持 `aarch64`
- 依赖 `optimization_kb.json`
- 只读分析，不执行任何系统变更
- 支持在线分析和离线闭环分析两种模式

## Routing Rules

优先走外部 `library-replacement` 的条件：

1. 当前架构是 `aarch64`
2. 外部路径存在
3. `optimization_kb.json` 可被找到
4. 用户场景允许只读分析
5. 用户提供了 `target_pid` 或 `launch_command`，或接受仅做依赖检查后回退

如果任一关键条件不满足：

- 不中断主流程
- 记录 `fallback_reason`
- 回退到当前内部通用高性能库选型逻辑

## Internal Generic Focus

当外部路径不可用、平台不匹配或依赖不足时，继续使用内部通用分析路径。

### 分析规则

按热点函数类型判断替换方向。

#### 前置检查：malloc 热点是否值得替换

在评估任何 malloc 库替换之前，必须先从 perf 数据中提取 malloc 相关函数的 CPU 占比：

```bash
# 从 perf report 中提取 malloc 相关热点合计
perf report --stdio -i perf.data 2>/dev/null | grep -E 'malloc|free|calloc|realloc|__libc_malloc|_int_malloc|tc_malloc|je_malloc' | awk '{sum+=$1} END {print sum}'
```

决策规则：

| malloc 热点合计 | 决策 | 理由 |
|-----------------|------|------|
| < 2% | **跳过库替换**，不推荐任何 malloc 库变更 | 开销太低，替换收益不可测，只做 MALLOC_CONF 调优 |
| 2-5% | 检查当前链接状态后考虑 `MALLOC_CONF` 调优或轻量替换 | 可测量但需权衡代价 |
| > 5% | 进入完整 malloc 库选型流程 | 高收益预期，值得重编译或 LD_PRELOAD |

**注意**：clean-baseline 测试数据证实：perf 中 `malloc (libjemalloc.so.2)` 仅 0.57% → LD_PRELOAD jemalloc 收益为 -1.23%（负收益），验证了 < 2% 阈值规则的正确性。

#### 内存分配（malloc）选型决策树

```
perf malloc 热点 > 2%?
  ├─ 否 → 跳过库替换，仅检查 MALLOC_CONF
  └─ 是 → ldd /proc/<pid>/exe | grep -E 'jemalloc|tcmalloc'
           ├─ jemalloc 已链接 → 仅调 MALLOC_CONF，禁止 LD_PRELOAD 重注入
           ├─ tcmalloc 已链接 → 仅调 TCMALLOC 环境变量
           └─ 未链接任何优化分配器 → 进入选型
```

**选型标准**：

| 场景特征 | 推荐分配器 | 判断依据 |
|----------|-----------|----------|
| 通用服务、中小对象、线程数 < 32 | **jemalloc** | arena 自调节，碎片控制好，MySQL/PG 官方推荐 |
| 多线程高并发、大量小对象分配 (`< 256B`)、线程数 > 32 | **tcmalloc** | per-thread cache，小对象分配延迟更低 |
| aarch64 + 内存拷贝热点也高 | **jemalloc + libmem** | jemalloc 管理分配，libmem 加速 memcpy/memset |

**tcmalloc 判断线索**（perf 热点中出现以下信号时优先考虑）：
- 热点函数 `malloc`/`free` 调用频次 > 100K/s（`perf stat -e cache-misses` 确认）
- 大量 `__libc_malloc` 内部锁竞争（`perf annotate` 中 `pthread_mutex_lock` 在 malloc 路径上）
- 线程数 > 32 且 `pidstat -t` 显示多线程同时高频分配

**始终优先构建期集成**：
- jemalloc：`cmake -DWITH_JEMALLOC=system`（MySQL）或 `-Djemalloc_prefix=je_`
- tcmalloc：`cmake -DWITH_TCMALLOC=ON` 或 `LDFLAGS=-ltcmalloc`
- LD_PRELOAD 只在**完全无法重编译**时作为 fallback，且必须先确认进程未链接优化分配器

**LD_PRELOAD 避坑**：
- 若 `ldd /proc/<pid>/exe | grep jemalloc` 非空 → 禁止 `LD_PRELOAD=libjemalloc.so`，重复注入可能导致符号冲突、性能退化
- clean-baseline 数据点：mysqld 已被编译链接 jemalloc（构建期集成），再次 `LD_PRELOAD=libjemalloc.so.2` 导致 -1.23% TPS 退化
- 容器场景下 `LD_PRELOAD` 的 .so 必须在容器内可见，宿主机路径不可用

#### 内存拷贝（memcpy）替换

- 热点函数包含 `memcpy`/`memmove`/`memset` 占比 > 3%
- 检查当前 glibc 版本是否已包含优化实现
- aarch64 场景可考虑 libmem 中的优化版本：
  ```bash
  # 检查 glibc memcpy 是否已使用 SIMD
  objdump -d /usr/lib64/libc.so.6 | grep -A5 '<memcpy>:' | head -10
  # aarch64 上检查是否使用 NEON/SVE 指令
  perf annotate memcpy | grep -E 'ld1|st1|ldp|stp|lsl|asr' | head -10
  ```
- `LD_PRELOAD=libmem.so` 替换 memcpy/memset/memmove 实现，预期 3-8% 热点路径加速

**压缩/解压缩**
- 热点函数包含 `deflate`/`inflate`/`compress`
- 候选：zstd（高压缩比）、lz4（高速度）、isa-l（Intel 专用）

**加密/解密**
- 热点函数包含 `AES`/`SHA`/`RSA` 相关函数
- 候选：ISA-L、Kunpeng 加速引擎（aarch64）

**校验与序列化**
- 热点函数包含 CRC32/Adler32/XXHash
- 候选：isa-l CRC、xxHash

### 替换方式评估

| 方式 | 优势 | 风险 | 适用场景 |
|------|------|------|---------|
| 构建期集成 | 符号绑定可靠，无兼容风险 | 需要重编译 | 可重编译的应用 |
| `LD_PRELOAD` | 无需重编译 | 版本兼容性、容器挂载限制 | 无法重编译时的 fallback |

### 与编译器优化的协同

- 编译器 `-O3` 可能内联部分库调用，替换前需确认热点是否仍在库函数中
- PGO 采样后再做库替换可能更精准（热点已收敛）

## jemalloc MALLOC_CONF 预检测与调优

在推荐 jemalloc 运行时调优之前，必须先检查进程当前是否已配置 `MALLOC_CONF`：

```bash
# 检查当前进程的 MALLOC_CONF
cat /proc/<pid>/environ | tr '\0' '\n' | grep MALLOC_CONF
# 或
ps eww -p <pid> -o command | tr ' ' '\n' | grep MALLOC_CONF
```

检查 jemalloc 是否已链接：`ldd /proc/<pid>/exe | grep jemalloc` 或 `readelf -d /proc/<pid>/exe | grep jemalloc`

- 若已配置：基于当前值给出差异化建议，不要重新推荐同一条
- 若未配置：按默认未优化状态给出初始建议

### MALLOC_CONF 调优参数

当 jemalloc 已链接且 `MALLOC_CONF` 为空时，按以下顺序逐条验证：

| 参数 | 建议值 | 适用场景 | 预期效果 |
|------|--------|----------|----------|
| `background_thread:true` | 启用后台线程 | 多线程长运行进程，减少 arena 归零时的前台停顿 | 降低 P99 尾延迟 |
| `dirty_decay_ms:1000` | 1s（默认 10s） | 内存中对象生命周期短 | 更快释放 dirty page，降低 RSS |
| `muzzy_decay_ms:1000` | 1s（默认 10s） | 同上 | 更快释放 muzzy page |
| `metadata_thp:auto` | 自动大页 | aarch64 64K page，jemalloc ≥ 5.0 | 减少 TLB miss |
| `narenas:<n>` | 核数/2 或固定 4-8 | CPU 核数多时减少 arena 数量，避免多 arena 碎片 | 降低 RSS 波动 |
| `lg_tcache_max:16` | 16（默认 15=32KB） | 频繁分配 32-64KB 对象 | 减少大对象直接 mmap/munmap |

验证方式：

```bash
# 1. 追加 MALLOC_CONF 到启动环境
export MALLOC_CONF="background_thread:true,dirty_decay_ms:1000,muzzy_decay_ms:1000"

# 2. 重启 mysqld（必须重启，MALLOC_CONF 只在 jemalloc 初始化时读取）

# 3. 验证已生效
cat /proc/<pid>/environ | tr '\0' '\n' | grep MALLOC_CONF

# 4. 压测对比 TPS/RSS/P99
```

**每次只改一个参数**，逐个验证收益，避免多参数混淆归因。负收益时回退该参数，保留正向参数进入下一轮。

## Dependencies

| 工具 | 用途 | 缺失影响 |
|------|------|---------|
| `perf` | 热点函数分析 | 无法定位库级别热点 |
| `readelf` | 检查当前链接库 | 无法判断是否已使用优化库 |
| `ldd` | 检查动态链接依赖 | 依赖分析降级 |
| `lsof` | 检查运行时加载的库 | 运行时分析降级 |

## Recommended Inputs

- `architecture`
- `target_pid`
- `launch_command`
- `prefer_external_library_replacement`
- `optimization_kb_path`
- `external_skill_paths`
- `perf_available`
- `current_malloc_conf` — 从 `/proc/<pid>/environ` 获取的当前 MALLOC_CONF 值（若已配置 jemalloc）

## Expected Outputs

- `library_selection_mode`
  - `external_arm_library_replacement`
  - `internal_generic_library_selection`
- `external_skill_used`
- `external_skill_path`
- `external_skill_dependency_status`
- `external_skill_installation_status`
- `library_replacement_findings`
- `fallback_reason`
- `next_install_steps`

输出应包括替换建议、证据链、兼容性风险、验证方法、与编译器优化的协同关系，以及是否启用了外部 ARM 专项 skill。

在迭代编排语义下，本子 skill 还应明确：

- 性能库替换是否应进入当前轮优先动作
- 哪些库替换建议应作为下一轮候选动作继续保留
- 若证据不足或兼容性风险过高，是否应在当前轮暂缓

## Candidate Action Contract

每个 `candidate_actions[]` 必须包含 `action_id`、`action_type`、`precondition`、`commands_dry_run`、`commands_execute`、`expected_gain`、`risk`、`validation`、`rollback`、`stop_or_reject_condition` 和 `evidence_sources`。LD_PRELOAD、重新链接、替换 allocator/string/crypto/compression 库和运行时环境变量调整必须在 rollback 中包含恢复原启动环境、库路径、二进制链接关系和目标实例身份复核步骤。
