#!/bin/bash
# 统一库类型识别脚本 (静态 + 动态联合分析)
# 输入: JSON_REPORT_PATH (前置脚本生成的整合 JSON 报告文件绝对路径)
# 输出: JSON 格式，包含 detected_libraries 数组
# 检测类别: allocators, hash_functions, compression, crypto, json,
#           memory_operations, pattern_matching, linear_algebra, math,
#           dnn, fft, video, network, kv_storage

set -euo pipefail

REPORT_FILE=$1

if [[ -z "$REPORT_FILE" ]]; then
    echo '{"error": "JSON report path is required"}'
    exit 1
fi

if [[ ! -f "$REPORT_FILE" ]]; then
    echo '{"detected_libraries": [], "error": "report file not found"}'
    exit 0
fi

declare -a DETECTED=()

# 辅助函数：格式化并推入检测结果
add_detection() {
    local cat="$1"
    local lib="$2"
    local method="$3"
    local ev="$4"
    DETECTED+=("{\"category\":\"$cat\",\"current_lib\":\"$lib\",\"detection_method\":\"$method\",\"evidence\":\"$ev\"}")
}

# ==============================================================================
# 核心检测逻辑：通过 grep 查询 JSON 报告中的库名和函数符号 ("symbol": "...")
# ==============================================================================

# 1. 内存分配器 (allocators)
if grep -qi "jemalloc" "$REPORT_FILE"; then
    add_detection "allocators" "jemalloc" "综合" "检测到 jemalloc 特征库或调用"
elif grep -qi "tcmalloc" "$REPORT_FILE"; then
    add_detection "allocators" "tcmalloc" "综合" "检测到 tcmalloc 特征库或调用"
elif grep -qiE "\"symbol\": \"[^\"]*(malloc|calloc|free|realloc)" "$REPORT_FILE"; then
    add_detection "allocators" "glibc malloc" "动态" "热点包含标准 libc 内存分配函数"
fi

# 2. 哈希函数 (hash_functions)
if grep -qi "xxhash" "$REPORT_FILE"; then
    add_detection "hash_functions" "xxhash" "综合" "检测到 xxhash 库或热点函数"
elif grep -qiE "\"symbol\": \"[^\"]*hash" "$REPORT_FILE"; then
    add_detection "hash_functions" "builtin" "动态" "热点包含通用 hash 计算函数"
fi

# 3. 压缩库 (compression)
if grep -qi "isa-l" "$REPORT_FILE"; then
    add_detection "compression" "ISA-L" "综合" "检测到英特尔 ISA-L 加速库"
elif grep -qiE "libz\.so|\"symbol\": \"[^\"]*(deflate|inflate|crc32|compress)" "$REPORT_FILE"; then
    add_detection "compression" "zlib" "综合" "检测到 zlib 或标准压缩函数热点"
fi

# 4. 加密库 (crypto)
if grep -qi "gmssl" "$REPORT_FILE"; then
    add_detection "crypto" "GMSSL" "综合" "检测到国密 GMSSL 库"
elif grep -qiE "libcrypto|libssl|openssl|\"symbol\": \"[^\"]*(AES|SHA|MD5|SM4|SSL|TLS)" "$REPORT_FILE"; then
    add_detection "crypto" "openssl" "综合" "检测到 OpenSSL 或标准加密计算热点"
fi

# 5. JSON 解析 (json)
if grep -qi "sonic" "$REPORT_FILE"; then
    add_detection "json" "sonic-cpp" "综合" "检测到 sonic-cpp 高性能解析库"
elif grep -qi "rapidjson" "$REPORT_FILE"; then
    add_detection "json" "RapidJSON" "综合" "检测到 RapidJSON 解析库"
elif grep -qiE "\"symbol\": \"[^\"]*json" "$REPORT_FILE"; then
    add_detection "json" "builtin json" "动态" "热点包含通用 JSON 解析函数"
fi

# 6. 内存操作 (memory_operations)
if grep -qi "libmem" "$REPORT_FILE"; then
    add_detection "memory_operations" "libmem" "综合" "检测到 ARM 优化的 libmem 库"
elif grep -qi "libco" "$REPORT_FILE"; then
    add_detection "memory_operations" "libco" "综合" "检测到 libco 协程/内存库"
elif grep -qiE "\"symbol\": \"[^\"]*(memcpy|memset|memcmp|memmove)" "$REPORT_FILE"; then
    add_detection "memory_operations" "libc" "动态" "热点包含高频 libc 内存拷贝/初始化"
fi

# 7. 正则匹配 (pattern_matching)
if grep -qiE "hyperscan|libhs" "$REPORT_FILE"; then
    add_detection "pattern_matching" "Hyperscan" "综合" "检测到 Hyperscan 正则引擎"
elif grep -qiE "libpcre|\"symbol\": \"[^\"]*(regex|pcre)" "$REPORT_FILE"; then
    add_detection "pattern_matching" "PCRE" "综合" "检测到通用 PCRE 或正则匹配函数"
fi

# 8. 线性代数 (linear_algebra)
if grep -qi "openblas" "$REPORT_FILE"; then
    add_detection "linear_algebra" "OpenBLAS" "综合" "检测到 OpenBLAS 计算库"
elif grep -qiE "libblas|\"symbol\": \"[^\"]*(gemv|gemm|cblas|blas)" "$REPORT_FILE"; then
    add_detection "linear_algebra" "BLAS" "综合" "检测到通用 BLAS 库或矩阵运算热点"
fi

# 9. 数学运算 (math)
if grep -qiE "libvml|libsvml" "$REPORT_FILE"; then
    add_detection "math" "VML/SVML" "综合" "检测到向量化数学库 (VML/SVML)"
elif grep -qiE "libm\.so|\"symbol\": \"[^\"]*(sin|cos|exp|log)" "$REPORT_FILE"; then
    add_detection "math" "Libm" "综合" "检测到标准 Libm 或标量数学函数热点"
fi

# 10. 深度学习 (dnn)
if grep -qiE "\"symbol\": \"[^\"]*(conv[0-9]|pool[0-9]|relu|matmul|dnn)" "$REPORT_FILE"; then
    add_detection "dnn" "DNN Framework" "动态" "检测到深度学习算子特征"
fi

# 11. 傅里叶变换 (fft)
if grep -qiE "\"symbol\": \"[^\"]*(fft|ifft|dft)" "$REPORT_FILE"; then
    add_detection "fft" "FFT" "动态" "检测到傅里叶变换计算特征"
fi

# 12. 视频编解码 (video)
if grep -qi "x265" "$REPORT_FILE"; then
    add_detection "video" "X265" "综合" "检测到 X265 编码器"
elif grep -qi "x264" "$REPORT_FILE"; then
    add_detection "video" "X264" "综合" "检测到 X264 编码器"
elif grep -qiE "\"symbol\": \"[^\"]*(encode|decode|h26)" "$REPORT_FILE"; then
    add_detection "video" "Generic Video Codec" "动态" "检测到通用视频编解码热点"
fi

# 13. 网络通信 (network)
if grep -qiE "ktls|\"symbol\": \"[^\"]*(tls_tx|tls_rx)" "$REPORT_FILE"; then
    add_detection "network" "KTLS" "综合" "检测到内核 TLS (KTLS) 加速特征"
elif grep -qiE "\"symbol\": \"[^\"]*(send|recv|tcp_|udp_)" "$REPORT_FILE"; then
    add_detection "network" "Standard Network" "动态" "检测到标准网络协议栈收发热点"
fi

# 14. 键值存储引擎 (kv_storage)
if grep -qiE "librocksdbjni|librocksdb" "$REPORT_FILE"; then
    add_detection "kv_storage" "RocksDB" "综合" "检测到 RocksDB 动态库 (librocksdbjni/librocksdb)"
elif grep -qiE "\"symbol\": \"[^\"]*rocksdb::|\"symbol\": \"[^\"]*(DBImpl::Write|WriteBatch|CompactionJob|MemTable|BlockBasedTable)" "$REPORT_FILE"; then
    add_detection "kv_storage" "RocksDB" "动态" "热点包含 RocksDB LSM 引擎符号"
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