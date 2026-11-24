---
name: uadk-accelerator-optimization
description: 针对热点函数中的密码学（AES/SM4/DES/SM3/SHAx/MD5/AEAD/HMAC/RSA/DH）与压缩（gzip/zlib）算法做 UADK 硬件加速卸载评估，输出硬件加速替换建议与配置或 UADK 不适用结论；当热点函数命中 UADK 支持的算法、crypto/compression 类别热点达到阈值，或用户显式询问硬件加速卸载时触发。作为服务器应用优化 Agent 候选优化 skill 列表中的 UADK 硬件加速 skill。
---

# UADK Accelerator Optimization

使用本 skill 处理架构图中的"鲲鹏硬件加速器（UADK）"路径：判断热点函数中的密码学/压缩算法是否适合卸载到 UADK 硬件加速器，并输出建议配置或不适用的明确结论。

## 何时触发

- `hotspot_function_rank` 或 perf 报告中出现密码学/压缩算法热点（`AES_*`、`SHA*`、`MD5*`、`SM3*`、`SM4*`、`DES_*`、`HMAC_*`、`RSA_*`、`DH_*`、`EVP_Encrypt*`、`deflate`、`inflate`、`compress`、`uncompress` 等）适用性判断为三层判据（热点证据、环境可行性、性能适用性预判），详见 Step 4。
- `performance-library-selection` 的 crypto/compression 类别热点占比达到阈值（≥0.5%）
- 用户显式询问"能否用硬件加速器/加速卡卸载加密或压缩"

## 热点输入来源（复用既有 skill）

本 skill 不重复建设采集能力，热点函数按以下优先级获取：

1. **主流程证据快照**：`performance_signal_summary.json` 的 `hotspot_function_rank`（由主流程 `scripts/collect_evidence_snapshot.sh` 产出，必须属于本轮 `current_run_id`）。
2. **performance-library-selection 的采样产出**：该 skill `scripts/run_and_profile.sh`（统一采样：在线 `--pid` 只读附加 / 离线命令闭环启动）的 perf 数据与 JSON 报告（路径：`../performance-library-selection/scripts/`）。
3. **兜底采样**：以上均缺失时，按 `run_and_profile.sh` 的在线/离线模式自行采样一次；无法采样时降级为静态依赖分析（`lsof`/`/proc/<pid>/maps` 查看 libcrypto/libssl/libz 链接），`confidence` 上限 `experience_only`。

## Inputs

- 本轮 `current_run_id` 的热点函数证据（`hotspot_function_rank` 或 perf 报告）
- 进程/业务信息：worker 线程数、NUMA 部署、报文/数据块典型长度（pktlen）
- 目标机器环境指纹：CPU 型号、OS 版本、内核、加速器设备与驱动状态
- `agent_action_mode`（只读分析 / dry-run / 允许在线变更）

## 必读 Reference

- UADK 支持算法目录、热点匹配模式、设备与算法映射：`references/uadk-algorithm-map.json`
- 环境检查 SOP、安装部署 SOP、建议配置推导规则、验证命令：`references/uadk-playbook.md`（§1 环境可行性检查、§2 安装部署、§3 队列环境变量、§4 建议配置推导、§5 接入与验证、§6 回退）

## 安全与架构红线

1. **只读探测**：本 skill 阶段所有探测命令（`lscpu`、`cat /etc/os-release`、`ls /sys/class/uacce/`、`ls /dev/hisi_*`、`rpm -q`、`openssl engine -t`、`lsof`、`/proc/<pid>/maps`、`perf`）只读。`modprobe`/`yum install`/环境变量注入等变更只能以候选动作形式输出，由主框架 `approved_execute` 后落地。
2. **禁止历史数据替代现场证据**：热点匹配必须基于本轮 `current_run_id` 的现场证据；历史报告或旧轮次结果只能作为候选方向参考，不得作为匹配依据。
3. **不建议盲目卸载**：小报文（默认阈值 <1KB，可按业务实测覆盖）场景硬件卸载存在 per-op 调度开销，可能负收益；必须按"建议配置推导"给出 pktlen 验证要求，由主框架 A/B 验证裁决。

## Workflow

**硬门控**：结论生成前必须完成 Step 1（热点输入）与 Step 3（算法匹配）。热点证据缺失时只能输出 `blocked`/`degraded`，不得凭猜测下结论。

```
Step 1: 获取热点函数证据（复用既有 skill 产出，来源与 run_id 校验）
         ↓ 产出: hotspot_evidence{source, run_id, hotspot_function_rank}
Step 2: 环境可行性检查（只读）
         ↓ 产出: uadk_feasibility{cpu/os/iommu_sva/devices/libwd/uadk_engine}
Step 3: UADK 算法匹配（逐类 pattern 匹配 + 占比汇总）
         ↓ 产出: matched_algorithms[]（算法、设备、热点占比、证据）
Step 4: 结论生成（recommended / not_applicable / blocked）+ 建议配置推导
         ↓ 产出: uadk_conclusion、suggested_config、candidate_actions[]
Step 5: 验证流程设计（uadk_tool / openssl speed / 业务 A/B）
         ↓ 产出: validation_plan、rollback
```

### Step 1：热点函数证据获取

按"热点输入来源"三级优先级获取。必须校验证据属于本轮 `current_run_id`，不一致时停止在 `evidence_gate`。热点数据结构要求至少包含 `(函数名, 占比%)` 列表；无占比信息时记录 `degraded`。

### Step 2：环境可行性检查（只读）

按 `references/uadk-playbook.md` §1 的命令清单逐项检查并记录：

- **CPU**：鲲鹏 9xx 系列（`lscpu` 型号）；非鲲鹏平台记录 `feasibility=cpu_unmatched`，仍可完成算法匹配但建议置信降级为 `experience_only`（其他厂商加速器不在本 skill 范围）。
- **OS/内核**：openEuler 22.03 / 24.03 及以上，内核支持 IOMMU & SVA（`ls /sys/kernel/iommu_groups` 非空、`ls /sys/class/uacce/` 存在）。
- **加速器设备**：`/dev/hisi_hpre`（RSA/DH）、`/dev/hisi_sec`（对称/摘要）、`/dev/hisi_zip`（压缩）哪些在场；`/sys/class/uacce/<dev>/` 读取 `api`、`flags`。
- **软件栈**：`libwd`、`uadk_engine` 是否已安装；`openssl engine -t uadk_engine` 是否可用。

设备不在场不阻断结论生成：若算法命中且机型匹配，输出 `recommended`（feasibility 标注 `device_not_ready`，候选动作包含驱动加载与安装步骤），`confidence` 上限 `medium`。

### Step 3：UADK 算法匹配

加载 `references/uadk-algorithm-map.json`，对热点函数逐类匹配：

```
FOR each alg in uadk-algorithm-map.json.algorithms:
  hits = hotspot 函数名匹配 alg.hotspot_patterns 且不命中 alg.exclude_patterns
  pct  = sum(命中函数占比)
  IF pct >= 0.5%: matched_algorithms.append({algorithm, device, pct, hits})
```

- 阈值与 `performance-library-selection` 对齐：类别热点占比 ≥0.5% 才进入推荐，<0.5% 视为噪声。
- **false positive 控制**：`crc32`/`adler32` 单独热点不算 gzip/zlib 压缩热点（UADK ZIP 卸载的是 deflate/inflate 流处理，非孤立校验和），命中 `exclude_patterns` 时该算法不推荐，改由 `performance-library-selection`（ISA-L 等）承接。
- **通用 EVP 符号归属**：`EVP_EncryptUpdate`/`EVP_DecryptUpdate` 属于 cipher 族级匹配，无法单独归因到 AES/SM4/DES 具体算法；需要 call stack 或 EVP_CIPHER_CTX 上下文进一步归因，无法归因时按 cipher 族推荐并说明。
- 摘要类（MD5/SM3/SHA）热点在 `*_Update/*_Final/*_block`（摘要计算本体）才算可卸载计算热点；占比阈值自然过滤协议栈一次性调用。

### Step 4：结论生成与建议配置

**适用性判据（三层，全部满足才输出 `recommended`）**：① 热点证据——命中算法占比 ≥0.5%（Step 3）；② 环境可行性——鲲鹏 9xx + UACCE 设备/驱动状态（Step 2）；③ 性能适用性预判——卸载收益取决于算法计算密度与数据搬运开销：业务典型 pktlen、调用频率/批量特征对照 per-op 卸载调度开销，pktlen <1KB 或低占比场景显式标注负收益风险；算子级预判用 `uadk_tool benchmark`（SW 基线 vs `--mode sva`，业务 pktlen，见 Step 5 与 playbook §5.2）。三层判据不齐时降低 confidence 并在结论中注明；最终收益由主框架业务级 A/B 裁决。

三种结论，必居其一：

1. **`recommended`（命中）**：建议用户将命中算法替换为硬件加速器实现，并按"建议配置推导"输出完整配置；`candidate_actions[]` 按 Candidate Action Contract 输出（驱动加载、安装、engine 接入、环境变量配置均为独立或组合动作）。
2. **`not_applicable`（未命中）**：输出明确结论——本轮热点函数中未发现 UADK 支持的算法（密码学/压缩）热点；报告必须列出已检查的全部算法类别与各自占比（含 <0.5% 的噪声类），说明无需 UADK，并指出热点实际落点，建议交回 `performance-library-selection` 或其他 subskill。
3. **`blocked`（证据不足）**：热点证据无法获取且静态降级也失败时输出，附最小补采建议；不得输出任何匹配结论。

### Step 5：验证流程设计（不执行真实变更）

- **算子级性能预判（可选，用于适用性判据第 ③ 层）**：`uadk_tool benchmark --alg <命中算法> --mode sva --pktlen <业务值>` 与 SW 基线对照，作为推荐置信度依据；业务级 A/B 仍由主框架裁决。
- **功能验证**：`openssl engine -t uadk_engine`；openssl 命令样例见 `references/uadk-playbook.md` §5。
- **算子级性能对比**：`uadk_tool benchmark`（SW 基线 vs `--mode sva`）与 `openssl speed -elapsed [-engine uadk_engine]`，命令模板见 playbook §5.2。
- **业务级 A/B**：按业务 pktlen 与真实并发验证端到端收益；判定标准、回退方案写入 `validation_plan`/`rollback`。

## 建议配置推导规则

按 `references/uadk-playbook.md` §4 执行，要点：

| 配置项 | 推导规则 |
|---|---|
| `uacce_mode` | 用户态加速必须 `uacce_mode=1`（SVA 模式）；默认值 0 不支持用户态 |
| 驱动加载顺序 | `uacce.ko` → `hisi_qm.ko` → `hisi_hpre.ko` / `hisi_sec2.ko` / `hisi_zip.ko`（按命中算法选择） |
| `WD_*_CTX_NUM` | 格式 `sync:N@node,async:M@node`；默认起步 `sync:2,async:4`；async 队列数 ≥ 应用 worker 线程数；node 取应用所在 NUMA 节点 |
| `pf_q_num` | SEC 默认 256，HPRE/ZIP 默认 64；ctx 不足时按并发扩容 |
| engine 接入 | OpenSSL 生态业务 → `uadk_engine`（engine id 固定）；自研实现 → UADK 原生 API（wd_*）；压缩无 engine 路径，走 wd_comp |
| pktlen 阈值 | 建议 ≥1KB 起测；业务实际报文长度代入 `uadk_tool --pktlen` 验证，<1KB 场景显式标注负收益风险 |
| NUMA 亲和 | 队列 node 与应用 worker 绑核 node 一致，避免跨 NUMA 访问队列 |

## 输出契约

- **`uadk_feasibility`** — Step 2 各项检查结果（`ready` / `device_not_ready` / `cpu_unmatched` / `os_unsupported`）
- **`matched_algorithms[]`** — `{algorithm, family, device, hotspot_pct, hotspot_hits[], evidence_source}`
- **`uadk_conclusion`** — `recommended` / `not_applicable` / `blocked`（附 `conclusion_reason`，未命中时必须含已检查类别与占比明细）
- **`suggested_config`** — 命中时输出：驱动加载命令、安装命令、`WD_*_CTX_NUM` 建议、engine 接入方式、pktlen 验证要求
- **`candidate_actions[]`** — 仅 `recommended` 输出；`not_applicable`/`blocked` 不产生候选动作
- **`validation_plan` / `rollback`**

`confidence` 取值 `high`（≥5% 且设备就绪）/ `medium`（0.5-5% 或设备未就绪）/ `low` / `experience_only`（仅静态证据）；本 skill 恒不标 `measured`，真实验证由主框架执行后回写。

## 与 performance-library-selection 的互斥关系

crypto/compression 热点的承接方案存在互斥：UADK 硬件卸载 vs ISA-L/isa-l_crypto/GMSSL 等 CPU 库替换（作用于同一热点）。本 skill 输出的 `candidate_actions[]` 必须声明 `mutually_exclusive_with: performance-library-selection(crypto/compression)`；由主框架按互斥方案全量验证规则分别成轮 A/B 后选优。执行顺序上本 skill 的分析可与库替换并行，落地互斥。

## Candidate Action Contract

每个 `candidate_actions[]` 必须包含 `action_id`、`title`、`category`、`priority`、`change_mode`、`requires_root`、`risk`、`implementation_plan`、`validation_plan`、`rollback`、`expected_effect`、`expected_gain_metric`、`rejection_criteria` 和 `evidence_refs`，并按互斥规则附带 `mutually_exclusive_with` 字段。驱动加载/卸载、`yum install`、openssl.cnf 修改等动作必须包含：变更前状态记录命令、完整执行命令、可独立执行的 `reverse_cmd`（`modprobe -r` / `yum remove` / 配置还原）。

## 错误处理

| 场景 | 处理方式 |
|------|---------|
| 热点证据缺失且无法采样 | 输出 `blocked`，附最小补采建议（perf 授权/提供启动命令） |
| 非 aarch64 / 非鲲鹏 CPU | 匹配流程照常执行，结论置信降级 `experience_only`，说明 UADK 鲲鹏加速器不适用该平台 |
| `/sys/class/uacce/` 不存在 | `feasibility=device_not_ready`；命中算法时照常输出 `recommended`（候选动作含驱动加载），未加载驱动前不得执行 `uadk_tool` 验证 |
| `libwd`/`uadk_engine` 未安装 | 候选动作包含安装步骤（yum 源/RPM/源码三选一，见 playbook §2） |
| `uadk_tool` 不存在 | 验证降级为 `openssl speed -engine uadk_engine`，并标注降级 |
| perf 权限不足 | 与用户交互请求授权；拒绝时降级静态分析（`confidence<=experience_only`） |

## Dependencies

| 工具/资源 | 用途 | 缺失影响 |
|------|------|---------|
| 主流程 `performance_signal_summary.json` 或 perf 采样 | 热点函数证据 | 输出 `blocked`/静态降级 |
| `../performance-library-selection/scripts/run_and_profile.sh` | 兜底采样复用（统一采样：在线 --pid / 离线闭环） | 降级为内联采样或静态分析 |
| `references/uadk-algorithm-map.json` | 算法目录与匹配模式 | 无法匹配，输出 `blocked` |
| `references/uadk-playbook.md` | 环境检查/安装/配置/验证 SOP | 建议配置缺失细节 |
| `lscpu`/`ls /sys/class/uacce/`/`rpm`/`openssl engine` | 环境可行性只读检查 | 对应项标 `unknown`，置信降级 |
| `uadk_tool`（libwd 提供） | 算子级性能验证 | 降级 openssl speed |

## Knowledge Mapping

- 热点在密码学/压缩且命中本 skill → 输出硬件卸载建议，与 `performance-library-selection` 互斥竞争。
- 热点在 `crc32`/`hash`/`malloc`/`memcpy` 等非 UADK 范畴 → 交回 `performance-library-selection`，本 skill 结论 `not_applicable`。
- 加速器设备规格、容量不足等非 UADK 议题 → `accelerator-optimization` / `hardware-upgrade-analysis`。
- 通用路由映射见 `../../references/knowledge-technique-routing.md`。
