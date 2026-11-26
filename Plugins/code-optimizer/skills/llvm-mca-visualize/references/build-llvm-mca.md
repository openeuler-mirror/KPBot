# 构建 patched llvm-mca（IssueTraceView / `-issue-trace`）

本 skill 的端口热力图、逐资源端口压力和真实 issue 数据都来自打过 `IssueTraceView` 补丁的 llvm-mca；未提供时工具**直接报错**，没有近似回退。

## 已验证环境

- openEuler 24.03 LTS-SP2，aarch64，CPU part `0xd06`（hip12）。
- Python 3.11（仅标准库）。
- LLVM 17.0.6，openEuler `dev_17.0.6` 分支，实测 commit `382821b707e9`。
- 构建依赖：`git cmake ninja-build gcc gcc-c++ python3`。
- 资源：AArch64-only Release 构建；首次构建预留 ≥20 GiB 磁盘，`JOBS` 控制并行（内存不足时调小）。

## 1. 取源码（必须带 HiSilicon 调度模型）

```bash
# GitHub 镜像
git clone --depth 1 -b dev_17.0.6 \
  https://github.com/openeuler-mirror/llvm-project.git ~/llvm-project
# GitHub 不可达时用 Gitee 镜像
# git clone --depth 1 -b dev_17.0.6 \
#   https://gitee.com/openeuler/llvm-project.git ~/llvm-project

# 可选：固定到本 skill 实测过的 commit（可复现）
# git -C ~/llvm-project fetch --depth 1 origin 382821b707e9
# git -C ~/llvm-project checkout 382821b707e9

# 校验模型：应看到 HIP10C/HIP11/HIP12
ls ~/llvm-project/llvm/lib/Target/AArch64/AArch64SchedHIP12*.td
```

## 2. 一键构建（推荐）

```bash
bash <skill_dir>/scripts/bootstrap_llvm_mca.sh ~/llvm-project ~/llvm-project/build-mca
# 内存不足时：JOBS=8 bash <skill_dir>/scripts/bootstrap_llvm_mca.sh ...
```

脚本会：拷贝 `IssueTraceView.{h,cpp}` → 运行 `patch_llvm_mca.py` → `cmake`（Release，仅 AArch64）→ `ninja llvm-mca` → 校验 `-issue-trace` 存在。

## 3. 手动构建（等价步骤）

```bash
cp <skill_dir>/scripts/llvm-mca-issue-trace/IssueTraceView.{h,cpp} \
   ~/llvm-project/llvm/tools/llvm-mca/Views/
python3 <skill_dir>/scripts/llvm-mca-issue-trace/patch_llvm_mca.py ~/llvm-project

cmake -S ~/llvm-project/llvm -B ~/llvm-project/build-mca -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_TARGETS_TO_BUILD=AArch64 -DLLVM_ENABLE_PROJECTS= \
  -DLLVM_INCLUDE_TESTS=OFF -DLLVM_INCLUDE_BENCHMARKS=OFF
ninja -C ~/llvm-project/build-mca llvm-mca
```

补丁脚本是幂等的（已打过会跳过），可安全重跑。

## 4. 验收

```bash
~/llvm-project/build-mca/bin/llvm-mca --help | grep issue-trace

python3 <skill_dir>/scripts/mca_visualize.py \
  --check-env --mca-bin ~/llvm-project/build-mca/bin/llvm-mca
# 期望："issue_trace_supported": true；目标 uarch（如 hip12）supported: true
```

把二进制路径固定下来：后续传 `--mca-bin <path>` 或设置 `MCA_BIN=<path>`。

## 5. 常见构建问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `不是 LLVM monorepo 源码根` | 源码根传错（应含 `llvm/tools/llvm-mca`） | 检查路径；重新 clone `dev_17.0.6` |
| 未找到 `AArch64SchedHIP12*.td` | 用了标准 LLVM / 分支不对 | 换 openEuler `dev_17.0.6` |
| 构建 OOM / 很慢 | 并行过高 | `JOBS=8`（或更低）重跑 |
| 编译报 `IssueTraceView` 未定义 | 未拷贝补丁文件或 CMake 未更新 | 重跑第 3 步；确认 `CMakeLists.txt` 含 `Views/IssueTraceView.cpp` |
| `--help` 无 `issue-trace` | 补丁未生效 / 用了别的二进制 | 检查构建目录与 `--mca-bin` 是否一致；重跑补丁并 rebuild |
| 预检 `supported:false` | 模型缺失 | 换源码重建；或显式 `--uarch` 到实际支持的型号 |
