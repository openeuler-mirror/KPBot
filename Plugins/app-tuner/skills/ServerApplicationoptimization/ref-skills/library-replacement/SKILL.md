---
name: library-replacement
description: >
  Linux 芯片级库替换性能调优体检工具。通过读取本地的 optimization_kb.json 知识库，
  结合系统指纹采集、perf 负载观测与进程依赖分析，输出调优建议报告。支持两种工作模式：
  (1) 在线分析 - 目标程序已作为进程运行，持续分析；
  (2) 离线分析 - 程序尚未启动，执行指定命令将其启动后再分析。
  支持全类别库替换分析：内存分配器、哈希函数、压缩库、加密库、网络库、JSON 解析库、
  数学库、线性代数库、视频编解码库、正则匹配库、键值存储库等。
  当用户提到"性能调优"、"库替换调优"、"CPU 热点分析"、"LD_PRELOAD"、"NoSQL 调优"、
  "allocator 调优"、"哈希函数优化"、"压缩库优化"、"加密库调优"、"系统瓶颈体检"、
  "benchmark 调优"、"perf 分析"、"jemalloc"、"tcmalloc"、
  "zlib 替换"、"openssl 优化"、"sonic-json"、"Hyperscan"或类似场景时触发。
  技能自动执行多阶段探测（系统指纹 + perf 采样 + 库依赖分析），与知识库规则严格匹配，
  输出 Markdown 格式调优体检报告。注意：本技能仅提供方案建议，不执行任何系统变更。
triggers:
  - "性能调优"
  - "库替换调优"
  - "CPU 热点分析"
  - "LD_PRELOAD"
  - "NoSQL 调优"
  - "allocator 调优"
  - "哈希函数优化"
  - "压缩库优化"
  - "加密库调优"
  - "JSON 解析优化"
  - "数学库优化"
  - "系统瓶颈体检"
  - "benchmark 调优"
  - "perf 分析"
  - "jemalloc"
  - "tcmalloc"
  - "zlib 替换"
  - "openssl 优化"
  - "sonic-json"
  - "Hyperscan"
  - "xxhash"
  - "RocksDB"
  - "RocksDB 状态后端"
  - "Flink RocksDB"
  - "LSM 调优"
  - "嵌入式 KV"
  - "键值存储优化"
  - "KAL-rocksdb"
  - "performance tuning"
  - "library replacement"
  - "调优体检"
  - "分析一下当前运行的程序"
  - "perf 火焰图"
---

# library-replacement

**功能定位**：非侵入式 Linux 全类别库替换性能调优体检。通过 perf 采样 + 库依赖分析，检测进程当前使用的库，根据 `optimization_kb.json` 知识库推荐最优替换路径并估算收益。**本工具仅提供方案，不执行任何变更**。

**安全与架构红线**：
1. **只读探测**：只读探测命令（`cat`, `uname`, `lscpu`, `top`, `lsof`, `ps`, `readelf`, `/proc`、`perf`），禁止任何写入/修改/export 注入/重启/Kill 操作。
2. **无状态脚本依赖**：**不允许脚本和脚本之间存在非文本信息的依赖关系**（例如禁止脚本 A 启动进程返回 PID，再由脚本 B 接收 PID 去监控）。如果需要起一个进程观测，必须在单一脚本内部完成“启动 -> 观测采样 -> 收集结果”的全生命周期闭环，仅通过纯文本（JSON）进行结果交互。

---

## 核心概念

### 支持的库类别（来自 optimization_kb.json）

| 类别 | 库列表 | 典型替换场景 |
|------|--------|-------------|
| allocators | jemalloc, tcmalloc | 内存分配器升级 |
| hash_functions | xxhash | 哈希函数加速 |
| compression | zlib, isa-l, isa-l_crypto | 压缩/解压缩加速 |
| crypto | openssl, GMSSL, isa-l_crypto | 加密算法硬件加速 |
| json | sonic-cpp, RapidJSON | JSON 解析加速 |
| memory_operations | libmem, libco | 内存操作优化（ARM） |
| pattern_matching | hyperscan | 正则匹配加速 |
| linear_algebra | vectorBLAS, BLAS | 矩阵运算加速 |
| sparse_linear_algebra | SparseBLAS | 稀疏矩阵运算 |
| math | Libm, VML, SVML, Interp_Spline, autoGEMM | 数学函数向量化 |
| dnn | DNN | 深度学习算子加速 |
| fft | FFT | 傅里叶变换加速 |
| video | X264, X265 | 视频编解码加速 |
| serialization | Protobuf | 序列化优化 |
| sql_acceleration | sparksql_native | Spark SQL 加速 |
| network | KTLS | 内核 TLS 加速 |
| kv_storage | RocksDB, KAL-rocksdb | 嵌入式 KV 存储引擎 LD_PRELOAD 加速 |

### 库检测策略

本技能采用 **两级检测** 策略，对所有类别的库进行无差别识别：

1. **静态检测**：通过 `lsof` / `/proc/<PID>/maps` 分析进程加载的动态库，直接匹配特征。
2. **动态检测**：通过 `perf record -g` 采样热点函数，识别被调用频率高的库函数路径。

---

## 模式检测（前置必做）

技能被触发后，**必须首先确认工作模式**：

```bash
# 检查是否有进程正在运行（指定进程名或 PID）
# 用户若指定了 PID，进入"在线模式"
# 用户若提供启动命令，进入"离线模式"
```

### 模式 A：在线分析（程序已作为进程运行）

用户告知进程已存在（如 "kv_main 正在运行"、"分析 PID 12345"），技能直接用 `lsof -p <PID>` 和 `/proc/<PID>/maps` 分析。

### 模式 B：离线分析（程序未启动）

用户指定要执行的命令（如 `./kv_main benchmark 500000 0.8 4`）。为满足无状态依赖要求，系统将使用闭环脚本 `scripts/run_and_profile_offline.sh`，在脚本内部同时完成进程启动与性能采样。

> 注意：仅执行用户**显式提供**的命令，不自行推断或构造命令。

---

## Phase 1：挂载知识库

优先在当前工作目录查找 `optimization_kb.json`。

```bash
paths=(
  "./optimization_kb.json"
  "./docs/optimization_kb.json"
  "../optimization_kb.json"
)

for p in "${paths[@]}"; do
  if [[ -f "$p" ]]; then
    echo "FOUND: $p"
    cat "$p"
    exit 0
  fi
done
echo "MISSING_KB"
```

**判定规则**：

- 文件存在 → 解析 JSON，构建匹配规则树
- 文件不存在 → **立即终止**，输出缺失提示

---

## Phase 2：环境指纹采集

```bash
uname -a
uname -m
lscpu
nproc
```

**提取字段**：

| 字段 | 来源 | 示例 |
|------|------|------|
| OS 类型 | `uname -s` | Linux |
| 内核版本 | `uname -r` | 5.4.0-arm64 |
| CPU 架构 | `uname -m` | aarch64 |
| CPU 型号 | `lscpu \| grep 'Model name'` | Neoverse-N1 |
| 逻辑核心 | `nproc` | 64 |

**架构约束检测**：

> 本工具**仅支持 ARM64 架构**，必须在 aarch64 服务器上运行。

```bash
ARCH=$(uname -m)
if [[ "$ARCH" != "aarch64" ]]; then
  echo "ARCH_NOT_SUPPORTED: $ARCH"
  echo "本工具仅支持 ARM64 (aarch64) 架构，当前架构为 $ARCH"
  exit 1
fi
```

---

## Phase 3：进程采样与分析

### 模式 A：在线采样

**步骤 1：基础信息采集**

```bash
lsof -p <PID> 2>/dev/null | grep '\.so' | awk '{print $NF}' | sort -u
ps -p <PID> -o pid=,comm=,%cpu=,%mem=,vsz=,rss= --no-headers 2>/dev/null
cat /proc/<PID>/maps 2>/dev/null | grep '\.so' | awk '{print $6}' | grep -oP '[^/]+$' | sort -u
```

**步骤 2：perf 热点采样（在线）**

```text
脚本路径: scripts/perf_sampling_online.sh
输入: <PID>
输出: JSON {perf_available: bool, perf_report: path, perf_error: string, needs_authorization: bool, auth_command: string}
权限: 需要 root 权限或 /proc/<PID> 可访问
```

如果 `needs_authorization: true`，与用户交互请求授权：
```bash
sudo chmod 755 /proc/<PID>
# 重试采样
```

### 模式 B：离线采样（闭环单脚本）

> **设计约束**：因禁止跨脚本传递活动进程 PID，启动目标程序和执行 perf 采样必须在单一脚本内一站式完成，并以纯文本返回结果。

**步骤 1：闭环启动与采样**

```text
脚本路径: scripts/run_and_profile_offline.sh
输入: <command> <args...>
输出: JSON {pid, command, exit_code, process_found, libraries: [], ps_info: {}, perf_available: bool, perf_report: path, needs_authorization: bool}
权限: 无特殊要求（perf 若需权限脚本内部会判断）
```

脚本内部逻辑说明：
1. 执行 `eval "$@" &` 在后台启动目标进程，并捕获自身的 `$!`。
2. 短暂 sleep 等待进程初始化，执行 `ps` 和 `lsof` 抓取静态库依赖。
3. 直接在当前脚本生命周期内，对捕获的 PID 执行 `perf record`（采样 5 秒）。
4. 将 `perf report` 结果和依赖项打包为 JSON 文本输出。
5. （可选）等待进程结束或直接返回。

**脚本失败时的内联重试（直接执行 Bash 闭环）**：

若脚本返回 `perf_available: false` 或找不到进程，使用以下内联方式启动并抓取（同样保持单代码块闭环）：

```bash
# 闭环执行：启动 -> 提取状态 -> 抓取采样
$COMMAND &
SAMPLED_PID=$!
sleep 0.5

# 抓取依赖
lsof -p $SAMPLED_PID 2>/dev/null | grep '\.so' | awk '{print $NF}' | sort -u > "/tmp/libs_${SAMPLED_PID}.txt"

# perf 尝试（无 bpf 兼容模式）
perf record -p $SAMPLED_PID --no-bpf -o "/tmp/perf_${SAMPLED_PID}.data" -- sleep 2 2>&1
perf report --stdio -i "/tmp/perf_${SAMPLED_PID}.data" -n --pretty 2>/dev/null | head -100 > "/tmp/perf_report_${SAMPLED_PID}.txt"

# 输出结果并等待进程
cat "/tmp/libs_${SAMPLED_PID}.txt"
cat "/tmp/perf_report_${SAMPLED_PID}.txt"
wait $SAMPLED_PID
```

---

## Phase 4：统一库类型识别

全类别统一识别。根据 Phase 3 输出的依赖文本和 Perf 报告进行综合分析，所有类别的库采用相同的静态与动态匹配策略：

```text
脚本路径: scripts/detect_all_libraries.sh
输入: LIBS_TEXT_PATH (依赖列表文本文件), PERF_REPORT_PATH (perf文本路径)
输出: JSON {detected_libraries: [{category: string, current_lib: string, detection_method: "static"|"dynamic", evidence: string}]}
```

**综合检测映射表**（知识库匹配依据）：

| 库类别 | 静态库特征 (lsof/maps) | 动态关键词 (perf 热点) | 典型当前库 / 默认兜底 |
|--------|----------------------|-----------------------|---------------------|
| allocators | `jemalloc`, `tcmalloc` | `jemalloc\|tcmalloc\|malloc` | jemalloc / tcmalloc / glibc malloc |
| hash_functions | `xxhash` | `xxhash\|xxh64\|hash` | xxhash / builtin |
| compression | `libz`, `libisa-l` | `deflate\|inflate\|crc32\|compress` | zlib / ISA-L |
| crypto | `libcrypto`, `libssl` | `AES\|SHA\|MD5\|SM4\|SSL\|TLS` | openssl / GMSSL |
| json | `sonic`, `rapidjson` | `json_parse\|sonic_parse\|rapidjson` | sonic-cpp / RapidJSON |
| memory_operations | `libmem`, `libco` | `memcpy\|memset\|memcmp` | libmem / libco / libc |
| pattern_matching | `hyperscan`, `hs` | `hyperscan\|regex\|pcre` | Hyperscan / PCRE |
| linear_algebra | `libblas`, `libopenblas` | `gemv\|gemm\|blas\|cblas` | BLAS / OpenBLAS |
| math | `libm` | `sin\|cos\|exp\|log\|vml\|svml` | Libm / VML / SVML |
| kv_storage | `librocksdbjni`, `librocksdb` | `rocksdb::\|DBImpl\|CompactionJob\|MemTable\|BlockBasedTable` | RocksDB |

---

## Phase 5：规则引擎匹配

将 Phase 2（环境指纹）、Phase 4（统一库类型识别）注入知识库规则树，计算替换路径。

### 匹配算法

```text
FOR each item in detected_libraries:
  category = item.category
  current_library = item.current_lib
  
  FOR each competitor in knowledge_base[category]:
    IF competitor != current_library
      THEN add (current → competitor) to candidate_paths
```

**核心逻辑**：
1. 根据识别出的库类别，在知识库 `library_profiles` 中查找该类别的所有优化库。
2. 无论当前库是默认库（如 glibc malloc、builtin hash）还是第三方库，只要同类别中有更优替代方案，即推荐替换。
3. **目标库不需要当前已安装**——只需知识库中有记录，后续通过 LD_PRELOAD 导入即可。
4. 替换路径的详细性能数据从 `library_profiles.<category>.<lib>.best_scenarios` 读取。

---

## 最终输出报告（强制模板）

无论哪种模式，分析完成后必须输出以下格式：

```markdown
# 🩺 芯片级库替换调优体检报告

## [1] 硬件底座指纹

| 字段 | 值 |
|------|----|
| OS 类型 | <uname -s> |
| 内核版本 | <uname -r> |
| CPU 架构 | <aarch64> |
| CPU 型号 | <Model name from lscpu> |
| 逻辑核心数 | <nproc> |
| Perf 支持 | <是 / 否> |

## [2] 分析模式

| 字段 | 值 |
|------|----|
| 模式 | 在线 / 离线（单点闭环） |
| 目标进程 | <PID> / <命令> |
| 进程运行时长 | <足够 / 过短> |
| Perf 采样 | <成功 / 失败 / 未尝试> |

## [3] 进程采样结果

### 热点进程

| PID | 进程名 | CPU% | MEM% | VSZ | RSS |
|-----|--------|------|------|-----|-----|
| <PID> | <comm> | <cpu%> | <mem%> | <vsz> | <rss> |

### 动态链接库依赖（lsof 采样）

```
<库路径 1>
<库路径 2>
...
```

### Perf 热点函数（Top 10，5秒采样）

> 如果 perf 可用且采样成功

```
<函数名 1>  <百分比>%
<函数名 2>  <百分比>%
...
```

## [4] 库类型识别

基于依赖库静态特征和 perf 动态热点，统一识别结果如下：

| 类别 | 检测到的当前库 | 检测方式 | 判定依据 |
|------|---------------|---------|---------|
| <category 1> | <current_lib> | <静态/动态/综合> | <lsof: libxx / perf热点: xxx> |
| <category 2> | <current_lib> | <静态/动态/综合> | <lsof: libyy / perf热点: yyy> |
| ... | ... | ... | ... |

> 注：完整库类别列表见知识库 `optimization_kb.json` 的 `library_profiles` 字段。

## [5] 替换路径推荐

> 预期收益数据来自 `optimization_kb.json` 的 `library_profiles.<category>.<lib>`。

### 检测到可优化的库

| 类别 | 当前库 | 可替换目标 | 典型场景 | 说明 |
|------|--------|-----------|---------|------|
| <category> | <current_lib> | <target_lib> | <best_scenarios> | < strengths > |

### 仅动态热点暗示的潜在优化

| 类别 | 热点函数关键词 | 建议优化方向 | 预期收益来源 |
|------|--------------|-------------|-------------|
| <category> | <关键词> | <目标库> | knowledge_base |

> 详细性能数据（吞吐提升、延迟改善）请查阅知识库。

## [6] 现场实施 SOP

> ⚠️ **本工具仅提供方案建议，不执行任何系统变更。以下步骤需由手动确认执行。**

**库替换实施**：
```bash
LD_PRELOAD='<目标库.so>' <command>
```

> 详细替换命令和验证步骤请查阅 `optimization_kb.json` 中对应库的 `verification_steps` 字段。

## [7] Perf 热点分析结论

<如果 perf 成功>：
- 热点函数集中在：<函数列表>
- 建议优先优化类别：<category>

<如果 perf 权限不足但用户拒绝授权>：
- 未能采集 perf 数据，仅基于 lsof 静态分析
- 建议：后续可通过以下命令手动授权后重新分析
  ```bash
  sudo chmod 755 /proc/<PID>
  # 然后重新触发 skill
  ```
```

---

## 错误处理

| 场景 | 处理方式 |
|------|---------|
| `optimization_kb.json` 不存在 | 终止并提示缺失知识库 |
| 用户未指定 PID 也未提供命令 | 输出"请指定要分析的程序 PID 或提供启动命令" |
| 离线模式进程启动或采样脚本失败 | 提示"离线分析闭环执行异常"，并尝试直接内联启动进行抓取。 |
| perf 权限不足 | **与用户交互请求授权**，授予后重试闭环采样；若拒绝则仅作 lsof 分析 |
| perf 不存在 | 报告错误信息 `perf 命令不存在，请安装（apt install linux-tools-generic）`，询问是否继续（跳过 perf 分析） |

---

## 数据来源约束

**严禁自行编造数据**。所有输出数据必须 100% 来自知识库或现场采集。脚本与脚本之间交互**仅限文本文件/JSON（如读取包含库列表的 txt）**，绝不依赖进程状态跨脚本驻留。
```