#!/usr/bin/env bash
# torch_npu（2.4.0-7.1.0 全系）× 毕昇 5.2.0（clang 19.1.7）缺省包含补丁
# 用法: <本脚本> <源码树> <组件> <版本>（由 AK_PATCH_SCRIPT(S) 钩子调用，须幂等）
#
# 适用判定: torch_npu 2.4.0 ~ 2.6.0-7.1.0 各分支（实测全系存在；该修复对
# 无此问题的分支无副作用——grep 幂等保护）。
# 症状: 编译报 use of undeclared identifier 'CLOCK_MONOTONIC_RAW'（utils.h）
#       / std::array 隐式实例化失败（tensorpipe socket.h）
# 根因: gcc 头文件传递包含宽松掩盖了缺失的显式包含，clang 严格模式下不可见。
# 版本断言: $3 已知且早于 2.4.0 → 跳过；未知 → 模式匹配保护应用。
set -euo pipefail
SRC="${1:?用法: patch.sh <源码树> <组件> <版本>}"
COMPONENT="${2:-}"
VERSION="${3:-}"
[[ "${COMPONENT}" == "torch_npu" ]] || { echo "[patch] 组件不匹配(${COMPONENT})，跳过"; exit 0; }

if [[ -n "${VERSION}" && ! "${VERSION}" =~ ^2\.[4-9]\. ]]; then
  echo "[patch] 版本 ${VERSION} 不在适用范围（2.4.0+），跳过"
  exit 0
fi
[[ -z "${VERSION}" ]] && echo "[patch] 未提供版本号，按代码模式匹配保护应用（不匹配自动 no-op）"

# 修改前一次性备份（幂等: cp -n；与主脚本 .bak_* 约定一致，便于回退与审计）
bak() { if [ -f "$1" ]; then cp -n "$1" "$1.bak_ak_patch" 2>/dev/null || true; fi; }

f="${SRC}/torch_npu/csrc/toolkit/profiler/common/utils.h"
if [ -f "$f" ] && ! grep -q '#include <time.h>' "$f"; then
  bak "$f"
  sed -i 's|#include <stdint.h>|#include <stdint.h>\n#include <time.h>|' "$f"
  echo "[patch] utils.h 补 <time.h>"
fi

f="${SRC}/third_party/Tensorpipe/tensorpipe/common/socket.h"
if [ -f "$f" ] && ! grep -q '#include <array>' "$f"; then
  bak "$f"
  sed -i 's|#include <sys/socket.h>|#include <sys/socket.h>\n#include <array>|' "$f"
  echo "[patch] socket.h 补 <array>"
fi

# 3) NPUException.h 用 std::memset 缺 <cstring>（v2.7.1 实测；同类传递包含问题）
f="${SRC}/torch_npu/csrc/core/npu/NPUException.h"
if [ -f "$f" ] && ! grep -q '#include <cstring>' "$f"; then
  bak "$f"
  sed -i 's|#include <stdexcept>|#include <stdexcept>\n#include <cstring>|' "$f"
  echo "[patch] NPUException.h 补 <cstring>"
fi

echo "[patch] torch_npu 缺省包含补丁完成（幂等；备份后缀 .bak_ak_patch）"
