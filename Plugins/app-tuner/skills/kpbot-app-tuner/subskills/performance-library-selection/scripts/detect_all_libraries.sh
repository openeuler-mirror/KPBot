#!/bin/bash
# 统一库类型识别脚本 (静态 + 动态联合分析)
# 输入: REPORT_PATH — 支持以下格式（自动检测）:
#   1. 含 "symbol" 字段的标准 JSON 报告（原始格式）
#   2. perf report 文本（含 "perf report" 或 "%" + "[.]" 行）
#   3. 主框架 evidence_report.json（含 perf_data_path / evidence_files 等字段）
#   4. 包含 maps_libs / lsof_libs 路径引用的 JSON
# 输出: JSON 格式，包含 detected_libraries 数组
# 检测类别: allocators, hash_functions, compression, crypto, json,
#           memory_operations, pattern_matching, linear_algebra, math,
#           dnn, fft, video, network, kv_storage, sparse_linear_algebra,
#           serialization, sql_acceleration, ascend_runtime

set -euo pipefail

REPORT_FILE=$1

if [[ -z "$REPORT_FILE" ]]; then
    echo '{"error": "report path is required"}'
    exit 1
fi

if [[ ! -f "$REPORT_FILE" ]]; then
    echo '{"detected_libraries": [], "error": "report file not found"}'
    exit 0
fi

# ==============================================================================
# 输入格式自动检测与归一化
# 将所有输入格式统一转换为含 "symbol" 字段的 JSON，供后续 grep 检测使用
# ==============================================================================
NORMALIZED_FILE=""
CLEANUP_TEMP=false

# 检查输入是否已经是含 symbol 字段的标准 JSON
if grep -qE '"symbol"\s*:' "$REPORT_FILE" 2>/dev/null; then
    NORMALIZED_FILE="$REPORT_FILE"
else
    # 需要归一化，创建临时文件
    NORMALIZED_FILE=$(mktemp /tmp/detect_libs_input_XXXXXX.json)
    CLEANUP_TEMP=true

    # 检测输入格式类型
    INPUT_TYPE="unknown"
    if grep -qE '^\s+[0-9]+\.[0-9]+%\s+' "$REPORT_FILE" 2>/dev/null && grep -qE '\[\.\]' "$REPORT_FILE" 2>/dev/null; then
        INPUT_TYPE="perf_report"
    elif grep -qE '"perf_data_path"|"evidence_files"|"perf_report_path"|"loaded_libraries"|"perf_report"|"maps_libs_path"|"perf_available"' "$REPORT_FILE" 2>/dev/null; then
        INPUT_TYPE="evidence_report_json"
    elif grep -qE '"hot_symbols"|"detected_libraries"' "$REPORT_FILE" 2>/dev/null; then
        INPUT_TYPE="structured_json_no_symbol"
    fi

    case "$INPUT_TYPE" in
        perf_report)
            # 从 perf report 文本提取符号，构建含 "symbol" 字段的 JSON
            python3 -c "
import re, json, sys
symbols = []
dso_lines = []
with open('$REPORT_FILE', errors='ignore') as f:
    for line in f:
        m = re.match(r'\s+([\d.]+)%\s+\S+\s+\S+\s+\[.\]\s+(.+)', line)
        if m:
            pct = float(m.group(1))
            sym = m.group(2).strip()
            if pct > 0.01 and not sym.startswith('0x'):
                symbols.append({'symbol': sym, 'overhead_pct': pct})
        if '.so' in line:
            dso_lines.append(line.strip())
report = {'hot_symbols': symbols[:100], 'perf_report_text': dso_lines[:50], 'raw_text': open('$REPORT_FILE', errors='ignore').read()[:5000]}
print(json.dumps(report, ensure_ascii=False))
" > "$NORMALIZED_FILE" 2>/dev/null || true
            # 如果 perf_report 文件为空或提取失败，直接用原始文件内容
            if [[ ! -s "$NORMALIZED_FILE" ]]; then
                cp "$REPORT_FILE" "$NORMALIZED_FILE"
            fi
            ;;
        evidence_report_json)
            # 从主框架 evidence_report.json 提取信息并读取引用的外部文件
            # 注意：仅读取已有 perf report 文本，不调用 perf report 解析 perf.data（避免检测阶段阻塞）
            python3 -c "
import json, os, re, sys
with open('$REPORT_FILE', errors='ignore') as f:
    text = f.read()
report = {}
try:
    data = json.loads(text)
    # 提取已加载的库列表
    for key in ('loaded_libraries', 'maps_libs', 'lsof_libs'):
        if key in data:
            report['loaded_libraries'] = data[key]
            break
    # 提取 perf report 文本路径（仅读取已有文本，不调用 perf report）
    # 优先级：perf_self_report_path（Self% 干净三列格式）> perf_report_path > evidence_files.perf_report > 顶层 perf_report
    evidence_files = data.get('evidence_files', {})
    perf_report_path = ''
    # 1. 优先使用 perf_self_report_path（perf_sampling_online.sh 生成的 --no-children 格式）
    if data.get('perf_self_report_path'):
        perf_report_path = data['perf_self_report_path']
    # 2. perf.perf_report_path（主框架格式）
    elif isinstance(data.get('perf'), dict):
        perf_report_path = data['perf'].get('perf_report_path', '')
    # 3. evidence_files.perf_report
    if not perf_report_path and isinstance(evidence_files, dict):
        perf_report_path = evidence_files.get('perf_report', '')
    # 4. 顶层 perf_report（perf_sampling_online.sh 输出，可能为 call-graph 格式）
    if not perf_report_path:
        perf_report_path = data.get('perf_report', '')
    perf_text = ''
    if perf_report_path and os.path.isfile(perf_report_path) and os.path.getsize(perf_report_path) > 0:
        with open(perf_report_path, errors='ignore') as pf:
            perf_text = pf.read()
    symbols = []
    dso_lines = []
    if perf_text:
        for line in perf_text.split('\n'):
            m = re.match(r'\s+([\d.]+)%\s+\S+\s+\S+\s+\[.\]\s+(.+)', line)
            if m:
                pct = float(m.group(1))
                sym = m.group(2).strip()
                if pct > 0.01 and not sym.startswith('0x'):
                    symbols.append({'symbol': sym, 'overhead_pct': pct})
            if '.so' in line:
                dso_lines.append(line.strip())
    # 读取 evidence_files 中引用的外部文件（maps_libs、lsof_libs、dso_rank、cann_libs）
    extra_text_parts = []
    if isinstance(evidence_files, dict):
        for fk in ('maps_libs', 'lsof_libs', 'dso_rank', 'cann_libs', 'perf_report', 'env_vars'):
            fp = evidence_files.get(fk, '')
            if fp and os.path.isfile(fp) and os.path.getsize(fp) > 0:
                try:
                    with open(fp, errors='ignore') as ef:
                        extra_text_parts.append(ef.read())
                except Exception:
                    pass
    # 兼容 perf_sampling_online.sh 直接输出的 *_path 顶层字段
    for pk in ('maps_libs_path', 'lsof_libs_path', 'environ_path'):
        fp = data.get(pk, '')
        if fp and os.path.isfile(fp) and os.path.getsize(fp) > 0:
            try:
                with open(fp, errors='ignore') as ef:
                    extra_text_parts.append(ef.read())
            except Exception:
                pass
    extra_text = '\n'.join(extra_text_parts)
    # 同时把原始 JSON 文本也包含进来，供 grep 检测库名
    report['hot_symbols'] = symbols[:100]
    report['perf_report_text'] = dso_lines[:50]
    report['extra_evidence_text'] = extra_text
    report['raw_json'] = text
    print(json.dumps(report, ensure_ascii=False))
except json.JSONDecodeError:
    # 非 JSON，当作纯文本处理
    print(json.dumps({'raw_text': text[:5000]}, ensure_ascii=False))
" > "$NORMALIZED_FILE" 2>/dev/null || true
            ;;
        structured_json_no_symbol)
            # 已是 JSON 但不含 symbol 字段，尝试从嵌套结构提取
            python3 -c "
import json, re
with open('$REPORT_FILE', errors='ignore') as f:
    text = f.read()
report = {}
try:
    data = json.loads(text)
    # 递归查找 symbol 相关字段
    def extract_symbols(obj, depth=0):
        if depth > 5:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ('symbol', 'func', 'function', 'name') and isinstance(v, str):
                    report.setdefault('hot_symbols', []).append({'symbol': v})
                else:
                    extract_symbols(v, depth+1)
        elif isinstance(obj, list):
            for item in obj:
                extract_symbols(item, depth+1)
    extract_symbols(data)
    report['raw_json'] = text
except json.JSONDecodeError:
    report = {'raw_text': text[:5000]}
print(json.dumps(report, ensure_ascii=False))
" > "$NORMALIZED_FILE" 2>/dev/null || true
            ;;
        *)
            # 未知格式：直接使用原始文件内容，grep 可能仍能匹配库名
            # 尝试从纯文本中提取 .so 和函数符号
            python3 -c "
import re, json
symbols = []
with open('$REPORT_FILE', errors='ignore') as f:
    text = f.read()
    for line in text.splitlines():
        m = re.match(r'\s+([\d.]+)%\s+\S+\s+\S+\s+\[.\]\s+(.+)', line)
        if m:
            pct = float(m.group(1))
            sym = m.group(2).strip()
            if pct > 0.01 and not sym.startswith('0x'):
                symbols.append({'symbol': sym, 'overhead_pct': pct})
# raw_text 保留原始内容，供 grep 匹配库名（lsof/maps 纯文本列表场景）
report = {'hot_symbols': symbols[:100], 'raw_text': text[:5000]}
print(json.dumps(report, ensure_ascii=False))
" > "$NORMALIZED_FILE" 2>/dev/null || true
            # 如果 python3 失败，直接用原文件
            if [[ ! -s "$NORMALIZED_FILE" ]]; then
                cp "$REPORT_FILE" "$NORMALIZED_FILE"
            fi
            ;;
    esac
fi

# 后续检测统一使用 NORMALIZED_FILE
# 如果归一化失败（临时文件为空），回退到原始文件
if [[ ! -s "$NORMALIZED_FILE" ]]; then
    NORMALIZED_FILE="$REPORT_FILE"
    CLEANUP_TEMP=false
fi

# 清理临时文件的 trap
trap '[ "$CLEANUP_TEMP" = true ] && [ -f "$NORMALIZED_FILE" ] && rm -f "$NORMALIZED_FILE"' EXIT

declare -a DETECTED=()

# 辅助函数：格式化并推入检测结果
# 参数: <category> <current_lib> <method> <evidence> [evidence_source]
# evidence_source: current（现场 perf symbol 命中）/ static_only（仅静态库名命中）/ blocked（perf 失败或降级）
# 默认 static_only；仅当该检测分支命中 perf "symbol" 字段时才由调用方显式传 current
add_detection() {
    local cat="$1"
    local lib="$2"
    local method="$3"
    local ev="$4"
    local esrc="${5:-static_only}"
    DETECTED+=("{\"category\":\"$cat\",\"current_lib\":\"$lib\",\"detection_method\":\"$method\",\"evidence_sources\":[\"$ev\"],\"evidence_source\":\"$esrc\"}")
}

# ==============================================================================
# 通用检测函数：静态(grep库名) + 动态(grep symbol字段) 联合分析
# 参数: category static_specs dynamic_pattern default_lib
#   static_specs: 逗号分隔的 "pattern:displayname" 对（无 : 时 pattern 即显示名）
#   dynamic_pattern: symbol 字段 grep -E 模式（空=无动态检测）
#   default_lib: 动态命中时的默认当前库
# ==============================================================================
detect_category() {
    local category="$1" static_specs="$2" dynamic_pat="$3" default_lib="$4"
    local static_hit=false dynamic_hit=false
    local detected_lib=""

    # 静态检测：遍历 static_specs 中的每个 pattern:displayname 对
    if [[ -n "$static_specs" ]]; then
        local IFS=','
        for spec in $static_specs; do
            local pat name
            if [[ "$spec" == *:* ]]; then
                pat="${spec%%:*}"
                name="${spec##*:}"
            else
                pat="$spec"
                name="$spec"
            fi
            if grep -qiE "$pat" "$NORMALIZED_FILE"; then
                static_hit=true
                detected_lib="$name"
                break
            fi
        done
    fi

    # 动态检测：在 symbol 字段中匹配
    if [[ -n "$dynamic_pat" ]]; then
        grep -qiE "\"symbol\": \"[^\"]*($dynamic_pat)" "$NORMALIZED_FILE" && dynamic_hit=true
    fi

    if $static_hit && $dynamic_hit; then
        add_detection "$category" "$detected_lib" "综合" "检测到 $detected_lib 库及相关热点函数" "current"
    elif $static_hit; then
        add_detection "$category" "$detected_lib" "静态" "检测到 $detected_lib 特征库" "static_only"
    elif $dynamic_hit; then
        add_detection "$category" "$default_lib" "动态" "检测到 $category 相关热点函数" "current"
    fi
}

# ==============================================================================
# 18 类库检测
# 1-17 使用通用 detect_category 函数；18 (ascend_runtime) 有特殊门控逻辑
# ==============================================================================

# 1. 内存分配器 (allocators)
detect_category "allocators" "jemalloc:jemalloc,tcmalloc:tcmalloc" "malloc|calloc|free|realloc" "glibc malloc"

# 2. 哈希函数 (hash_functions) — 动态模式排除 std::hash/std::_Hashtable 误匹配
detect_category "hash_functions" "xxhash:xxhash" "xxh64|xxh32|xxhash|XXH" "builtin"

# 3. 压缩库 (compression)
detect_category "compression" "isa-l:ISA-L,libz\.so:zlib" "deflate|inflate|crc32|compress" "zlib"

# 4. 加密库 (crypto)
detect_category "crypto" "gmssl:GMSSL,libcrypto|libssl|openssl:openssl" "AES|SHA|MD5|SM4|SSL|EVP_" "openssl"

# 5. JSON 解析 (json)
detect_category "json" "sonic:sonic-cpp,rapidjson:RapidJSON" "json_parse|sonic_parse|rapidjson" "builtin json"

# 6. 内存操作 (memory_operations)
detect_category "memory_operations" "libmem:libmem,libco\.so:libco,libstringlib:bisheng-stringlib" "memcpy|memset|memcmp|memmove" "libc"

# 7. 正则匹配 (pattern_matching)
detect_category "pattern_matching" "hyperscan|libhs:Hyperscan,libpcre:PCRE" "regex|pcre" "PCRE"

# 8. 线性代数 (linear_algebra)
detect_category "linear_algebra" "openblas:OpenBLAS,libblas:BLAS" "gemv|gemm|cblas|blas" "BLAS"

# 9. 稀疏矩阵运算 (sparse_linear_algebra)
detect_category "sparse_linear_algebra" "libspblas:SparseBLAS" "spmv|spgemm|sparse_blas" "SparseBLAS"

# 10. 数学运算 (math)
detect_category "math" "libvml|libsvml:VML/SVML,libm\.so:Libm" "\bsin\b|\bcos\b|\bexp\b|\blog\b|\bsqrt\b|\bpow\b|\bsincos\b" "Libm"

# 11. 深度学习 (dnn) — 仅动态检测
detect_category "dnn" "" "conv[0-9]|pool[0-9]|relu|matmul|dnn" "DNN Framework"

# 12. 傅里叶变换 (fft)
detect_category "fft" "libfftw3:FFTW" "fft|ifft|dft" "FFTW"

# 13. 视频编解码 (video)
detect_category "video" "x265:X265,x264:X264" "encode|decode|h26" "Generic Video Codec"

# 14. 序列化 (serialization)
detect_category "serialization" "libprotobuf:Protobuf" "protobuf|ParseFromArray|SerializeToString" "Protobuf"

# 15. SQL 加速 (sql_acceleration)
detect_category "sql_acceleration" "libsparksql_native:sparksql_native" "sparksql|codegen|native_sql" "sparksql_native"

# 16. 网络通信 (network)
detect_category "network" "ktls:KTLS" "tls_tx|tls_rx|send|recv|tcp_|udp_" "Standard Network"

# 17. 键值存储引擎 (kv_storage)
detect_category "kv_storage" "librocksdbjni|librocksdb:RocksDB" "rocksdb::|DBImpl|CompactionJob|MemTable|BlockBasedTable" "RocksDB"

# 18. Ascend NPU 运行时 (ascend_runtime) — 特殊门控逻辑
# 检测 CANN/torch_npu 库加载，并识别其内部容器操作隐含的 malloc 锁竞争
# 硬门控：必须有本轮现场 perf 采集成功（report 中含非空 symbol 字段）才能输出推荐结论；
#         perf 失败/降级/缺失时，ascend_runtime 标记为 blocked 供规则引擎门控（规则引擎不得基于此输出推荐结论）
if grep -qiE "libruntime_v100|libtorch_npu|libhccl\.so|libascendcl" "$NORMALIZED_FILE"; then
    ASCEND_LOCK_HIT=false
    PERF_CURRENT=false
    # 判断是否本轮现场 perf 采集成功（report 中含非空 symbol 字段）
    if grep -qE "\"symbol\":\s*\"[^\"]+\"" "$NORMALIZED_FILE"; then
        PERF_CURRENT=true
    fi
    if [[ "$PERF_CURRENT" == "true" ]]; then
        # 计算锁竞争相关 symbol 的总占比（阈值 >5% 才判定为 CANN 锁竞争）
        # 锁竞争口径：pthread_mutex/rwlock 的 lock+unlock 变体（含 _usercnt 后缀），不含 sem_ 信号量
        # 兼容两种 JSON 格式：{"overhead":"X%","symbol":"..."} 和 {"symbol":"...","overhead_pct":X}
        LOCK_PCT=$(python3 -c "
import json, re
try:
    with open('$NORMALIZED_FILE') as f:
        text = f.read()
    total = 0.0
    # 锁竞争 symbol 模式：去掉 __ 前缀以同时匹配 __pthread_mutex_lock 和 pthread_mutex_lock
    lock_re = r'pthread_mutex_lock|pthread_mutex_unlock|pthread_rwlock_unlock|pthread_rwlock_rdlock|pthread_rwlock_wrlock'
    # 方式1: 解析为 JSON 后遍历 hot_symbols / hotspots 数组
    try:
        data = json.loads(text)
        arr = data.get('hot_symbols', data.get('hotspots', []))
        for item in arr:
            if not isinstance(item, dict):
                continue
            sym = item.get('symbol', '')
            ov = item.get('overhead', item.get('overhead_pct', 0))
            if isinstance(ov, str):
                ov = float(ov.rstrip('%'))
            else:
                ov = float(ov)
            if re.search(lock_re, sym):
                total += ov
    except (json.JSONDecodeError, TypeError):
        pass
    # 方式2: 正则兜底（处理非标准 JSON 文本）
    if total == 0.0:
        for m in re.finditer(r'(?:\"overhead\":\s*\"([\d.]+)%\"|\"overhead_pct\":\s*([\d.]+)).*?\"symbol\":\s*\"([^\"]+)\"', text):
            pct = float(m.group(1) or m.group(2))
            sym = m.group(3)
            if re.search(lock_re, sym):
                total += pct
    print(f'{total:.2f}')
except:
    print('0.00')
" 2>/dev/null || echo "0.00")
        # 锁竞争占比 >5% 才标记 CANN 锁竞争，否则仅标记 CANN 库加载
        if awk "BEGIN{exit !($LOCK_PCT > 5)}"; then
            ASCEND_LOCK_HIT=true
        fi
        if [[ "$ASCEND_LOCK_HIT" == "true" ]]; then
            add_detection "ascend_runtime" "glibc malloc (CANN 锁竞争)" "综合" "检测到 CANN/torch_npu 库加载且本轮 perf 锁竞争热点合计 ${LOCK_PCT}%（>5% 阈值），建议二次归因后评估 tcmalloc 替换" "current"
        else
            add_detection "ascend_runtime" "glibc malloc" "综合" "检测到 CANN/torch_npu 库加载，本轮 perf 采集成功但锁竞争热点合计仅 ${LOCK_PCT}%（低于 5% 阈值），CANN 锁竞争信号弱" "current"
        fi
    else
        # perf 采集失败/降级/缺失：ascend_runtime 标记 blocked，禁止基于知识库历史数据推荐
        add_detection "ascend_runtime" "glibc malloc" "静态" "检测到 CANN/torch_npu 库加载，但本轮 perf 采集失败或缺失，ascend_runtime 类别标记 blocked，禁止基于历史数据推荐替换" "blocked"
    fi
fi

# ==============================================================================
# 组装输出
# ==============================================================================
if [[ ${#DETECTED[@]} -eq 0 ]]; then
    echo '{"detected_libraries": []}'
else
    # 使用逗号拼接数组元素，构建标准 JSON 输出
    JSON_LIBS=$(IFS=,; echo "${DETECTED[*]}")
    echo "{\"detected_libraries\": [${JSON_LIBS}]}"
fi
