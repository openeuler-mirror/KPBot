---
name: kptools-tpsplit-optimization
description: >-
  TP 拆分、TP size 变更、张量并行度调整、单机 TP 闭环、去 Ray 化、显式 DP 组网（rpc-port 16600）、单机双实例 9000/9001 分流：连接远端把跨机 TP32+Ray Prefill 迁移为新 TP size（TP8/TP4/TP16）+ Mooncake tp/dp 对齐，按 tpsplit 可行性报告或用户给定值部署，含配置调用链梳理与拉起验证（L0-L3）。本 skill 是部署执行面（方案/停旧/生成分发/拉起/切流/回滚/迁移后配置错配排查）；部署完成后的交付验证（L4-L6：功能/性能对比/稳定性观察）转 kptools-tpsplit-optimization-verify（配套 PR #441 交付）——分层验证、性能对比、稳定性观察、迁移后测试清单类请求不要发给本 skill。
license: Proprietary
compatibility: 远端 Linux（openEuler 等，aarch64）；SSH 访问所有涉及主机；NPU 环境（npu-smi、ASCEND_RT_VISIBLE_DEVICES）；vLLM（--data-parallel-size 外部 DP 拉起）+ Mooncake/AscendStore KV 传输；优先 ssh-mcp-server upload/execute-command/download，无 MCP 回退本地 ssh/scp
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: tp_topology_migration
  version: "0.6.0"
---

# kptools-tpsplit-optimization：新 TP size 集群部署（TP 拓扑变更）

把推理集群从旧 TP 拓扑（典型：跨机 TP32 + Ray 托管）迁移到新 TP 拓扑（典型：单机 TP8 闭环 + 显式 DP 组网 + Mooncake 对齐）。新 TP size 来自用户输入，或来自 TP 可行性分析报告（由仓内 `Plugins/app-tuner/skills/kptools-tpsplit-check/` 的 `analyze_feasibility.py` 产出，样例报告见其 `examples/glm52_report.*`，以报告文件路径交接）。产出：已拉起并通过 L0-L3 拉起验证的新集群 + 已切流的网关 + 一份部署指南 `.md`。**部署完成后的交付验证（L4-L6：功能/性能对比/稳定性）由 `kptools-tpsplit-optimization-verify`（配套 PR #441 交付） 承接**（独立 PR 交付），交接物即部署指南；本 skill 到切流为止。

核心原则（原理详见 `references/tp-topology-principles.md`，动手前先读）：

- **Mooncake 的 tp/dp 必须与 Prefill 实际拓扑严格一致**：错配不是启动报错，而是 KV 路由错乱、丢 KV、偶发 NPU 报错/hangs——现场极难排查，每个阶段都做一致性自检
- **两层 DP 不要混淆**：vLLM `--data-parallel-size`（引擎组内 DP）≠ Mooncake `dp_size`（全局 Prefill DP 副本总数，跨所有主机累加），写错任何一个都会 KV 寻址异常
- **拓扑变更是全量切换，不是增量修改**：新旧拓扑混跑 = KV 错配事故
- **先出方案、用户确认、再动远端**：方案未经用户确认前，不在远端做任何变更性操作

## 快速决策

1. 用户直接给了新 TP size（数字）→ 阶段 1 收集其余拓扑信息
2. 用户给了可行性分析报告或说"按上次分析的结论部署" → 阶段 1 优先解析 `.json` 提取结论，复述确认后补齐缺的信息
3. 用户只要脚本和配置（远端自己拉）→ 该分支**止步于本地交付，不做任何远端变更**：按用户确认后的布局表生成全部部署脚本/配置（阶段 2 输出 + 阶段 4 生成规则），完成**本地**静态检查（`bash -n`、KV `json.loads`、实例 diff、dp 守恒，可用 `references/l0_static_check.py` 本地跑），不停旧集群、不向远端分发；停服、分发、拉起与拉起验证（L0-L3）写成**可照做的步骤**放进交付指南，由用户自行执行；L4-L6 交付验证在指南中标注由 `kptools-tpsplit-optimization-verify`（配套 PR #441 交付） 执行
4. 部署后 KV 报错 / NPU 偶发错误 / hangs → 优先按阶段 5 的 L3 判据排查一致性（大概率 Mooncake tp/dp 错配或混跑），再考虑回滚；纯计划性验证/观察（无故障征兆、只要分层验证或稳定性观察）转 `kptools-tpsplit-optimization-verify`（配套 PR #441 交付）
5. 部署与 L0-L3 完成、切流已做，用户要功能/性能/稳定性验证 → 转 `kptools-tpsplit-optimization-verify`（配套 PR #441 交付），交接物为部署指南（布局表 + 修改点清单 + L0-L3 结果 + 备份位置 + 停服前归档的旧基线数据）

## SSH/容器操作约定（工具链实测，贯穿全程）

- ssh-mcp-server 的 `download` **不会自动创建本地父目录**——下载前先本地建目录
- **复杂校验/长命令不拼内联**：多层转义必挂，本地写好脚本 `upload` 到远端（容器可见 bind mount 路径）再执行
- 容器内产物写 bind mount 目录（/tmp 不与宿主机共享，需归档先 cp 出来）

## 阶段 1：输入解析与信息收集（缺什么问什么，不要猜）

### 1.1 确定 TP size 来源

- **用户直接输入**：确认目标 TP size。追问只改 P 侧还是 D 侧也动——默认 **P 侧 Prefill TP 变更**（D 侧通常 tp=1 不动，但 KV_TRANSFER_CONFIG prefill 段必须同步改）
- **用户没给 TP size**：按卡数推导默认值再求证一轮（16 卡机 → TP8×2；8 卡机 → TP8×1；4 卡机 → TP4×1），不要空手反问"你要多大 TP"
- **TP 可行性分析报告**（来源：仓内 `Plugins/app-tuner/skills/kptools-tpsplit-check/` 的 `analyze_feasibility.py`，样例见其 `examples/glm52_report.*`）：同目录 `.json`/`.md` 同在时**优先解析 `.json`**，`.md` 用于向用户复述。关键字段：`recommended.tp` / `current.*` / `candidates[]`（含 `dp_per_group`、`cluster_dp`、`verdict`）。**两层 DP 映射固定**：`dp_per_group`（组内DP）→ vLLM `--data-parallel-size`（每组大小）；`cluster_dp`（集群DP）是**容量口径**（集群总卡数 ÷ (TP×PP)，即集群可容纳的组内副本总数）——写 Mooncake `dp_size` 前必须与 2.1 布局表实际实例总数交叉核对，不一致即向用户澄清，不得把容量口径值直接当全局实例总数使用。⚠️ 报告"建议启动参数 diff"里的 `--data-parallel-size` **分两种情形**：EP 启用时上游输出的是**组内 DP**（注释为「组内 DP = ep_group ÷ TP（单组启动命令）」并附集群布局行），**可直接采用**、与 `dp_per_group` 一致；仅**非 EP 的"单 serve 拉起"形态** diff 才是集群级值（注释为「卡数 / (TP×PP)」），显式 DP 组网时取 `dp_per_group`、不得照抄。字段结构不符时按字段名尽力解析，**缺失或解析不出即向用户确认，绝不按缺省值硬写 dp/dp_size**。**提取后必须复述确认**（"报告推荐 TP8、组内 DP 4、集群 DP 20，按这个部署吗？"），报告没写的（主机清单、路径、Proxy 类型）继续问用户；结论与用户口头说法冲突以用户为准并提醒差异

### 1.2 必须收集的拓扑信息

| 信息 | 说明 | 缺失处理 |
| --- | --- | --- |
| P 侧主机清单 | 每台 IP、SSH 认证方式、NPU 卡数 | 必须问 |
| 每台实例数 | TP8 × 16 卡机 = 2 实例 | 按卡数 ÷ TP size 推导，确认 |
| DP 组大小 | 引擎组内 DP（一组几个 TP8 实例） | 默认 4，确认 |
| Mooncake 全局 dp_size | 所有主机 P 实例总数 | 布局推导自检；报告给定双向核对 |
| D 侧现状与位置 | dp_size/tp_size、启动脚本所在主机与路径 | 必须问 |
| 配置调用链 | 参数真实来源：脚本内 / source env / systemd / 容器 env / 父级编排 | 按 1.3 梳理，改文件前必做；P/D 共享拓扑配置重点确认 |
| 旧方案形态 | 执行后端、现有 TP size、KV connector | 读旧启动脚本识别，与用户核对；**不要假设一定是 TP32+Ray** |
| 旧集群状态 | 是否在跑、是否要停 | **必须问，绝不静默停** |
| P 侧脚本与配置路径 | vLLM 启动脚本、KV_TRANSFER_CONFIG 位置 | 必须问 |
| Proxy 网关 | 类型（MindIE Motor / Nginx / 自研等）与配置路径 | 必须问；类型决定改法与 reload 方式 |
| 模型与业务参数 | 模型路径、served-model-name 等 | 旧脚本继承，但见 2.3 复核 |

信息收集齐之前不做任何远端**变更性**操作；只读排查允许——答不上来的项，SSH 上去 `ps -ef | grep vllm` 找到位置后再与用户核对。

### 1.3 配置调用链梳理（改任何文件前必做）

用户说"改这些参数"只说明了**改什么**，没说明参数**在哪定义**。线上脚本参数常从 `source` 的 env 文件、systemd `EnvironmentFile=`、容器 env 或父级编排传入；**P/D 共享同一份拓扑配置文件的情况重点确认**。直接改下游脚本的后果：上游一刷新就覆盖修改，或 D 侧仍读旧值——KV 错配当场发生。SSH 只读追：

```
grep -nE '^\s*(source|\.)\s' <启动脚本>            # 1. source 了哪些外部文件
grep -n 'p_dp_size\|p_tp_size\|KV_TRANSFER_CONFIG' <脚本及其 source 的每个文件>   # 2. 参数真正定义处
ps -ef | grep vllm                                 # 3. 反查父进程（谁拉起）
systemctl cat <unit>; docker inspect <容器>         # 4. systemd/容器场景看 env 来源
```

产出《配置修改点清单》（参数 → 定义文件 → 消费方 → 改一处是否覆盖，模板见 `references/deployment-details.md`），用户确认后才进阶段 2。原则：**改源头文件，不改下游脚本**；参数写死在脚本内的自包含形态才直接改脚本。追不清时把已知链路画给用户指认，不要猜。

## 阶段 2：拓扑推演与部署方案（先算清楚，用户确认后才动手）

生成《部署方案表》展示给用户：**实例布局表**（下述）+ **配置修改点清单**（来自 1.3）。**确认前不在远端做任何变更**。

### 2.1 实例布局表（每实例一行）

| 实例 | 主机 | ASCEND_RT_VISIBLE_DEVICES | DP 组 | DP rank | http 端口 |
| --- | --- | --- | --- | --- | --- |
| P-0 | 10.x.x.1 | 0,1,2,3,4,5,6,7 | 组1 | 0 | 9000 |
| P-1 | 10.x.x.1 | 8,...,15 | 组1 | 1 | 9001 |

推导规则：**实例数/台 = 卡数 ÷ TP size**，卡分配互斥——同机两实例同卡 = 资源冲突，本场景最高频事故，布局表逐行核对；**Mooncake 全局 dp_size = Σ 所有主机实例数**（全局实例总数），与用户给定值/报告值对不上先停下来澄清，不要硬凑；DP 组内 rank 0/1/2/…、主节点 IP 与 rpc-port 16600 统一（跨主机组内 16600 互通）；http 端口同机递增 9000/9001/…。**kv_port / lookup_rpc_port / engine_id 不变动**，沿用旧值（以现场旧脚本实际值为准）。

### 2.2 Mooncake 参数推导

KV_TRANSFER_CONFIG 的**改动面只有两个值**：`prefill.dp_size`（= Mooncake 全局 dp_size）和 `prefill.tp_size`（= 新 TP size）。其余字段一律不动：`decode` 段保持 D 侧现状，`engine_id` / `kv_port` / `lookup_rpc_port` 原值保留。D 侧所有实例的 prefill 段同步改为同一组值。生成后守恒自检：所有 P 实例 prefill.dp_size 完全一致且等于全局实例总数。⚠️ **D 侧生效时机与切换顺序绑定**：Decode 实例若在启动时读取共享 env / KV 配置，改文件≠生效——已运行的实例仍持有旧 Prefill 拓扑。切换顺序中必须逐个 D 实例列明其重载/重启时机（改完共享配置后按 PID 逐台重启，勿宽匹配 pkill），每个实例重启后核对其**实际生效**的 `prefill.dp_size` / `prefill.tp_size`（启动日志或进程实际持有的配置）为新值，核对通过才把它接入新 P——未核对的实例不得进入 L3。

### 2.3 业务参数复核（TP 变小的影响，必须让用户拍板）

TP32→TP8 后单请求算力/显存池变小：`--max-model-len`、`--max-num-batched-tokens`、`--max-num-seqs` 从旧脚本继承前逐项向用户确认是否要调，方案表里单独列出 + 建议值，不要静默改。

## 阶段 3：环境预检与旧集群处置（SSH 连上后先检查再动手）

每台 P 侧主机依次执行：

```
npu-smi info                                # 卡数与健康，与布局表核对
ss -ltn 2>/dev/null | grep -E ':(9000|9001|16600|30000|10001)\b'   # 端口占用；30000/10001 以旧脚本实际值为准
ps -ef | grep -E 'vllm|ray' | grep -v grep  # 旧实例与 Ray 残留
```

红线（细则见 `references/deployment-details.md`）：旧集群**必须问过才能停**，按 PID 逐个 kill（**勿用宽匹配 pkill**）；旧方案确实用 Ray 时，先核对每个 Ray 进程归属本集群、按 PID 逐个停——**共享主机禁用 `ray stop`**（作用于本机全部 Ray 进程，会误停其他业务）；旧方案有 Ray 时清理旗标/环境变量残留；跨主机 DP 组 16600 互通；⚠️ **新旧拓扑禁止混跑**（D 侧整体指向同一套 P 拓扑）；卡/端口被其他业务占用 → 并入"拉起前置条件"交用户协调（只停卡不够）；预检异常 → 反馈用户停下，不要自行换端口/换卡继续。**停服前基线采样（默认必做——停服后无法补采；用户明确放弃 L5 性能对比并留痕时除外）**：旧集群停服即无法再测——停旧实例前先完成旧集群基线压测采样，**逐项确认三项指标的实测值、单位和原始结果位置**（① Prefill 吞吐（req/s 或 token/s）② TTFT（P50/P99，ms）③ P-D KV 传输带宽（GB/s，附传输侧计数器/日志来源）——三项缺一不可，停服后无法补采），连同负载/并发/计时口径（数据集、请求长度分布、并发数、压测时长、预热策略、统计窗口）一并记录归档，随部署指南交付给 `kptools-tpsplit-optimization-verify`（配套 PR #441 交付） 的 L5 消费。

## 阶段 4：生成脚本与配置分发

**形态取舍判据（先判后生成）**：现场已有驱动编排（如 `launch_online_dp.py` + 位置参数模板）→ **保留驱动形态，只改模板与传参**；无编排才用 `references/templates/prefill-tp8-instance.sh` 生成每实例独立脚本。两种形态下布局表、守恒自检、L0 的验证口径一致。旧方案基线 `references/templates/legacy-prefill-tp32.sh` 只是 **TP32+Ray 样例**，现场后端/TP size/KV connector 以实际读取为准，不要硬套。占位符表与生成要点全文见 `references/deployment-details.md`，红线：

- 新脚本**不写 `--distributed-executor-backend`**（旧脚本带 ray backend 的删除；本来没有的保持没有）
- 独立脚本形态下实例间差异项只有 `--data-parallel-rank` / `ASCEND_RT_VISIBLE_DEVICES` / `--port` 三处（组级：DP 主节点 IP / 组大小）——生成后 diff，差异项之外的改动都是笔误
- **保持现场现有传参形态**：脚本内定义的脚本内定义；env source 进来的继续 source 同一文件、只改 env 文件值——不要改造调用链形态
- **KV_TRANSFER_CONFIG 只改 prefill 段 dp/tp 两个值**，engine_id / kv_port / lookup_rpc_port / decode 段原样保留，改在 1.3 梳理出的**源头定义处**（共享 env 文件就改 env 文件，脚本自包含才改脚本）；**旧脚本与配置一律备份后再改**（同目录 `*.bak.<日期>`）——回滚靠它
- DP 组网旗标对齐 vLLM 官方口径（`--data-parallel-size/rank/address` + `--data-parallel-rpc-port 16600`；旗标名以现场 `vllm serve --help` 实测为准，兜底见 deployment-details.md）
- 分发：每台主机只放自己的实例脚本；远端自检（`bash -n` + KV `json.loads`）通过才进阶段 5

## 阶段 5：拉起验证（L0-L3，逐层过，不过不进下一层）

拉起期的故障多数不是"起不来"，而是"起来了但 KV 错配"——拉起验证分层铺开，每层通过标准与执行口径详见 `references/layered-testing.md`（L0-L3）。

| 层 | 内容与通过判据（简） |
| --- | --- |
| L0 静态检查（拉起前） | `bash -n`；KV `json.loads`；实例 diff；dp 守恒自检；卡互斥；端口错开；无 Ray 旗标，全 PASS。守恒自检分执行口径（实例总数 = 2.1 布局表全局实例数）：全量目录用 `--expect-dp <全局实例数>`；按主机分目录只核各实例 dp_size 一致、**不与本机实例数比较**。参考实现 `references/l0_static_check.py`（调用口径见 layered-testing.md） |
| L1 单实例启动 | 按 DP 组序拉起（先组内 rank 0）：`/v1/models` 探活通过；TP 与目标一致；`npu-smi` 卡占用与布局表一致 |
| L2 DP 组网 | rank 全部入组、16600 组内互通，日志无掉队/重试报错 |
| L3 KV 链路 | 经 Proxy 或直连发真实 completion 正常出 token，双侧日志无 Mooncake/DP 报错 |

失败处理：L0-L2 失败 = 配置/环境问题，修复后从 L1 重来；L3 失败先怀疑 Mooncake 错配或新旧混跑。结果逐层记录进部署指南。

**L0-L3 全绿即本 skill 的验证终点**：交付验证（L4 功能 / L5 性能对比 / L6 稳定性）由 `kptools-tpsplit-optimization-verify`（配套 PR #441 交付） 执行（输入 = 部署指南 + 停服前归档的旧基线），本 skill 不代做，也不在部署指南里预填 L4-L6 结论。verify 判定 L4 失败时的容量参数复核（max-model-len 等）回到本 skill 阶段 2.3 重议；L5 不达标的调参/回滚决策与执行同样回到本 skill。

## 阶段 6：Proxy 更新与交付

L1-L3 全绿后才切流量：P 后端列表换成新实例地址（`<主机>:9000`、`<主机>:9001` …），按用户网关类型改配置并 reload（改前备份，改后探活）；MindIE Motor 类网关注册表是内存态，coordinator 重启或后端换血后必须重新执行 `register.sh` 注册，否则请求报 No instance available。交付《部署指南 `.md`》（内容见 `references/deployment-details.md`）：拓扑摘要、启动顺序、L0-L3 结果、回滚步骤、停服前归档的旧基线数据（供 verify 的 L5 对比），并注明 L4-L6 交付验证由 `kptools-tpsplit-optimization-verify`（配套 PR #441 交付） 执行。

## 上线检查清单（写进部署指南，逐项打勾）

1. 卡隔离：同机实例 ASCEND_RT_VISIBLE_DEVICES 无交集
2. DP 组通信：16600 组内互通，防火墙/安全组放行
3. Mooncake 同步：所有 P 侧 + D 侧 KV_TRANSFER_CONFIG 同一组值，无新旧混跑
4. 后端旗标：新实例无 `--distributed-executor-backend`；旧方案用 Ray 的已确认无残留
5. 端口：http 与 16600 无冲突；kv_port / lookup_rpc_port / engine_id 保持旧值未动
6. 全局 DP：Mooncake dp_size == 所有主机 P 实例数之和（逐台数）
7. 容量参数：max-model-len / max-num-batched-tokens / max-num-seqs 已复核
8. 测试记录：L0-L3 结果已记录；停服前旧基线已归档；L4-L6 验证窗口/责任人已与 kptools-tpsplit-optimization-verify（配套 PR #441 交付） 交接；改动文件均已备份

## 回滚

任一环节失败且短期修不好：停新实例（按 PID），恢复备份的旧脚本/配置，按旧方案**原样**重启（含不含 Ray 以备份旧脚本为准），D 侧 KV_TRANSFER_CONFIG 恢复旧 prefill 段；**已切过流量（阶段 6）的必须同步回滚网关**——Proxy 后端列表恢复为原实例地址，按网关类型 reload 配置或重新注册旧实例（MindIE Motor 注册表是内存态，须重跑 `register.sh`），否则网关仍把请求发往已停止的新地址；最后**经网关发一条真实请求**，确认路由到旧实例且响应正常，才算回滚完成。**回滚前先确认旧形态全部依赖可达**（跨机 Ray worker、mooncake_master 等，缺一个 = 回滚不完整）。回滚命令在部署指南里写完整，不要临场发挥。

## 边界与配套拆分说明

- **本 skill 只做部署执行面**（方案推演/调用链梳理/停旧/生成分发/拉起 L0-L3/切流/回滚/迁移后配置错配排查）；交付验证（L4-L6）由 `kptools-tpsplit-optimization-verify`（配套 PR #441 交付） 承载（独立 PR 交付）：输入为本 skill 的部署指南 + 停服前归档的旧基线，不达标时由 verify 给出调参/回滚建议、回本 skill 执行。两 PR 建议同批合入，避免过渡期 L4-L6 触发面悬空。
- 本 skill 由原「部署+验证一体」形态（v0.5.x 及之前，L0-L6 全包）拆分而来：L4-L6 细则随拆分迁至 verify 侧，本仓不再维护。
- 相邻边界：TP 可行性测算（能不能切/值不值）归 `kptools-tpsplit-check`（上游，报告交接见 1.1）；TTFT 阶段拆解的收益对比报告（两套 ttft_table.csv 出 .md/.html/.json 三件套）归 `kptools-tpsplit-report`——verify 的 L5 只做现场达标判定，正式收益报告转它。

## 模板与参考

- `references/tp-topology-principles.md` — 拓扑变更原理与风险详解
- `references/deployment-details.md` — 阶段细则（1.3 清单模板、阶段 3 处置、阶段 4 占位符表与生成要点、阶段 6 交付清单）
- `references/layered-testing.md` — L0-L3 拉起验证通过标准与 `l0_static_check.py` 调用口径（L4-L6 交付验证见 kptools-tpsplit-optimization-verify（配套 PR #441 交付））
- `references/l0_static_check.py` — L0 静态检查参考实现（不依赖 NPU）
- `references/templates/prefill-tp8-instance.sh` — 新方案实例启动模板（占位符版）
- `references/templates/legacy-prefill-tp32.sh` — 旧方案基线（TP32+Ray 样例）
