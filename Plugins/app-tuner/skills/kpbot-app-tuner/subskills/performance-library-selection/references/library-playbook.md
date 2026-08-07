# Performance Library Playbook

本 playbook 是 `performance-library-selection` 子 skill 的操作经验库，覆盖各候选库的安装、LD_PRELOAD 接入、运行时调优、推荐规则细则与验证方法学。主 SKILL.md 仅保留核心步骤流程与关键方案，详细操作查阅本文件。

## 证据 + 经验推荐规则细则

推荐结论可基于：① 现场热点证据 ② 经验库匹配。两者并非串联门控，而是组合置信度——证据+经验已形成明确判断时可直接给结论。真实验证由主框架执行阶段按验证流程设计落地，本子 skill 不执行真实验证；以下分类用于指导主框架执行阶段的验证优先级。

### 推荐结论的生成依据

| 依据来源 | 可独立给出结论 | 适用场景 | 置信度标注 |
|----------|----------------|----------|------------|
| 现场热点证据 + 经验匹配 | 可 | malloc+memcpy 合计 >2% 且热点函数与候选库匹配 | `confidence: medium/high`（按阈值） |
| 仅经验匹配（证据不足） | 可 | 热点<2% 但场景经验明确（如 aarch64+memcpy→stringlib） | `confidence: experience_only` |
| 仅现场证据（无经验匹配） | 可 | 出现未知的第三方 .so 热点，无经验库对应 | `confidence: evidence_only`，建议主框架验证后定论 |
| 主框架实测验证 | 叠加提升 | 主框架执行阶段按验证流程设计落地后回写 | 升级为 `confidence: measured` |
| 无证据 + 经验不匹配 | 可（not_recommended） | 热点无相关库、架构不匹配、已链接同类型库 | `confidence: experience_only` |

### 可直接给出结论的合法情形（无需主框架验证）

以下情形**可基于证据+经验直接给出结论，无需主框架执行验证**：

| 情形 | 直接结论 | 依据 |
|------|----------|------|
| malloc+memcpy 合计 <2% 且场景为纯计算密集型（NPU 推理、GPU 训练） | `not_recommended`（低优先级） | 证据不足 + 经验显示 host 侧优化收益微小 |
| memcpy 热点 <3% 且无相关 DSO 加载 | `not_recommended`（低优先级） | 证据不足 + 经验不匹配 |
| 进程已链接 jemalloc/tcmalloc | `not_recommended`（重复 LD_PRELOAD） | 经验：重复注入导致符号冲突/退化 |
| 架构不匹配（x86 推荐 bisheng-stringlib） | `not_recommended` | 经验：bisheng-stringlib 仅 aarch64 |
| 候选库无法安装且无法源码编译 | `inconclusive` | 客观条件限制，非结论判断 |
| 文档/历史推荐某库但当前场景无相关热点 | `not_recommended`（低优先级） | 证据不匹配 + 经验优先级低 |

### 建议主框架优先验证的情形

以下情形建议在验证流程设计中标注为主框架执行阶段的优先验证项：

| 情形 | 建议验证理由 | 主框架验证后回写 |
|------|--------------|------------------|
| malloc+memcpy 合计 >5% 且候选库匹配 | 高证据，验证可量化具体收益百分比 | `confidence: measured` |
| memcpy 热点 >3% 且 aarch64 | stringlib 预期 3-8% 加速，验证可确认 | `confidence: measured` |
| Ascend 场景 + CANN DSO 高占比 | CANN 内部隐含 malloc，验证可确认 host 侧收益 | `confidence: measured` |
| 用户明确要求量化收益 | 用户需求驱动 | `confidence: measured` |

### 验证产出格式

> 以下 JSON 模板分两类：**本子 skill 产出模板**（恒为 `not_verified`）和 **主框架回写模板**（验证后回写 `verified` + `measured`）。本子 skill 只产出前者，后者由主框架执行阶段按验证流程设计落地后回写。

**本子 skill 产出 — 未验证但已给出结论的库**（基于证据+经验）：

```json
{
  "library_name": "jemalloc",
  "verification_status": "not_verified",
  "confidence": "medium|low|experience_only|evidence_only",
  "recommendation": "recommended|not_recommended",
  "reason": "本子 skill 不执行真实验证，建议主框架执行 N 次基准对比；malloc 热点 3.2% + 多线程场景经验匹配（推荐，未量化）/ malloc 热点 0.8% 纯计算场景，经验显示收益微小（不推荐）",
  "evidence_sources": ["perf report malloc 热点 0.8%", "场景经验：NPU 推理 host 侧 malloc 优化收益低"]
}
```

**本子 skill 产出 — 无法安装/编译的库**：

```json
{
  "library_name": "jemalloc",
  "verification_status": "not_verified",
  "confidence": "experience_only",
  "installation_feasibility": "not_feasible",
  "installation_check_error": "通过只读检查（yum list available / 源码仓库可达性）判定无法安装的具体原因",
  "recommendation": "inconclusive",
  "reason": "客观条件限制（通过只读检查判定无法安装到容器且无法源码编译），无法验证，不构成推荐或不推荐结论"
}
```

**主框架回写模板 — 验证后的库**（主框架执行阶段按验证流程设计落地后回写）：

```json
{
  "library_name": "tcmalloc",
  "verification_status": "verified",
  "confidence": "measured",
  "baseline_samples": {"tps": [v1, v2], "latency_p99": [v1, v2]},
  "optimized_samples": {"tps": [v1, v2, v3], "latency_p99": [v1, v2, v3]},
  "baseline_median": {"tps": v, "latency_p99": v},
  "optimized_median": {"tps": v, "latency_p99": v},
  "gain_pct": {"tps": x.x, "latency_p99": x.x},
  "recommendation": "recommended|not_recommended",
  "reason": "实测收益 X%（推荐）/ 实测负收益 Y%（不推荐）"
}
```

> metric key 命名规范：按验证场景选择业务关键指标，常用 key 如 `tps`（吞吐）、`latency_p99`（尾延迟）、`rss_mb`（内存）、`cpu_pct`（CPU 占比）。同一库的多轮验证须保持 metric key 一致。

### 基准对比方法学（主框架执行阶段适用）

1. **基线对照**：每次验证库替换前，必须先运行 ≥1 次基线（无库替换），确认当前环境基线稳定
2. **运行次数**：按证据等级决定（低证据 ≥1 次、中证据 ≥2 次、高证据 ≥3 次），取中位数对比
3. **单变量原则**：每次只验证一个库的 LD_PRELOAD，不叠加其他变更
4. **加载验证**：每次 LD_PRELOAD 后必须用 `grep <libname> /proc/<pid>/maps` 确认库实际加载，不能仅凭命令执行成功
5. **回退确认**：验证完毕后恢复原始 LD_PRELOAD，运行 1 次基线确认恢复

## 替换方式评估

| 方式 | 优势 | 风险 | 适用场景 |
|------|------|------|---------|
| 构建期集成 | 符号绑定可靠，无兼容风险 | 需要重编译 | 可重编译的应用 |
| `LD_PRELOAD` | 无需重编译 | 版本兼容性、容器挂载限制 | 无法重编译时的 fallback |

始终优先构建期集成，LD_PRELOAD 只在完全无法重编译时作为 fallback，且必须先确认进程未链接优化分配器。

构建期集成命令：
- jemalloc：`cmake -DWITH_JEMALLOC=system`（MySQL）或 `-Djemalloc_prefix=je_`
- tcmalloc：`cmake -DWITH_TCMALLOC=ON` 或 `LDFLAGS=-ltcmalloc`

### LD_PRELOAD 通用避坑

- 若 `ldd /proc/<pid>/exe | grep jemalloc` 非空 → 禁止 `LD_PRELOAD=libjemalloc.so`，重复注入可能导致符号冲突、性能退化
- 历史参考：mysqld 已被编译链接 jemalloc（构建期集成），再次 `LD_PRELOAD=libjemalloc.so.2` 导致 -1.23% TPS 退化（历史数据，仅作参考）
- 容器场景下 `LD_PRELOAD` 的 .so 必须在容器内可见，宿主机路径不可用
- 若进程已有 LD_PRELOAD（如 libgomp.so），必须追加而非覆盖
- **多库 LD_PRELOAD 顺序**：将待注入的优化库（tcmalloc/jemalloc/stringlib）置于已有 LD_PRELOAD 项之前，确保 malloc/string 符号优先由优化库解析；libgomp 等 OpenMP 运行时保留在后。例如 `LD_PRELOAD=/usr/lib/.../libtcmalloc.so.4 /lib/.../libgomp.so.1`。容器场景下 .so 路径须在容器内可见。
- 加载验证必须用 `/proc/<pid>/maps`，不能用 ldd（ldd 在自身环境运行，不继承目标进程的 LD_PRELOAD）

### 与编译器优化的协同

- 编译器 `-O3` 可能内联部分库调用，替换前需确认热点是否仍在库函数中
- PGO 采样后再做库替换可能更精准（热点已收敛）

## jemalloc 安装与 LD_PRELOAD 接入

当目标进程未链接 jemalloc 且无法重编译时，通过安装 jemalloc 动态库并以 `LD_PRELOAD` 方式接入。接入完成后进入 MALLOC_CONF 调优流程。

### 安装与路径确认

```bash
# 方法一（推荐）：系统包管理器安装
# openEuler/CentOS:
yum install jemalloc
# Ubuntu/Debian:
apt install libjemalloc-dev

# 方法二：GitHub Releases 源码编译安装
# 源码地址：https://github.com/jemalloc/jemalloc/releases
# Release tarball 自带预生成的 configure 脚本，无需 autoconf/automake/libtool
# 仅需 make 和 gcc
JEMALLOC_VERSION=5.3.1
curl -L -o jemalloc-${JEMALLOC_VERSION}.tar.bz2 \
    https://github.com/jemalloc/jemalloc/releases/download/${JEMALLOC_VERSION}/jemalloc-${JEMALLOC_VERSION}.tar.bz2
tar xjf jemalloc-${JEMALLOC_VERSION}.tar.bz2
cd jemalloc-${JEMALLOC_VERSION}
./configure --prefix=/usr/local
make -j$(nproc)
make install
ldconfig

# 确认动态库路径
find /usr -name 'libjemalloc.so*' 2>/dev/null
# openEuler/CentOS 一般在 /usr/lib64；Ubuntu/Debian 一般在 /usr/lib/aarch64-linux-gnu
```

动态库文件 `libjemalloc.so` 或 `libjemalloc.so.版本号`（如 `libjemalloc.so.2`）均可使用。

### LD_PRELOAD 接入 SOP

```bash
# 1. 前置检查：确认进程当前未链接 jemalloc
#    若已链接 → 禁止 LD_PRELOAD 重注入，直接进入 MALLOC_CONF 调优
ldd /proc/<pid>/exe | grep jemalloc

# 2. 确认 jemalloc 动态库路径
JEMALLOC_LIB=$(find /usr -name 'libjemalloc.so*' 2>/dev/null | head -1)

# 3. 设置 LD_PRELOAD（保留已有 LD_PRELOAD 项）
EXISTING_LD_PRELOAD=$(cat /proc/<pid>/environ | tr '\0' '\n' | grep '^LD_PRELOAD=' | cut -d= -f2-)
if [[ -n "$EXISTING_LD_PRELOAD" ]]; then
    export LD_PRELOAD="${EXISTING_LD_PRELOAD} ${JEMALLOC_LIB}"
else
    export LD_PRELOAD="${JEMALLOC_LIB}"
fi

# 4. 重启目标进程（LD_PRELOAD 只在进程启动时生效）
# 5. 验证 jemalloc 已加载（必须用 /proc/<pid>/maps，不能用 ldd）
grep jemalloc /proc/<new_pid>/maps
# 6. 压测对比吞吐和 RSS
```

## jemalloc MALLOC_CONF 预检测与调优

推荐 jemalloc 运行时调优前，先检查进程当前是否已配置 `MALLOC_CONF`：

```bash
cat /proc/<pid>/environ | tr '\0' '\n' | grep MALLOC_CONF
# 检查 jemalloc 是否已链接
ldd /proc/<pid>/exe | grep jemalloc
```

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

每次只改一个参数，逐个验证收益，避免多参数混淆归因。负收益时回退该参数，保留正向参数进入下一轮。

**轮次边界（单变量原则）**：allocator 库替换（jemalloc/tcmalloc 接入）与 MALLOC_CONF/TCMALLOC 环境变量调优分属不同优化变量，不得并入同一轮次。即使接入新 allocator 与调参都需要一次进程重启，也必须拆为独立轮次：第 N 轮仅注入新 allocator（验证库替换收益），第 N+1 轮起逐个验证运行时参数（每个参数单独一轮）。合并执行时收益必须标记为 `confounded`。

```bash
export MALLOC_CONF="background_thread:true,dirty_decay_ms:1000,muzzy_decay_ms:1000"
# 重启进程（MALLOC_CONF 只在 jemalloc 初始化时读取）
cat /proc/<pid>/environ | tr '\0' '\n' | grep MALLOC_CONF  # 验证生效
```

## tcmalloc 安装与 LD_PRELOAD 接入

### 预检测

```bash
# 检查当前进程是否已链接 tcmalloc
ldd /proc/<pid>/exe | grep -iE 'tcmalloc'
# 或检查运行时 maps
grep -i tcmalloc /proc/<pid>/maps
# 检查当前 LD_PRELOAD（可能已通过 LD_PRELOAD 注入 tcmalloc）
cat /proc/<pid>/environ | tr '\0' '\n' | grep -E 'LD_PRELOAD|TCMALLOC'
```

- 若已链接 tcmalloc → 仅调 TCMALLOC 环境变量，禁止重复 LD_PRELOAD 注入
- 若未链接 → 按 LD_PRELOAD SOP 接入

### 安装与路径确认

```bash
# 方法一（推荐）：系统包管理器安装
# openEuler/CentOS:
yum install gperftools-libs
# Ubuntu/Debian:
apt install libgoogle-perftools-dev

# 方法二：源码安装（依赖 libunwind）
# 确保 libunwind 已安装: yum install libunwind-devel / apt install libunwind-dev
# 然后从 gperftools 源码编译

find /usr -name 'libtcmalloc.so*' 2>/dev/null
# openEuler 一般在 /usr/lib64；Ubuntu/Debian 一般在 /usr/lib/aarch64-linux-gnu
```

### LD_PRELOAD 接入 SOP

```bash
# 1. 确认 tcmalloc 动态库路径
TCMALLOC_LIB=$(find /usr -name 'libtcmalloc.so*' 2>/dev/null | head -1)

# 2. 设置 LD_PRELOAD（保留已有 LD_PRELOAD 项）
EXISTING_LD_PRELOAD=$(cat /proc/<pid>/environ | tr '\0' '\n' | grep '^LD_PRELOAD=' | cut -d= -f2-)
if [[ -n "$EXISTING_LD_PRELOAD" ]]; then
    export LD_PRELOAD="${EXISTING_LD_PRELOAD} ${TCMALLOC_LIB}"
else
    export LD_PRELOAD="${TCMALLOC_LIB}"
fi

# 3. 重启目标进程
# 4. 验证 tcmalloc 已加载（必须用 /proc/<pid>/maps）
grep tcmalloc /proc/<new_pid>/maps
# 5. 压测对比吞吐和 RSS
```

### tcmalloc 运行时参数调优

当 tcmalloc 已通过 LD_PRELOAD 接入后，可通过环境变量调优：

| 环境变量 | 建议值 | 适用场景 | 预期效果 |
|----------|--------|----------|----------|
| `TCMALLOC_MAX_TOTAL_THREAD_CACHE_BYTES` | 104857600（100MB，默认 1GB） | 线程数极多（>100）时控制线程缓存总内存上限 | 防止 RSS 过高 |
| `TCMALLOC_AGGRESSIVE_DECOMMIT` | 1 | 内存紧张场景 | 更积极归还内存给 OS，降低 RSS |
| `TCMALLOC_RATE` | 1（默认） | heap profiling 时设置采样率 | 每 1MB 分配采样一次 |
| `HEAPPROFILE` | `/tmp/heap.prof` | 内存泄漏诊断 | 输出 heap profile 供 pprof 分析 |

```bash
export LD_PRELOAD="${TCMALLOC_LIB}"
export TCMALLOC_MAX_TOTAL_THREAD_CACHE_BYTES=104857600
# 重启进程
cat /proc/<pid>/environ | tr '\0' '\n' | grep TCMALLOC  # 验证生效
```

## bisheng-stringlib / libstringlib 安装与 LD_PRELOAD 接入

aarch64 场景替换 memcpy/memset/memmove/memchr/memcmp/strlen/strcpy 等，预期 3-8% 热点路径加速。两个来源产出的库文件都叫 `libstringlib.so`，且都原生导出标准符号名（memcpy/memmove/memset/memcmp），可直接 LD_PRELOAD，无需编译 wrapper 或桥接库。

| 来源 | 获取方式 | 优势 |
|------|----------|------|
| **BiShengCompiler** | 下载解压即用 | 无需编译，部署最简单 |
| **ARM-software/optimized-routines** | 源码编译 `make all-string` | 源码可控，支持 SVE/SVE2 优化选择 |

> optimized-routines 的 `libstringlib.so` 和 `libstringlib.a` 中的函数符号为带前缀形式（`__memcpy_aarch64`、`__memset_aarch64` 等），但 `make all-string` 生成的 `libstringlib.so` **已导出标准符号别名**，可直接 LD_PRELOAD。若 `nm -D libstringlib.so` 确认未导出标准符号（旧版本或自定义编译），才需要 fallback 到 wrapper 方案。

**SVE2 工具链约束**：string/aarch64 下 `strchr-sve2.S`、`strchrnul-sve2.S` 含 `.arch armv9-a+sve2` 指令，需要 binutils ≥ 2.36 且目标 CPU 支持 SVE2。若工具链或 CPU 不支持，需排除这两个文件降级编译。

### 方案 A（推荐）：BiShengCompiler — 解压即用

```bash
# 1. 下载 BiShengCompiler 软件包（华为云镜像，可直接 wget）
wget https://mirrors.huaweicloud.com/kunpeng/archive/compiler/bisheng_compiler/BiShengCompiler-4.2.0-aarch64-linux.tar.gz --no-check-certificate

# 2. 解压（tarball 无 install.sh，解压即用）
mkdir -p /opt/compiler
tar xzf BiShengCompiler-4.2.0-aarch64-linux.tar.gz -C /opt/compiler
# 解压后目录：/opt/compiler/BiShengCompiler-4.2.0-aarch64-linux/{bin,lib,include}

# 3. 确认 libstringlib.so 路径
find /opt/compiler -name 'libstringlib.so' 2>/dev/null
# 预期路径：/opt/compiler/BiShengCompiler-4.2.0-aarch64-linux/lib/libstringlib.so
```

### 方案 B：optimized-routines — 源码编译

```bash
# 依赖：make + gcc
git clone https://github.com/ARM-software/optimized-routines.git
cd optimized-routines
cp config.mk.dist config.mk
# config.mk 默认 ARCH=aarch64，SUBS 含 string，无需修改

# 检查工具链是否支持 SVE2（binutils ≥ 2.36）
as --version | head -1
# 若 binutils < 2.36 或 CPU 不支持 SVE2，移除 SVE2 文件降级编译：
mkdir -p /tmp/sve2-backup
mv string/aarch64/strchr-sve2.S string/aarch64/strchrnul-sve2.S /tmp/sve2-backup/

# 编译 string 子项目（生成 build/lib/libstringlib.a 和 libstringlib.so）
make all-string

# 确认标准符号已导出
nm -D build/lib/libstringlib.so | grep -E ' T (memcpy|memset|memmove|memcmp)$'
# 预期输出应包含 T memcpy, T memset, T memmove, T memcmp

# 安装
sudo cp build/lib/libstringlib.so /usr/local/lib/
sudo ldconfig
```

### libstringlib LD_PRELOAD 接入 SOP

```bash
# 1. 确认 libstringlib.so 路径（BiSheng 或 optimized-routines 均可）
STRINGLIB_LIB=$(find /opt/compiler /usr -name 'libstringlib.so' 2>/dev/null | head -1)

# 2. 设置 LD_PRELOAD（保留已有 LD_PRELOAD 项）
EXISTING_LD_PRELOAD=$(cat /proc/<pid>/environ | tr '\0' '\n' | grep '^LD_PRELOAD=' | cut -d= -f2-)
if [[ -n "$EXISTING_LD_PRELOAD" ]]; then
    export LD_PRELOAD="${EXISTING_LD_PRELOAD} ${STRINGLIB_LIB}"
else
    export LD_PRELOAD="${STRINGLIB_LIB}"
fi

# 3. 重启目标进程
# 4. 验证 libstringlib.so 已加载（必须用 /proc/<pid>/maps）
grep stringlib /proc/<new_pid>/maps
# 5. 压测对比吞吐和 RSS
```

### glibc 已优化实现的检查

替换 memcpy 前检查当前 glibc 版本是否已包含优化实现：

```bash
# 检查 glibc memcpy 是否已使用 SIMD
objdump -d /usr/lib64/libc.so.6 | grep -A5 '<memcpy>:' | head -10
# aarch64 上检查是否使用 NEON/SVE 指令
perf annotate memcpy | grep -E 'ld1|st1|ldp|stp|lsl|asr' | head -10
```

### 方案选择建议

- 已安装或可安装 BiShengCompiler → 优先 BiSheng libstringlib（无需编译，解压即用，部署最简单）
- 无 BiShengCompiler 或需自定义编译选项 → 使用 optimized-routines 方案（源码可控，支持 SVE/SVE2 优化选择）
- 两个方案产出的库文件都叫 `libstringlib.so`，都导出标准符号名，LD_PRELOAD 接入方式完全相同

## tcmalloc 判断线索（perf 热点中出现以下信号时优先考虑）

- 热点函数 `malloc`/`free` 调用频次 > 100K/s（`perf stat -e cache-misses` 确认）
- 大量 `__libc_malloc` 内部锁竞争（`perf annotate` 中 `pthread_mutex_lock` 在 malloc 路径上）
- 线程数 > 32 且 `pidstat -t` 显示多线程同时高频分配
- Ascend 场景补充信号见 `ascend-playbook.md`
