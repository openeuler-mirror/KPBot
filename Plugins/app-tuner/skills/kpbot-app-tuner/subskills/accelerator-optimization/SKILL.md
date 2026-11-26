---
name: accelerator-optimization
description: 分析 HPRE/KAE 密码加速卡瓶颈：用 UADK/ASF（e_sse、uadk_engine）把 TLS 握手 RSA 卸载到硬件，覆盖 uacce 设备与引擎判定、依赖与 ASF 使能、quota 与 worker 调优、SQE 计数与 A/B 验证、回滚，已实现完整采集/使能/验证脚本。NPU+vLLM 场景只做委托路由（转 vllm-ascend-profiling）；GPU/NPU 通用计算卡分析不在本 skill 范围。作为服务器应用优化 Agent 候选优化 skill 列表中的加速卡优化 skill 使用。
---

# Accelerator Optimization

本 skill 只处理 HPRE/KAE 密码加速卡的硬件卸载路径，范围与不适用项见下表。

| 范围 | 内容 | 处理方式 |
|---|---|---|
| 覆盖 | HPRE/KAE + UADK/ASF + OpenSSL 1.1 引擎，把 TLS 握手 RSA 卸载到硬件 | 走密码卸载流程（已实现完整工作流） |
| 委托路由 | NPU + vLLM 推理服务场景 | 直接转 `../vllm-ascend-profiling/SKILL.md` 执行其完整工作流，见"NPU+vLLM 专项引用" |
| 不覆盖 | AES 等对称加密体数据、ECDSA/Ed25519 证书 | HPRE RSA 卸载不适用，直接给 `stop_or_reject_condition` |
| 不覆盖 | GPU/NPU 通用计算卡分析（利用率/显存/算子/CPU feed） | 不在本 skill 范围；被 `gpu_npu_bottleneck` 误派发时输出 `stop_or_reject_condition` 并指向 `../vllm-ascend-profiling/SKILL.md`（vLLM 推理）或 `../hardware-upgrade-analysis/SKILL.md`（规格/容量），不得给空结论 |

## 判定（密码加速卡）

四项同时成立才进入使能，任一不成立先输出修复动作：

| 证据 | 期望 | 不满足的结论 |
|---|---|---|
| `/sys/class/uacce/hisi_hpre-*` + `/dev/hisi_hpre-*` | 存在 | 设备未注册，先修 `uacce_mode`（编号是全系统 uacce 注册序号，不保证为 0） |
| `uacce_mode` | `2` | `0` 无 uacce 设备；`1` 在 VM 中失败 |
| `openssl engine -t uadk_engine` / `-t e_sse` | `[ available ]` | 引擎、依赖库或环境变量问题 |
| 证书 `Public Key Algorithm` | `rsaEncryption` | 卸载不生效，不是故障 |

补充判据：worker `/proc/<pid>/maps` 无 `e_sse.so`/`uadk_engine.so`/`libasf.so` 说明未加载；`QM_DFX_SEND_SQE_TO_ACC_CNT` 不随握手增长说明未真正卸载；`hotspot_function_rank` 中 TLS/RSA 符号占比高且握手率随并发上升，才说明有收益空间。

## 密码加速卡卸载流程（HPRE/KAE）

完整判定依据、组件来源、调优参数、排障与回滚见 `references/uadk-hpre-playbook.md`；执行全部由本目录 `scripts/` 承载。

### Inputs

- HPRE/KAE 设备证据：`lspci`、`/sys/class/uacce/hisi_hpre-*`、`/dev/hisi_hpre-*`、`uacce_mode`
- 引擎与依赖状态：`engines-1.1/{e_sse,uadk_engine}.so`、`ldd` not found、`openssl engine -t`
- ASF 配置：`scheduler.json` 的 `quota`、`dev_metadata/uadk_engine.json` 的 `throughput`/`enabled`
- 应用侧：TLS 证书算法、`ssl_engine`、`ssl_session_cache`/`tickets`、worker 数
- 瓶颈证据：握手率、TLS/RSA 符号热点占比、`QM_DFX_SEND_SQE_TO_ACC_CNT` 基线

### Workflow

1. 采集证据：`scripts/collect_hpre_evidence.sh --evidence-dir <evidence_snapshot_dir> --current-run-id <id> --current-run-started-at <ts> --target-identity <identity.json> --nginx-conf <path> --pid <worker-pid>`（只读）。绑定模式四项必填（`--evidence-dir`/`--current-run-id`/`--current-run-started-at`/`--target-identity`），且 `snapshot_time` 与 `current_run_started_at` 必须为可解析的 ISO 8601。退出码契约：成功 exit 0 并原子写入 `hpre_offload_evidence.json`；落盘失败 exit 1；`snapshot_metadata.json` 缺失或不可解析 exit 4；元数据可解析但 run_id/状态/时间/身份不匹配或缺失 exit 3；失败均不改变已有证据文件。未传 `--evidence-dir` 时以 unbound 模式运行（stdout 输出 `current_evidence_status=unbound` 的证据 JSON，exit 0），unbound 证据不参与门控，仅供人工预检。
2. 用“判定”表做四项门控，不满足时只输出修复动作。若本节点无 HPRE（`crypto_accelerator_present=false`），输出 `status=degraded`、`crypto_offload_status=not_present`，并给一条 `accel-uadk-000` 空操作（`change_mode=analysis_only`）——`scripts/merge_subagent_results.py --gate-check` 会把空 `candidate_actions` 判为 `candidate_actions_empty`，也会把 `status=blocked/failed` 判为 gate 失败。若本次派发来自 `gpu_npu_bottleneck` 且现场是 GPU/NPU 通用计算卡问题（无 HPRE 证据），不按“无卡降级”处理，而是输出 `stop_or_reject_condition` 指向 `../vllm-ascend-profiling/SKILL.md`（vLLM 推理）或 `../hardware-upgrade-analysis/SKILL.md`（规格/容量）。
3. 使能：`scripts/enable_uadk_offload.sh --nginx-conf <path> --ssl-cnf <path>`（默认 dry-run；真实执行需 `--execute --approved-change-id <id>`）。脚本负责 `uacce_mode=2`、依赖与引擎安装、OpenSSL 引擎 cnf、`/etc/asf_conf/*`、nginx `ssl_engine e_sse;` 与引擎自检；覆盖写 `/etc/asf_conf/*`、已有 `$SSL_CNF` 与 nginx 配置前均生成 `.bak.<ts>`，改写后用 `--nginx-bin`（缺省自动探测）跑 `nginx -t`。`ssl_engine` 是 nginx **main 上下文**指令（全文件唯一，顶层），脚本会删除 http/server 内的错误位置并保留/插入一条顶层 `ssl_engine e_sse;`。`nginx -t` 失败或无法执行（缺二进制、缺运行库）时自动用 `.bak` 回滚 nginx.conf 与本次改写的 cnf 并 exit 1 / exit 3，不得以 exit 0 留下未校验的配置。
4. 调优：`--only-quota` 单变量逐档调整 `quota`（不得超过 `throughput`），worker 数按 `1×/2×/4× vCPU` 扫描。脚本在写 `scheduler.json`/`dev_metadata` 前强制校验同单位下 `quota <= throughput`；`--only-quota --all-quotas` 会按 `scheduler.json` 的 SN/processor/device 逐项匹配 `dev_metadata` 的 throughput，任一上限缺失、单位不可比或超限即 exit 2，不做“只校验第一条”的宽松处理。
5. 验证：`scripts/verify_hpre_offload.sh --url https://<host>:<port>/index.html --requests 50 --concurrency 20`（非 nginx 服务端加 `--pid <pid>`），要求 HTTPS 握手成功、SQE 计数增量与成功握手数匹配且进程已加载引擎；再做“仅增删 `ssl_engine` 一行”的 A/B。脚本限制总请求数 ≤10000、在途并发 ≤200 并分批等待；记录成功握手数与 HTTP 状态；共享 PF 计数无法归因（增量低于成功握手数或超过 3 倍容差）时返回 `INSUFFICIENT` 而非 PASS。`--url` 必须为 https，`--requests`/`--concurrency`/`--pid` 校验为数字，远端 `--ssh` 使用 `StrictHostKeyChecking=accept-new`（可用 `SSH_OPTS` 显式收紧）。
6. 不满足验收时回退，并在候选动作中写明 `stop_or_reject_condition`。

### 硬约束

- OpenSSL 必须 1.1.1 + `engines-1.1`；OpenSSL 3 加载会失败。
- 虚拟机必须透传 HPRE VF，且 `uacce_mode=2`；`uacce_mode=1` 需要 SVA，VM 中不成立。
- 证书必须 RSA；短连接场景必须关闭 session cache/tickets。
- `ssl_engine e_sse;` 是 nginx **main 上下文**指令（全文件唯一，顶层，如 `worker_processes` 之前）；写在 `http {}`/`server {}` 内会被 `nginx -t` 拒绝。
- 改 nginx 配置后必须通过 `nginx -t` 才允许重启；`enable` 已内置该检查，失败/无法执行时用 `.bak` 回滚并 exit 1 / exit 3，不留未校验配置。
- `quota` 不得超过同算法 `throughput`；`enable_uadk_offload.sh` 写盘前强制校验，超限 exit 2。
- 驱动安装、`modprobe`、SR-IOV/VF 透传、设备重置属于高风险动作，只在主流程 `approved_execute` 后执行。

## Outputs

密码卸载字段写入结果 JSON 的 `findings`（顶层字段见“输出契约”）：

- `accelerator_class`：`crypto_accelerator` / `none`
- `crypto_offload_status`：`not_present` / `present_not_enabled` / `enabled_unverified` / `verified` / `blocked`
- `crypto_findings`
- `hardware_capacity_recommendation`
- `validation_plan`
- `rollback`

只有 `verify_hpre_offload.sh` 返回 PASS 且 A/B 为正，才允许写 `verified`。

## 输出契约（流水线强制）

分析结果必须包含 14 个顶层字段，缺任一即被 `scripts/merge_subagent_results.py --gate-check` 判为 `missing_result_fields`：

| 字段 | 说明 |
|---|---|
| `subskill_name` | `accelerator-optimization` |
| `current_run_id` | 必须与 `snapshot_metadata.json` 一致 |
| `current_evidence_status` | 正常分析路径必须为 `current`；门控失败（`status=blocked`）时如实记录门控判定的证据状态：`stale`（元数据状态非 current 或快照早于本轮）、`mismatched`（run_id / target_identity 不一致）、`unbound`（未接入证据快照），不得改写为 `current` |
| `status` | `ok` / `degraded` / `blocked` / `failed` |
| `confidence` | `high` / `medium` / `low` |
| `analysis_timestamp` | ISO 8601 |
| `evidence_sources` | 证据路径，含 `hpre_offload_evidence.json`（密码卸载路径） |
| `findings` | 上述路径字段 |
| `candidate_actions` | 见下 |
| `required_evidence` | 缺失证据清单，无缺失为空数组 |
| `fallback_notes` | 降级说明，无降级为空数组 |
| `timing` | `analysis_seconds` 等耗时 |
| `subagent_id` | 平台返回的子任务 ID |
| `result_path` | 结果 JSON 落盘路径 |

`candidate_actions[]` 必须包含 14 个字段：`action_id`、`title`、`category`、`priority`（`high`/`medium`/`low`）、`change_mode`、`requires_root`、`risk`（`low`/`medium`/`high`）、`implementation_plan`、`validation_plan`、`rollback`、`expected_effect`、`expected_gain_metric`、`rejection_criteria`、`evidence_refs`；建议同时给 `source_subskill` 和 `commands_dry_run`/`commands_execute`。

`change_mode` 取值：`analysis_only` / `dry_run` / `online` / `restart_required` / `system_reboot` / `rebuild_required` / `hardware_advice`。驱动安装、固件升级、设备重置、MIG/分区调整、SR-IOV/VF 透传只在主流程 `approved_execute` 后执行。

## 证据门控

> **真源声明**：退出码与门控行为的唯一事实来源是 `scripts/collect_hpre_evidence.sh`（绑定模式四项必填；元数据缺失/不可解析 exit 4，校验不匹配 exit 3，写盘失败 exit 1，均不改变已有证据；无 `--evidence-dir` 为 unbound 模式 exit 0 仅 stdout 输出）。本节与 Workflow 的描述必须与其保持一致，`cases/unit-gate-and-dryrun/` 用例看守该契约；不一致时以脚本为准。

分析前必须满足：`current_run_id` 与 `evidence_snapshot_dir/snapshot_metadata.json` 一致、`current_evidence_status=current`、`snapshot_time` 不早于 `current_run_started_at`、`target_identity` 一致。任一不满足时只输出 `status=blocked` 且 `candidate_actions=[]`，不得给候选动作或收益结论；结果 JSON 的 `current_evidence_status` 必须写门控判定的实际状态（`stale` / `mismatched` / `unbound`），不得写 `current`。

采集脚本以同一套规则自检：`--evidence-dir` 校验通过后原子写入 `hpre_offload_evidence.json`（exit 0）；门控失败或写盘失败直接退出且不改变已有证据文件（缺失/不可解析 exit 4；不一致 exit 3；写盘失败 exit 1）。

## Dependencies

| 工具/输入 | 用途 | 分级 | 缺失时处理 |
|---|---|---|---|
| `lspci` | 识别 HPRE/KAE 设备与 PCIe 链路 | 推荐 | 降级设备识别，标记 `accelerator_tool_missing` |
| `openssl`（1.1） | 引擎自检与证书算法判定 | 必需 | 输出 `blocked_by_missing_dependency` |
| `modprobe` / sysfs `/sys/class/uacce` | HPRE 设备与 `uacce_mode` | 必需 | 输出 `blocked`，提示 VF 透传与驱动检查 |
| `ldd` | 引擎运行时依赖 | 推荐 | 降级依赖校验，仅按文件存在性判断 |
| `python3` | 读取 `snapshot_metadata.json`、解析 nginx 配置、校验 quota 计划 | 必需 | 输出 `blocked`，提示缺失解释器 |
| `ssh` | 宿主机与 VM 分离部署时执行验证 | 推荐 | 只在单机形态执行 |

公共依赖与降级口径见 `../../references/prerequisites.md`。

## Knowledge Mapping

密码卸载证据指向 `ssl_engine` 以外的应用参数（并发、超时、缓冲）时归 `application-config-optimization`；worker 绑核最终方案归 `cpu-affinity-optimization`；设备算力不足转 `hardware-upgrade-analysis`。本 skill 只负责密码加速卡路径的专项判断。

## NPU+vLLM 专项引用（vllm-ascend-profiling）

当现场同时满足 NPU 与 vLLM 推理服务时，本 skill 必须引用 `subskills/vllm-ascend-profiling/SKILL.md`（相对本目录即 `../vllm-ascend-profiling/SKILL.md`）执行其完整工作流（Stage-1 环境诊断与 TTFT 分解 profiling），诊断与采集结论回流到本 skill 的结果 JSON，不得由主 agent 手写替代。

触发条件（任一命中即引用）：

1. 进程/服务证据表明 vLLM（`import vllm`、vLLM server 日志、`vllm_ascend` pip 包或 NPU 上以 python 运行的 vLLM worker）。
2. TTFT 证据未达标：`TTFT > 目标值`、首 token 延迟劣化、或对比基线存在显著首包时延回退。
3. TTFT 分解 profiling 显式需求：任务要求 kernel/host/device 各段耗时分解，或现场出现 `trace_view.json` / vLLM profiler 采集需求。

缺席条件：无 vLLM 证据且 TTFT 无信息时，不引用；仅 GPU 场景不引用（本映射针对 Ascend NPU）。

本节是**委托路由**而非本 skill 的分析路径：命中触发条件时直接引用执行，本 skill 不对 GPU/NPU 通用瓶颈做任何判断；引用产物（Stage-1 环境诊断结论、TTFT 计时/trace 证据）必须带证据路径回填结果 JSON。
