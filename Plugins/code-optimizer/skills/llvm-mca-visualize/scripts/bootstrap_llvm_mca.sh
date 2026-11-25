#!/usr/bin/env bash
# Bootstrap a patched llvm-mca (IssueTraceView, -issue-trace) for llvm-mca-visualize.
#
# Usage:
#   bootstrap_llvm_mca.sh [LLVM_SRC_ROOT] [BUILD_DIR]
#
#   LLVM_SRC_ROOT  default: $HOME/llvm-project
#   BUILD_DIR      default: <LLVM_SRC_ROOT>/build-mca
#
# Env:
#   JOBS=N         parallel build jobs (default: nproc)
#   SKILL_DIR      skill root (default: parent dir of this script)
#
# This script does NOT download LLVM. Obtain openEuler 17.0.6 source first
# (it contains AArch64SchedHIP10C/HIP11/HIP12 models):
#   git clone --depth 1 -b dev_17.0.6 \
#     https://github.com/openeuler-mirror/llvm-project.git ~/llvm-project
#   # Gitee mirror:
#   git clone --depth 1 -b dev_17.0.6 \
#     https://gitee.com/openeuler/llvm-project.git ~/llvm-project
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="${SKILL_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
SRC="${1:-$HOME/llvm-project}"
BUILD="${2:-$SRC/build-mca}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 8)}"

die() { echo "ERROR: $*" >&2; exit 1; }
warn() { echo "WARN: $*" >&2; }

[ -d "$SRC/llvm/tools/llvm-mca" ] || die "不是 LLVM monorepo 源码根: $SRC（请先 clone dev_17.0.6，见脚本头部 URL）"
[ -f "$SRC/llvm/lib/Target/AArch64/AArch64SchedHIP12.td" ] || \
  warn "未找到 AArch64SchedHIP12.td：hip11/hip12 模型可能缺失，请使用 openEuler dev_17.0.6 源码"

command -v python3 >/dev/null 2>&1 || die "缺少 python3"
command -v cmake >/dev/null 2>&1 || die "缺少 cmake（dnf install -y cmake）"
command -v ninja >/dev/null 2>&1 || die "缺少 ninja（dnf install -y ninja-build）"
command -v c++ >/dev/null 2>&1 || command -v g++ >/dev/null 2>&1 || \
  die "缺少 C++ 编译器（dnf install -y gcc gcc-c++）"

PATCH_DIR="$SKILL_DIR/scripts/llvm-mca-issue-trace"
[ -f "$PATCH_DIR/IssueTraceView.h" ] && [ -f "$PATCH_DIR/IssueTraceView.cpp" ] || \
  die "未找到补丁源: $PATCH_DIR/IssueTraceView.{h,cpp}"

echo "== 1/4 copy IssueTraceView =="
cp "$PATCH_DIR/IssueTraceView.h" "$PATCH_DIR/IssueTraceView.cpp" \
   "$SRC/llvm/tools/llvm-mca/Views/"

echo "== 2/4 patch llvm-mca =="
python3 "$PATCH_DIR/patch_llvm_mca.py" "$SRC"

echo "== 3/4 cmake + ninja (Release, AArch64 only, -j$JOBS) =="
cmake -S "$SRC/llvm" -B "$BUILD" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_TARGETS_TO_BUILD=AArch64 \
  -DLLVM_ENABLE_PROJECTS= \
  -DLLVM_INCLUDE_TESTS=OFF \
  -DLLVM_INCLUDE_BENCHMARKS=OFF
ninja -C "$BUILD" -j "$JOBS" llvm-mca

echo "== 4/4 verify =="
BIN="$BUILD/bin/llvm-mca"
[ -x "$BIN" ] || die "构建结束但未找到可执行文件: $BIN"
"$BIN" --help 2>&1 | grep -q -- 'issue-trace' || \
  die "$BIN 不含 -issue-trace，补丁未生效（检查源码版本/补丁步骤）"

echo "OK: $BIN"
echo "运行: python3 $SKILL_DIR/scripts/mca_visualize.py --asm <file.s> --mca-bin $BIN"
