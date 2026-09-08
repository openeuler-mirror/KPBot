# A+K 场景编译优化经验库

> A+K = Ascend + Kunpeng（昇腾 NPU + 鲲鹏 CPU 组合场景）。基于昇腾官方文档（Ascend Extension for PyTorch 6.0.0 - 编译优化章节）整理。
> 平台限定: openEuler（22.03/24.03）
>
> **⚠️ 本节经验值用于对照判断，不是可直接输出给用户的答案。**
> 必须先采集当前编译环境后，将当前状态与本节推荐配置对照，才能生成 candidate_actions。
> A+K 场景识别逻辑见 `compiler-optimization/SKILL.md` 分析流程步骤 2。

## 编译顺序

A+K 场景下三个组件必须按顺序编译，使用毕昇编译器（BiSheng Compiler）：

```
Python → PyTorch → torch_npu
```

- Python 先编译安装（LTO + PGO）
- PyTorch 依赖 Python，必须用毕昇编译后安装
- torch_npu 依赖 PyTorch，必须先装毕昇编译的 PyTorch 再编译

允许从中间开始：如果前置依赖已满足（毕昇版），可直接编译后续组件。

## 前置环境检查与准备

### 源码获取

Agent 询问用户是否有源码路径：

> 是否有组件源码路径？如果三个组件源码都在本地，请提供路径。
> 如果没有，Agent 会根据当前安装的版本自动下载对应源码。

根据用户回复：
- 用户提供源码路径 → 记录路径，编译时使用
- 用户无源码 → Agent 采集当前安装版本，自动下载对应版本源码：
  - Python: `python3 --version` → 从 https://www.python.org/downloads/source/ 下载对应版本
  - PyTorch: `python3 -c "import torch; print(torch.__version__)"` → `git clone -b v<版本号> https://github.com/pytorch/pytorch.git`
  - torch_npu: `python3 -c "import torch_npu; print(torch_npu.__version__)"` → `git clone -b v<版本号> https://gitee.com/ascend/pytorch.git`

> **版本一致性要求**：编译优化版的版本号必须与当前安装版本一致，只优化编译选项，不升级版本。

**源码完整性保障**（PyTorch/torch_npu 源码量大，子模块多，容易拉取不完整导致编译失败）：

1. **完整性校验**：clone 和 submodule update 后检查子模块状态：
   ```
   git submodule status
   ```
   无空行或前缀为 `-` 的条目表示完整；发现不完整则重新拉取 `git submodule update --init --recursive`

2. **重试机制**：submodule update 失败时自动重试（最多 3 次），每次重试前清理：
   ```
   git submodule deinit --all
   git submodule update --init --recursive
   ```

3. **网络优化**：GitHub 拉取慢时使用镜像加速：
   - PyTorch: 优先从 gitee 镜像克隆；gitee 无对应版本时使用 gitclone.com 镜像或 GitHub codeload ZIP（`https://codeload.github.com/pytorch/pytorch/zip/refs/tags/v<版本号>`）；使用 `--depth 1 --shallow-submodules` 减少拉取量
   - torch_npu: 直接从 gitee.com/ascend/pytorch 克隆（已在 gitee 上）

4. **编译前预检**：编译前验证源码完整性，避免编译中途因源码缺失失败：
   - 检查关键目录是否存在（如 PyTorch 的 `third_party/`、`aten/src/`）
   - 检查 `requirements.txt` 是否存在且可读
   - 如预检失败，提示用户检查网络后重新拉取

### 毕昇编译器检测与安装

Agent 检测毕昇编译器是否已安装：

```
bash -c 'clang --version 2>/dev/null | grep -i "bisheng"'
```

- 输出包含 "BiSheng" → 已安装，确认环境变量配置（PATH、LD_LIBRARY_PATH、CC、CXX）
- 输出为空 → 未安装（注意：系统自带的 LLVM clang 不是毕昇编译器），Agent 询问用户是否安装：
  > 未检测到毕昇编译器（系统自带的 clang 不是毕昇编译器）。是否需要安装？
  > 是 → Agent 执行以下安装步骤
  > 否 → 编译优化无法进行，标注限制原因

**安装步骤**：

1. 下载毕昇编译器（华为云 OBS 直接下载，无需认证，约 835MB）：
   ```
   wget -O /tmp/BiShengCompiler.tar.gz "https://kunpeng-repo.obs.cn-north-4.myhuaweicloud.com/BiSheng%20Enterprise/BiSheng%20Enterprise%20206.0.0/BiShengCompiler-5.2.0-aarch64-linux.tar.gz"
   ```

2. 解压安装到 `/opt/bisheng`：
   ```
   mkdir -p /opt/bisheng
   tar -xzf /tmp/BiShengCompiler.tar.gz -C /opt/bisheng --strip-components=1
   ```

3. 配置环境变量（写入 `/etc/profile.d/bisheng.sh` 持久化）：
   ```
   cat > /etc/profile.d/bisheng.sh << 'EOF'
   export PATH=/opt/bisheng/bin:$PATH
   export LD_LIBRARY_PATH=/opt/bisheng/lib:$LD_LIBRARY_PATH
   export CC=clang
   export CXX=clang++
   EOF
   source /etc/profile.d/bisheng.sh
   ```

4. 验证安装：
   ```
   clang --version | grep -i bisheng
   ```
   输出包含 "BiSheng" 表示安装成功。

**容器内安装**：通过 `docker exec` 在容器内执行上述步骤，安装路径同 `/opt/bisheng`。

### 容器环境处理

当 Agent 运行在物理机，A+K 环境运行在容器中时：

**检测目标容器**：
```
bash -c 'docker ps --format "{{.ID}} {{.Names}} {{.Image}}" 2>/dev/null || crictl ps --quiet 2>/dev/null | head -5 || kubectl get pods --no-headers 2>/dev/null | head -5'
```

- 发现运行中的容器 → 询问用户目标容器名称/ID
- 未发现容器 → 检查 Agent 自身是否在容器内（`cat /proc/1/cgroup`），如果是则直接在当前环境编译

**容器内编译流程**：
- Agent 通过 `docker exec` 或 `kubectl exec` 进入容器执行编译命令
- 编译前检查容器内环境：
  - 毕昇编译器是否在容器内安装（容器内独立安装，与物理机隔离）
  - 编译依赖是否齐全（容器内可能缺少 dnf/yum 或开发包）
  - 磁盘空间是否足够（编译 PyTorch 需要数 GB 空间）
  - 内存是否足够（LTO 编译内存消耗大）
- 如果容器内缺少毕昇编译器 → 在容器内安装
- 如果容器内缺少编译依赖 → 在容器内安装（需容器有 dnf/yum 或 apt）
- 如果容器内无包管理器或权限不足 → 提示用户在宿主机准备好环境或使用有权限的容器

**编译后安装**：
- 编译生成的 whl 包在容器内安装（`pip install *.whl --force-reinstall --no-deps`）
- 运行环境也需要毕昇编译器运行时（libomp.so），确保容器内 LD_LIBRARY_PATH 配置正确
- ThinLTO 编译的 PyTorch 运行时需要 LD_PRELOAD `libsleef.so` + `libtlfloat.so`（SVE 矢量化数学库），否则 SVE 符号未定义

**运行时环境变量配置**：
```
export PATH=/opt/bisheng/bin:$PATH
export LD_LIBRARY_PATH=/opt/bisheng/lib:$LD_LIBRARY_PATH
# ThinLTO 编译的 PyTorch 额外需要:
export LD_PRELOAD="<torch_lib_dir>/libsleef.so:<torch_lib_dir>/libtlfloat.so"
```

## 毕昇编译器环境配置

**安装毕昇编译器**：参考昇腾官方文档安装毕昇编译器并配置环境变量。

**环境变量配置**（所有组件编译前必须设置）：
```
export CC=clang
export CXX=clang++
```

**运行环境要求**：
- 运行环境需安装毕昇编译器包，设置 LD_LIBRARY_PATH 找到 libomp.so
- 如报 `Error while loading shared libraries: libomp.so`，检查毕昇编译器安装和 LD_LIBRARY_PATH

## 编译优化-Python

**前置条件**：
- 安装毕昇编译器并配置环境变量（CC=clang, CXX=clang++）
- 安装编译依赖（openEuler 使用 dnf/yum）：
  ```
  sudo dnf install gcc gcc-c++ gdb lzma glibc-devel libstdc++-devel openssl-devel \
  readline-devel zlib-devel libffi-devel bzip2-devel xz-devel \
  sqlite sqlite-devel sqlite-libs libuuid-devel gdbm-libs perf \
  expat expat-devel mpdecimal python3-pip
  ```

**编译参数**：
```
export CC=clang
export CXX=clang++
./configure --prefix=<安装目录> --with-lto --enable-optimizations --enable-shared
make -j
make install
```

**优化项**：
- `--with-lto`：开启 LTO
- `--enable-optimizations`：开启 PGO（Python 3.6+ 支持 LTO 与 PGO，跑 Python 自带 benchmark，耗时可能超过 30 分钟）
- `--enable-shared`：生成共享库 libpython3.x.so（PyTorch 编译时需要链接，必须开启）

**注意事项**：
- 编译完的 Python 可迁移到其他机器，注意 glibc 版本（低→高可以，高→低不行）
- 运行时如报 .so 找不到，检查编译依赖是否安装完全
- 如用 conda 管理环境，可将 Python 安装目录指向 conda 空环境目录

## 编译优化-PyTorch

**前置条件**：
- 已用毕昇编译安装 Python
- 安装毕昇编译器并配置环境变量（CC=clang, CXX=clang++）
- 推荐在容器中编译

**编译参数（LTO）**：
```
export CMAKE_C_FLAGS="-flto=thin -fuse-ld=lld"
export CMAKE_CXX_FLAGS="-flto=thin -fuse-ld=lld"
export CC=clang
export CXX=clang++
export USE_XNNPACK=0
export USE_NNPACK=0
export USE_PYTORCH_QNNPACK=0
export USE_TENSORPIPE=0
export USE_KINETO=0
export BUILD_CUSTOM_PROTOBUF=OFF
cd pytorch-2.1.0
git clean -dfx
python3 setup.py bdist_wheel
pip3 install /path/to/*.whl --force-reinstall --no-deps
```

**编译参数（LTO+PGO）**：
```
# 一次编译（插桩）
export CMAKE_C_FLAGS="-flto=thin -fuse-ld=lld -fprofile-generate=/tmp/profile"
export CMAKE_CXX_FLAGS="-flto=thin -fuse-ld=lld -fprofile-generate=/tmp/profile"
export CC=clang
export CXX=clang++
export USE_XNNPACK=0
export USE_NNPACK=0
export USE_PYTORCH_QNNPACK=0
export USE_TENSORPIPE=0
export USE_KINETO=0
export BUILD_CUSTOM_PROTOBUF=OFF
cd pytorch-2.1.0
git clean -dfx
python3 setup.py bdist_wheel
pip3 install /path/to/*.whl --force-reinstall --no-deps

# 运行模型采集 profile
export OMP_PROC_BIND=false
export LLVM_PROFILE_FILE=/tmp/profile/default_%m.profraw
# 正常跑模型...

# Profile 数据转换
llvm-profdata merge /tmp/profile -o /tmp/profile/default.profdata

# 二次编译（使用 Profile）
export CC=clang
export CXX=clang++
export USE_XNNPACK=0
export USE_NNPACK=0
export USE_PYTORCH_QNNPACK=0
export USE_TENSORPIPE=0
export USE_KINETO=0
export BUILD_CUSTOM_PROTOBUF=OFF
export CMAKE_C_FLAGS="-flto=thin -fuse-ld=lld -fprofile-use=/tmp/profile/default.profdata"
export CMAKE_CXX_FLAGS="-flto=thin -fuse-ld=lld -fprofile-use=/tmp/profile/default.profdata"
cd pytorch-2.1.0
git clean -dfx
python3 setup.py bdist_wheel
pip3 install /path/to/*.whl --force-reinstall --no-deps
```

**优化项**：
- `-flto=thin`：ThinLTO（比 full LTO 内存友好，适合大型项目）
- `-fuse-ld=lld`：使用 lld 链接器（毕昇自带）
- `USE_XNNPACK=0`：禁用 XNNPACK（NPU 场景不需要）
- PGO 两轮编译：插桩编译 → 跑模型采集 → 二次编译

**注意事项**：
- 需修改 CMakeLists.txt 屏蔽 `-Werror=cast-function-type` 告警
- 运行模型前必须设置 `OMP_PROC_BIND=false`（影响性能）
- 运行环境需安装毕昇编译器包，设置 LD_LIBRARY_PATH 找到 libomp.so
- 如报 c++11 abi 不一致，设置 `export _GLIBCXX_USE_CXX11_ABI=0` 重新编译
- `USE_TENSORPIPE=0`：clang 19 与 libnop 模板不兼容，必须禁用
- `USE_KINETO=0`：kineto 子模块可能缺失，禁用后需创建占位头文件 `ActivityType.h`（包含完整枚举值）
- ONNX FetchContent 下载 protobuf 卡住时，设置 `FETCHCONTENT_SOURCE_DIR_PROTOBUF` 指向本地源码
- nlohmann/json.hpp 缺失时，通过 jsdelivr CDN 下载单头文件
- ThinLTO 编译的 PyTorch 运行时需 LD_PRELOAD `libsleef.so` + `libtlfloat.so`（SVE 矢量化数学库）。如果 LD_PRELOAD 后仍有 SVE 符号未定义错误，需重新编译 sleef 共享库时开启 `SLEEF_ENABLE_SVE=ON`

## 编译优化-torch_npu

**前置条件**：
- 已用毕昇编译安装 PyTorch（必须先装好毕昇版 PyTorch）
- **ABI 一致性检查**：编译 torch_npu 前必须确认当前 PyTorch 是毕昇编译版（`readelf -p .comment torch/_C*.so | grep bisheng`）。如果 PyTorch 仍是 gcc 预编译版，必须先重编译 PyTorch，否则 ABI 不兼容导致 torch_npu 运行时崩溃
- 安装毕昇编译器并配置环境变量（CC=clang, CXX=clang++）
- 推荐在容器中编译

**编译参数（LTO）**：
```
export CC=clang
export CXX=clang++
cd torch_npu
git clean -dfx
bash ci/build.sh --python=<Python版本号> --enable_lto
pip install torch_npu-*.whl --force-reinstall --no-deps
```

> `<Python版本号>` 从 `python3 --version` 采集，如 3.8/3.9/3.10/3.11，不硬编码。

**编译参数（LTO+PGO）**：
```
# 一次编译（插桩）
export CC=clang
export CXX=clang++
cd torch_npu
git clean -dfx
bash ci/build.sh --python=<Python版本号> --enable_lto --enable_pgo=1
pip3 install /path/to/*.whl --force-reinstall --no-deps

# 运行模型采集 profile
export OMP_PROC_BIND=false
export LLVM_PROFILE_FILE=/tmp/profile/default_%m.profraw
# 正常跑模型...

# Profile 数据转换
llvm-profdata merge /tmp/profile -o /tmp/profile/default.profdata

# 二次编译（使用 Profile）
export CC=clang
export CXX=clang++
cd torch_npu
git clean -dfx
bash ci/build.sh --python=<Python版本号> --enable_lto --enable_pgo=2
pip3 install /path/to/*.whl --force-reinstall --no-deps
```

**优化项**：
- `--enable_lto`：开启 LTO
- `--enable_pgo=1`：一次编译（插桩）
- `--enable_pgo=2`：二次编译（使用 Profile）
- LTO 和 PGO 可单独使用，也可叠加

**注意事项**：
- PyTorch 和 torch_npu 的 profile 生成路径可相同，可合并使用同一个 profdata
- 运行环境需安装毕昇编译器包，设置 LD_LIBRARY_PATH 找到 libomp.so
- 如报 c++11 abi 不一致，检查 PyTorch 和 torch_npu 的 DGLIBCXX_USE_CXX11_ABI 值是否一致
- clang 19 严格 const 检查可能导致 `op_api_common.cpp` 的 `reinterpret_cast` 报错，需用 `const_cast<void*>` 修复

## 优化手段选择决策

Agent 采集当前编译环境后，向用户展示可用优化手段：

| 优化手段 | 前置条件 | 需跑模型采集 | 使用难度 | 适用组件 |
|---------|---------|------------|---------|---------|
| Python LTO+PGO | 毕昇编译器 | 否（PGO 内置） | 中 | Python |
| PyTorch LTO | 毕昇编译器 + 毕昇版 Python | 否 | 中 | PyTorch |
| PyTorch LTO+PGO | 毕昇编译器 + 毕昇版 Python | 是 | 高 | PyTorch |
| torch_npu LTO | 毕昇编译器 + 毕昇版 PyTorch | 否 | 中 | torch_npu |
| torch_npu LTO+PGO | 毕昇编译器 + 毕昇版 PyTorch | 是 | 高 | torch_npu |

推荐排序规则:
1. 优先推荐使用难度低、不需跑模型采集的（LTO 优于 LTO+PGO）
2. 按编译顺序推荐（Python → PyTorch → torch_npu）
3. PGO 需要跑模型采集 profile，标注为高难度，排在后面
4. 不可用的手段列在最后，标注限制原因

> Agent 不得自动执行任何编译，必须向用户展示可用清单并等待用户选择。

## 编译流程与状态管理

```
编译状态:
  - python: not_compiled | compiling | compiled | failed | skipped
  - pytorch: not_compiled | compiling | compiled | failed | skipped
  - torch_npu: not_compiled | compiling | compiled | failed | skipped

规则:
  - 用户指定单组件时，只编译该组件，其他标记 skipped
  - 用户指定全编时，按顺序编译，前一个未完成不能开始下一个
  - Python PGO 为内置 PGO（跑 Python 自带 benchmark），不需要用户跑模型，但耗时较长（可能超过 30 分钟）
  - PyTorch/torch_npu PGO 为工作负载 PGO，需要: 插桩编译 → 安装 → 跑模型 → 采集 profile → 二次编译 → 安装
```

> **编译前预检**: 完整编译三个组件需要 10-15GB 磁盘空间，编译前检查可用空间是否充足。

## 验证方法

每个组件编译完成后验证：
- **Python**: `./bin/python3 --version` 确认版本；`readelf -p .comment ./bin/python3 | grep -i bisheng` 确认使用毕昇编译器编译
- **PyTorch**: `pip3 install *.whl` 后 `python3 -c "import torch; print(torch.__version__)"`；`readelf -p .comment $(python3 -c "import torch,os; print(os.path.join(os.path.dirname(torch.__file__), '_C'))")*.so | grep -i bisheng` 确认毕昇编译；检查 libomp.so 链接（`ldd $(which python3) | grep omp`）
- **torch_npu**: `pip install *.whl` 后 `python3 -c "import torch_npu; print(torch_npu.__version__)"`；`readelf -p .comment $(python3 -c "import torch_npu,os; print(os.path.dirname(torch_npu.__file__))")/*.so | grep -i bisheng` 确认毕昇编译；检查 libomp.so 链接
- **功能 smoke test**: 跑一个最小的训练/推理脚本确认无报错
- **性能对比**: 用相同模型和压测命令对比编译前后的训练/推理性能

## PGO Profile 生成工作流（AI 推理场景）

> 来源：Ascend910 + vLLM qwen2.5-1.5b 实战经验。PGO profile 必须由目标负载生成，否则无效甚至有害。

### 有效性阈值

- profile 文件大小 > 50MB
- profile 函数数 > 100000
- 不满足以上阈值的 profile 视为覆盖不足，禁止用于 `-fprofile-use` 编译

### 被拒绝的 PGO 案例

| profile 来源 | 收益 | 拒绝原因 |
|---|---|---|
| `w00664011` 通用 profile（非目标负载生成） | -1.33% | profile 非目标负载生成，不匹配 |
| 纯 ThinLTO torch_npu 无 PGO | -22.4% | 无 PGO 的 LTO 可能破坏热点布局 |

### 生成模板

```bash
# 1. 启动目标服务
python -m vllm.entrypoints.openai.api_server \
  --model qwen2.5-1.5b \
  --tensor-parallel-size 1 &

# 2. 运行代表性 workload 生成 profile（prompt 分布、batch size 须与正式压测一致）
python benchmark_throughput.py \
  --model qwen2.5-1.5b \
  --input-len 1024 --output-len 256 \
  --num-prompts 5000

# 3. 收集并合并 profile（需 BiSheng compiler 的 profdata 工具）
llvm-profdata merge /tmp/profile -o /tmp/profile/default.profdata

# 4. 验证 profile 有效性
ls -la /tmp/profile/default.profdata  # size > 50MB
llvm-profdata show /tmp/profile/default.profdata --counts | wc -l  # functions > 100000
```

### 要求

1. profile 采集运行必须使用与正式压测相同的 model、tokenizer、prompt 分布、batch size
2. profile 来源必须记录在 `performance_signal_summary.json` 的编译信号字段中
3. 源码变化超过约 10% 或热点路径明显变化时重新采集

## 多机分布式 vLLM PGO 采集与多机一致性实战坑（DeepSeek-V4-Flash / DSV4 用例）

> 来源：Ascend910 + 鲲鹏双机（prefill P 节点 + decode D 节点）、DeepSeek-V4-Flash、vLLM + Mooncake KV、毕昇 PGO2 全链编译实战。

### 1. vLLM v1 多进程：profile 采集不到 Worker 热点

vLLM v1 的 `APIServer`、`EngineCore`、`Worker` 多进程 + vLLM-Ascend/CANN 在子进程初始化时重建/扩展环境（实测 Worker 环境由 ~84 变量扩到 ~2500），导致 `LLVM_PROFILE_FILE` 只在最外层 APIServer、**EngineCore/Worker 全部丢失**。真正承担算子计算的 Worker 退出时不落盘或落到 CWD 默认 `default.profraw` 互相覆盖。

**排查**（逐进程确认 + 按 pid 后缀统计 profraw）：
```bash
for p in $(pgrep -f "VLLM::Worker_"); do tr '\0' '\n' < /proc/$p/environ | grep '^LLVM_PROFILE_FILE='; done
# 若 Worker 有 __LLVM_PROFILE_RT_INIT_ONCE 但无 LLVM_PROFILE_FILE → 环境被重建丢弃
ls <profile_dir>/*.profraw | sed 's/.*_\([0-9]*\)\.profraw/\1/' | sort | uniq -c   # 无 Worker pid → 没刷盘
```

**修复 A：`sitecustomize.py` 注入**（env 继承在 fork 层就被破坏，改 `run_dp_template.sh` 无效——实测 APIServer 有该变量、EngineCore/Worker 无）。
```python
# /usr/local/python3.11.10/lib/python3.11/site-packages/sitecustomize.py
import os
os.environ["LLVM_PROFILE_FILE"] = "/path/profile_o3_default/default_%m_%p.profraw"
```
- 必须 `%p`（pid）保证每进程独立文件；只用 `%m` 会让 fork 同源进程写同一文件互相覆盖。
- sitecustomize 在解释器启动即执行，早于 CANN/vLLM 线程，能穿透环境重建。

**修复 B：优雅关闭必须对「全部」vllm 进程一次性 SIGINT**：
- LLVM profile 运行时靠 **atexit** 刷盘；Python 默认对 **SIGTERM 不执行 atexit**（SIGTERM 关 Worker 不刷盘）。
- 只对 `vllm serve` 发 SIGINT → vLLM 优雅关闭会**级联强杀**（不经 atexit）Worker → 数据再丢。
- 正确：先建全部 pid 列表一次性 `kill -INT`，Worker/EngineCore/APIServer 各自独立走 atexit 刷盘：
```bash
ps -eo pid,args | grep -E "vllm serve|VLLM::EngineCore|VLLM::Worker_" | grep -v grep | awk '{print $1}' > pids.txt
while read p; do kill -INT $p; done < pids.txt    # 勿顺序 pkill
```

**注意**：PGO2 二进制是 `-fprofile-use`（非插桩），服务跑 PGO2 后**不再产 profraw**；采集必须用 instrumented 的 PGO1 二进制。

### 2. PyTorch 与 torch_npu 的 ABI 环境变量「变量名不同」

- **PyTorch** 读取 `GLIBCXX_USE_CXX11_ABI`（**无下划线**）；设 `_GLIBCXX_USE_CXX11_ABI=0`（有下划线）对 PyTorch **无效**，PyTorch 按默认编成**新 ABI=1**。
- **torch_npu** 的 `setup.py` 读取 `_GLIBCXX_USE_CXX11_ABI`（**有下划线**）。
- 同一带下划线变量不可能同时控制两者 → 出现 `pytorch=新ABI(1)` / `torch_npu=旧ABI(0)` 错配。

**错配报错**：
```
ImportError: libtorch_npu.so: undefined symbol: _ZN5torch7LibraryC1ENS0_4KindESsSt8optionalIN3c1011DispatchKeyEEPKcj
```
mangled 里 `Ss` = 旧 ABI `std::string` → 与 libtorch_cpu.so 新 ABI 的 `NSt7__cxx1112basic_string...` 对不上。

**正确做法（按目标分变量名、值统一）**：
```bash
export GLIBCXX_USE_CXX11_ABI=0    # PyTorch（无下划线）
export _GLIBCXX_USE_CXX11_ABI=0   # torch_npu（有下划线），值与 PyTorch 一致
```
核实：
```bash
python3 -c "import torch; print(torch._C._GLIBCXX_USE_CXX11_ABI)"
nm -D .../torch_npu/lib/libtorch_npu.so | grep " U _ZN5torch7LibraryC1.*Ss"    # 旧ABI符号（应为0）
nm -D .../torch_npu/lib/libtorch_npu.so | grep -c NSt7__cxx1112basic_string    # 新ABI符号数
nm -D .../torch/lib/libtorch_cpu.so | grep "LibraryC1.*NSt7__cxx11"            # PyTorch 侧新/旧ABI符号
```

### 3. PyTorch ThinLTO 折叠 COMDAT 弱符号 `new_qtensor`

现象：torch_npu import 报 `undefined symbol: at::new_qtensor(...)`。
检查：`nm -D .../torch/lib/libtorch_cpu.so | grep -c new_qtensor` 为 0 = 被折叠。
修复（PyTorch LDFLAGS 加 `-Wl,-u` 强制导出）：
```bash
NEWQT="_ZN2at11new_qtensorEN3c108ArrayRefIlEERKNS0_13TensorOptionsENS0_13intrusive_ptrINS_9QuantizerENS0_6detail34intrusive_target_default_null_typeIS7_EEEE"
export LDFLAGS="-O3 -Wl,-mllvm,-instcombine-reorder-sum-of-reduce-add=false -fuse-ld=lld -Wl,-u,$NEWQT"
```
> 同源码双机可能「一个导出、一个不导出」——必须逐节点 `nm` 核实，不能一台 pass 就认为另一台 OK。

### 4. 重编译环境与多机一致性

- **`build/packages/torch_npu 不存在`**：往往是 CMake 原生编译(`build_clib/build_ext`)被跳过（`has_ext_modules()/has_c_libraries()` 判定失败），构建日志只有几十行无 `[n/m] Building`（成功机 6 万行 + 上千 Building 记录；排查：`grep -cE "\[[0-9]+/[0-9]+\] Building" 构建日志`，成功机上千、失败机 0）。解法：用与成功机相同的**隔离 venv 完整构建**（`include-system-site-packages=false` 的 venv，含 torch+setuptools+wheel+ninja+pyyaml，走 `ci/build.sh --enable_lto --enable_pgo=2`）；不要用 `prefix=/usr` 损坏的 python 建的 venv。
- **`Python.h not found`**（torchair `npu_wrapper.cpp` 或 PyTorch `torch/csrc/stub.c`）：构建 python 的 include 解析错误 → 补 `CPLUS_INCLUDE_PATH`/`C_INCLUDE_PATH` 指向真实 `.../include/python3.11`。
- **op-plugin `reinterpret_cast ... casts away qualifiers`**（clang17 严格）：源码加 `const_cast<void*>`；op-plugin 是独立 CMake 子项目，`-Wno-cast-qual` 未必传导，改源码最可靠。
- **`No module named 'setuptools.command.bdist_wheel'`**（PyTorch setup.py 用）vs `No module named 'wheel'`（torch_npu setup.py 用）：ensurepip 自带 setuptools 常缺 bdist_wheel。按成功机版本装：`pip install "setuptools==68.2.2" "wheel==0.41.3"`，逐机验证 `import setuptools.command.bdist_wheel`。
- **`ls dist/*.whl | head -1` 选错旧包**：会按字母序选旧的（如 git449b176 而非新编 gitunknown）。必须显式 `pip install --force-reinstall --no-deps <指定新 whl>`，并用 `torch-*.dist-info/direct_url.json` 复核实际来源。
- **容器持久性**：`/home`、`/data` 常是 host bind-mount（构建产物/profile 安全）；`/usr/local`（系统 python、sitecustomize、已装 whl）是容器本地层。容器被 `SIGKILL`(ExitCode=137) 后用 `docker start <cid>` 重启同一容器保留本地层，不要重建；`docker inspect -f '{{.State.Status}} {{.State.OOMKilled}} {{.State.FinishedAt}}' <cid>` 判断退出原因。

