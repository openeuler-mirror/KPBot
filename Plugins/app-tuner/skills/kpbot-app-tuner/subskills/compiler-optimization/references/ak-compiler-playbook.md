# A+K 场景编译优化经验库

> A+K = Ascend + Kunpeng（昇腾 NPU + 鲲鹏 CPU 组合场景）。基于昇腾官方文档（Ascend Extension for PyTorch 6.0.0 - 编译优化章节）与全链编译实战经验整理。
> 平台限定: openEuler（22.03/24.03），aarch64 架构。
>
> **⚠️ 本节经验值用于对照判断，不是可直接输出给用户的答案。**
> 必须先采集当前编译环境后，将当前状态与本节推荐配置对照，才能生成 candidate_actions。
> A+K 场景识别逻辑见 `compiler-optimization/SKILL.md` 分析流程步骤 2。

## 适用场景与预期收益

### 场景定义

A+K 场景指在鲲鹏 aarch64 CPU 主机上搭配昇腾 NPU 运行 PyTorch 负载的组合。典型特征：

- 主机：鲲鹏 aarch64 架构，多核，多 NUMA node
- 加速器：昇腾 NPU，配套 CANN
- 软件栈：PyTorch + torch_npu + torchair inductor 扩展
- 负载：训练或推理（本文重点覆盖训练场景全链编译）

触发条件：profiling 显示 Computing/Stage 偏低、Free/Stage 偏高、大量算子 device_total=0（纯 host 开销）时，瓶颈在 host 侧 CPU 计算，用毕昇编译器对 Python/PyTorch/torch_npu 做全链 LTO+PGO 优化收益最大。

注意：hostbound 结论必须用同卡稳态 TPS 证实，不能仅凭单步 profiling 下结论。

### 实证收益（对照参考）

以下为全链毕昇编译（ThinLTO+PGO）在训练场景的同卡 A/B 实证结果，作为收益量级对照参考，非可直接输出的答案。测试负载为 DeepSeek V4 1B 训练，同卡对比，稳态 TPS 取 steps 21-30 均值。

| 指标 | GCC baseline | BiSheng ThinLTO+PGO | 提升 |
|------|-------------|----------------------|------|
| 稳态 TPS | 620.2 | 824.6 | +33.0% |
| MFU | 1.10% | 1.45% | +31.8% |
| step 耗时 | ~0.92s | ~0.70s | -24% |

### 收益归因

PGO 是全链优化的必要条件，不是可选项。纯 ThinLTO 无 PGO 会负收益，因为 LTO 内联决策缺乏运行时 profile 引导，反而劣化热路径。PGO profile 必须由目标负载（训练负载）生成，不能跨场景复用。

## 编译顺序

A+K 场景下三个组件必须按顺序编译，使用毕昇编译器（BiSheng Compiler）：

```
Python → PyTorch → torch_npu
```

- Python 先编译安装（LTO + PGO）
- PyTorch 依赖 Python，必须用毕昇编译后安装
- torch_npu 依赖 PyTorch，必须先装毕昇编译的 PyTorch 再编译

允许从中间开始：如果前置依赖已满足（毕昇版），可直接编译后续组件。

每步前置验证清单（前一步未通过不进下一步）：

1. `readelf -p .comment <artifact> | grep -i bisheng` 命中（确认是毕昇编译的，不是系统 GCC）
2. import 无 segfault（确认 dlopen 正常，链接段修复生效）
3. 功能 smoke test 通过（Python import ssl/hashlib 等；PyTorch matmul；torch_npu device_count）

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
  - torch_npu: `python3 -c "import torch_npu; print(torch_npu.__version__)"` → `git clone -b v<版本号> https://github.com/ascend/pytorch.git`

> **版本一致性要求**：编译优化版的版本号必须与当前安装版本一致，只优化编译选项，不升级版本。

**源码完整性保障**（PyTorch/torch_npu 源码量大，子模块多，容易拉取不完整导致编译失败）：

1. **完整性以"实际文件存在性"为准，禁止用 `git submodule status` 文本判断**（实测踩坑）：中断的 checkout 会留下 status 无 `-` 前缀但目录为空的假完整态（pybind11/psimd/FXdiv 均实测出现），编译时报 `Could not find any of CMakeLists.txt ... in third_party/pybind11`。可靠判断：逐个子模块路径检查目录存在且非空：
   ```bash
   git submodule status --recursive | awk '{print $2}' | while read p; do
     [ -d "$p" ] && [ -n "$(ls -A "$p" 2>/dev/null)" ] || echo "不完整: $p"
   done
   ```
2. **定向修复，禁止反复全量拉取**：仅对检出的不完整路径执行
   `git submodule update --init --recursive --force --depth 1 -- <缺失路径>`（`--force` 是关键，普通 update 对"HEAD 正常但工作树空"的假完整态不会重新 checkout）；修复触发条件含两类：文件缺失/空目录、**HEAD != 父仓 gitlink（版本被镜像污染成分支 tip，实测出现）**；完整时零网络操作直接通过
3. **修复后复查**：重新跑文件存在性检查，仍缺失则报镜像/网络问题，不进入编译
4. **编译前预检**：检查关键目录（`third_party/`、`aten/src/`）、`requirements.txt`；预检失败先修子模块再编译

5. **网络优化（机制版本无关，随环境自动探测版本并拉取对应源码；下列数字均为实测基线样本：鲲鹏 aarch64 华为云环境，采样自 pytorch v2.5.1——其他版本的子模块数量/镜像覆盖率/耗时随版本浮动，机制不变）**：
   - Python: 优先华为云镜像 `https://mirrors.huaweicloud.com/python/<版本>/Python-<版本>.tgz`（wget 加 `-c --tries=3` 断点续传），python.org 兜底
   - PyTorch 主仓库三级回退链（实测 `--depth 1` 全量）：gitee 镜像 329MB/18s → gitcode `https://gitcode.com/gh_mirrors/py/pytorch.git` 329MB/39s → github 直连 341MB/33s；每次尝试硬超时（`timeout` + `AK_GIT_TIMEOUT`，默认 900s）——弱网慢速爬行（>1KB/s）低速判死抓不住，必须墙钟硬上限 failover，否则实测会拖到 4-5 小时
   - **PyTorch 子模块是拉取慢的主因**（以 v2.5.1 为样本 35 个 = 34 github + 1 gitlab eigen；其他版本子模块清单不同，探测与收敛机制相同），分层镜像收敛（`ak_compile_optimize.sh` 已内置，实测全新拉取 266-350s，对比串行约 30 分钟）：
     - 镜像覆盖实测（逐仓 pinned-SHA 浅取验证，动态变化）：gitcode gh_mirrors 约 21/34、ghfast.top 通用代理补约 11/34，残余 onnx/psimd 等走 github 直连；eigen 是唯一 gitlab 源，github 加速器全不覆盖，直连
     - `submodule.fetchJobs=8` 并行（默认串行逐个往返）；`http.lowSpeedLimit/lowSpeedTime` 卡死判死；`GIT_TERMINAL_PROMPT=0` 防凭据交互挂死
     - ⚠️ **insteadOf 三个实测陷阱**：① `-c` 的 key 不能带引号（git 不解析引号，引号泄漏进 key 改写失效）；② 镜像 URL 不带 `.git` 后缀（.gitmodules URL 多带 .git，前缀重写保留残余 `.git`，镜像 URL 也带 .git 会拼出 `.git.git` 403）；③ 改写到镜像不存在的仓库（gitcode 403）会让整个 update 中止——必须逐仓 SHA 级验证后生成改写层（验证到仓库存在不够：gloo 实测仓库在但 pinned SHA 缺失）
     - ⚠️ **`--depth 1` 版本污染陷阱**：浅克隆成功但 pinned SHA 补取失败时，工作树被静默留在默认分支 tip（实测 pybind11 停在旧 master）。子模块完整性必须含 `HEAD == gitlink` 本地校验，仅查文件存在性会放过错误版本
     - 被杀（硬超时/Ctrl-C）的 update 会留 `shallow.lock` 残锁且并行兄弟仓库半途而废——每轮前清理 `.git/modules/*.lock` 并有界轮次（3 轮）收敛
   - **源码缓存（二次拉取零网络）**：首次拉取成功后打包源码树到 `AK_SRC_CACHE`（默认 `/tmp/ak-src-cache/<component>-<version>.tar`，`AK_SRC_CACHE=0` 关闭）；同版本重跑、chain 断点续跑、重建容器场景直接解包，全量网络拉取降为本机解包耗时。缓存树若不完整，由源码完整性检查（文件存在性 + HEAD==gitlink）兜底定向补拉
   - pip 一律走内网镜像：`pip install -i https://mirrors.huaweicloud.com/repository/pypi/simple ...`（实测装 setuptools 全套约 2s）
   - torch_npu: 直接 gitee.com/ascend/pytorch 克隆（加 `--depth 1`，同硬超时）
   - **慢网确认门禁（skill 自动化，无需用户手算参数）**：拉取前对首选源测速（30MiB 早停探针，快网秒级结束），按数据量粗估总耗时（pytorch≈1600MB / torch_npu≈120MB 模型，版本无关）；预估超 `AK_SLOW_NET_THRESHOLD`（默认 600s）→ TTY 下内联询问"预估约 X 分钟，是否接受"；Agent 编排（非 TTY）输出 `slow_net_confirmation_required` 结构化失败，Agent 据此向用户转述预估并询问——**用户接受后 `AK_SLOW_NET=accept` 重跑，脚本按测得速度自动放宽超时预算（total/速度×1.5，夹 [900s, 6h]），不需要手动换算 AK_GIT_TIMEOUT**。缓存已就绪不触发门禁。
   - ⚠️ GitHub codeload ZIP 兜底**禁止用于编译**：ZIP 无 .git 与子模块，third_party 拉不到必然编译失败；仅可用于读源码

### 毕昇编译器检测与安装

Agent 检测毕昇编译器是否已安装。**没有固定安装路径要求，按真实环境探测**，检测顺序：

1. PATH 直查：`which clang && clang --version | grep -i "bisheng"`
2. source profile.d 后再查：`source /etc/profile.d/bisheng.sh 2>/dev/null; clang --version | grep -i bisheng`
   **非登录 shell（`docker exec <c> bash -c`、CI runner、cron）不会加载 profile.d**，即使毕昇已装且 profile.d 存在也检测不到——此时必须先 source 再检测
3. 按用户提供的实际安装位置检查：`<bisheng-prefix>/bin/clang --version | grep -i "bisheng"`

> `<bisheng-prefix>` 为毕昇编译器的**实际安装前缀**，由真实环境决定（预装位置、用户指定路径或下文安装步骤选择的位置均可），不预设固定值。
> `ak_compile_optimize.sh` 自动按此顺序探测，并把解析到的毕昇绝对路径固化到 CC/CXX，编译产物用 readelf 复核。

- 输出包含 "BiSheng" → 已安装，确认环境变量配置（PATH、LD_LIBRARY_PATH、CC、CXX）
- 输出为空 → 未安装（注意：系统自带的 LLVM clang 不是毕昇编译器），Agent 询问用户是否安装：
  > 未检测到毕昇编译器（系统自带的 clang 不是毕昇编译器）。是否需要安装？
  > 是 → Agent 执行以下安装步骤
  > 否 → 编译优化无法进行，标注限制原因

**关键陷阱：毕昇可能已装但未接入 PATH。** 若 `/etc/profile.d/bisheng.sh` 缺失，裸 shell 里 `clang --version` 会返回系统自带 LLVM（如 openEuler 17.0.x）而非毕昇，`clang --version | grep -i bisheng` 检测会失败，尽管毕昇确实存在于其真实安装位置。检测时按真实安装路径直接检查 `<bisheng-prefix>/bin/clang`，或先 source profile.d 再检测。若 profile.d 不存在，创建它（见下文安装步骤）。**只依赖继承 PATH 的检测在 docker exec/CI 场景必然失效**（非登录 shell 不加载 profile.d）。

**安装步骤**：

### 毕昇编译器下载速查表（禁止探索，直接使用）

> Agent 不得自行搜索或尝试其他下载源。以下直链与校验和为钉死事实（实测验证），直接使用。

| 项 | 值 |
|---|---|
| 下载直链（唯一推荐源） | `https://kunpeng-repo.obs.cn-north-4.myhuaweicloud.com/BiSheng%20Enterprise/BiSheng%20Enterprise%20206.0.0/BiShengCompiler-5.2.0-aarch64-linux.tar.gz` |
| 版本 | BiSheng Enterprise 206.0.0（clang 19.1.7），aarch64 |
| 大小 | 835MB |
| sha256 | `17e2b9b3858a8a2f0591f6f54e0b2bbc1ccbbcb86e4938fb0a10bd3b473a4455` |
| 实测速度 | 华为云内网约 20-40MB/s（835MB 约 23s）；公网视带宽 |
| 溯源参考 | 华为鲲鹏开发者官网 hikunpeng.com（毕昇编译器产品页）；OBS 桶版本目录模式 `BiSheng Enterprise/<企业版号>/BiShengCompiler-<x.y.z>-aarch64-linux.tar.gz` |

**规则**：
1. 直接用直链下载（加 `-c --tries=3` 断点续传），下载后必须 `sha256sum` 校验，不匹配则重下，重下仍不匹配才允许按桶目录模式探测新版本并同步更新校验和
2. ⚠️ 陷阱：`mirrors.huaweicloud.com/bisheng/` 是镜像门户的**假 200**——任意路径都返回 200 但实际是 portal HTML（实测下载速度 0.1MB/s），**不是**二进制镜像，禁止使用
3. ⚠️ gitee `kunpeng-control` 组织已失效（404），不要作为候选
4. 一键安装：`ak_compile_optimize.sh --install-bisheng <前缀>`（下载→sha256 校验→解压→写 /etc/profile.d/bisheng.sh→验证，无需探索）

1. 下载毕昇编译器（华为云 OBS 直接下载，无需认证，约 835MB，华为云内网实测约 20-40s；加 `-c` 断点续传）：
   ```
   wget -c -O /tmp/BiShengCompiler.tar.gz "https://kunpeng-repo.obs.cn-north-4.myhuaweicloud.com/BiSheng%20Enterprise/BiSheng%20Enterprise%20206.0.0/BiShengCompiler-5.2.0-aarch64-linux.tar.gz"
   sha256sum /tmp/BiShengCompiler.tar.gz   # 必须等于 17e2b9b3858a8a2f0591f6f54e0b2bbc1ccbbcb86e4938fb0a10bd3b473a4455
   ```

2. 解压安装到 `<bisheng-prefix>`（按真实环境选择安装位置）：
   ```
   mkdir -p <bisheng-prefix>
   tar -xzf /tmp/BiShengCompiler.tar.gz -C <bisheng-prefix> --strip-components=1
   ```

3. 配置环境变量（写入 `/etc/profile.d/bisheng.sh` 持久化）：
   ```
   cat > /etc/profile.d/bisheng.sh << 'EOF'
   export PATH=<bisheng-prefix>/bin:$PATH
   export LD_LIBRARY_PATH=<bisheng-prefix>/lib:$LD_LIBRARY_PATH
   export CC=clang
   export CXX=clang++
   EOF
   source /etc/profile.d/bisheng.sh
   ```

4. 验证安装（全部通过才继续）：
   ```
   which clang          # 应返回 <bisheng-prefix>/bin/clang
   which clang++        # 应返回 <bisheng-prefix>/bin/clang++
   clang --version | grep -i bisheng    # 非空
   $CC --version        # 返回毕昇
   $CXX --version       # 返回毕昇
   clang -E -x c /dev/null              # 编译通过
   ls <bisheng-prefix>/lib/libomp.so    # 存在
   ```

**容器内安装**：通过 `docker exec` 在容器内执行上述步骤，安装位置按容器内真实环境选择。如毕昇为容器内预装，不要重新下载（既浪费带宽又有覆盖风险），仅需补全 profile.d 并 source。

**所有组件编译前必须设置** `export CC=clang`、`export CXX=clang++`（缺省时 cmake 探测用系统 g++，拒绝毕昇专属标志如 `-fGNU-compatibility`）。运行环境的 libomp 依赖见"运行时环境配置"章。

### 编译器逃逸防线（强制门禁，不可跳过）

> 生产环境普遍存在环境依赖脚本（CANN `set_env.sh`、`/etc/profile.d/*.sh`、`~/.bashrc`），它们通常**没有把新装的毕昇纳入 PATH**，甚至中途重置 PATH/CC。任何"只靠继承环境"的编译都会被系统 clang/gcc 静默替代——产物正常生成但不是毕昇编译。因此逃逸防护必须做成强制门禁（`ak_compile_optimize.sh` 已内置四道，按序生效）。

| 防线 | 时机 | 机制 | 失败行为 |
|------|------|------|---------|
| 0 预检门禁 | 每个编译函数入口 | 在真实构建 shell（应用环境前缀后）中验证 `command -v clang` 解析到毕昇绝对路径 | `bisheng_env_escape` fail_json，不启动编译 |
| 1 PATH 前置 | 每条 target 命令 | 显式 `export PATH=<毕昇前缀>/bin:$PATH`，子构建裸调 clang/gcc/cc 也不依赖 profile.d；CC/CXX 用毕昇绝对路径 | —（结构性防御） |
| 2 逐文件审计 | 构建+安装后 | 对全部构建产物 `.so` 逐个 readelf `.comment`，任一缺失 bisheng 标识即逃逸（聚合计数会掩盖单文件逃逸）；`libgomp*` 上游预捆绑库豁免 | `compiler_escape_detected` fail_json，列出逃逸文件及其编译器身份 |
| 3 日志指纹审计 | 构建后 | cmake 打印 `compiler identification is GNU` 即 gcc 逃逸实锤；Clang 版本与毕昇不符则告警 | `compiler_escape_in_log` fail_json |

- 越权开关：`AK_ALLOW_MIXED_COMPILER=1`（降级为告警，不推荐）
- 实测基线：BiSheng 5.2 + PyTorch 2.5.1 全链构建，四道防线全部通过（GNU 指纹 0、逐文件复核通过）
- 排障入口：给脚本加 `--log-file`，逃逸时按"逃逸产物编译器身份"+"日志指纹行"定位到具体子构建

### 容器环境处理

当 Agent 运行在物理机，A+K 环境运行在容器中时：

**检测目标容器**：
```
bash -c 'docker ps --format "{{.ID}} {{.Names}} {{.Image}}" 2>/dev/null || crictl ps --quiet 2>/dev/null | head -5 || kubectl get pods --no-headers 2>/dev/null | head -5'
```

- 发现运行中的容器 → 询问用户目标容器名称/ID
- 未发现容器 → 检查 Agent 自身是否在容器内（`cat /proc/1/cgroup`），如果是则直接在当前环境编译

**容器内编译流程**：
- Agent 通过 `docker exec` 或 `kubectl exec` 进入容器执行编译命令，或通过 SSH 连接目标主机后再进入容器
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
- ThinLTO 编译的 PyTorch 运行时需 LD_PRELOAD SVE 运行库（见"运行时环境配置"章）

**运行时环境变量**：PATH/LD_LIBRARY_PATH 与 SVE LD_PRELOAD 的完整配置，见"运行时环境配置"章。

### PyPI 源速查表（禁止探索，按序使用）

> Agent 不得自行搜索其他 PyPI 镜像。脚本 `ak_compile_optimize.sh` 已内置以下候选的自动探测选源（环境准备阶段执行）。

| 序 | 源 | URL | 实测备注 |
|---|----|-----|---------|
| 1 | 华为云 | `https://mirrors.huaweicloud.com/repository/pypi/simple` | 快，但对部分环境限流（实测返回 429） |
| 2 | 清华 | `https://pypi.tuna.tsinghua.edu.cn/simple` | 实测华为云 429 后自动切换成功 |
| 3 | 阿里云 | `https://mirrors.aliyun.com/pypi/simple` | 候选 |
| 4 | 腾讯云 | `https://mirrors.cloud.tencent.com/pypi/simple` | 候选 |
| 5 | 官方 | `https://pypi.org/simple` | 兜底，海外环境慢 |

**规则**：
1. **自动模式**（默认）：按上表顺序探测（8s 超时），首个可达者胜出，选择结果记录到日志与 check-only JSON 的 `pip_index` 字段
2. **显式模式**：`--pip-index <url>` 视为唯一权威源（客户内部 Nexus/私源场景），不可达直接 `pip_index_unreachable` 快速失败，不回退候选表
3. 自签证书私源加 `--pip-trusted-host <host>`
4. 所有 pip 调用统一 `--timeout 30 --retries 2` 快速失败，不长时间悬挂
5. **离线环境**：联网机 `pip download -r requirements.txt -d /path/wheels`，目标机 `pip install --no-index --find-links /path/wheels -r requirements.txt`；或挂载内部 Nexus 后 `--pip-index` 指定
6. 快速检查一行命令：
   ```bash
   python3 -c "import urllib.request as u; print(u.urlopen('https://mirrors.huaweicloud.com/repository/pypi/simple', timeout=8).status)"
   ```

### 常用依赖速查表（快速体检清单）

> 一键体检：`ak_compile_optimize.sh --check-only`（自动探测毕昇 + PyPI 选源 + 下方全部条目探测，输出各组件 OK/缺失清单）。

**通用工具**（三组件共用）：

| 依赖 | 用途 | 快速检查 |
|------|------|---------|
| git/wget/tar/make/readelf | 源码获取与构建 | `command -v git wget tar make readelf` |
| patchelf | Python rpath 修复（必须） | `command -v patchelf`，缺失 `dnf install patchelf` |

**python 组件**（编译毕昇版 Python）：

| 依赖 | 缺失后果 | 快速检查 |
|------|---------|---------|
| openssl/ssl.h 等 7 头文件 | ssl/readline/zlib/bz2/lzma/sqlite3/_ctypes 模块编译不出 | `printf '#include <openssl/ssl.h>' \| clang -E -x c - -o /dev/null`（逐个替换头文件） |
| uuid/uuid.h、gdbm.h | 可选：缺失仅跳过 _uuid/dbm.gnu 模块 | 同上 |
| dnf 包清单 | — | `gcc gcc-c++ gdb lzma glibc-devel libstdc++-devel openssl-devel readline-devel zlib-devel libffi-devel bzip2-devel xz-devel sqlite sqlite-devel sqlite-libs gdbm-libs perf expat expat-devel mpdecimal python3-pip patchelf` |
| openEuler 24.03 备注 | 无 libuuid-devel 包，`_uuid` 模块会被跳过，属正常 | — |

**pytorch 组件**：

| 依赖 | 缺失后果 | 快速检查 |
|------|---------|---------|
| cmake / ninja | 无法配置构建/回退 make 极慢 | `command -v cmake ninja`，pip 可补装 |
| Python.h | 立即编译失败 | `python3 -c "import sysconfig,os;print(os.path.exists(os.path.join(sysconfig.get_paths()['include'],'Python.h')))"`；系统 Python 缺则 `dnf install python3-devel` |
| setuptools（70.1≤v<84） | `No module named 'setuptools.command.bdist_wheel'` | `python3 -c "import setuptools.command.bdist_wheel"`；推荐 `pip install setuptools==75.8.0`（实测 68.2.2 缺 bdist_wheel） |
| wheel | torch_npu setup.py 报 `No module named 'wheel'` | `python3 -c "import wheel"`；`pip install wheel==0.41.3` |
| requirements.txt 依赖 | 编译中途缺模块 | 脚本 best-effort 安装，缺啥按报错补装 |

**torch_npu 组件**：

| 依赖 | 缺失后果 | 快速检查 |
|------|---------|---------|
| CANN toolkit | ci/build.sh 无法构建 | `ls /usr/local/Ascend/ascend-toolkit/latest` 或 `$ASCEND_HOME_PATH`；需先 `source set_env.sh` |
| 毕昇版 PyTorch | ABI 不兼容运行时崩溃（脚本硬门禁） | `readelf -p .comment torch/_C*.so \| grep -ci bisheng` |
| setuptools/wheel/patchelf/git | 同 pytorch 组件 | 同上 |



### 编译范围决策

Agent 采集环境后，向用户展示编译范围选项并等待选择。推荐全链 LTO+PGO（Python + PyTorch + torch_npu 全部毕昇编译），收益最大。

- **全链 vs 局部**：选全链。**vLLM-Ascend、PyTorch、torch_npu 三者 ABI 必须一致**（统一 `_GLIBCXX_USE_CXX11_ABI=1`），局部编译收益有限
- **LTO vs LTO+PGO**：选 LTO+PGO。PGO 是必要条件（纯 ThinLTO 无 PGO 负收益，见"收益归因"）
- **ThinLTO vs full LTO**：选 ThinLTO（`-flto=thin`）。full LTO 链接 libtorch_cpu 需 1-2h、60-100GB 内存，仅诊断用

## ABI 双栈共存

> 双 ABI 栈并存是全链编译最大的坑。inductor 源码里的 ABI 硬编码是全链唯一真源，处理不当会让 GCC 生产栈的 wrapper.so 全部编译成错误 ABI，清 JIT 缓存无效。

### 双栈 ABI 事实

- **ABI 一致性硬要求：vLLM-Ascend、PyTorch、torch_npu 三者必须一致，统一 `ABI=1`（NEW/cxx11）**
- 生产 GCC torch/torch_npu = NEW ABI（cxx11 符号）
- 毕昇全链同用 NEW ABI=1（与官方生态一致）
- 两套栈各自自洽，不混用。混用则 dlopen 报 undefined symbol

### inductor 源码 ABI 硬编码是全链唯一真源

`torchair/experimental/_inductor_npu_ext/python/inductor_npu_ext/compiler/_compiler.py` 里有 ABI 硬编码：

```python
ABI = "-D_GLIBCXX_USE_CXX11_ABI=0"    # 错误硬编码：与全链 ABI=1 不符
```

当它存在时，ABI=1 栈（含 GCC 生产栈与毕昇栈）编译的 wrapper.so 全用错误 ABI，dlopen 失败报：

```
undefined symbol: _ZN6at_npu6native9OpCommand10RunOpApiV2ERKSs...
```

清 JIT 缓存无效。曾出现反复清缓存不解决的情况，直到定位到这个硬编码才是真根因，不要把 ABI 硬编码问题误判为 kernel 缓存污染。

`revert_ascir.py` 同样有 ABI 硬编码（`.bak` 为 `=1`，被改成 `=0`），需同步处理。

### 正确做法

**方案 A（推荐，适合生产长期共存）：ABI 改为运行时读 env**

`_compiler.py` 改为运行时读 `_GLIBCXX_USE_CXX11_ABI` 环境变量（不设时默认 1）：

```python
import os
ABI = "-D_GLIBCXX_USE_CXX11_ABI=" + os.environ.get("_GLIBCXX_USE_CXX11_ABI", "1")
```

毕昇栈与生产栈 activate 均 `_GLIBCXX_USE_CXX11_ABI=1`（或不设走默认）。`revert_ascir.py` 同理修改。修改前必须备份（`.bak_abifix_<timestamp>`）。

**方案 B（适合 A/B 对比验证）：每组跑前 sed swap + 清缓存**

用既有 A/B harness（`<ab-harness-script>`），在每次运行前：

1. sed 替换 `_compiler.py` 的 ABI 值为 `=1`（与全链一致）
2. 清 JIT 缓存（`/tmp/.npu_kernels_root` 等）
3. 清 pycache
4. 固定同卡

## 编译优化-Python

**前置条件**：
- 安装毕昇编译器并配置环境变量（CC=clang, CXX=clang++）
- 安装编译依赖（openEuler 使用 dnf/yum）：
  ```
  sudo dnf install gcc gcc-c++ gdb lzma glibc-devel libstdc++-devel openssl-devel \
  readline-devel zlib-devel libffi-devel bzip2-devel xz-devel \
  sqlite sqlite-devel sqlite-libs libuuid-devel gdbm-libs perf \
  expat expat-devel mpdecimal python3-pip patchelf
  ```

**编译参数**：
```
export CC=clang
export CXX=clang++
./configure --prefix=<isolation-prefix>/python311-lto --with-lto --enable-optimizations --enable-shared
make -j
make install
```

**优化项**：
- `--with-lto`：开启 LTO
- `--enable-optimizations`：开启 PGO（Python 3.6+ 支持 LTO 与 PGO，跑 Python 自带 benchmark，耗时可能超过 30 分钟）
- `--enable-shared`：生成共享库 libpython3.x.so（PyTorch 编译时需要链接，必须开启）

**rpath 坑（必须修复）**：`--enable-shared` 不自动设置 rpath。隔离前缀的 python3 二进制在无 LD_LIBRARY_PATH 时会加载系统 libpython（版本不匹配），产生危险 ABI 不匹配。编译安装后用 patchelf 修复 rpath：

```
# 备份后修复
cp <isolation-prefix>/python311-lto/bin/python3.<minor> \
   <isolation-prefix>/python311-lto/bin/python3.<minor>.orig
patchelf --set-rpath '$ORIGIN/../lib' \
   <isolation-prefix>/python311-lto/bin/python3.<minor>
```

修复后 python3 自解析到 prefix/lib/libpython3.x.so，无需 LD_LIBRARY_PATH。

**PGO 期间的正常现象（非缺陷）**：PROFILE_TASK 运行时个别测试报 "env changed" 或少量失败属 PGO 插桩构建的正常产物，profile-opt 内部用 `|| true` 继续，llvm-profdata merge 成功即可。mpdecimal 不在 openEuler repo 不是问题（Python 3.11 自带 libmpdec）。libuuid-devel 由 util-linux-devel 提供。

**后续步骤的环境前置**：
```
source /etc/profile.d/bisheng.sh
export PATH=<isolation-prefix>/python311-lto/bin:$PATH
export LD_LIBRARY_PATH=<isolation-prefix>/python311-lto/lib:$LD_LIBRARY_PATH
```

**注意事项**：
- 编译完的 Python 可迁移到其他机器，注意 glibc 版本（低→高可以，高→低不行）
- 运行时如报 .so 找不到，检查编译依赖是否安装完全
- 如用 conda 管理环境，可将 Python 安装目录指向 conda 空环境目录

## 编译优化-PyTorch

**前置条件**：
- 已用毕昇编译安装 Python
- 安装毕昇编译器并配置环境变量（CC=clang, CXX=clang++）
- **构建工具版本钉版**：setuptools 必须 `bdist_wheel` 可用且 `<84`（>=84 绕过自定义 build_py 导致 whl 静默残缺；**<70.1 不内置 bdist_wheel，实测 68.2.2 缺**）。推荐 `setuptools==75.8.0 wheel==0.41.3`（实测可用组合），装后验证 `python3 -c "import setuptools.command.bdist_wheel, wheel"`
- 安装编译依赖：`pip install -i https://mirrors.huaweicloud.com/repository/pypi/simple -r requirements.txt`（best-effort，缺什么按编译报错补装）
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
- 如报 c++11 abi 不一致，统一全链 ABI（vLLM-Ascend/PyTorch/torch_npu 三者，默认 `=1`）后重新编译
- **USE_TENSORPIPE 实测修正（BiSheng 5.2 / clang 19.1.7 / torch 2.5.1）**：TP 开启时 libnop `variant.h` 报 clang19 新诊断 `error: a template argument list is expected after a name prefixed by the template keyword [-Wmissing-template-arg-list-after-template-kw]`（`third_party/tensorpipe/third_party/libnop/include/nop/types/variant.h:241` 等 3 处）。**不必禁用 TP**：在 CMAKE_CXX_FLAGS 加 `-Wno-error=missing-template-arg-list-after-template-kw` 压制后 TP 完整编译通过，实测 `rpc available: True`、matmul 正常。直接 `USE_TENSORPIPE=0` 会得到 rpc 注册不完整的非标构建，`import torch` 报 `cannot import name '_DEFAULT_NUM_WORKER_THREADS' from 'torch._C._distributed_rpc' (unknown location)`。仅当压制后仍编译失败才回退 `USE_TENSORPIPE=0`（并预期 rpc 缺失风险）
- **⚠️ TP=0 的连带损失（问题16）**：TCPStore 的 libuv 后端编译宏 `TORCH_USE_LIBUV` 经 `if(USE_DISTRIBUTED AND USE_TENSORPIPE)` 链路启用（cmake/Dependencies.cmake:1158）——TP=0 会**连带砍掉 libuv 后端**，而 torch 2.5 的 TCPStore 默认 `use_libuv=True`，运行时直接崩溃。故 TP=0 的代价是「rpc 缺失 + TCPStore 崩溃」双重损失，务必用 TP=1+压制旗方案。验证命令：`python3 -c "from torch.distributed import TCPStore; s=TCPStore('127.0.0.1',0,is_master=True,wait_for_workers=False,use_libuv=True); s.set('k','v')"`（脚本 --verify 已内置）
- `USE_KINETO=0`：kineto 子模块可能缺失，禁用后需创建占位头文件 `ActivityType.h`（包含完整枚举值）
- ONNX FetchContent 下载 protobuf 卡住时，设置 `FETCHCONTENT_SOURCE_DIR_PROTOBUF` 指向本地源码
- nlohmann/json.hpp 缺失时，通过 jsdelivr CDN 下载单头文件
- ThinLTO 编译的 PyTorch 运行时需 LD_PRELOAD `libsleef.so` + `libtlfloat.so`（SVE 矢量化数学库）。如果 LD_PRELOAD 后仍有 SVE 符号未定义错误，需重新编译 sleef 共享库时开启 `SLEEF_ENABLE_SVE=ON`
- **安装后运行库自修复（实测踩坑）**：系统 protobuf soname 不匹配时（如系统 25.x，构建需 3.13），cmake 会把自带 protobuf 编到 `build/lib/` 与 `torch/lib64/`，但 whl 安装后 `torch/lib` RPATH 覆盖不到，`import torch` 报 `libprotobuf.so.3.13.0.0: cannot open shared object file`。修复：安装后扫描 `ldd torch/lib/*.so | grep 'not found'`，从 `build/lib/`（或 `torch/lib64/`）把缺失库 `cp` 进 `site-packages/torch/lib/`。`ak_compile_optimize.sh` 已内置该自修复

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
- 如报 c++11 abi 不一致，检查 vLLM-Ascend、PyTorch 和 torch_npu 三者的 DGLIBCXX_USE_CXX11_ABI 值是否一致（默认 1）
- clang 19 严格 const 检查可能导致 `op_api_common.cpp` 的 `reinterpret_cast` 报错，需用 `const_cast<void*>` 修复

## 优化手段选择决策

Agent 采集当前编译环境后，向用户展示可用优化手段：

| 优化手段 | 前置条件 | 需跑模型采集 | 操作形态 | 实测收益锚点（同类场景） | 适用组件 |
|---------|---------|------------|---------|----------------------|---------|
| Python LTO+PGO | 毕昇编译器 | 否（PGO 内置） | 单次构建 | **+71.6%**（hostbound vLLM，解释器甜点） | Python |
| PyTorch LTO | 毕昇编译器 + 毕昇版 Python | 否 | 单次构建 | **-26%/-11%**（CPU 算子 matmul/sort） | PyTorch |
| PyTorch LTO+PGO | 毕昇编译器 + 毕昇版 Python | 是 | 两阶段+采集 | dispatch 路径再增（参与 +13.3% 增量）；CPU 算子净增≈0 | PyTorch |
| torch_npu LTO | 毕昇编译器 + 毕昇版 PyTorch | 否 | 单次构建 | **-22.4% 负收益**（纯 ThinLTO 无 PGO，实测退化） | torch_npu |
| torch_npu LTO+PGO | 毕昇编译器 + 毕昇版 PyTorch | 是 | 两阶段+采集 | 满收益配置（-22.4% 被挽回为正） | torch_npu |
| Python BRBE(CSSPGO) | 毕昇 + BRBE 硬件 + llvm-profgen | 是（perf 采样，免插桩） | gen/collect/use 三段 | 与插桩 PGO 等价（Python 侧实测） | Python |
| PyTorch CSSPGO | 毕昇 + 毕昇版 Python + BRBE 硬件 + llvm-profgen | 是（perf 采样，免插桩） | gen/collect/use 三段 | gen→CSS +7.6%（vs 插桩 +11.0%，少 3~4%） | PyTorch |
| torch_npu CSSPGO | 毕昇 + 毕昇版 PyTorch + BRBE 硬件 + llvm-profgen | 是（perf 采样，免插桩） | gen/collect/use 三段 | 未单测（同 PyTorch 机制） | torch_npu |

推荐排序规则（收益证据定优先级，操作形态只描述成本不降低优先级）：
1. **组件正确性优先于难度**：torch_npu **必须** LTO+PGO——纯 LTO 为实测负收益（-22.4%），禁止以"难度低/免采集"为由推荐 LTO-only（对照实验豁免见脚本门禁 AK_ALLOW_TORCH_NPU_LTO_ONLY）
2. **收益锚点定推荐**：热点在解释器/dispatch 路径 → Python LTO+PGO 是最大单手段收益（+71.6%）且 PGO 内置免采集、操作最简；热点在 CPU 算子 → PyTorch LTO 已是收益主力，PGO 按需
3. 按编译顺序推荐（Python → PyTorch → torch_npu），前置依赖满足才可进位
4. "使用难度"描述操作复杂度（两阶段+采集），**不是**优先级依据——收益不明晰时以 A/B 实测裁决，不得因难度降序
5. 不可用的手段列在最后，标注限制原因
6. **CSSPGO(BRBE) 与插桩 PGO 之间默认选插桩 PGO**：torch 侧 BRBE 少约 3~4% 收益（实测 gen→PGO2 +11.0% vs gen→CSS +7.6%），Python 侧两者等价。仅当无法部署插桩版/只能对生产进程采样/插桩多进程刷盘不可解时推荐 BRBE，完整决策矩阵见 `references/csspgo-brbe-playbook.md`


> Agent 不得自动执行任何编译，必须向用户展示可用清单并等待用户选择。收益估算与锚点冲突时见 SKILL.md「候选排除纪律」7/8（经验库实测优先于 agent 估算）。

## 编译流程与状态管理

```
编译状态:
  - python: not_compiled | compiling | compiled | failed | skipped
  - pytorch: not_compiled | compiling | compiled | failed | skipped
  - torch_npu: not_compiled | compiling | compiled | failed | skipped

规则:
  - bisheng编译顺序按python、pytorch、torch_npu（前一个未完成不能开始下一个）
  - 用户指定单组件时，只编译该组件，其他标记 skipped
  - Python PGO 为内置 PGO（跑 Python 自带 benchmark），不需要用户跑模型，但耗时较长（可能超过 30 分钟）
  - PyTorch/torch_npu PGO 为工作负载 PGO，需要: 插桩编译 → 安装 → 跑模型 → 采集 profile → 二次编译 → 安装
```

> **编译前预检**: 完整编译三个组件需要 10-15GB 磁盘空间，编译前检查可用空间是否充足。
> 以上流程可用 `scripts/ak_compile_optimize.sh` 自动化：
> `--component python|pytorch|torch_npu --optimize lto|lto_pgo|css_pgo --stage compile|profile_gen|profile_use|gen|collect|use`（python 的 PGO 内置，stage=compile；`css_pgo`（BRBE 采样式 PGO）用 `--stage gen|collect|use`，支持 `--pid`/`--cmd`/`--binary`/`--duration`/`--page-size`/`--css-profile`），
> 支持 `--container`/`--source-dir`/`--install-dir`/`--python-executable`/`--bisheng-prefix`/`--max-jobs`/`--pip-index`/`--no-github-accelerate`/`--log-file`/`--check-only`/`--verify`/`--dry-run`。
> 脚本内置门禁：毕昇检测（含 source profile.d）→ 绝对路径 CC/CXX → 产物/whl readelf 毕昇标识复核 →
> 前置组件毕昇版校验（pytorch 需毕昇版 Python，torch_npu 需毕昇版 PyTorch，`AK_ALLOW_NON_BISENG_PYTHON=1` 可越权）→
> PGO profile 有效性阈值（size>50MB 且函数数>100000，`AK_PGO_SKIP_THRESHOLD_CHECK=1` 可跳过）→
> 安装后运行库自修复（ldd not found → 从 build/lib 补齐）。

## 编译耗时预期与长时间任务运行纪律（Agent 必读）

### 耗时基线（384 核 / 374 并行实测，按并行度近似线性缩放）

| 阶段 | 基线耗时 |
|------|---------|
| Python LTO+PGO（内置 benchmark） | ~13 min |
| PyTorch ThinLTO（--optimize lto） | ~15 min |
| PyTorch PGO1 插桩编译 | ~16 min |
| PyTorch PGO2 二次编译 | ~20 min |
| torch_npu ThinLTO+PGO | ~15-20 min |

> **版本支持边界（实测，毕昇 5.2.0 / clang 19.1.7，aarch64）**：pytorch v2.1.0/v2.4.0/v2.5.1 全链 PGO1 通过；**v2.6.0 经分版补丁后全链 PGO1 通过**（见下方补丁集）；源码拉取机制版本无关（各版本实测 266-1636s，随镜像 SHA 覆盖浮动）。
>
> **分版补丁集（`references/patches/`，经 `AK_PATCH_SCRIPT` 钩子挂载，主脚本不内置分版处理）**：
> | 补丁文件 | 适用 | 解决 |
> |---|---|---|
> | `torch_npu-clang19-missing-includes.sh` | torch_npu 2.4.0-7.1.0 全系 | profiler utils.h 缺 `<time.h>`、tensorpipe socket.h 缺 `<array>`（clang 严格模式不可见的传递包含） |
> | `torch_npu-v2.4.0-6.0.0-abi-env.sh` | 仅 v2.4.0-6.0.0 | setup.py 硬编码 ABI=0 且不透传 GLIBCXX——与 ABI=1 全链基线混链（须先恢复该文件后经钩子挂载） |
> | `pytorch-bisheng520-sve256-off.sh` | pytorch 全系（实测 2.6.0/2.7.1） | ATen SVE256 内核触发毕昇 InstCombine 编译期+ThinLTO 链接期双段错误（剔除该能力变体，运行时回落 DEFAULT） |
>
> 新版本踩坑处置流程：拉取/编译失败 → 栈定位具体文件与 pass → 单文件复现 + 旗标矩阵试错 → 源码/cmake 级幂等补丁 → 存入 patches/（按家族或版本命名）→ 经钩子挂载验证。

典型目标环境（64-96 核）预计 **1-2 小时，属预期行为，不是异常**。

### 纪律（硬性禁止）

1. **禁止因耗时跳过编译阶段或缩短流程**——跳过=用户目标未达成；**同样禁止在候选分析/任务包阶段以耗时或自设时间预算为由把编译动作排除出候选面**（耗时属用户裁决维度，决策层规则见 SKILL.md「候选排除纪律」：正收益候选必须呈现、高成本上浮用户裁决、可行性假设须当次现场验证）
2. **禁止未经用户确认的"加速"**：降优化等级（去 PGO/去 ThinLTO/降 O 级）、删改测试、临时改构建参数均属擅自变更。历史教训：纯 ThinLTO 无 PGO **负收益 -22.4%**，任何"加速"都可能毁掉全部收益
3. 禁止反复 kill/重启构建进程；进程中断后用 `--reuse-build` 增量续编，不要全量重来
4. 禁止在阻塞式前台调用中干等——用下面的后台协议

### 推荐运行方式（长任务协议）

1. 分离后台运行：`setsid nohup ak_compile_optimize.sh ... --log-file build.log &`（与 Agent 会话解耦，会话中断不影响编译）
2. 每 5-10 分钟轮询：`tail -3 build.log`——ninja 的 `[n/m]` 即实时进度
3. 脚本启动时输出按并行度缩放的**预期耗时参考**，首轮可据此向用户预告
4. 用户认为过久时，提供三个正当选项并**等待用户选择**：
   - a. 后台运行 + 定时汇报（推荐，不损失任何收益）
   - b. 降级为纯 LTO（省去 PGO 采集与二次编译，收益减少，需用户明确确认）
   - c. 分阶段交付（先交付 Python+PyTorch 的 LTO 版，PGO 作为二期）
5. ThinLTO 后端缓存默认开启（`--thinlto-cache-dir`，AK_THINLTO_CACHE=0 关闭），显著加速修复性重链/增量重链；缓存目录在源码树内，可随源码清理
> css_pgo 的 gen/collect/use 三段流程与决策矩阵见 `references/csspgo-brbe-playbook.md`。


## 验证方法

每个组件编译完成后验证：
- **Python**: `<isolation-prefix>/python311-lto/bin/python3 --version` 确认版本；`readelf -p .comment ./bin/python3 | grep -i bisheng` 确认使用毕昇编译器编译；`import ssl, hashlib, decimal, sqlite3, ctypes, lzma, bz2, zlib, _uuid, readline` 全部通过；`ldd` 确认 libpython 自解析到 prefix/lib（rpath 修复生效）
- **PyTorch**: `pip3 install *.whl` 后 `python3 -c "import torch; print(torch.__version__)"`（从干净 CWD 运行）；`readelf -p .comment $(python3 -c "import torch,os; print(os.path.join(os.path.dirname(torch.__file__), '_C'))")*.so | grep -i bisheng` 确认毕昇编译；检查 libomp.so 链接（`ldd $(which python3) | grep omp`）；matmul smoke test 通过
- **torch_npu**: `pip install *.whl` 后 `python3 -c "import torch_npu; print(torch_npu.npu.is_available(), torch_npu.npu.device_count())"`；`readelf -p .comment $(python3 -c "import torch_npu,os; print(os.path.dirname(torch_npu.__file__))")/*.so | grep -i bisheng` 确认毕昇编译；检查 libomp.so 链接
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
| 通用 profile（非目标负载生成） | -1.33% | profile 非目标负载生成，不匹配 |
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

### Profile 空文件排查决策树（PGO1 采集后 profile 为空/过小）

| 现象 | 根因 | 处置 |
|------|------|------|
| `${PROFILE_DIR}` 无任何 .profraw | workload 没跑/秒退 | 前台直接运行 workload 看报错（缺 CANN env、模型路径错、依赖缺失均会秒退） |
| 无 .profraw 且产物无 `__llvm_prf` 段 | 运行的是 PGO2/非插桩二进制 | `readelf -SW <torch>/lib/libtorch_cpu.so \| grep -c __llvm_prf`，为 0 须重装 PGO1 插桩版 |
| 无 .profraw，多进程（vLLM v1） | 子进程重建环境丢弃 `LLVM_PROFILE_FILE` | `sitecustomize.py` 注入（fork 层生效，改 run_dp_template.sh 无效）+ 文件名带 `%p` |
| 无 .profraw 但运行目录有杂散 profraw | `LLVM_PROFILE_FILE` 未生效，写了默认位置 | 移入 `${PROFILE_DIR}` 后重跑 merge |
| profraw 存在但 0 字节/计数全零 | 进程被 SIGTERM/SIGKILL，atexit 未刷盘 | 对**全部** workload 进程一次性 SIGINT（见下文 §1 修复 B）；PGO1 采集期间禁止 kill |
| profraw 正常但函数数 < 100000 | workload 覆盖不足/运行过短 | 延长运行、覆盖 prefill+decode 等主要路径，与正式压测同 model/batch/prompt 分布 |

> 脚本 `check_profile` 失败时输出 `instrumented_sections`（插桩复核）与 `stray_profraw`（杂散搜索）诊断字段，可直接按上表处置；`profile_gen` 完成即验证插桩段（`artifact_not_instrumented` 门禁），避免带病采集。

### Profile runtime 崩溃问题（lprofFindFirstDirSeparator NULL deref）

现象（历史用例）：插桩版 PyTorch 启动即崩，`lprofFindFirstDirSeparator` NULL deref in `__llvm_profile_initialize` of `libtorch_global_deps.so`。

**BiSheng 5.2.0（LLVM 19.1.7）实测不存在该 bug**——双矩阵验证（本机 aarch64）：
- 最小插桩程序（`clang -fprofile-generate`）：`LLVM_PROFILE_FILE` unset/空/`%c`/`%c路径`/normal/`%m`/混合模式 8 种取值全部正常退出（`%c` 变体仅有无害告警 `__llvm_profile_counter_bias is undefined`）
- 真实场景：插桩版 `import torch`（libtorch_global_deps.so 含 `__llvm_prf` 段）在上述关键取值下全部 import-ok

若在**旧版毕昇/LLVM（≤16 时代）**触发，规避方案：
1. **采集前显式 export 非空合法路径**（脚本采集清单第 3 步已内置）：`export LLVM_PROFILE_FILE=<dir>/default_%m_%p.profraw`——空值/未设置是已知触发条件
2. 避免裸 `%c`（continuous mode 旧版解析路径有缺陷）
3. 升级毕昇至 5.2+

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

### 2. PyTorch 与 torch_npu 的 ABI 环境变量「变量名不同」（vLLM-Ascend/PyTorch/torch_npu 三者须一致，统一 ABI=1）

- **PyTorch** 读取 `GLIBCXX_USE_CXX11_ABI`（**无下划线**）；设带下划线变量对 PyTorch **无效**，PyTorch 按默认编成**新 ABI=1**。
- **torch_npu** 的 `setup.py` 读取 `_GLIBCXX_USE_CXX11_ABI`（**有下划线**）。
- 同一带下划线变量不可能同时控制两者 → 显式设置时须分变量名设置且值统一（=1），否则两者 ABI 错配。

**错配报错**：
```
ImportError: libtorch_npu.so: undefined symbol: _ZN5torch7LibraryC1ENS0_4KindESsSt8optionalIN3c1011DispatchKeyEEPKcj
```
mangled 里 `Ss` 表示报错组件是旧 ABI 构建 → 与 ABI=1 组件的 `NSt7__cxx1112basic_string...` 对不上，据此定位错配组件并重编为 ABI=1。

**正确做法（按目标分变量名、值统一为 1）**：
```bash
export GLIBCXX_USE_CXX11_ABI=1    # PyTorch（无下划线），默认 NEW ABI=1
export _GLIBCXX_USE_CXX11_ABI=1   # torch_npu（有下划线），值与 PyTorch 一致；vLLM-Ascend 侧同样须为 1
```

### 全链 ABI 一致性校验清单（脚本已自动化）

**不变量是「全链一致」而非具体值**：生产栈可能整体运行旧 ABI(0)，此时强制 =1 反而制造新错配。基线取值规则：PyTorch 实际 ABI（`int(torch._C._GLIBCXX_USE_CXX11_ABI)`，Python bool 须归一化）→ torch_npu 按同值构建；全新链构建时默认 1（`AK_GLIBCXX_USE_CXX11_ABI` 可覆盖以适配 ABI=0 既有栈）。

torch_npu 编译阶段校验（`ak_compile_optimize.sh` 内置，越权开关 `AK_ALLOW_ABI_MISMATCH=1`，不推荐）：

| 时机 | 校验 | 命令 | 判定 |
|------|------|------|------|
| 编译前 | 读取 PyTorch ABI 作为全链基线 | `python3 -c "import torch; print(int(torch._C._GLIBCXX_USE_CXX11_ABI))"` | 0/1 均可作为基线；无法读取 → `torch_abi_unknown` |
| 构建 | torch_npu 按基线同值构建（双变量名导出） | `GLIBCXX_USE_CXX11_ABI=<基线>` 与 `_GLIBCXX_USE_CXX11_ABI=<基线>` | — |
| 编译后 | **标记比对**（首选） | cpu 定义侧 `nm -D libtorch_cpu.so \| grep '_ZN5torch7LibraryC1'` vs npu 引用侧 `nm -D libtorch_npu.so \| grep ' U _ZN5torch7LibraryC1'`，各提取 `NSt7__cxx1112basic_string`/`Ss` 标记 | 标记相同=一致；不同 → `torch_npu_abi_mismatch` |
| 编译后 | 计数回退（标记提取失败时） | V=1: `Ss` 计数须为 0；V=0: `NSt7__cxx11` 计数须为 0 | 违反 → `torch_npu_abi_mismatch` |
| 任意 | vLLM-Ascend 提示 | 检测到 vllm 即告警 | 三链之一，须与基线一致，脚本不覆盖其重编译 |

**标记比对四组合实测矩阵**（mock 编译验证，毕昇 clang 19.1.7 ThinLTO）：

| PyTorch | torch_npu | 判定 |
|---------|-----------|------|
| ABI=1（NSt7__cxx11） | ABI=1（NSt7__cxx11） | 一致 PASS |
| ABI=0（Ss） | ABI=0（Ss） | 一致 PASS（旧"强制=1"检查会误杀此组合） |
| ABI=1 | ABI=0（Ss） | 错配 DETECTED |
| ABI=0 | ABI=1（NSt7__cxx11） | 错配 DETECTED |

> 比对前提：`LibraryC1` 在 libtorch_cpu 中为强符号（T，out-of-line 定义）；inline 定义会被 ThinLTO 内部化导致标记缺失（同 new_qtensor 机制），此时走计数回退。

核实命令（错配定位）：
```bash
python3 -c "import torch; print(int(torch._C._GLIBCXX_USE_CXX11_ABI))"
nm -D .../torch_npu/lib/libtorch_npu.so | grep " U _ZN5torch7LibraryC1.*Ss"    # 旧ABI符号（基线=1时应为0）
nm -D .../torch_npu/lib/libtorch_npu.so | grep -c NSt7__cxx1112basic_string    # 新ABI符号数
nm -D .../torch/lib/libtorch_cpu.so | grep "LibraryC1.*NSt7__cxx11"            # PyTorch 侧标记
```
核实：
```bash
python3 -c "import torch; print(torch._C._GLIBCXX_USE_CXX11_ABI)"
nm -D .../torch_npu/lib/libtorch_npu.so | grep " U _ZN5torch7LibraryC1.*Ss"    # 旧ABI符号（应为0）
nm -D .../torch_npu/lib/libtorch_npu.so | grep -c NSt7__cxx1112basic_string    # 新ABI符号数
nm -D .../torch/lib/libtorch_cpu.so | grep "LibraryC1.*NSt7__cxx11"            # PyTorch 侧新/旧ABI符号
```

### 3. PyTorch ThinLTO 内部化 inline 函数 `new_qtensor`（torch_npu import 报 undefined symbol）

现象：torch_npu import 报 `undefined symbol: at::new_qtensor(...)`。
检查：`nm -D .../torch/lib/libtorch_cpu.so | grep -c new_qtensor` 为 0 = 已内部化。

**根因（torch 2.5 + lld 实测）**：`new_qtensor` 在 `aten/src/ATen/quantized/Quantizer.cpp` 中是 **inline 定义**（IR 层 `linkonce_odr` + comdat）。lld ThinLTO 会把「仅被 DSO 内部引用的 inline 弱符号」内部化（静态符号表可见 `t` 类型 `.local` 克隆）——**它不是 COMDAT 折叠，而是 internalization**。

**已实测无效的链接旗标（勿再尝试）**：`-Wl,-u,<sym>`、`-Wl,--export-dynamic(-E)`、`-Wl,--export-dynamic-symbol,<sym>`——全部无法阻止内部化，因为对 lld 而言该符号的动态导出判定发生在内部化之后（最小复现：inline + ThinLTO + `-fprofile-generate` + `-fvisibility=hidden` + lld `-shared` 必现）。

**⚠️ 另一陷阱：`-Wl,-u` 不能进全局 `LDFLAGS`**。cmake 会把它初始化进 `CMAKE_EXE_LINKER_FLAGS`，try_compile 配置探针继承后因探针程序无该符号而链接失败，整个配置阶段报 `The C compiler is not able to compile a simple test program`。

**唯一可靠修复（源码级，实测导出成功）**：
```bash
# 1. 把 inline 定义改为外部链接（备份后 sed）
cp aten/src/ATen/quantized/Quantizer.cpp aten/src/ATen/quantized/Quantizer.cpp.bak_inline
sed -i 's/^inline Tensor new_qtensor(/Tensor new_qtensor(/' aten/src/ATen/quantized/Quantizer.cpp
# 2. 增量重链（单 TU 重编 + 链接，不触发全量编译）
python3 setup.py bdist_wheel
# 3. 复核：nm -D libtorch_cpu.so | grep -c new_qtensor  ≥1
```
> 同源码双机可能「一个导出、一个不导出」——必须逐节点 `nm` 核实。
> `ak_compile_optimize.sh` 已自动化（`repair_link_defects`：构建后 nm 检测 → 源码补丁 → 增量重链 → 复核）。

**泛化（不止 new_qtensor）**：任意量化相关 inline 符号都可能被同样内部化，torch_npu import 依次报不同 undefined symbol。脚本在 torch_npu verify 阶段内置通用修复回路（`fix_internalized_symbols`）：import 失败 → 提取缺失符号 → 确认在 libtorch_cpu 中为 local(t) → demangle 定位定义源文件（grep 源码树 `^inline .*<函数名>(`）→ 去 inline（备份）→ 增量重链 PyTorch → 复核导出。**torch_npu 无需重编**（符号经 dynsym 动态解析）；定位失败的符号输出清单转人工。

### 4. 静态 libopenblas.a 拉入 Fortran LAPACK 成员 → `_gfortran_concat_string` 未解析

现象：`import torch` 报 `undefined symbol: _gfortran_concat_string`（或 `ldd -r libtorch_cpu.so` 检出）。
根因：torch cmake 强制 `BLA_STATIC`，openEuler 的 FindBLAS 选中 `/usr/lib64/libopenblas.a`（静态）；PGO 插桩构建的链接拉入了其中 **Fortran 编译的 LAPACK 成员**，其 `_gfortran_*` 引用进入 libtorch_cpu，但 DT_NEEDED 缺 libgfortran。
修复：CMakeCache 的 **SHARED/MODULE** linker flags 追加 `-Wl,--no-as-needed -lgfortran`，touch CMakeLists.txt 后增量重链。
**⚠️ 两个陷阱（实测踩坑）**：
1. **严禁波及 `CMAKE_STATIC_LINKER_FLAGS`/`CMAKE_EXE_LINKER_FLAGS` 等其他变量**——CMake 把 SHARED/MODULE LINKER_FLAGS 同时喂给 `llvm-ar` 归档命令，导致全部静态库归档失败（`llvm-ar: unknown option W`）
2. **cmake 不感知 CMakeCache.txt 的外部修改**（build.ninja 重生成依赖里没有 cache）——补丁后必须 `touch CMakeLists.txt` 强制重新配置才能生效（探针结果已缓存，不会重跑 try_compile）
> `ak_compile_optimize.sh` 已自动化（`repair_link_defects`：`ldd -r` 检测 → 仅修 SHARED/MODULE 两键 → 一次增量重链 → 复核）。

### 5. CANN dlsym 机制被 ThinLTO 破坏（npu_mhc_* 算子失败）

现象：`npu_mhc_*` 等算子运行时报错，`check_aclnn_kernel_available`/`GetOpApiFuncAddr` 的 dlsym 查找失败。
根因：CANN runtime 经 `dlsym(RTLD_DEFAULT)` 在 caller（PyTorch 进程全局作用域）的 `.dynsym` 中**按名**查找运行时符号；这些符号常以 **inline 定义**（linkonce_odr）存在于 CANN 头文件并被编译进 caller——ThinLTO 将「仅被内部引用的 inline 弱符号」内部化后从 `.dynsym` 消失，dlsym 落空。**该类符号无链接期引用，import 阶段不报错，只有算子运行时暴露**，与 §3 的 import 失败是不同触发面。

**模拟复现结论**（mock：CANN runtime + caller + 主程序三件套，毕昇 clang 19.1.7 ThinLTO）：

| caller 侧定义形态 | .dynsym | dlsym 结果 |
|------------------|---------|-----------|
| inline 定义（仅内部引用） | 0 条 | **失败**（问题14 复现） |
| 去 inline：声明 + 单一强定义 | 1 条（T） | 成功 |

模拟要点（踩坑记录）：① dlsym 按字面名查找，C++ 函数需 `extern "C"` 或用修饰名；② caller 必须在全局作用域（DT_NEEDED 依赖天然满足，dlopen 需 RTLD_GLOBAL）；③ 头文件带函数体的 inline 定义无法在别处补强定义（重定义冲突），去 inline = 头文件改声明 + 函数体移入单一 TU。

**检测**：脚本 `audit_dlsym_export_surface` 门禁（pytorch/torch_npu 安装后各一次）——从 CANN 库（libopapi/libacl/libascendcl）字符串提取 dlsym 候选名，与 torch 产物求交：`.dynsym` 无而 symtab 为 local(t) = 内化命中。命中后优先走 `fix_internalized_symbols` 自动修复，无源码则 fail_json 转人工。
**预防**：无法修改的 CANN 头文件 inline 定义，如确认被 dlsym 依赖，可反馈 CANN 侧改为宏/非 inline 形态；PyTorch 侧符号由脚本自动修复。

### 18. 符号链接导致迁移后产物不可用（拷贝编译产物到 B 容器后 import 失败）

**模拟矩阵**（mock 安装树：真实库 + 相对链接 + 绝对链接 + 跨目录链接）：

| 迁移方式 | 结果 |
|---------|------|
| `cp -r`（不带 -a，跟随链接） | 链接被解除为实体拷贝（内容重复，树外目标拷贝失败即缺文件） |
| `tar` 管道（保留链接） | **绝对路径链接在容器 B 断链**——链接指向构建容器路径，B 容器不存在 → import 失败（问题直接机制） |
| `cp -a` / `rsync -a` / `tar`（链接保留） | 结构完整，可用 |

**规则**：
1. **迁移必须保留符号链接**：`cp -a`、`rsync -a`、`tar`（默认保留）；禁止 `cp -r` 裸用、禁止 zip 打包（部分工具丢链接）
2. **发布树内禁止绝对路径链接**：构建后审计改写（树内目标 → 相对链接；树外目标 → 先捆绑目标文件再相对化，参照 libprotobuf 捆绑模式）
3. 迁移后校验：`ak_portability_manifest.json` 的 `symlinks` 段逐一核对 + `ldd -r` + import

**检测**：脚本 `verify_symlink_integrity`（pytorch 可迁移性审计与 torch_npu 安装后各一次）——绝对链接树内自动改写、树外与断链 `symlink_migration_hazard` 拦截。

### 19. `.rodata` 段布局与 64KB 页兼容（是否需要 `--no-rosegment`）


**结论：BiSheng 5.2（LLVM 19.1.7）不需要。**

- 4KB 页宿主机上的测试结论**适用于 64KB 页目标**：链接期布局由 lld `max-page-size`（aarch64 默认 65536）决定，与宿主机页大小无关——4KB 宿主机产物实测全部 LOAD 段 `Align=0x10000`（即布局本就是 64KB 口径）
- 历史问题（`section .rodata load segment overlap`，lld 旧版 RELRO + 独立 RO 段布局缺陷）在 LLVM 17 前已修复；LLVM 19 不触发
- 64KB 页特有的加载期属性已实测判定通过（libtorch_cpu.so，4 个 LOAD 段）：段对齐 ≥0x10000 ✓、`offset ≡ vaddr (mod p_align)` 同余 ✓、段区间无重叠 ✓
- 脚本已内置确定性判定门 `verify_elf_page_compat`（任何宿主机上运行均可回答"64KB 内核能否加载"）；若旧工具链真触发段重叠，构建日志检出 `load segment overlap` 后自动补丁 `--no-rosegment` 重链，无需预设

### 4. 重编译环境与多机一致性

- **`build/packages/torch_npu 不存在`**：往往是 CMake 原生编译(`build_clib/build_ext`)被跳过（`has_ext_modules()/has_c_libraries()` 判定失败），构建日志只有几十行无 `[n/m] Building`（成功机 6 万行 + 上千 Building 记录；排查：`grep -cE "\[[0-9]+/[0-9]+\] Building" 构建日志`，成功机上千、失败机 0）。解法：用与成功机相同的**隔离 venv 完整构建**（`include-system-site-packages=false` 的 venv，含 torch+setuptools+wheel+ninja+pyyaml）；`ci/build.sh` 的 `build` vs `build_py` 命令行差异与预创建包目录解法见 `torchnpu-wheel-packaging-playbook.md`。
- **op-plugin `reinterpret_cast ... casts away qualifiers`**（clang17 严格）：源码加 `const_cast<void*>`；op-plugin 是独立 CMake 子项目，`-Wno-cast-qual` 未必传导，改源码最可靠。
- **`No module named 'setuptools.command.bdist_wheel'`**（PyTorch setup.py 用）vs `No module named 'wheel'`**（torch_npu setup.py 用）**：ensurepip 自带 setuptools 常缺 bdist_wheel；且 **setuptools<70.1 本身不内置 bdist_wheel（实测 68.2.2 即缺）**。装 `setuptools==75.8.0 wheel==0.41.3`（bdist_wheel 可用且 <84，实测组合），逐机验证 `import setuptools.command.bdist_wheel` 与 `import wheel`。
- **构建环境与多机一致性**：`Python.h` include 路径（`CPLUS_INCLUDE_PATH`/`C_INCLUDE_PATH`）、whl 内容指纹校验、安装选包（勿 `ls dist/*.whl | head -1`）、容器 `/usr/local` 本地层持久性——详见 `torchnpu-wheel-packaging-playbook.md`（多机一致性 / 验证清单章节）。

