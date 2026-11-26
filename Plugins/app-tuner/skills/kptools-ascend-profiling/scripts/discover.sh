#!/bin/bash
# ascend-profiling discover — 检查链：NPU 设备 → torch_npu → msserviceprofiler → vLLM 可选
set -euo pipefail

print_help() {
    cat <<'EOF'
ascend-profiling discover — Check profiling capability per component

Output: 分项状态 + 两方案可用性结论（AVAILABLE / OPTIONAL_ABSENT / MISSING）
  exit 0: 至少一方案可用；2: 两方案均不可用
EOF
    exit 0
}

[ "${1:-}" = "--help" ] && print_help

missing_any=0

# 1. NPU 设备
if ls /dev/davinci* &>/dev/null; then
    echo "NPU device: AVAILABLE ($(ls /dev/davinci* 2>/dev/null | wc -l) nodes；可先用 kptools-ascend-smi 诊断)"
else
    echo "NPU device: NO_ASCEND_HARDWARE（无 /dev/davinci* 设备）"
    missing_any=1
fi

# 2. torch_npu（方案一）
if python3 -c "import torch, torch_npu" &>/dev/null; then
    echo "torch_npu: AVAILABLE（方案一 Ascend PyTorch Profiler 可用）"
else
    echo "torch_npu: MISSING（pip install torch torch_npu；方案一不可用）"
    missing_any=1
fi

# 3. msserviceprofiler（方案二）
if command -v msserviceprofiler &>/dev/null; then
    echo "msserviceprofiler: AVAILABLE（方案二 MS Service Profiler 可用）"
else
    echo "msserviceprofiler: MISSING（CANN toolkit 自带或从 gitcode.com/Ascend/msserviceprofiler 构建；方案二不可用）"
    missing_any=1
fi

# 4. vLLM 服务（仅在提供了 -u 时探测）
if [ $# -gt 0 ] && [ "$1" = "-u" ] && [ -n "${2:-}" ]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$2/health" 2>/dev/null) || code="000"
    if [ "$code" = "200" ]; then
        echo "vLLM service: AVAILABLE ($2)"
    else
        echo "vLLM service: SERVICE_UNREACHABLE ($2, HTTP $code；方案一需服务以 --profiler-config 启动)"
    fi
fi

if [ "$missing_any" = "1" ]; then
    echo "status: 2（两方案均不可用或部分组件缺失，见上）"
    exit 2
fi
echo "status: READY"
