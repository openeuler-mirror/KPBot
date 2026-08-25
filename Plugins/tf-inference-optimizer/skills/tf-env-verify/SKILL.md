---
name: tf-env-verify
description: 验证 TF 推理优化的环境（本地/远程环境询问与记录、测试方法确认、远程执行链路、binary 部署与 kernel 符号检查、进程/端口/gflags 状态、编译产物核对、server 启动与 ready 判定），用于鲲鹏 aarch64 服务器。触发：环境验证、binary 检查、部署验证、nm 符号、remote-exec、堡垒机、predictor_server 进程、kernel 是否链接进 binary、环境信息询问。
---

# TF 推理优化 - 环境验证

在开始任何优化或压测前，先确认环境与部署产物状态正确。目标是排除「环境问题」和「产物没生效」这两类最耗时的坑。

## 0. 环境信息询问与记录（用户交互门控，优先执行）

进入本 skill 后，**第一步先确认环境信息已记录**，避免每次会话重复询问。环境信息写入**工作目录根目录** `./AGENTS.md`。

### 0.1 复用检查（幂等）

检查 `./AGENTS.md` 是否已有「TF 推理优化环境」记录：

- **已有记录** → 展示摘要，一键确认 `[复用 / 更新 / 重填]`：
  - 复用 → 跳过 0.2/0.3/0.4，检查工作目录 `./remote-exec.ps1`/`./remote-exec.exp` 是否存在（不存在则补执行 0.5），直接进入第 1 节。
  - 更新/重填 → 走 0.2/0.3/0.4 重问并覆盖写回，再执行 0.5 刷新脚本。
- **无记录** → 逐项询问。

### 0.2 询问执行环境（本地 / 远程）

- **本地**：记录本机 CPU 架构（是否为 aarch64）、TF 版本、编译工具链、依赖库。
- **远程**：先确认是否经堡垒机，再按子场景询问。

**必问（无论是否经堡垒机）**：

| 项 | 说明 | 示例 |
|---|---|---|
| 目标服务器 IP | 实际跑 binary 的机器 | `<TARGET_IP>` |
| 认证方式 | 密码 / 密钥 / 免密 | password |

**堡垒机（可选，仅「经堡垒机」场景，直连则跳过）**：

| 项 | 说明 | 示例 |
|---|---|---|
| 堡垒机地址/用户 | 登录跳板（直连目标机时不问） | `<BASTION_USER>@<BASTION_HOST>` |
| 登录链路跳数 | 便于描述与排查（直连时为 `本地→target`） | `本地→bastion→ext→target` |

> ⚠️ **执行链路与信息收集正交**：本地/远程、是否经堡垒机只决定「命令怎么执行」（第 1 节的 `remote-exec.ps1`/`.exp`），**不影响 0.3/0.4 的信息收集范围**——无论哪种连接方式，源码路径、binary、模型路径、测试用例（正确性/性能）都照常询问。

### 0.3 确认产物信息（逐项）

| 项 | 说明 | 示例 |
|---|---|---|
| 源码路径 | TF fork 源码仓库本地路径（图优化/kernel 改动的代码所在） | `<TF_SRC_DIR>` |
| 源码分支 | 优化分支是否正确，**必须向客户追问确认** | `<BRANCH>` |
| 执行脚本 | 压测/启动入口脚本 | `<MODEL_DIR>/start_benchmark.sh` |
| 二进制程序位置 | baseline/opt 可能有多个版本，逐一确认 | `<BIN_SERVER_OPT>` / `<BIN_SERVER_BASELINE>` |
| 模型路径 | MODEL_DIR（含 gflags 目录） | `<MODEL_DIR>` |
| 输出数据目录 | trace/metadata、results 落点 | `<MODEL_DIR>/metadata/` |
| 压测客户端 + 测试数据 | 压测与输入 | `<PRESS_DIR>/press`、`<TEST_DATA>` |
| 服务端耗时埋点日志 | 目标服务是否输出「逐请求耗时分解」埋点（**非所有场景都有**，需确认）；日志位置/字段名 | `<LOG_PATH>`（无则标「无」） |

> ⚠️ **binary 命名澄清（易错）**：`BIN_SERVER_OPT`（带加速 flag）是「当前已优化状态」，它是进一步优化的 **baseline**；`BIN_SERVER_BASELINE`（无加速 flag）是「原生基线」，仅首次评估整体加速比时对照使用。**不要按名字把「opt」当最终结果、把「baseline」当进一步优化的对照起点**——判断标准是「当前已合入了哪些优化点」，不是 flag/binary 的名字。

### 0.4 确认测试方法（逐项）

确定每个优化点的验证方法，后续优化点 subagent 直接继承，不再各自猜测：

#### 正确性测试（正确性测试用例，由用户确认）

正确性验证统一使用**用户提供的正确性测试用例**，不自行臆造口径。逐项询问并记录：

| 项 | 说明 | 示例 |
|---|---|---|
| 测试样本集 | 真实脱敏请求样本（数量、来源） | `<N>` 条真实脱敏请求 |
| golden output | 原生产版本输出（正确性对照基准） | `<GOLDEN_OUTPUT_PATH>` |
| 正确性口径 | 用户定义的正确性判据（如 Top-K 一致率、分数分布、逐元素误差等），由用户明确给出 | `<CORRECTNESS_CRITERION>`（由用户指定） |
| 边界/异常样本 | 含 NaN/Inf/极值的样本（若用户提供） | `<EDGE_CASE_SET>`（无则标「无」） |

> ⚠️ **正确性口径由用户定义**：优化验证必须基于用户提供的正确性测试用例与判据，禁止默认「Top-1」或自行假设指标。

#### 性能测试

| 项 | 说明 | 示例 |
|---|---|---|
| QPS 档位 | 限 QPS 压测档位列表 | `QPS_LIST='290 300 310'` |
| 每档时长 + 重复 | 压测时长、复测确认噪声 | `DURATION=60`，异常档复测 |
| 测量区间/统计口径 | 冷启动排除、预热、测量窗口、重复次数、方差/置信区间（**主动询问用户**，不做统一量化假设） | `WARMUP=10s` `REPEAT=3`（用户确认） |
| 并发/资源约束 | inter/intra、cpuset/NUMA policy | `inter=16 intra=16`，固定 cpuset |

#### op 级验证

| 项 | 说明 | 示例 |
|---|---|---|
| trace 抓取 | QPS=1 逐请求，抓几套 | `QPS=1`，baseline + opt 各一套 |

### 0.5 拷贝脚本模板并填充凭据

本 skill 的 `scripts/` 目录下的 `remote-exec.ps1` / `remote-exec.exp` 是**脱敏模板**（IP/账号/密码为占位符），**仅适用于「经堡垒机」链路**（本地→堡垒机→ext→目标机）。

- **经堡垒机**：拷贝并填充真实信息：
  1. 拷贝 `scripts/remote-exec.ps1` → `./remote-exec.ps1`，将 `$Target` 默认值 `<TARGET_IP>` 替换为真实目标 IP。
  2. 拷贝 `scripts/remote-exec.exp` → `./remote-exec.exp`，将 `<BASTION_HOST>` / `<BASTION_USER>` / `<BASTION_PASS>` 替换为真实堡垒机地址/账号/密码。
  3. 拷贝后工作目录下的脚本即为实际执行入口（见第 1 节）。
- **直连（无堡垒机）**：不拷贝堡垒机脚本，直接用 `ssh` 直连目标机执行命令（如 `ssh <USER>@<TARGET_IP> "<command>"`，或用户已有直连方式）。后续第 1 节的所有 `.\remote-exec.ps1 -Command ...` 示例，替换为对应的直连 `ssh` 形式即可。

> 本 skill `scripts/` 内的模板保持脱敏；填充真实凭据的副本只存在于工作目录，不写回插件。

### 0.6 写回 `./AGENTS.md`

写回工作目录根 `./AGENTS.md`（登录链路含密码，明文，沿用 `remote-exec.exp` 风格）：

```markdown
## TF 推理优化环境（tf-env-verify 自动记录）
- 执行环境: remote（经堡垒机 / 直连目标机）
- 登录链路: 经堡垒机 → 本地 → <BASTION_HOST>(<USER>, 密码 <PASS>, 选1) → ext(root) → <TARGET_IP>(root 免密)
           直连 → 本地 → <TARGET_IP>(<USER>, 免密/密码)
- 源码路径: <TF_SRC_DIR>
- 源码分支: <BRANCH>（已与客户确认）
- BIN_SERVER(opt): <BIN_SERVER_OPT>
- BIN_SERVER(baseline): <BIN_SERVER_BASELINE>
- MODEL_DIR: <MODEL_DIR>
- 执行脚本: <MODEL_DIR>/start_benchmark.sh
- 输出目录: <MODEL_DIR>/metadata/
- 服务端耗时埋点日志: <LOG_PATH>（无则「无」）
- 测试样本集: <N> 条真实脱敏请求
- golden output: <GOLDEN_OUTPUT_PATH>
- 正确性口径: <CORRECTNESS_CRITERION>（用户定义的正确性测试用例与判据）
- 边界/异常样本: <EDGE_CASE_SET>（无则「无」）
- 压测配置: QPS_LIST='290 300 310' DURATION=60 WARMUP=10s REPEAT=3 inter=16 intra=16
- 测量区间/统计口径: <WARMUP/REPEAT/置信区间，用户确认>
- CPU governor: <performance/powersave，用户确认，非 performance 告警>
- 记录时间: <ISO8601>
```

## 1. 远程执行链路

通过堡垒机在远端服务器执行命令。入口脚本 `remote-exec.ps1`（本地 Windows + WSL expect）。

```powershell
# 执行简单命令（短命令 Bash 超时设 ≥ 120000ms，链路需 25-30s）
.\remote-exec.ps1 -Command "ls -la <BIN_SERVER_OPT>"

# 长耗时命令用 -Timeout（单位秒，不是 ms）
.\remote-exec.ps1 -Command "cd <MODEL_DIR> && bash start_benchmark.sh" -Timeout 490

# 复杂脚本（含引号/变量）用 base64 传，避免 expect 引号转义问题
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($script))
.\remote-exec.ps1 -Command "echo $b64 | base64 -d > /tmp/x.py && python3 /tmp/x.py"
```

关键约束：
- `-Timeout` 是 `remote-exec.ps1` 的参数（秒，默认 10s），**不是** Bash 工具的 timeout 参数（ms）。
- PowerShell 命令里避免 `$(...)` 子表达式（会被本地 PowerShell 展开），用固定路径或 base64 传脚本。
- 长时压测/trace 抓取用 `setsid nohup ... &` 后台跑 + 轮询结果，避免 expect 伪终端卡住 press 客户端。

## 2. binary 部署验证（最关键）

图优化生成了 fused op 节点，但运行时找不到 kernel，是「图改对了但 server 优雅退出且无报错」的经典根因。验证 kernel 是否真的编译进 binary：

```bash
# kernel 符号计数（QKV 为例，应为 22）
nm -C <BIN_SERVER> 2>/dev/null | grep -c KPFusedQKVProjectionOp

# 时间戳（确认是重新编译后的新产物）
ls -la <BIN_SERVER>
```

判断：
- 符号计数 = 0 → kernel 未链接进 binary（BUILD 依赖漏了），图优化生成的节点运行时找不到 OpKernel。
- 只有 rewriter 符号（`...Rewriter`）、没有 kernel 符号（`...Op`）→ 只编译了图优化层，没编译 kernel 层。
- 时间戳是旧的 → 跑的还是旧产物，重新编译后需重新部署。

## 3. 进程 / 端口 / gflags 检查

```bash
# 进程（跑压测前确认无残留，避免端口占用）
ps aux | grep predictor_server | grep -v grep | wc -l
pkill -9 -f predictor_server   # 清理残留

# gflags（确认 port、线程数、tf_pre_run 等）
cat <MODEL_DIR>/script/predictor_server.gflags

# 部署目录结构（bin/script/conf/tf_models 是否齐全）
ls <MODEL_DIR>/
```

注意：`<MODEL_DIR>` 是部署目录（含 `script/predictor_server.gflags`），但可能没有 `bin/`，binary 用 `--flagfile=script/predictor_server.gflags` 启动，`cd` 到部署目录让相对路径（conf、log_dir）生效。

### 3.1 CPU governor 检查（压测前置，非 performance 告警）

压测与 profiling 默认假设 CPU 运行在 **performance** 模式。每次压测前确认，若为非 performance 模式必须**告警**（频率波动会造成 5~10% 测量噪声）：

```bash
# aarch64 上查看当前 governor（每个在线核）
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
# 或一次性查看全部核
for i in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo -n "$i: "; cat $i; done
```

- `performance` → 正常，可继续压测。
- `powersave` / `ondemand` / `schedutil` → **告警用户**：测量结果会受 DVFS 影响，建议切到 `performance`（`cpupower frequency-set -g performance`）后再测，否则收益归因不可信。

## 4. server 启动与 ready 判定

```bash
cd <MODEL_DIR> && setsid nohup <BIN_SERVER> \
  --flagfile=script/predictor_server.gflags \
  --enable_kdnn=true --annc_cf_matmul_batchnorm=2 --annc=true --annc_fused_matmul=true \
  > /tmp/server.log 2>&1 < /dev/null &
```

ready 标志：日志出现 `setup logger successfully`。若 server 在 feature convertor（`simple_hash_op` 初始化）之后优雅退出（`going to quit`）且无 error 日志，通常是 kernel 未链接或 AsyncOpKernel 在同步环境 runner==nullptr 报错（见 `tf-kernel-optimize`）。

## 5. 常用部署路径速查（脱敏占位符）

| 项 | 路径 |
|---|---|
| 部署目录 (MODEL_DIR) | `<MODEL_DIR>` |
| gflags | `<MODEL_DIR>/script/predictor_server.gflags`（port 6001） |
| opt 对照 binary | `<BIN_SERVER_BASELINE>` |
| 优化 binary | `<BIN_SERVER_OPT>` |
| 压测客户端 | `<PRESS_DIR>/press` |
| 测试数据 | `<TEST_DATA>` |

## 6. 踩过的坑速查

- **expect 卡住 press 客户端**：直跑长时 benchmark，press 在 qps 阶段卡住（PREWARM 后无 sent 行）。改用 `setsid nohup ... &` 后台跑。
- **fprintf(stderr) 看不到**：server 是守护进程，stderr 被重定向，调试日志用 `LOG(INFO)`/`VLOG` 而非 fprintf。
- **`$(...)` 被本地 PowerShell 展开**：远程命令里的 shell 变量用 base64 传脚本，别用 `\$` 转义。
