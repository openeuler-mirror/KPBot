---
name: kptools-tpsplit-optimization
description: >
  连接远端机器部署新 TP size 的推理集群，典型场景是把跨机 TP32+Ray 的 Prefill 集群迁移为
  单机 TP 闭环 + 显式 DP 组网（TP8/TP4/TP16 均可），旧集群无论是否 Ray 托管均自动适配
  （读旧启动脚本识别形态，无 Ray 时跳过去 Ray 步骤）。当用户提到以下场景时必须使用此技能：
  TP 拆分、TP size 变更、按新 TP size 部署集群、tpsplit、张量并行度调整、单机 TP 闭环、
  去 Ray 化部署、显式 DP 组网（DP rank 分配 / rpc-port 16600）、Mooncake KV_TRANSFER_CONFIG
  的 tp/dp 对齐、单机双实例 9000/9001 分流、ASCEND_RT_VISIBLE_DEVICES 切分、按可行性分析
  结论部署新拓扑。新 TP size 可能来自用户直接输入，也可能来自上游可行性分析 skill 输出的
  .md 报告，两种入口都要支持。即使用户只说"帮我把集群改成 TP8"或"按上次分析的结论重新
  部署"，也应使用此技能。
license: Proprietary
compatibility: 远端 Linux（openEuler/EulerOS 等，aarch64）；需要 SSH 访问所有涉及主机（P 侧与 D 侧）；NPU 环境（npu-smi、ASCEND_RT_VISIBLE_DEVICES）；vLLM（支持 --data-parallel-size 外部 DP 拉起口径）+ Mooncake/AscendStore KV 传输（MultiConnector）；连接方式优先 ssh-mcp-server 的 upload/execute-command/download，无 MCP 时回退本地 ssh/scp
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: tp_topology_migration
  version: "0.3.0"
---

# kptools-tpsplit-optimization：新 TP size 集群部署（TP 拓扑变更）

把推理集群从旧 TP 拓扑（典型：跨机 TP32 + Ray 托管）迁移部署到新 TP 拓扑（典型：单机 TP8 闭环 + 显式 DP 组网 + Mooncake 对齐 + 单机双实例）。新 TP size 来自用户输入或上游可行性分析 `.md` 报告。产出：已拉起并验证的新集群 + 一份部署指南 `.md`。

核心原则（背后的原理详见 `references/tp-topology-principles.md`，动手前先读）：

- **Mooncake 的 tp/dp 必须与 Prefill 实际拓扑严格一致**。分片错配的后果不是启动报错，而是 KV 路由错乱、丢 KV、Decode 拿错 KV 分片、偶发 NPU 报错/hangs——现场极难排查，所以本 skill 在每个阶段都做一致性自检
- **两层 DP 不要混淆**：vLLM `--data-parallel-size`（引擎组内 DP，如一组 4 个 TP8 实例）≠ Mooncake `dp_size`（全局 Prefill DP 副本总数，跨所有主机累加，如 20）。写错任何一个都会 KV 寻址异常
- **拓扑变更是全量切换，不是增量修改**：所有 P 侧、D 侧的 KV_TRANSFER_CONFIG 必须同步更新，新旧拓扑混跑 = KV 错配事故
- **先出方案、用户确认、再动远端**：TP 拓扑变更牵一发动全身（停旧集群、改所有配置），部署方案未经用户确认前，不在远端做任何变更性操作

## 快速决策

1. 用户直接给了新 TP size（数字）→ 阶段 1 收集其余拓扑信息
2. 用户给了可行性分析报告（.md 路径）或说"按上次分析的结论部署" → 阶段 1 先解析 .md 提取推荐 TP size 与拓扑结论并向用户复述确认，再补齐报告里没有的信息（主机清单等）
3. 用户只要生成部署脚本和配置（远端自己拉）→ 走完阶段 1-4，阶段 5 的拉起验证部分改为写进部署指南由用户执行
4. 部署完成后出现 KV 报错 / NPU 偶发错误 / hangs → 优先对照阶段 5 的验证清单和上线检查清单排查配置一致性（大概率是 Mooncake tp/dp 错配或新旧混跑），再考虑回滚

## 阶段 1：输入解析与信息收集（缺什么问什么，不要猜）

### 1.1 确定 TP size 来源（二选一）

- **用户直接输入**：确认目标 TP size（如 8）。追问一句是只改 P 侧还是 D 侧也要动——本 skill 默认场景是 **P 侧 Prefill TP 变更**（D 侧 Decode 通常 tp=1 不动，但其 KV_TRANSFER_CONFIG 里的 prefill 段必须同步改）。若用户确实要改 D 侧拓扑，同样流程，但 D 侧实例布局也要重排
- **上游可行性分析 .md**：读取报告，提取推荐 TP size 与拓扑建议。报告格式不固定——找"推荐 TP / recommended_tp_size / 结论 / 建议部署 / 实例布局"等关键信息。**提取后必须向用户复述确认**（"报告建议 TP8、全局 20 个 P 实例，按这个部署吗？"），报告没写的信息（主机清单、路径、Proxy 类型）继续问用户。报告结论与用户口头说法冲突时以用户为准并提醒差异

### 1.2 必须收集的拓扑信息

| 信息 | 说明 | 缺失处理 |
| --- | --- | --- |
| P 侧主机清单 | 每台 IP、SSH 认证方式、NPU 卡数 | 必须问 |
| 每台实例数 | TP8 × 16 卡机 = 2 实例 | 按卡数 ÷ TP size 推导，向用户确认 |
| DP 组大小 | 引擎组内 DP（一组几个 TP8 实例） | 默认 4，确认 |
| Mooncake 全局 dp_size | 所有主机 P 实例总数 | 由实例布局推导并自检；分析报告给定则双向核对 |
| D 侧现状与位置 | D 侧 dp_size/tp_size（如 64/1）、D 侧启动脚本所在主机与路径 | 必须问 |
| 旧方案形态 | 执行后端（ray / mp / 无旗标）、现有 TP size、KV connector 结构 | **读旧启动脚本自动识别**，结果与用户核对；**不要假设旧方案一定是 TP32+Ray** |
| 旧集群状态 | 是否在跑、是否要停 | **必须问，绝不静默停** |
| P 侧脚本与配置路径 | vLLM 启动脚本、KV_TRANSFER_CONFIG 所在位置 | 必须问 |
| Proxy 网关 | 类型（MindIE Motor / Nginx / 自研等，各种可能都有）与配置文件路径 | 必须问；类型决定改法与 reload 方式 |
| 模型与业务参数 | 模型路径、served-model-name、max-model-len、max-num-batched-tokens、max-num-seqs、parser 等 | 从旧脚本继承，但见 2.3 必须复核 |

信息收集齐之前不要在远端做任何**变更性**操作；只读排查允许——用户答不上来的项（如路径记不清），SSH 上去 `ps -ef | grep vllm`、找启动脚本位置后再与用户核对。

## 阶段 2：拓扑推演与部署方案（先算清楚，用户确认后才动手）

生成《部署方案表》展示给用户，**确认前不在远端做任何变更**。

### 2.1 实例布局表（每实例一行）

| 实例 | 主机 | ASCEND_RT_VISIBLE_DEVICES | DP 组 | DP rank | http 端口 |
| --- | --- | --- | --- | --- | --- |
| P-0 | 10.x.x.1 | 0,1,2,3,4,5,6,7 | 组1 | 0 | 9000 |
| P-1 | 10.x.x.1 | 8,...,15 | 组1 | 1 | 9001 |
| … | | | | | |

推导规则：

- **实例数/台 = 卡数 ÷ TP size**。卡分配必须互斥：同机两个实例看到同一批卡 = 资源冲突，这是本场景最高频的事故，布局表生成后逐行核对一遍
- **Mooncake 全局 dp_size = Σ 所有主机实例数**，必须等于用户给定值 / 分析报告值，对不上先停下来向用户澄清，不要硬凑
- **DP 组划分**：组内实例分配 rank 0/1/2/…，统一主节点 IP（组内 rank 0 所在主机），rpc-port 组内统一 16600。DP 组跨主机时，组内每台机器都要对 16600 互通
- **端口规划必须整表核对**：http 端口（同机按实例递增 9000/9001/…）与 `--data-parallel-rpc-port` 16600 无冲突即可。**KV 配置里的 kv_port / lookup_rpc_port / engine_id 不涉及变动**，沿用旧值（30000 / 10001 / 0），不要画蛇添足去改

### 2.2 Mooncake 参数推导

KV_TRANSFER_CONFIG 的**改动面只有两个值**：`prefill.dp_size`（p_dp_size → Mooncake 全局 dp_size）和 `prefill.tp_size`（p_tp_size → 新 TP size）。其余字段一律不动：`decode` 段保持 D 侧现状（如 64/1）、`engine_id` / `kv_port` / `lookup_rpc_port` 原值保留。D 侧所有实例的 KV_TRANSFER_CONFIG 的 prefill 段同步改为同一组值。生成后做一次守恒自检：所有 P 实例配置里 prefill.dp_size 完全一致，且等于实例总数。

### 2.3 业务参数复核（TP 变小的影响，必须让用户拍板）

TP32→TP8 后单请求算力 / 显存池变小，从旧脚本原样继承以下参数前必须逐项向用户确认是否要调：

- `--max-model-len`（如 204800 的超长上下文在 TP8 上可能放不下）
- `--max-num-batched-tokens`、`--max-num-seqs`（单 TP8 的 batch 上限下降）

方案表里单独列出这三项 + 建议值，等用户确认，不要静默改。

## 阶段 3：环境预检与旧集群处置（SSH 连上后先检查再动手）

每台 P 侧主机依次执行：

```
npu-smi info                                # 卡数与健康状态，与布局表核对
ss -ltn 2>/dev/null | grep -E ':(9000|9001|16600|30000|10001)\b'   # 计划端口占用；30000/10001 为旧脚本的 kv_port/lookup_rpc_port（以旧脚本实际值为准），被占多半是旧实例没停干净
ps -ef | grep -E 'vllm|ray' | grep -v grep  # 旧实例与 Ray 残留进程
```

- **旧集群处置**：阶段 1 已问过要不要停，此处执行。停 P 侧旧实例：`ps -ef | grep vllm` 找到 PID 逐个 kill（**勿用宽匹配 pkill**，会误杀其他进程）；**仅当旧方案确实用 Ray 时**（脚本含 ray backend 或现场有 ray 进程）才在每台执行 `ray stop`；停完复查 `ps -ef | grep -E 'vllm|ray'` 确认干净、端口已释放
- **去 Ray 残留（仅旧方案有 Ray 时）**：检查旧启动脚本与用户 shell 环境，确认无 `--distributed-executor-backend ray`、无 Ray 相关环境变量（RAY_ADDRESS 等），新脚本中不引入。旧方案本来就没有 Ray 时这步无事可做，直接过
- **防火墙/安全组**：DP 组跨主机时，组内所有主机对 16600 互通；Proxy 到各实例 http 端口可达
- ⚠️ **新旧拓扑禁止混跑**：切换窗口内 D 侧必须整体指向同一套 P 拓扑。"先改一半 D 实例试试" = KV 错配，不允许分批切换 P 拓扑
- 预检发现端口被占、卡数对不上、旧进程清不掉，立即反馈用户并停下，不要自行换端口/换卡继续

## 阶段 4：生成脚本与配置分发

基于 `references/templates/prefill-tp8-instance.sh`（新方案占位符模板）为每个 P 实例生成启动脚本。旧方案基线见 `references/templates/legacy-prefill-tp32.sh`（识别现有部署、对比差异用）；legacy 基线只是 **TP32+Ray 形态的样例**，现场旧脚本的后端/TP size/KV connector 以实际读取为准，不要硬套基线。

占位符与数据来源：

| 占位符 | 来源 |
| --- | --- |
| `<RANK>`（--data-parallel-rank） | 实例布局表 DP rank |
| `<DP_GROUP_SIZE>`（--data-parallel-size） | DP 组大小（组内所有实例一致） |
| `<DP_MASTER_IP>`（--data-parallel-address） | DP 组主节点 IP（组内一致） |
| `<DEVICES>` | 实例布局表 ASCEND_RT_VISIBLE_DEVICES |
| `<HTTP_PORT>` | 实例布局表 |
| `<P_TP_SIZE>` / `<P_DP_SIZE>` | 拓扑参数（P_DP_SIZE 全局值，所有实例一致） |
| `<D_DP_SIZE>` / `<D_TP_SIZE>` | D 侧现状（从旧脚本继承） |
| 模型路径 / served-model-name / max-model-len 等 | 旧脚本继承 + 2.3 用户确认结果 |

生成要点：

- 新脚本**不写 `--distributed-executor-backend`**（TP 单机闭环用默认后端即可）。旧脚本带 ray backend 的，生成时删除；旧脚本本来就没有的，保持没有——新方案任何情况下都不引入该旗标
- 每实例单独一份脚本，实例间差异项只有 `--data-parallel-rank` / `ASCEND_RT_VISIBLE_DEVICES` / `--port` 三处，其余参数全组一致——生成后 diff 一遍，差异项之外的改动都是笔误
- DP 组网参数口径（对齐 vLLM 官方旗标）：`--data-parallel-size`（组大小）、`--data-parallel-rank`（本实例 rank）、`--data-parallel-address`（组主节点 IP）、`--data-parallel-rpc-port 16600`（组内统一）。注意 `--data-parallel-rank` 外部 LB 模式适用于 **MoE 模型**（GLM 系列 `--enable-expert-parallel` 场景即 MoE）；现场 vLLM-Ascend 版本若旗标名不同，以 `vllm serve --help | grep -i data-parallel` 实际输出为准
- **KV_TRANSFER_CONFIG 只改 p_dp_size / p_tp_size 两个值**，engine_id / kv_port / lookup_rpc_port / decode 段一律原样保留；**D 侧同样只改 prefill 段的两个值**，其他启动参数不动；改前备份原文件。这一原则不依赖 KV connector 的具体结构——旧脚本无论用 MultiConnector 还是其他 connector，改动面都只是 prefill 段的 dp/tp 两个值，模板里的 MultiConnector 结构以现场旧脚本实际结构为准
- **旧脚本与配置一律备份后再改**（同目录 `*.bak.<日期>` 或 backup/ 子目录，禁止原地覆盖删除）——回滚靠的就是这些备份
- 分发：每台主机只放自己的实例脚本；上传用 ssh-mcp-server upload 或 scp
- 远端自检通过才进阶段 5：`bash -n <脚本>` 逐个过；KV_TRANSFER_CONFIG 是嵌在脚本里的字符串，用一段 python 从脚本中抽出该变量并 `json.loads` 校验合法性

## 阶段 5：拉起、验证与交付

1. **启动顺序**：先启 DP 组 rank 0（主节点）再启组内其余实例（rendezvous 依赖主节点先就位）；不同 DP 组之间无顺序要求；同机双实例无顺序要求。启动用 nohup 后台 + 日志落盘（`nohup bash xxx.sh > xxx.log 2>&1 &`），逐实例确认进程存活
2. **逐实例探活**：`curl -m 5 http://127.0.0.1:<http_port>/v1/models`，任一实例起不来先看日志（`grep -iE 'error|fail' <实例日志>`），常见原因：卡冲突、端口占用、DP 组网失败（主节点没先起）、KV 配置 JSON 拼写错
3. **KV 端到端验证（Mooncake 对齐是否成功的唯一可靠判据）**：通过 Proxy（或直连任一 P 实例）发一条真实 completion 请求，确认能正常出 token 且各实例日志无 Mooncake/DP 通信报错。`kv_load_failure_policy: fail` 下 KV 传输失败会直接报错——这是好事，失败立即暴露而不是静默丢 KV
4. **Proxy 更新**：把 P 后端列表换成新实例地址（`<主机>:9000`、`<主机>:9001` …），按用户网关类型改其配置文件并 reload（改前备份，改后探活）。MindIE Motor 类网关通过其 `register.sh` 重新注册新实例——注册表是内存态，coordinator 重启或后端换血后必须重新执行注册，否则请求报 No instance available
5. **观察**：各实例日志持续观察数分钟，确认无 KV 路由报错、无偶发 NPU 错误
6. **产出部署指南 `.md`**（用户指定目录或部署目录），内容：本次拓扑信息摘要（布局表 + Mooncake 参数）、启动顺序与命令、验证方法、回滚步骤（恢复备份的旧脚本/配置 + 重启旧集群）、压测提醒
7. **压测提醒必须写进指南**：TP8 的单请求算力/显存池、单序列长度上限、最大 batch 相比 TP32 下降，**需重新压测 Prefill 吞吐与 P-D KV 传输带宽**，旧压测数据不能直接沿用

## 上线检查清单（写进部署指南，逐项打勾）

1. 卡隔离：同机实例 ASCEND_RT_VISIBLE_DEVICES 无交集
2. DP 组通信：16600 端口组内互通，防火墙/安全组放行
3. Mooncake 同步：所有 P 侧 + D 侧 KV_TRANSFER_CONFIG 的 tp/dp 已更新为同一组值，无新旧拓扑混跑
4. 后端旗标：新实例启动命令无 `--distributed-executor-backend`；旧方案用 Ray 的，已确认无 Ray 进程/环境变量残留（旧方案本身无 Ray 则只查前半句）
5. 端口规划：http（同机实例 9000/9001…）与 data-parallel-rpc-port 16600 无冲突；kv_port / lookup_rpc_port / engine_id 保持旧值未动
6. 全局 DP 总数：Mooncake dp_size 精确等于所有主机 P 实例数之和（逐台数一遍）
7. 负载模型：max-model-len / max-num-batched-tokens / max-num-seqs 已按新 TP 复核，压测计划已安排

## 回滚

任一环节失败且短期修不好时：停新实例（按 PID），恢复备份的旧脚本/配置，按旧方案**原样**重启集群（旧方案含 Ray 就含 Ray，不含就不含——一切以备份的旧脚本为准），D 侧 KV_TRANSFER_CONFIG 恢复旧 prefill 段。回滚步骤在部署指南中写完整命令，不要临场发挥。

## 模板与参考

- `references/templates/prefill-tp8-instance.sh` — 新方案实例启动模板（TP 闭环 + 显式 DP + 无 Ray，占位符版）
- `references/templates/legacy-prefill-tp32.sh` — 旧方案基线（TP32 + Ray + Mooncake MultiConnector），识别现有部署/对比差异用
- `references/tp-topology-principles.md` — 拓扑变更原理与风险详解（两层 DP、Mooncake 分片对齐、故障域、收益代价分析）
