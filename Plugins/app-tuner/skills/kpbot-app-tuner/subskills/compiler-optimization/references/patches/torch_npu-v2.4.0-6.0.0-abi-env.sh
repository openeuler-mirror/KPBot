#!/usr/bin/env bash
# torch_npu v2.4.0-6.0.0 × 毕昇 5.2.0（clang 19.1.7）兼容补丁集
# 用法: <本脚本> <源码树> <组件> <版本>（由 AK_PATCH_SCRIPT(S) 钩子调用，须幂等）
#
# 适用判定: torch_npu v2.4.0-6.0.0 分支（该分支代码在本 clang 下必现以下问题；
# 其他版本无需本补丁——2.5.1+/2.6.0 分支已自行处理）。
# 版本断言: $3 已知且非适用范围 → 跳过（防误挂到已自行修复的分支）；
#           $3 未知（空）→ 提示后按 sed 模式匹配保护应用（不匹配即 no-op）。
set -euo pipefail
SRC="${1:?用法: patch.sh <源码树> <组件> <版本>}"
COMPONENT="${2:-}"
VERSION="${3:-}"
[[ "${COMPONENT}" == "torch_npu" ]] || { echo "[patch] 组件不匹配(${COMPONENT})，跳过"; exit 0; }

if [[ -n "${VERSION}" && ! "${VERSION}" =~ ^2\.4\.0-6\.0 ]]; then
  echo "[patch] 版本 ${VERSION} 不在适用范围（v2.4.0-6.0.0），跳过（2.5.1+/2.6.0 已自行处理）"
  exit 0
fi
[[ -z "${VERSION}" ]] && echo "[patch] 未提供版本号，按代码模式匹配保护应用（不匹配自动 no-op）"

# 修改前一次性备份（幂等: cp -n；与主脚本 .bak_* 约定一致，便于回退与审计）
bak() { if [ -f "$1" ]; then cp -n "$1" "$1.bak_ak_patch" 2>/dev/null || true; fi; }

# 1) profiler utils.h 缺 <time.h>：CLOCK_MONOTONIC_RAW 靠传递包含，
#    gcc 头文件链宽松掩盖，clang 下报 use of undeclared identifier
#    （与 torch_npu-clang19-missing-includes.sh 同款修复，双脚本各自幂等，
#     同时挂载时 grep 守卫保证只生效一次；⚠️ 若修改此处模式，须同步另一脚本——
#     根治方案见 P-B 补丁注册表/签名库外置）
f="${SRC}/torch_npu/csrc/toolkit/profiler/common/utils.h"
if [ -f "$f" ] && ! grep -q '#include <time.h>' "$f"; then
  bak "$f"
  sed -i 's|#include <stdint.h>|#include <stdint.h>\n#include <time.h>|' "$f"
  echo "[patch] utils.h 补 <time.h>"
fi

# 2) tensorpipe socket.h 缺 <array>：std::array 同因（传递包含差异）
f="${SRC}/third_party/Tensorpipe/tensorpipe/common/socket.h"
if [ -f "$f" ] && ! grep -q '#include <array>' "$f"; then
  bak "$f"
  sed -i 's|#include <sys/socket.h>|#include <sys/socket.h>\n#include <array>|' "$f"
  echo "[patch] socket.h 补 <array>"
fi

# 3) setup.py 硬编码 ABI=0 且不给 cmake 传 GLIBCXX：与 ABI=1 全链基线混链，
#    wheel 内部 libtorch_npu(新) vs libtensorpipe(旧) 符号不一致，import 必挂。
#    补丁让 setup.py 认环境变量（cmake -D + define_macros 双处）
f="${SRC}/setup.py"
if [ -f "$f" ]; then
  bak "$f"
  python3 - "$f" <<'PYABI'
from pathlib import Path
import sys

path = Path(sys.argv[1])
before = path.read_text()
abi = "os.environ.get('GLIBCXX_USE_CXX11_ABI', '1')"
anchor = "cmake_args.append('-DBUILD_TENSORPIPE=on')"
addition = f"cmake_args.append('-DGLIBCXX_USE_CXX11_ABI=' + {abi})"
lines = before.splitlines(keepends=True)
if not any(line.strip() == addition for line in lines):
    for index, line in enumerate(lines):
        if line.strip() == anchor:
            indent = line[:len(line) - len(line.lstrip())]
            lines.insert(index + 1, indent + addition + "\n")
            break
after = "".join(lines)
for name in ("_GLIBCXX_USE_CXX11_ABI", "GLIBCXX_USE_CXX11_ABI"):
    after = after.replace(f"('{name}', '0')", f"('{name}', os.environ.get('{name}', '1'))")
if after != before:
    path.write_text(after)
PYABI
fi

echo "[patch] torch_npu v2.4.0-6.0.0 兼容补丁完成（幂等；备份后缀 .bak_ak_patch）"
