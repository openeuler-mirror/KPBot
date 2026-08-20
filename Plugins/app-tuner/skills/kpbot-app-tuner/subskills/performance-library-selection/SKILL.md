---
name: performance-library-selection
description: kpbot-app-tuner 的统一高性能库选型入口。基于热点证据（perf 采样 + 库依赖分析）结合场景经验推荐高性能库或替代实现（malloc/memcpy/压缩/加密等），输出候选方案与验证流程设计供主框架执行阶段落地。
---

# Performance Library Selection

当热点函数表明标准库或当前依赖库存在性能瓶颈时使用本子 skill。作为统一入口，内置 aarch64 全类别库替换性能调优体检能力：检测进程当前使用的库，根据 `references/optimization_kb.json` 知识库推荐最优替换路径并估算收益，结合证据分级与场景经验给出推荐结论。

> **本子 skill 的边界（权威定义见"安全与架构红线 #1/#3"与"输出契约"）**：体检阶段仅提供方案和验证流程设计，不执行任何变更、不执行真实 LD_PRELOAD 验证。所有候选库 `verification_status` 恒为 `not_verified`、`confidence` 不得标 `measured`；变更和真实验证通过候选动作在主框架执行阶段落地，验证完成后回写结果升级 `confidence`。

## 何时触发

- 热点 DSO/so 中存在高占比第三方库
- 热点函数落在 malloc/memcpy/memset/string/allocator/压缩/加密/CRC 等外部库
- `hotspot_dso_rank` 中第三方 `.so` 占比达阈值

## 输入确认

分析前需明确输入模式（决定后续采样方式）：

- **在线分析**：用户指定已运行进程 PID → 用 `lsof -p <PID>` 和 `/proc/<PID>/maps` 分析
- **离线分析**：用户提供启动命令 → 用 `scripts/run_and_profile_offline.sh` 闭环启动+采样。仅执行用户**显式提供**的命令，不自行推断或构造命令

输入证据（优先使用本轮 `current_run_id` 的当前证据）：

- `architecture`、`target_pid`、`launch_command`、`perf_available`
- `current_malloc_conf`、`current_ld_preload`（从 `/proc/<pid>/environ` 获取）

## 必读 Reference

按需加载：

- 通用安装 SOP、运行时调优参数、推荐规则细则、验证方法学：`references/library-playbook.md`
- Ascend NPU 场景经验线索与推荐策略（含 host 侧 malloc 优化、tcmalloc 依赖陷阱、vLLM Caching Allocator 交互）：`references/ascend-playbook.md`
- 内存分配器选型决策树（场景→分配器决策路径、依据映射、跨场景经验）：`references/allocator-decision-guide.md`
- 全类别库替换知识库（库画像、最佳场景、验证步骤、Ascend 经验）：`references/optimization_kb.json`
- 在线 PID 采样参考脚本（在线模式下获取目标 PID 并启动 perf 采样）：`references/scripts/sample_online_pid.sh`
- 公共依赖、perf/PMU 权限和降级：`../../references/prerequisites.md`

## 安全与架构红线

1. **只读探测**：体检阶段的探测命令（`cat`, `uname`, `lscpu`, `top`, `lsof`, `ps`, `readelf`, `/proc`、`perf`）只读，禁止任何写入/修改/export 注入/重启/Kill 操作（脚本内部管理自身启动的进程生命周期除外）。变更动作只能以候选动作形式输出，由执行阶段落地。
2. **无状态脚本依赖**：**不允许脚本和脚本之间存在非文本信息的依赖关系**（例如禁止脚本 A 启动进程返回 PID，再由脚本 B 接收 PID 去监控）。如果需要起一个进程观测，必须在单一脚本内部完成"启动 -> 观测采样 -> 收集结果"的全生命周期闭环，仅通过纯文本（JSON）进行结果交互。
3. **禁止历史数据替代现场证据**：`ascend-playbook.md` 的"历史实测收益基线"仅作参照叙述，**严禁**用作现场判定或推荐依据。`ascend_runtime` 等 perf 依赖类别必须有本轮 `current_run_id` 的现场 perf 采集成功才能输出结论；现场采集失败、降级或 run_id 不一致时，该类别必须标记为 `blocked`，不得基于历史数据推荐替换库。

## 核心概念

### 支持的库类别与综合检测映射表

> 下表为类别总览与检测特征合一；Step 2.1 的静态/动态匹配均依据本表。

| 类别 | 候选库 | 静态特征 (lsof/maps) | 动态关键词 (perf 热点) | 当前库 / 默认兜底 | 替换场景 |
|------|--------|---------------------|-----------------------|-------------------|---------|
| allocators | jemalloc, tcmalloc | `jemalloc`, `tcmalloc` | `malloc\|jemalloc\|tcmalloc` | glibc malloc / jemalloc / tcmalloc | 内存分配器升级 |
| hash_functions | xxhash | `xxhash` | `xxh64\|xxh32\|xxhash\|XXH` | xxhash / builtin | 哈希函数加速 |
| compression | zlib, ISA-L, isa-l_crypto | `libz`, `libisal` | `deflate\|inflate\|crc32\|compress` | zlib / ISA-L | 压缩/解压缩加速 |
| crypto | openssl, GMSSL, isa-l_crypto | `libcrypto`, `libssl` | `AES\|SHA\|MD5\|SM4\|SSL\|EVP_` | openssl / GMSSL | 加密算法硬件加速 |
| json | sonic-cpp, RapidJSON | `sonic`, `rapidjson` | `json_parse\|sonic_parse\|rapidjson` | sonic-cpp / RapidJSON | JSON 解析加速 |
| memory_operations | libmem, bisheng-stringlib, libco | `libmem`, `libco\.so`, `libstringlib` | `memcpy\|memset\|memcmp` | libmem / bisheng-stringlib / libco / libc | 内存/字符串操作优化（ARM） |
| pattern_matching | Hyperscan | `hyperscan`, `libhs` | `hyperscan\|regex\|pcre` | Hyperscan / PCRE | 正则匹配加速 |
| linear_algebra | OpenBLAS, vectorBLAS | `libblas`, `libopenblas` | `gemv\|gemm\|blas\|cblas` | BLAS / OpenBLAS | 矩阵运算加速 |
| sparse_linear_algebra | SparseBLAS | `libspblas` | `spmv\|spgemm\|sparse_blas` | SparseBLAS | 稀疏矩阵运算 |
| math | Libm, VML, SVML, Interp_Spline, autoGEMM | `libm`, `libvml`, `libsvml` | `\bsin\b\|\bcos\b\|\bexp\b\|\blog\b\|vml\|svml` | Libm / VML / SVML | 数学函数向量化 |
| dnn | oneDNN | — | `conv[0-9]\|pool[0-9]\|relu\|matmul\|dnn` | DNN Framework | 深度学习算子加速 |
| fft | FFTW | `libfftw3` | `fft\|ifft\|dft` | FFTW | 傅里叶变换加速 |
| video | X264, X265 | `x264`, `x265` | `encode\|decode\|h26` | X264 / X265 | 视频编解码加速 |
| serialization | Protobuf | `libprotobuf` | `protobuf\|ParseFromArray\|SerializeToString` | Protobuf | 序列化优化 |
| sql_acceleration | sparksql_native | `libsparksql_native` | `sparksql\|codegen\|native_sql` | sparksql_native | Spark SQL 加速 |
| network | KTLS | `ktls` | `tls_tx\|tls_rx\|send\|recv\|tcp_\|udp_` | KTLS / Standard Network | 内核 TLS 加速 |
| kv_storage | RocksDB, KAL-rocksdb | `librocksdbjni`, `librocksdb` | `rocksdb::\|DBImpl\|CompactionJob\|MemTable\|BlockBasedTable` | RocksDB | 嵌入式 KV 存储引擎 LD_PRELOAD 加速 |
| ascend_runtime | tcmalloc_for_cann | `libruntime_v100`, `libtorch_npu`, `libhccl`, `libascendcl` | `__pthread_mutex_lock\|__pthread_rwlock_unlock\|std::_Rb_tree\|std::vector::_M_realloc_insert\|std::_Hashtable` (CANN DSO 内容器操作锁竞争) | glibc malloc（host 侧） | Ascend NPU 训练/推理场景 host 侧 malloc 优化（CANN runtime 锁竞争归因） |

### 库检测策略

采用 **两级检测** 策略，对所有类别的库进行无差别识别（具体采集命令见 Step 1.3）：

1. **静态检测**：通过 `lsof` / `/proc/<PID>/maps` 分析进程加载的动态库，直接匹配特征。
2. **动态检测**：通过 `perf record` 采样热点函数，识别被调用频率高的库函数路径。

## 核心步骤流程

**硬门控**：采集完成前禁止生成候选库列表。顺序不可打乱。

```
Step 1: 采集热点（前置判定 / 环境指纹 / 进程采样 / 全类别热点占比）
         ↓ 产出: library_selection_mode, category_hotspot_pct{allocators,memory_operations,...}, hotspot_dso_rank, detected_libraries
Step 2: 库类型识别 + 规则引擎匹配 + 证据分级与经验推荐
          ↓ 产出: candidate_library_list（中间态，含每库的 recommendation / confidence）
Step 3: 输出验证流程设计（不执行真实 LD_PRELOAD），作为候选库推荐的配套验证计划
          ↓ 产出: all_library_verification_results（最终输出，= candidate_library_list 追加验证计划字段，
                   均标 verification_status=not_verified）
```

**违规判定**：候选库列表或推荐结论在 Step 1 完成前已开始 → 流程违规，列表无效，回退 Step 1。

### Step 1：热点采集与进程采样

在评估任何库替换之前，必须先在当前场景采集**全 18 类别**对应的热点函数 CPU 占比，并完成进程依赖与热点采样。采集方式按环境能力选择，不得因某一种工具缺失而放弃采集。

#### 1.1 前置判定（路径决策）

按架构与知识库可用性选择路径，产出 `library_selection_mode`：

```bash
KB_PATH="references/optimization_kb.json"
ARCH=$(uname -m)
if [[ -f "$KB_PATH" ]] && [[ "$ARCH" == "aarch64" ]]; then
  echo "MODE: aarch64_full_detection"
  # 解析 JSON，构建匹配规则树
else
  echo "MODE: generic_experience"
  # 记录 fallback_reason=optimization_kb_missing 或 architecture_not_aarch64
fi
```

- **aarch64 检测路径**（`aarch64_full_detection`）：走"全类别库检测工作流"（Step 1.2 采样 → Step 2 识别+规则匹配 → Step 3 验证流程设计），覆盖上表全部 18 类库。
- **通用经验路径**（`generic_experience`）：不中断主流程，记录 `fallback_reason`，走简化流程：
  - Step 1.2-1.4：仅执行环境指纹 + 进程采样 + 热点占比提取。
  - Step 2：跳过脚本检测，直接基于全类别热点占比 + "关键方案：候选库类型映射"表匹配候选库，`confidence` 最高为 `experience_only`。
  - Step 3：同 aarch64 路径，输出验证流程设计。
  - 覆盖全 18 类别（malloc/memcpy/压缩/加密/校验/JSON/正则/BLAS/数学/DNN/FFT/视频/序列化/SQL/网络/KV/算子/ascend_runtime）。

#### 1.2 环境指纹采集

```bash
uname -a
uname -m
lscpu
nproc
```

#### 1.3 进程采样与 perf 热点

**在线采样**

```bash
# 基础信息采集 — lsof 和 /proc/maps 互为降级：lsof 返回空时用 /proc/maps 补充
lsof -p <PID> 2>/dev/null | grep '\.so' | awk '{print $NF}' | sort -u
ps -p <PID> -o pid=,comm=,%mem=,vsz=,rss= --no-headers 2>/dev/null
cat /proc/<PID>/maps 2>/dev/null | grep '\.so' | awk '{print $6}' | grep -oP '[^/]+$' | sort -u

# perf 热点采样（脚本输出 JSON：perf_available / perf_report / perf_data / needs_authorization）
bash scripts/perf_sampling_online.sh <PID>
```

> 若 `lsof -p <PID>` 返回空（容器内权限限制），降级使用 `/proc/<PID>/maps` 获取动态库列表，两者输出互补。

如果 `needs_authorization: true`，与用户交互请求授权后重试采样。

**离线采样（闭环单脚本）**

```bash
# 脚本内部完成 启动->采样->收集->清理 全生命周期，仅打印生成的 JSON 报告文件绝对路径
bash scripts/run_and_profile_offline.sh <command> [args...]
```

> 因禁止跨脚本传递活动进程 PID，启动目标程序和执行 perf 采样必须在单一脚本内一站式完成，并以纯文本返回结果。若脚本执行失败（非零退出码），提示"离线分析闭环执行异常"并报告错误。

#### 1.4 全类别热点占比提取

从 Step 1.3 产出的数据中提取**所有 18 类库对应的热点占比**，**不要重复执行 `perf record`**。每个类别按"核心概念"综合检测映射表的动态关键词匹配，存在热点的类别必须进入 Step 2 推荐流程。

**在线模式**（`perf_sampling_online.sh` 输出 `perf_data` 路径，用 `--no-children` 取 self%）：

```bash
# PERF_DATA 为 Step 1.3 输出的 perf_data 路径（.data 文件）
# 必须用 --no-children 取 self%，否则 call graph 下百分比会重复累加
PERF_REPORT=$(perf report --stdio -i "$PERF_DATA" --no-children 2>/dev/null)

# 全类别热点占比提取（关键词来自综合检测映射表"动态关键词"列）
echo "$PERF_REPORT" | grep -E 'malloc|free|calloc|realloc|__libc_malloc|_int_malloc|tc_malloc|je_malloc' | awk '{sum+=$1} END {print "allocators: " sum "%"}'
echo "$PERF_REPORT" | grep -E 'memcpy|memmove|memset|memcmp' | awk '{sum+=$1} END {print "memory_operations: " sum "%"}'
echo "$PERF_REPORT" | grep -E 'xxh64|xxh32|xxhash|XXH' | awk '{sum+=$1} END {print "hash_functions: " sum "%"}'
echo "$PERF_REPORT" | grep -E 'deflate|inflate|crc32|compress' | awk '{sum+=$1} END {print "compression: " sum "%"}'
echo "$PERF_REPORT" | grep -E 'AES|SHA|MD5|SM4|SSL|EVP_' | awk '{sum+=$1} END {print "crypto: " sum "%"}'
echo "$PERF_REPORT" | grep -E 'json_parse|sonic_parse|rapidjson' | awk '{sum+=$1} END {print "json: " sum "%"}'
echo "$PERF_REPORT" | grep -E 'hyperscan|regex|pcre' | awk '{sum+=$1} END {print "pattern_matching: " sum "%"}'
echo "$PERF_REPORT" | grep -E 'gemv|gemm|blas|cblas' | awk '{sum+=$1} END {print "linear_algebra: " sum "%"}'
echo "$PERF_REPORT" | grep -E 'spmv|spgemm|sparse_blas' | awk '{sum+=$1} END {print "sparse_linear_algebra: " sum "%"}'
echo "$PERF_REPORT" | grep -E '\bsin\b|\bcos\b|\bexp\b|\blog\b|vml|svml' | awk '{sum+=$1} END {print "math: " sum "%"}'
echo "$PERF_REPORT" | grep -E 'conv[0-9]|pool[0-9]|relu|matmul|dnn' | awk '{sum+=$1} END {print "dnn: " sum "%"}'
echo "$PERF_REPORT" | grep -E 'fft|ifft|dft' | awk '{sum+=$1} END {print "fft: " sum "%"}'
echo "$PERF_REPORT" | grep -E 'encode|decode|h26' | awk '{sum+=$1} END {print "video: " sum "%"}'
echo "$PERF_REPORT" | grep -E 'protobuf|ParseFromArray|SerializeToString' | awk '{sum+=$1} END {print "serialization: " sum "%"}'
echo "$PERF_REPORT" | grep -E 'sparksql|codegen|native_sql' | awk '{sum+=$1} END {print "sql_acceleration: " sum "%"}'
echo "$PERF_REPORT" | grep -E 'tls_tx|tls_rx|tcp_|udp_' | awk '{sum+=$1} END {print "network: " sum "%"}'
echo "$PERF_REPORT" | grep -E 'rocksdb::|DBImpl|CompactionJob|MemTable|BlockBasedTable' | awk '{sum+=$1} END {print "kv_storage: " sum "%"}'
echo "$PERF_REPORT" | grep -E '__pthread_mutex_lock|__pthread_rwlock_unlock|std::_Rb_tree|std::vector::_M_realloc_insert|std::_Hashtable' | awk '{sum+=$1} END {print "ascend_runtime: " sum "%"}'

# DSO 热点排名
echo "$PERF_REPORT" | grep -E '^\s+[0-9]' --sort=dso 2>/dev/null | head -20
perf report --stdio -i "$PERF_DATA" --no-children --sort=dso 2>/dev/null | grep -E '^\s+[0-9]' | head -20
```

**离线模式**（脚本 `hotspots` 字段已是解析后的结构化数据，按 `lib`/`symbol` 聚合提取，无需再调 `perf report`）：

```bash
# REPORT_JSON 为 Step 1.3 输出的 JSON 报告文件路径
# 从 hotspots 数组按 symbol 聚合全 18 类别占比
python3 -c "
import json, re
with open('$REPORT_JSON') as f:
    data = json.load(f)
hotspots = data.get('hotspots', [])

categories = {
    'allocators': r'malloc|free|calloc|realloc|__libc_malloc|_int_malloc|tc_malloc|je_malloc',
    'memory_operations': r'memcpy|memmove|memset|memcmp',
    'hash_functions': r'xxh64|xxh32|xxhash|XXH',
    'compression': r'deflate|inflate|crc32|compress',
    'crypto': r'AES|SHA|MD5|SM4|SSL|EVP_',
    'json': r'json_parse|sonic_parse|rapidjson',
    'pattern_matching': r'hyperscan|regex|pcre',
    'linear_algebra': r'gemv|gemm|blas|cblas',
    'sparse_linear_algebra': r'spmv|spgemm|sparse_blas',
    'math': r'\bsin\b|\bcos\b|\bexp\b|\blog\b|vml|svml',
    'dnn': r'conv[0-9]|pool[0-9]|relu|matmul|dnn',
    'fft': r'fft|ifft|dft',
    'video': r'encode|decode|h26',
    'serialization': r'protobuf|ParseFromArray|SerializeToString',
    'sql_acceleration': r'sparksql|codegen|native_sql',
    'network': r'tls_tx|tls_rx|tcp_|udp_',
    'kv_storage': r'rocksdb::|DBImpl|CompactionJob|MemTable|BlockBasedTable',
    'ascend_runtime': r'__pthread_mutex_lock|__pthread_rwlock_unlock|std::_Rb_tree|std::vector::_M_realloc_insert|std::_Hashtable',
}
for cat, pattern in categories.items():
    pct = sum(float(h['overhead'].rstrip('%')) for h in hotspots if re.search(pattern, h.get('symbol','')))
    if pct >= 0.5:
        print(f'{cat}: {pct}%')
"
```

> **关键**：任何类别热点占比 ≥ 0.5% 即表示该类别存在性能瓶颈信号，必须进入 Step 2 推荐流程。低于 0.5% 的类别跳过，不输出推荐。不要只关注 malloc/memcpy 而忽略其他类别。

### Step 2：库类型识别与推荐

采集完成后，先做全类别库类型识别与知识库规则匹配，再结合**热点证据强度**与**经验**生成候选库列表并给出推荐结论。证据+经验已能形成明确判断时可直接给结论；真实验证由主框架执行阶段按 Step 3 输出的验证流程设计落地。

#### 2.1 统一库类型识别

根据 Step 1 输出的依赖文本和 Perf 报告进行综合分析，所有类别的库采用相同的静态与动态匹配策略（依据"核心概念"综合检测映射表）：

```bash
# 输入: JSON 报告文件绝对路径；输出: {detected_libraries: [{category, current_lib, detection_method, evidence_sources, evidence_source}]}
bash scripts/detect_all_libraries.sh <JSON_REPORT_PATH>
```

> `current_run_id` 不由脚本输出，由调用方（LLM）在将 `detected_libraries` 注入规则引擎时绑定，用于 ascend_runtime 门控判定。

> **ascend_runtime 硬门控**：若 `evidence_source=blocked`，规则引擎不得输出推荐结论（见红线 #3）。

#### 2.2 规则引擎匹配

将环境指纹、统一库类型识别结果注入知识库规则树，计算替换路径：

```text
FOR each item in detected_libraries:
  category = item.category
  current_library = item.current_lib
  IF category == ascend_runtime AND item.evidence_source != current:
    → mark as blocked, skip recommendation  # 红线 #3 门控
  ELSE:
    FOR each competitor in knowledge_base[category]:
      IF competitor != current_library
        THEN add (current → competitor) to candidate_paths
    IF item.evidence_source != current:
      → cap confidence at experience_only/low  # 非门控类别降级
```

**核心逻辑**：
1. 根据识别出的库类别，在知识库 `library_profiles` 中查找该类别的所有优化库。
2. 无论当前库是默认库（如 glibc malloc、builtin hash）还是第三方库，只要同类别中有更优替代方案，即推荐替换。
3. **目标库不需要当前已安装**——只需知识库中有记录，后续通过 LD_PRELOAD 导入即可。
4. 替换路径的详细性能数据从 `library_profiles.<category>.<lib>.best_scenarios` 读取。
5. **run_id 绑定与历史数据隔离**：规则引擎输入的 `detected_libraries` 必须携带本轮 `current_run_id`。门控范围按类别区分：
   - **ascend_runtime 等 perf 依赖类别**：若 `evidence_source` 不是 `current`（即现场 perf 失败/降级/run_id 不一致），该类别必须标记为 `blocked`，不得输出推荐结论（权威定义见"安全与架构红线 #3"）。
   - **其他类别**：`evidence_source=static_only`（仅静态库命中）或 `evidence_source=blocked`（perf 失败）时仍可基于静态证据+经验给出推荐，但 `confidence` 不得高于 `experience_only`/`low`；`evidence_source=current` 时可按证据分级表标注 `medium`/`high`。

#### 2.3 证据分级与推荐策略

**全类别统一分级标准**：每个类别的热点占比独立评估，存在即推荐。

| 热点占比（按类别独立计算） | 证据等级 | 推荐策略 | 建议主框架验证次数 |
|---|---|---|---|
| > 5% | 高证据 | 强推荐匹配的候选库（`confidence: high`） | ≥3 次基准对比 |
| 2-5% | 中证据 | 推荐匹配的候选库（`confidence: medium`） | ≥2 次基准对比 |
| 0.5-2% | 低证据 | 按经验推荐（`confidence: low`） | ≥1 次基准对比 |
| < 0.5% | 无证据 | 该类别不推荐（`not_recommended`） | 跳过 |
| 无法采集 | 无证据 | 仅按经验给候选清单（`confidence: experience_only`） | 可选 |

> ascend_runtime 类别在"无法采集"时标记 `blocked`（见红线 #3）。
>
> **关键**：每个类别独立评估，不要求所有类别都达到阈值才推荐。只要某类别热点 ≥ 0.5%，就应给出对应的替换推荐。低于 0.5% 的类别视为噪声，跳过不推荐。

**经验推荐合法性**（可直接作为推荐/不推荐依据，无需强制实测）：
1. 热点函数与库匹配关系：malloc/free→allocator 类；memcpy/memset→stringlib 类。匹配则推荐，不匹配则不推荐。
2. 当前链接状态：`ldd`/`readelf` 显示已链接 jemalloc/tcmalloc → 直接进入运行时调优，不重复 LD_PRELOAD。
3. 历史跨场景经验：同类应用/同架构/同库的历史收益数据可作为推荐优先级依据。历史负收益可标"低优先级"，但不直接等同"不推荐"。
4. 场景适配性经验：aarch64+memcpy→bisheng-stringlib 推荐；x86+加密→ISA-L 推荐。架构/场景不匹配可直接 `not_recommended`。

详细的推荐依据表、可直接给出结论的合法情形、建议主框架优先验证的情形、验证产出 JSON 格式见 `references/library-playbook.md`。

### Step 3：验证流程设计

验证流程设计基于 Step 2 的证据分级生成，按证据等级决定推荐运行次数（低证据 ≥1 / 中证据 ≥2 / 高证据 ≥3），输出分为方法学与命令清单两部分（分别对应报告 [6] 与 [7]）：

1. **基准对比方法学**（报告 [6]）：基线对照 ≥1 次、单变量原则、加载验证必须用 `/proc/<pid>/maps`、回退确认。
2. **验证命令清单**（报告 [7]）：针对每个 `recommended` 库，输出 LD_PRELOAD 接入命令、加载验证命令、压测对比命令和回退命令（从 `references/library-playbook.md` 的安装 SOP 提取）。
3. **验证产出 JSON 模板**：主框架执行验证后回写结果，结构见 `references/library-playbook.md`。

## 关键方案：候选库类型映射

> 当某类别热点占比 ≥ 0.5% 时，按下表给出替换推荐。每类独立评估，不要求全部类别都有热点才推荐。低于 0.5% 的类别跳过。

| 热点类型 | 类别 | 候选库 | 选型优先顺序依据 |
|---|---|---|---|
| malloc/free | allocators | jemalloc、tcmalloc | 通用服务/中小对象/线程<32→jemalloc 优先；多线程高并发/线程>32→tcmalloc 优先；Ascend 场景→tcmalloc 优先（见 `references/ascend-playbook.md`） |
| memcpy/memset/memmove | memory_operations | bisheng-stringlib（含 optimized-routines 来源） | aarch64 预期 3-8% 加速；BiSheng 解压即用优先，optimized-routines 源码可控（详见 KB `memory_operations.bisheng-stringlib`） |
| 压缩（deflate/inflate） | compression | zlib、ISA-L | 按架构与压缩/速度偏好选 |
| 加密（AES/SHA/RSA） | crypto | openssl、isa-l_crypto、GMSSL | 通用→openssl；批量 AES→isa-l_crypto；国密合规→GMSSL |
| 校验（CRC32/Adler32/XXHash） | hash_functions | ISA-L CRC、xxhash | 按 CPU 架构选 |
| JSON 解析（json_parse/sonic_parse） | json | sonic-cpp、RapidJSON | sonic-cpp 在 aarch64 上性能最优；RapidJSON 跨平台兼容好 |
| 正则匹配（regex/pcre） | pattern_matching | Hyperscan | 高吞吐正则匹配，aarch64 需确认编译支持 |
| 矩阵运算（gemv/gemm/cblas） | linear_algebra | OpenBLAS、vectorBLAS | OpenBLAS 通用；vectorBLAS 在鲲鹏上 SVE 优化 |
| 稀疏矩阵（spmv/spgemm） | sparse_linear_algebra | SparseBLAS | 稀疏矩阵专用 |
| 数学函数（sin/cos/exp/log） | math | Libm、VML、SVML、autoGEMM | VML/SVML 向量化加速；autoGEMM 矩阵专用 |
| 深度学习算子（conv/pool/matmul） | dnn | oneDNN | CPU 侧 DNN 算子加速 |
| 傅里叶变换（fft/ifft） | fft | FFTW | 通用 FFT 库 |
| 视频编解码（encode/decode） | video | X264、X265 | 按编码格式选 |
| 序列化（protobuf） | serialization | Protobuf | 协议层优化 |
| SQL 加速（sparksql） | sql_acceleration | sparksql_native | Spark SQL 原生加速 |
| 网络 TLS/TCP（tls/tcp/udp） | network | KTLS | 内核 TLS 卸载 |
| KV 存储（rocksdb） | kv_storage | RocksDB、KAL-rocksdb | 嵌入式 KV 存储优化 |
| CANN runtime 锁竞争 | ascend_runtime | tcmalloc_for_cann | Ascend NPU host 侧 malloc 优化（需本轮 perf 证据门控） |

**替换方式**：始终优先构建期集成（符号绑定可靠），LD_PRELOAD 仅在完全无法重编译时作为 fallback，且必须先确认进程未链接优化分配器。详细安装 SOP、LD_PRELOAD 接入、运行时调优参数（MALLOC_CONF / TCMALLOC_*）见 `references/library-playbook.md`。

## 输出结构总览

本子 skill 的输出交付物由四部分组成（括号标注对应 Markdown 报告节）：

```
输出交付物                                → 报告节
├── 顶层元信息                             → [1][2]
│   ├── library_selection_mode      (aarch64_full_detection / generic_experience)
│   └── fallback_reason             (回退时填写)
├── all_library_verification_results[]   → [4][5][6]
│   └── { library_name, verification_status:not_verified, confidence, recommendation, reason, evidence_sources, ... }
├── candidate_actions[]                  → [7]
│   └── { action_id, title, category, priority, change_mode, implementation_plan, validation_plan, rollback, expected_gain_metric, evidence_refs, ... }
└── Markdown 体检报告                     → [1]-[8] 全文，内容取自上述结构化数据
```

- `candidate_actions[]` 与 `all_library_verification_results[]` 通过 `library_name` ↔ `action_id` 关联：`recommended` 库既出现在 results（含推荐理由/证据）也出现在 actions（含可执行命令/回退）；`not_recommended`/`inconclusive` 仅出现在 results。
- 单库 JSON 字段定义见 `references/library-playbook.md` 的"验证产出格式"。

## 最终输出报告（强制模板）

无论哪种模式，分析完成后必须输出以下 Markdown 格式（体检报告，仅方案不执行变更）：

```markdown
# 芯片级库替换调优体检报告

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
<库路径列表>
### Perf 热点函数（Top 10，5-10s 采样）
<函数名 百分比% 列表>

## [4] 库类型识别
| 类别 | 检测到的当前库 | 检测方式 | 证据来源 | 判定依据 |
|------|---------------|---------|---------|---------|
| <category> | <current_lib> | <静态/动态/综合> | <current/static_only/blocked> | <lsof/perf 依据> |

> 证据来源说明：`current`=本轮 perf 热点命中 / `static_only`=仅静态库（lsof/maps）命中 / `blocked`=perf 失败或降级（ascend_runtime 类别此时不得推荐）

## [5] 替换路径推荐
### 检测到可优化的库
| 类别 | 当前库 | 可替换目标 | 典型场景 | 预期收益 | 说明 |
|------|--------|-----------|---------|---------|------|
| <category> | <current_lib> | <candidate> | <best_scenarios 摘要> | <预期收益> | <说明> |
> 预期收益数据来自 `references/optimization_kb.json` 的 `library_profiles.<category>.<lib>.best_scenarios`；
> 历史实测参照见 `references/ascend-playbook.md`（仅作参照，不作判定依据）。

## [6] 验证流程设计（验证计划，不含具体命令）
> 具体命令清单见 [7]。

| 候选库 | 证据等级 | 建议验证次数 | 判定标准 |
|--------|---------|-------------|---------|
| <library> | <high/medium/low> | <N 次> | <基线对照+单变量+加载验证+中位数对比> |

## [7] 现场实施 SOP（命令清单，供主框架执行阶段使用）
> 以下命令需由候选动作在主框架执行阶段落地。完整 SOP 查阅 `references/optimization_kb.json` 中对应库的 `verification_steps` 字段与 `references/library-playbook.md`。

| 候选库 | LD_PRELOAD 接入命令 | 加载验证 | 压测对比 | 回退命令 |
|--------|---------------------|---------|---------|---------|
| <library> | <export LD_PRELOAD=...> | <grep <lib> /proc/<pid>/maps> | <压测命令> | <恢复原 LD_PRELOAD> |

## [8] Perf 热点分析结论
<perf 成功>：热点函数集中在 <函数列表>，建议优先优化类别 <category>（按热点占比从高到低逐类给出结论）。
<perf 权限不足且用户拒绝授权>：仅基于 lsof 静态分析，建议手动授权后重新分析。
```

## 输出契约

- `library_selection_mode`（`aarch64_full_detection` / `generic_experience`）
- `fallback_reason`
- **`all_library_verification_results`** — 所有候选库的推荐结论与验证流程设计（结构见"输出结构总览"与 `references/library-playbook.md`）
- **`candidate_actions[]`** — `recommended` 库的可执行候选动作（字段见 Candidate Action Contract）

每个库必须有明确的 `verification_status`（本子 skill 恒为 `not_verified`，真实验证由主框架执行阶段回写）、`recommendation`（`recommended` / `not_recommended` / `inconclusive`）、`confidence`（本子 skill 允许 `high` / `medium` / `low` / `experience_only` / `evidence_only`，**不得标 `measured`**）。`recommendation` 基于证据+经验生成；`not_recommended` 可基于证据不足+经验不匹配（`experience_only`/`low`）；`inconclusive` 仅用于无法安装/编译的客观限制。

在迭代编排语义下还应明确：性能库替换是否进入当前轮优先动作、哪些库替换建议作为下一轮候选保留、证据不足或兼容性风险过高时是否暂缓、哪些库基于证据+经验给结论（含依据）/ 哪些库无法验证（含原因）。主框架执行阶段按验证流程设计落地后回写 `verification_status=verified` 和 `confidence=measured`。

## Candidate Action Contract

每个 `candidate_actions[]` 必须包含 `action_id`、`title`、`category`、`priority`、`change_mode`、`requires_root`、`risk`、`implementation_plan`、`validation_plan`、`rollback`、`expected_effect`、`expected_gain_metric`、`rejection_criteria` 和 `evidence_refs`。`implementation_plan` 中包含候选 dry-run 命令（`commands_dry_run`）；`commands_execute` 由主框架执行阶段基于 `implementation_plan` 填充。LD_PRELOAD、重新链接、替换 allocator/string/crypto/compression 库和运行时环境变量调整必须在 rollback 中包含恢复原启动环境、库路径、二进制链接关系和目标实例身份复核步骤。

`not_recommended` 库不出现在 `candidate_actions[]`（仅在 results 中标注）。无法安装/编译时标记 `inconclusive`，记录 `installation_feasibility=not_feasible` 和通过只读检查（如包管理器查询 `yum list available` / 源码仓库可达性）获取的错误信息。本子 skill 不输出实测负收益结论；若主框架执行阶段验证出负收益，由主框架回写 `verification_status=verified` + `confidence=measured` + `recommendation=not_recommended`。

## 错误处理

| 场景 | 处理方式 |
|------|---------|
| `references/optimization_kb.json` 不存在 | 回退通用经验路径，记录 `fallback_reason=optimization_kb_missing` |
| 非 aarch64 架构 | 回退通用经验路径，记录 `fallback_reason=architecture_not_aarch64` |
| 用户未指定 PID 也未提供命令 | 输出"请指定要分析的程序 PID 或提供启动命令" |
| 离线模式进程启动或采样脚本失败 | 提示"离线分析闭环执行异常"，并尝试直接内联启动进行抓取 |
| perf 权限不足 | 与用户交互请求授权，授予后重试闭环采样；若拒绝则仅作 lsof 分析 |
| perf 不存在 | 报告错误信息 `perf 命令不存在，请安装（apt install linux-tools-generic）`，询问是否继续（跳过 perf 分析） |

## 数据来源约束

**严禁自行编造数据**。所有输出数据必须 100% 来自知识库或现场采集。脚本与脚本之间交互**仅限文本文件/JSON（如读取包含库列表的 txt）**，绝不依赖进程状态跨脚本驻留。

## Dependencies

| 工具/资源 | 用途 | 缺失影响 |
|------|------|---------|
| `perf` | 热点函数分析 | 无法定位库级别热点 |
| `readelf` | 检查当前链接库 | 无法判断是否已使用优化库 |
| `ldd` | 检查动态链接依赖 | 依赖分析降级 |
| `lsof` | 检查运行时加载的库 | 运行时分析降级 |
| `references/optimization_kb.json` | 全类别库替换知识库 | 回退通用经验路径 |
| `references/library-playbook.md` | 安装 SOP/运行时调参/推荐规则细则/验证方法学 | 操作细节缺失，仅能输出粗粒度方案 |
| `references/ascend-playbook.md` | Ascend NPU 场景经验线索与推荐策略 | Ascend 场景推荐降级为通用经验 |
| `../../references/prerequisites.md` | 公共依赖、perf/PMU 权限与降级 | 权限/降级处置无统一基准 |
| `scripts/detect_all_libraries.sh` | 统一库类型识别（静态+动态） | 库类型识别降级 |
| `scripts/perf_sampling_online.sh` | 在线进程 perf 采样 | 在线采样降级为内联方式 |
| `scripts/run_and_profile_offline.sh` | 离线闭环启动与采样 | 离线分析降级为内联方式 |

## Ascend NPU 实战经验与分配器选型（按需加载）

以下内容已沉淀到 references，按需加载：

- **Ascend NPU 推理 Host 侧 Malloc 优化**（背景/瓶颈/tcmalloc vs jemalloc 对比/LD_PRELOAD 注入/实战收益表/验证流程）→ `references/ascend-playbook.md` "Ascend NPU 推理 Host 侧 Malloc 优化" 章节
- **tcmalloc 运行时依赖陷阱**（libarcher TSAN 死锁/诊断方法/正确配置/rollback 要求）→ `references/ascend-playbook.md` "tcmalloc 运行时依赖陷阱" 章节
- **vLLM Caching Allocator 交互**（两层 allocator 架构/PYTORCH_NPU_ALLOC_CONF 调优/协同收益）→ `references/ascend-playbook.md` "vLLM Caching Allocator 交互" 章节
- **内存分配器选型决策树**（场景→分配器决策路径/依据映射/跨场景经验）→ `references/allocator-decision-guide.md`
