---
name: vllm-ascend-profiling
description: 用于自动化 vllm-ascend / vLLM Ascend NPU 的 profiling 数据采集与报告生成。触发关键词如 vllm-ascend、profiling、profile、trace_view.json、start_profile、stop_profile、VLLM_TORCH_PROFILER_DIR、NPU 性能报告、Top-N kernels、NPU 利用率、TTFT。仅用于 vllm-ascend（Ascend NPU）profiling 工作流。
---

# vllm-ascend Profiling Automation

端到端自动化 vllm-ascend profiling。vLLM 服务由**用户自行启动**；本 skill
负责准备环境、通过 HTTP 触发 3 秒采集窗口、归档并把 trace 解析为
**每 rank 一份** `trace_view.rank<N>.json`（多 rank 不再合并整机文件）、
按 rank 检查数据完整性、可用时从插桩日志推导 TTFT，最终渲染自包含的
单文件 HTML 报告（两级页签：机器页签 → 机器级数据 + rank 子页签）。

所有脚本位于本文件旁的 `scripts/`，用 `python3` 运行。
路径约定：下文命令里的 `scripts/`、`references/` 前缀相对**本 skill 目录**
（本文件所在目录，调用时拼上 skill 目录路径），`profiling_runs/` 等数据/
产物路径相对**项目根目录**（CWD 保持项目根）。运行数据统一放在
`./profiling_runs/<run_id>/`，数据产物 `trace_view.rank<N>.json`（单 rank
为 `trace_view.json`）、`completeness_report.rank<N>.json`（单 rank 为
`completeness_report.json`）、`metrics.json`、`report.html` 落在该 run 目录
（用户要求时也可放项目根）。

## 交互语言

运行本 skill 时，agent 与用户的交流（提问、确认、状态汇报、失败项与
`fix_hint` 的展示、最终总结）一律使用**中文**；脚本输出、JSON 字段与
报告生成物保持原样，不改语言。

## 容器自动探测

所有需要 `--container` 的脚本（`env_check` / `analyse_trace` /
`fix_pythonpath` / `fix_vllm_ascend` / `parse_metrics`）在省略该参数时会
**自动探测**：列出运行中的容器，逐个执行只读 `ps -ef | grep vllm` 探查
vllm 进程（**按进程判定，不依据容器名/镜像名**，防 shell 噪声、限时）。
唯一命中直接使用；多个容器都有 vllm 进程、或都没有时，脚本列出候选并
exit 2——此时再问用户选哪个容器。

## Stage 1 — 检查环境可用性

对**运行 vLLM 服务的 docker 容器**执行（服务必须已启动；本阶段检查的是
活进程）。

```bash
python3 scripts/env_check.py --container <container_name_or_id>
# 可选：同时探测 HTTP API
python3 scripts/env_check.py --container <name> --api-base http://127.0.0.1:7100
```

检查项按顺序执行（JSON `results`，任一 `fail` 则 exit 1）：

1. **docker_daemon** — `docker version` 可达
2. **container_running** — `docker inspect` 显示 `running true`
3. **vllm_process** — 容器内 `ps -ef | grep vllm` 找到 PID
4. **PYTHONMALLOC** — `cat /proc/<vllm-pid>/environ` 含 `PYTHONMALLOC=malloc`
5. **vllm_ttft_fork** — vllm 进程通过 `PYTHONPATH` 加载带 TTFT 插桩的 fork
   `rosinL/vllm@5d1d24c`（静态特征码 + pip 版本检查；特征码与版本定义在
   `references/vllm_fork.json`）。仅静态检查——**绝不**在容器内 `import vllm`。
6. **vllm_ascend** — 已安装的 vllm-ascend 是打过补丁的
   `vllm-project/vllm-ascend@7d73d9d63` 源码构建（pip 版本 `+g7d73d9d63` +
   已安装文件的特征码；详情见 `references/vllm_ascend.json`）。
7. **vllm_config** — 从 `ps -ef | grep vllm` 命令行及引用的配置文件
   （`--config /path`、`*.json`）要求 `"torch_profiler_with_stack": true`。
    `max_iterations` / `warmup_iterations` 仅展示不门禁；必要时用
    `--expect-config-json` 覆盖必检字段。

门禁：1–7 全部 `pass` 才能进 Stage 2。出现 `fail` 时：

1. 向用户展示失败项及其 `fix_hint`。
2. 其余修复均为**用户操作**（改配置后重启等）。skill 绝不改动容器或
   进程——例外是下面三个征询式修复。
3. 用户重启后重跑 Stage 1。

### PYTHONMALLOC 失败时的修复流程（需用户确认）

环境变量只在进程启动时生效，修复必须配合重启。

1. `PYTHONMALLOC` 失败 → 向用户展示 `fix_hint` 并询问是否执行修复。
2. 用户确认后运行（脚本幂等、不交互、不重启 vLLM；容器内写
   `/etc/profile.d/vllm_pythonmalloc.sh` 并复验内容）：

   ```bash
   python3 scripts/fix_pythonmalloc.py --container <name>
   ```

3. 提醒用户**重新进入容器**再重启 vLLM：`/etc/profile.d/` 只对 login
   shell 生效，重启需通过 login shell（如 `docker exec <name> bash -lc "..."`）。
4. 用户重启后重跑 Stage 1 验证。

### vllm_ttft_fork 失败时的修复流程（需用户确认）

`PYTHONPATH` 只在进程启动时生效，修复必须配合重启。

1. `vllm_ttft_fork` 失败 → 向用户展示 `fix_hint` 并询问是否执行修复。
2. 用户确认后运行（脚本不交互、不重启 vLLM；直接在容器内走 curl 下载
   `vllm_fork.json` 的 `tarball_url` 并解压，无离线兜底；幂等，已有
   合法 checkout 则复用）：

   ```bash
   python3 scripts/fix_pythonpath.py --container <name> [--fork-path /dir]
   ```

3. 提示用户重启 vLLM：`/etc/profile.d/` 只对 login shell 生效，重启需通过
   login shell（如 `docker exec <name> bash -lc "..."`）。
4. 用户重启后重跑 Stage 1 验证。

### vllm_ascend 失败时的修复流程（需用户确认）

分两种失败：

- **版本不对**（pip 版本不含 `+g7d73d9d63`）：**不做自动修复**，提示用户
  重装正确基线（vllm-project/vllm-ascend@7d73d9d63 源码安装）后重跑。
- **版本对但补丁缺失**：征询用户，确认后运行（脚本幂等、不交互、不重启；
  容器内先备份 `<file>.bak-<时间戳>` 再把 `references/vllm_ascend_patch/`
  里的完整修改文件覆盖过去并复验特征码）：

  ```bash
  python3 scripts/fix_vllm_ascend.py --container <name>
  ```

之后提示用户重启 vLLM（运行中进程仍用旧代码），重跑 Stage 1 验证。

可选的遗留宿主机检查：`env_check.py --local [--fix]`（profiler 目录、jinja2）。
`--fix` 仅对这些本地检查生效，且仍需用户确认。

注意：`VLLM_TORCH_PROFILER_DIR` 必须在**启动 vLLM 之前**设置，否则
profiler API 不会记录输出。容器需带常规启动参数（如 `max_iterations`），
这一点在上面的配置检查中确认。

## Stage 2 — 采集 profiling（3 秒窗口）

本 skill 不启动 vLLM。服务（docker 容器）由用户启动，Stage 1 负责校验。
随后：

### 2a. 触发采集（3 秒窗口）

```bash
python3 scripts/trigger_profile.py run --api-base http://127.0.0.1:7100 --window 3
```

- `--api-base` 默认 `http://127.0.0.1:7100`（约定端口）。连不上
  （connection refused / timeout）时**先问用户正确的端口或地址**，用
  `--api-base` 传入后重试，不要瞎猜端口。
- `endpoint_start_profile` 失败则停下告知用户（profiling API 缺失，或服务
  启动时未设置 `VLLM_TORCH_PROFILER_DIR`）。
- `trigger_profile.py run` 执行 `POST /start_profile` → 睡 3 秒 →
  `POST /stop_profile`。
- 采集在脚本运行时立即开始，无需等负载达到 `max_iterations`。

### 2b. analyse torch_npu 导出目录（容器内）

采集落盘为 `VLLM_TORCH_PROFILER_DIR` 下的
`dp*_pp*_tp*_*_ascend_pt` 目录（每个 rank 一个）。先在容器内对每个目录跑
torch_npu 离线解析，生成目录内 `ASCEND_PROFILER_OUTPUT/trace_view.json`，
这样归档时一并带回：

```bash
python3 scripts/analyse_trace.py --container <name> --profiler-dir <容器内目录>
```

- `--profiler-dir` **先问用户**（中文交互）；脚本从 vllm 进程环境读到的
  `VLLM_TORCH_PROFILER_DIR` 只作为候选提示打印，不自动采用，也不遍历文件系统
- 已含 trace_view.json 的目录自动跳过（幂等）；analyse 失败自动用 login
  shell 重试一次（部分镜像 CANN 环境只在 profile 里）
- 在 stop_profile 之后、归档之前运行

### 2c. 归档原始数据

```bash
python3 scripts/collect_data.py \
  --src "$VLLM_TORCH_PROFILER_DIR" \
  --dest profiling_runs \
  --window 3 \
  --api-base http://127.0.0.1:7100 \
  --extra-meta-json '{"max_iterations": "<ask user>", "model": "<ask user>"}'
```

多机布局下：`--dest profiling_runs/<时间戳> --run-id <本机IP>`。
原始文件（含已 analyse 的 `trace_view.json`）落入
`profiling_runs/<run_id>/raw/`，并写 `manifest.json`。

## Stage 3 — 解析与生成报告

**进入 Stage 3 前先判断场景（必做，二选一）**：

- **多机 bundle 流程**（多台机器各自采集、最后汇总出报告）：采集机**只执行
  3c**（产出 `ttft_logs.txt`），随后按 **3e** 打包回传。**3a/3b/3d 不在采集机
  执行**——汇总侧由 `build_report.py` 一条命令完成（内部自动执行
  3a/3b/metrics/报告生成）。回传物只有 `*_ascend_pt.tar.gz` + `ttft_logs.txt`，
  **没有也不需要 report.html**。
- **单机流程**（本机采集、本机出报告）：按 3a → 3b → 3c → 3d 顺序执行。

以下 3a/3b/3d 仅单机流程手动执行；bundle 流程中它们是 `build_report.py` 的
内部步骤。

### 3a. 解析为 trace_view.json

正常流程下 2b 的 analyse 已在归档前生成 `trace_view.json`，raw 目录内即含
`*_ascend_pt/ASCEND_PROFILER_OUTPUT/trace_view.json`，直接解析即可——
parse_trace **自动发现所有 `*_ascend_pt` 目录内的 `trace_view.json`，
并忽略噪声 json**：

```bash
python3 scripts/parse_trace.py \
  --raw-dir profiling_runs/<run_id>/raw \
  --out profiling_runs/<run_id>/trace_view.json
```

**多 rank 时不做合并**：只产出**每 rank 一份** `trace_view.rank<N>.json`
（写在 `--out` 同目录；`--out` 本身不写，残留的旧合并文件会被清掉）——
各 rank 时间线独立（跨 rank 时钟未对齐，合并无意义），报告按 rank 分
页签展示。**单 rank**（或散装 chrome trace json 无 rank 标识）仍写合并
输出到 `--out`（`trace_view.json`），报告为单页签。

若拿到的是未经 analyse 的数据（离线/手工场景），先在容器内补跑
（或用 2b 的 `analyse_trace.py`）：

```bash
docker exec <容器> python3 -c "from torch_npu.profiler.profiler import analyse; analyse(profiler_path='<...>_ascend_pt')"
```

同时聚合各 rank 的 msprof 汇总文件（`op_statistic` / `api_statistic` /
`step_trace_time` / `communication{,_matrix}.json` / profiler 元数据，多 rank
op/api/matrix 求和、step 求均值）产出 **`msprof_summary.json` sidecar**（与
`--out` 同目录）。报告的 Top-N 算子优先取 op_statistic（真实算子名，标题标注
来源），Host API 统计 / 逐步 Overlap / 通信分解章节均来自该 sidecar；无
sidecar 时相关章节自动省略。

也可以显式指定单个输入文件（跳过发现）：

```bash
python3 scripts/parse_trace.py --raw-dir <dir> --input-file /path/to/existing.json --out trace_view.json
```

输入为 torch_npu chrome-trace JSON，输出为规范化的 per-rank
`trace_view.rank<N>.json`（或单源 `trace_view.json`）数组——契约与进程
映射见 `references/trace_view.schema.json`。
若无 `*_ascend_pt` 目录（散装 chrome trace json），退回为发现目录下全部 json。

### 3b. 完整性分析（按 rank）

**多 rank 时对每个 rank 文件各跑一次**，产出
`completeness_report.rank<N>.json`（与 rank 文件同目录）；单 rank 仍为
`completeness_report.json`：

```bash
python3 scripts/check_completeness.py \
  --trace-view profiling_runs/<run_id>/trace_view.rank<N>.json \
  --out profiling_runs/<run_id>/completeness_report.rank<N>.json
# 单 rank：
python3 scripts/check_completeness.py \
  --trace-view profiling_runs/<run_id>/trace_view.json \
  --out profiling_runs/<run_id>/completeness_report.json
```

- 任一 rank `overall: "fail"` → **不得**生成宣称成功的最终报告。向用户
  汇总各 rank 失败项，并建议重新采集（回 Stage 2）。
- `overall: "warn"` → 继续，但在最终总结中明示警告。
- 报告的每个 rank 页签各自展示自己的完整性结果（窗口/事件数按 rank
  判定更准）。

### 3c. TTFT 分解（插桩日志）与外部工具（可选）

```bash
python3 scripts/parse_metrics.py \
  --container <name> \
  --log-file <容器内 vLLM 服务日志路径> \
  --out profiling_runs/<run_id>/metrics.json
```

内置解析器从容器内日志文件（**全量**，服务启动以来）抓取插桩行并聚合：

- `--log-file` 路径**先问用户**（vLLM 日志重定向到了容器内哪个文件）。
- 匹配 fork 的 `[TTFT_DEBUG]` 行与 vllm-ascend 补丁的 `[TTFT]` /
  `KV pool worker` 行（模式串内置在脚本里），容器内 `grep` 后只拉回匹配行。
  全部插桩行格式、字段含义与聚合映射见 `references/ttft_metrics.md`
  （`[Worker]`/`[Stats]` 仅日志留存，不聚合）。
- 输出 `metrics.json`：`ttft_ms` = 每请求 8 段链路合计的 **p50**（不含排队/
  分词/输出段，定义见 `ttft_definition`）；`ttft_breakdown` 含各段分位数统计、
  KV pool 细分、每请求链路分布与 token 命中统计。注意：各段计数基准不同
  （batch/step/请求级），占比不能相加还原单请求 TTFT。
- 日志无匹配行 → `available: false`（提示插桩版 vllm/vllm-ascend 未运行或路径错）。
- 汇总机可用 `--local-log <路径>` 直接解析回传的 `ttft_logs.txt`（无需容器）。

如需接入外部指标工具：`--tool-cmd "<模板 {input} {output}>"`
（与内置源互斥，内置优先）。未提供任何源时写入 stub，报告显示「数据源未接入」。

### 3d. 生成 HTML 报告（仅单机流程）

> **硬约束**：本节命令**只在单机流程**（本机采集、本机出报告）使用。
> 多机 bundle 流程下报告一律由**汇总机**的 `build_report.py` 生成（见 3e），
> **采集机绝不运行本节命令**。

单机与多机统一用 `--runs`（一个或多个 run 目录）：

```bash
python3 scripts/generate_report.py \
  --runs profiling_runs/<batch>/<machine-A> profiling_runs/<batch>/<machine-B> \
  --labels node-01 node-02 \
  --out profiling_runs/<batch>/report.html \
  --title "vllm-ascend Profiling Report <batch>"
```

- run 目录含 `trace_view.rank<N>.json`（多 rank）或 `trace_view.json`
  （单 rank）即可；`metrics.json` / `completeness_report{,.rank<N>}.json` /
  `msprof_summary.json` 存在时从同目录自动拾取。
- `--labels` 可选（默认取目录名）。
- **报告形态（两级页签）**：多机时顶层为**机器页签**；进入机器页签后，
  上方为该机**机器级数据**（环境元数据 / TTFT 分解 / msprof 表格：逐步
  Overlap、Host API、通信分解与矩阵），下方为该机的 **rank 子页签**
  （`rank0`…，切换各 rank 的图表与完整性）。单机多 rank → 无机器页签栏，
  直接机器级数据 + rank 子页签；单 rank → 单页签含全部内容（旧行为）。
  旧格式数据（合并 `trace_view.json` + rank 文件并存）→ 机器视图常显 +
  rank 子页签。

单机旧参数形式（仍支持，用于旧数据）：

```bash
python3 scripts/generate_report.py \
  --trace-view profiling_runs/<run_id>/trace_view.json \
  --metrics profiling_runs/<run_id>/metrics.json \
  --completeness profiling_runs/<run_id>/completeness_report.json \
  --out profiling_runs/<run_id>/report.html \
  --title "vllm-ascend Profiling Report <run_id>"
```

### 3e. 多机采集与回传（bundle 流程）

多台宿主机各自采集；**回传每 rank 的整个 `*_ascend_pt` 导出目录（压缩）
与插桩日志**，合并产物留在采集机，汇总侧解包重建每机报告。**bundle 直接
回传到汇总机项目根的 `profiling_runs/<时间戳>/<IP>/`**——该目录即此机
run 目录的最终位置，重建产物与 rank 压缩包原地共存（append-only）：

```
profiling_runs/
└── <时间戳>/
    └── <IP>/                             # 汇总机上该机的 run 目录
        ├── ttft_logs.txt                 # 插桩日志（3c 留存）
        ├── dp*_pp*_tp*_*_ascend_pt.tar.gz    # 每 rank 压缩包，整个 *_ascend_pt 导出目录
        │                                 # （trace_view.json + msprof CSV/JSON + 原始数据）
        ├── trace_view.rank<N>.json       # ↓ 以下由 build_report.py 原地重建
        ├── completeness_report.rank<N>.json  #   （每 rank 一份；单 rank 时为
        ├── msprof_summary.json           #    trace_view.json / completeness_report.json）
        ├── metrics.json
        └── report.html
```

前置：采集机需要本 skill（`scripts/` + `references/`）才能执行 Stage 1–3c 与
打包。跨机拷贝约定（不写 home / 系统路径）：skill 目录同步至**采集机**用
`/tmp/vllm-ascend-profiling/`（采集机无项目根，执行时 CWD 即该副本目录，
其副本与本地产出均为临时物，丢失重新同步/重采即可）；bundle 回传至
**汇总机**直接落项目根 `profiling_runs/<时间戳>/<IP>/`（汇总机有项目根，
一步到位，不经 /tmp 中转）。

流程：

1. **每台机器各自执行**（互不依赖、可并行，agent 用中文交互）：Stage 1 → 2a 触发
   → 2b analyse → 2c 归档（`--dest profiling_runs/<时间戳> --run-id <本机IP>`）→
   3c parse_metrics（产出 `ttft_logs.txt`；本地 metrics.json 仅参考，不回传）。
   **采集机到此为止**：禁止在采集机执行 3a/3b/3d（解析、完整性检查、报告
   生成）——这些一律由汇总机 `build_report.py` 完成。
2. **每台机器打包回传物**：

   ```bash
   python3 scripts/pack_ranks.py --run-dir profiling_runs/<时间戳>/<IP> --out <回传根目录>
   ```

   产出 `<回传根目录>/<时间戳>/<IP>/`（rank 压缩包 + ttft_logs.txt）。缺
   ttft_logs.txt 时输出 warning（bundle 无 TTFT 数据，报告显示「数据源未接入」）。
3. 把 `<回传根目录>/<时间戳>/` **scp/rsync 到汇总机项目根
   `profiling_runs/<时间戳>/`**（保持目录结构，不经 /tmp 中转）。
4. **汇总机对每个 IP 目录重建报告**：

   ```bash
   python3 scripts/build_report.py --bundle-dir profiling_runs/<时间戳>/<IP>
   ```

   解包 → parse_trace（每 rank 一份 `trace_view.rank<N>.json` + msprof
   sidecar，**多 rank 不产合并文件**；单 rank 仍合并）→ completeness
   （按 rank 各查一次）→ metrics（本地解析 ttft_logs.txt）→ 生成报告
   （机器页签 → 机器级数据 + rank 子页签）。**产物原地写回同一 run 目录**
   （trace_view.rank<N>.json、completeness_report.rank<N>.json、metrics.json、
   report.html 与压缩包并存）；若 bundle 被放在别处（如手工传到 /tmp），
   产物仍落 CWD 下 `profiling_runs/<时间戳>/<IP>/`；解包 raw 副本走临时
   目录，结束后清理。输出 JSON 汇总含 `metrics_available` /
   `completeness_overall`（多 rank 为每 rank 一项）——任一 fail 按 3b
   门禁处理，不得宣称成功。

采集机本地工作目录仍按 2c/3c 产出（raw/、metrics.json、manifest.json 等），
仅不回传。（可选）确需多机页签合并报告时，**在汇总机**用 3d 的 `--runs`
指向第 4 步重建的各机 run 目录（`profiling_runs/<时间戳>/<IP>`）生成，
默认不做。

报告结构（单文件自包含，图表由内联 ECharts 渲染——canvas 绘制、页签
惰性初始化、零外部网络依赖）：

**机器页签**（多机时；单机省略）内、rank 子页签上方——**机器级数据**：

1. 环境元数据（torch_npu/CANN 版本、采集配置、并行组）+ 概览
   （rank 数、事件合计、跨度）
2. TTFT 分解 — 插桩日志 TTFT 分解（分段条形图 + 链路/KV pool/token
   统计表），或「数据源未接入」占位
3. 逐步 Overlap 表（step_trace_time，多 rank 取均值，截断 200 行）
4. Host API 统计 — api_statistic Top 20
5. 通信耗时分解 + rank-pair 矩阵

**rank 子页签**（`rankN`，隶属所属机器页签）：

1. 汇总统计 — 时间跨度、事件数、进程清单、TTFT KPI
2. Top-N 算子 — 横条图 + task-type 饼图（优先 op_statistic.csv，trace 聚合
   兜底，标题标注来源）
3. NPU 利用率 — Overlap 柱图 + 时间线（ts 归一化、固定轨道顺序）
4. 内存活动代理 — Memcpy / empty_tensor / H2D-D2H 折线（trace 无 HBM
   计数器）
5. 通信 — 字节数/带宽 + 按 task type
6. 数据完整性 — 本 rank 的 completeness 清单

单 rank 机器为单页签，包含以上全部内容（旧行为）。

## 失败与边界规则

- **多机 bundle 流程：解析与报告只在汇总机进行**。采集机不运行
  parse_trace / check_completeness / generate_report，只做 采集→analyse→归档→
  3c→打包。
- 绝不编造 TTFT 数值。指标不可用时留空。
- 内存章节明确是**活动量代理**（memcpy / empty_tensor），不是 HBM 占用。
- `trigger_profile.py` 启动失败时不调用 stop，中止该 run 并上报。
- 用户只想从已有 `trace_view.json` 出报告时，直接跳到 Stage 3。
- `profiling_runs/` 下只增不改，不覆盖历史 run。

## 子代理输出契约

被主流程按候选子技能派发时（task package 的 `candidate_skill_list` 含
`vllm-ascend-profiling`），最终回复必须是单个 JSON 对象，覆盖主 skill 的
`../../references/subagent-orchestration.md` 的 14 个顶层字段，并回填
`../../scripts/merge_subagent_results.py` 额外要求的 `subagent_id` /
`result_path`（下表合计 16 项）。纯人工交互式调用不必输出该契约。

| 字段 | 类型 | 本 skill 的取值 |
|------|------|----------------|
| `subskill_name` | string | 固定 `vllm-ascend-profiling` |
| `current_run_id` | string | 必须与任务包一致 |
| `current_evidence_status` | string | 必须为 `current`；否则 `status=blocked` 且不产出候选动作 |
| `status` | string | `ok` / `degraded` / `blocked` / `failed`。Stage 1 的 7 项检查未全部 `pass`、或服务不可达时为 `blocked`；TTFT 数据缺失等非阻断降级为 `degraded` |
| `confidence` | string | `high` / `medium` / `low` |
| `analysis_timestamp` | string | ISO 8601 时间戳 |
| `evidence_sources` | string[] | 现场证据文件路径（`trace_view.rank<N>.json`、`completeness_report*.json`、`metrics.json`、`msprof_summary.json`、`report.html`、Stage 1 输出），必须真实存在于磁盘，不得填写预期路径 |
| `findings` | object | profiling 结论，结构见下 |
| `candidate_actions` | array | 环境修复与采集配置候选动作；无实施动作时也必须给出一条 `change_mode=no_change` 的说明性动作，不得为空数组 |
| `required_evidence` | string[] | 缺失证据；无缺失为空数组 |
| `fallback_notes` | string[] | 降级说明；无降级为空数组 |
| `timing` | object | 耗时统计，至少含 `analysis_seconds` |
| `independent_analysis_confirmation` | string | 固定声明：本分析已独立执行 subskill SKILL.md 完整流程，任务包背景信息未替代现场证据采集和独立判断 |
| `steps_covered` | array | 覆盖的步骤类别：`env_check`、`trigger_profile`、`analyse_trace`、`collect_data`、`parse_trace`、`check_completeness`、`parse_metrics`、`report`（`generate_report` 或 `build_report`） |
| `subagent_id` | string | 回填任务包给定值 |
| `result_path` | string | 回填本结果文件路径 |

`findings` 固定子结构（无数据的子项写 `available: false`，不编造数值）：

- `environment` — Stage 1 `env_check.py` 的 `results`（7 项检查的 `name` /
  `status` / `fix_hint`）与容器、vllm-ascend 版本、profiler 目录等元数据
- `profiling` — 采集窗口、rank 数、`*_ascend_pt` 与 trace 产物路径
- `ttft` — `metrics.json` 的 `ttft_ms` / `ttft_breakdown` 及其口径
- `completeness` — 每 rank 的 `overall` 与失败项
- `top_kernels` / `bottleneck_attribution` — Top-N 算子与瓶颈归因（附证据路径）

### candidate_actions 字段

每个元素必须包含 `action_id`、`title`、`category`、`priority`
（`high`/`medium`/`low`）、`change_mode`（`no_change` / `config_tune` /
`runtime_param` / `restart_required` / `model_reload` /
`quantization_change` / `parallelism_reconfigure`）、`requires_root`（布尔）、
`risk`（`low`/`medium`/`high`）、`implementation_plan`、`validation_plan`、
`rollback`、`expected_effect`、`expected_gain_metric`、
`rejection_criteria`、`evidence_refs`。动作只描述**征询式**变更
（`fix_pythonmalloc.py` / `fix_pythonpath.py` / `fix_vllm_ascend.py`、profiler
目录与采集窗口配置、重启验证），`change_mode=restart_required` 时 `rollback`
必须写明恢复原配置与重启验证步骤；采集过程中不得直接改动容器。

## Reference

- `references/ttft_metrics.md` — TTFT 插桩指标清单：全部插桩行格式、字段含义、
  metrics.json 聚合映射与口径（`[Worker]`/`[Stats]` 仅日志不聚合）

- `references/trace_view.schema.json` — 事件 schema 与进程/报告映射
- `references/completeness_rules.json` — 完整性阈值（窗口目标 3000ms ±1500ms）；
  可选 `--rules` 覆盖，与脚本内置默认一致
- `references/vllm_fork.json` — rosinL/vllm@5d1d24c fork 元数据（commit URL、
  下源 tarball_url、容器内安装路径、profile.d 文件、源码特征码），供
  `env_check.py`（`vllm_ttft_fork` 检查）与 `fix_pythonpath.py` 使用；
  安装走容器内直接下载，无离线兜底
- `references/vllm_ascend.json` — vllm-ascend 基线版本（vllm-project/
  vllm-ascend@7d73d9d63，pip `+g7d73d9d63`）、目标文件与补丁特征码，供
  `env_check.py`（`vllm_ascend` 检查）与 `fix_vllm_ascend.py` 使用
- `references/vllm_ascend_patch/` — 两个完整补丁文件（`kv_transfer.py`、
  `pool_worker.py`），基于 7d73d9d63 原版替换 TTFT 计时函数
  （`KVCacheStoreSendingThread._handle_request`、`KVPoolWorker.start_load_kv`、
  `KVPoolWorker.wait_for_save`）；由 `fix_vllm_ascend.py` 原样覆盖进容器
