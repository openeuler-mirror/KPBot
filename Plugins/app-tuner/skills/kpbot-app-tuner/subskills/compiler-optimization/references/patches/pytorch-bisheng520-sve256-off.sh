#!/usr/bin/env bash
# pytorch 全系（实测 2.6.0/2.7.1）× 毕昇 5.2.0（clang 19.1.7 / ld.lld）崩溃规避补丁
# 用法: <本脚本> <源码树> <组件> <版本>（由 AK_PATCH_SCRIPT(S) 钩子调用，须幂等）
#
# 适用判定: pytorch 全系（实测 v2.6.0/v2.7.1 必现；早期版本未测出不代表免疫——症状命中即挂载）。
# 症状: 编译期 clang 后端 EmitBackendOutput 段错误于
#       aten/src/ATen/native/cpu/ReduceOpsKernel.cpp.SVE256.cpp（SVE uint8
#       min_values 模板实例，instcombine pass），以及 ThinLTO 链接期 ld.lld
#       InstCombine 段错误（同一 IR 家族，-O0/-O1/--lto-O0 均不可绕）。
# 根因: 毕昇 5.2.0 LLVM 19.1.7 对该 SVE 内核 IR 的上游编译器 bug。
# 编译器探测: 毕昇 > 5.2.x（如 5.3+，上游已修）→ 跳过，避免无谓损失 SVE256
#            吞吐模式；探测不到编译器版本 → 按症状挂载（历史行为）。
# 方案: 剔除 SVE256 能力变体（运行时回落 DEFAULT 内核，功能正确仅吞吐模式
#       减少），编译期与链接期崩溃同时消除。
set -euo pipefail
SRC="${1:?用法: patch.sh <源码树> <组件> <版本>}"
COMPONENT="${2:-}"
VERSION="${3:-}"
[[ "${COMPONENT}" == "pytorch" ]] || { echo "[patch] 组件不匹配(${COMPONENT})，跳过"; exit 0; }

# 编译器版本断言：仅毕昇 5.2.x 需要；更新版本跳过（防"修复后仍剔除 SVE256"）。
# 注意: --version 输出首个版本号是 clang 内核（如 19.1.7），毕昇自版本号
# （如 5.2.0）只在含 "bisheng" 的行——必须先过滤该行再提取，否则恒误判跳过
if ! command -v clang >/dev/null 2>&1; then
  source /etc/profile.d/bisheng.sh 2>/dev/null || true
fi
CLANG_VER=""
if command -v clang >/dev/null 2>&1 && clang --version 2>/dev/null | grep -qi bisheng; then
  CLANG_VER=$(clang --version 2>/dev/null | grep -i bisheng | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
fi
if [[ -n "${CLANG_VER}" ]]; then
  MAJ="${CLANG_VER%%.*}"
  REST="${CLANG_VER#*.}"
  MIN="${REST%%.*}"
  if [[ "${MAJ}" -gt 5 || ( "${MAJ}" -eq 5 && "${MIN}" -gt 2 ) ]]; then
    echo "[patch] 检测到毕昇 ${CLANG_VER} > 5.2.x（InstCombine bug 上游已修），跳过剔除 SVE256"
    exit 0
  fi
  echo "[patch] 检测到毕昇 ${CLANG_VER}，适用（5.2.x InstCombine bug 规避）"
else
  echo "[patch] 未探测到毕昇编译器版本（非毕昇 clang 或未安装），按症状挂载（历史行为）"
fi

# 修改前一次性备份（幂等: cp -n；与主脚本 .bak_* 约定一致，便于回退与审计）
bak() { if [ -f "$1" ]; then cp -n "$1" "$1.bak_ak_patch" 2>/dev/null || true; fi; }

f="${SRC}/cmake/Codegen.cmake"
if [ -f "$f" ] && ! grep -q 'AK_PATCH_SVE256_OFF' "$f"; then
  bak "$f"
  sed -i 's|if(CXX_SVE256_FOUND)|if(OFF) # AK_PATCH_SVE256_OFF (bisheng instcombine crash on SVE kernels)|' "$f"
  echo "[patch] Codegen.cmake 剔除 SVE256 能力变体"
fi

echo "[patch] pytorch × bisheng 5.2.0 规避补丁完成（幂等；备份后缀 .bak_ak_patch；版本: ${VERSION:-未知}）"
