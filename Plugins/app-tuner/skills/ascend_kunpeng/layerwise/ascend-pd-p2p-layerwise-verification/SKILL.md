---
name: ascend-pd-p2p-layerwise-verification
description: 验证昇腾 vLLM-Ascend PD 分离 P2P Layerwise 是否真正生效且结果正确。覆盖启动健康检查、环境与版本证据采集、逐层传输标记、block 映射一致性、精度对比和失败注入；当用户问“怎么验证 layerwise”“请求成功但结果不对”“怎么采集证据”“怎么做精度/失败验证”时使用。作为 ascend_kunpeng/layerwise 子域使用。
compatibility: Linux + Ascend NPU; requires vLLM / vLLM-Ascend runtime and read-only access to P/D logs.
---

# P2P Layerwise 验证

## 何时触发

- 配置已完成，需要验证 P2P 逐层传输是否触发。
- 用户要求采集运行证据、日志或版本信息。
- 用户发现请求成功但输出异常，需要判断 KV 是否完整。
- 用户要求做失败注入或验证 recompute 兜底。

## 不进入本 skill

- 还没有完成准入或配置 → 先走 admission / config。
- 启动失败或明确报错 → 先走 troubleshooting。
- 只做 profiling trace 分析 → 走 `kptools-ascend-profiling-analysis`。

## 必读 Reference / Scripts

- `scripts/collect_layerwise_evidence.sh`：采集版本、配置、NPU/网络/进程事实。
- `scripts/verify_p2p_layerwise.sh`：服务健康、唯一 prompt、基础请求与检查提示。
- 本 skill 不包含固定机型、固定拓扑或历史性能数据。

## 前置条件

| 依赖 | 要求 |
|---|---|
| OS | Linux；在目标 P/D/Proxy 节点执行 |
| 设备 | Ascend NPU，`npu-smi` 可用 |
| Python | 可导入 `vllm`、`vllm_ascend`、`torch`、`torch_npu` |
| 网络 | 可访问 proxy / Prefill / Decode 健康地址 |
| 权限 | 只读目标日志、执行 `npu-smi`；不需要修改系统参数 |
| 工具 | `bash`、`curl`、`python`；版本以现场 `--version` 记录 |

本 skill 只做只读采集和验证；修改配置、重启服务、清理资源都需要单独授权。

## 脚本契约

`verify_p2p_layerwise.sh`：

- 输入：`PROXY_HOST`、`PROXY_PORT`、`MODEL_NAME`、`P_LOG`、`D_LOG`、`CURL_MAX_TIME`。
- 参数校验失败返回 2；健康检查或请求失败返回 1；关键日志不可读，或 P/D 日志中没有 layerwise 关键标记时返回 3；全部通过返回 0。
- `MODEL_NAME` 通过 Python `json.dumps` 生成请求体，不手工拼接 JSON。

`collect_layerwise_evidence.sh`：

- 输入：`P_LOG`、`D_LOG`、`CONFIG_JSON`、`OUT`、`FORCE`、`PYTHON_BIN`。
- 默认不覆盖已有 `OUT`；需要覆盖时显式设置 `FORCE=1`。
- `OUT` 与输入文件（含符号链接、硬链接）不允许是同一文件，碰撞返回 2。
- 只读取指定文件，不扫描无关进程或主机。
- 原始日志可能包含内部地址、端口或请求信息；对外分享前必须脱敏，产物保留和清理由执行者决定。

## Input

- 已完成准入和配置的 Prefill / Decode 服务。
- 现场 `--kv-transfer-config`、启动脚本、proxy 配置。
- 可用的健康检查地址、模型名和唯一 prompt 生成方式。
- 允许的失败注入方式（单层失败、Store load 失败、metaserver 超时）。

## Workflow

### 1. 健康检查

- `/v1/models` 或等价接口可用。
- Prefill / Decode / proxy / Mooncake Master 均处于预期状态。
- 记录各组件版本、启动参数和端口，不记录与本次无关的环境数据。

### 2. 采集 layerwise 证据

- 本 skill 的 `collect_layerwise_evidence.sh`。
- 确认 `num_kv_cache_groups`、`kv_caches.keys()`、`num_draft_attn_layers`、`attn_backend` 与准入结论一致。
- 确认 `remote_block_ids`、`remote_cached_tokens`、`transed_idx`、`to_trans_idx` 的日志可读。

### 3. 触发传输

- 使用唯一 prompt，避免 Decode 本地 prefix cache 影响传输范围。
- 发起一次正常请求，确认 Prefill 侧进入逐层 send、Decode 侧进入逐层 recv。
- 检查 P2P 连接、metaserver 回调、`done_sending` / `FAILED_SENDING_MSG` 日志。

### 4. Block 映射一致性

- 对比每个 group 的本地 block 数、远端 block 数、`to_trans_idx` 与 `transed_idx`。
- 如果 D 只返回后缀 `remote_block_ids`，确认 P 侧 `d_block_idx` 已按 per-rank 前缀块数偏移。
- 如果远端 block 数不足，确认请求被收敛为失败/重算，而不是静默跳过。
- 重点检查 `get_transfer_mappings` 是否出现越界、错块或空传输。

### 5. 精度对比

- 同一 prompt：本地非 PD 路径 vs PD + layerwise 路径。
- 关注输出文本、logprob 或业务指标，不只比较请求是否成功。
- 若精度异常，先查是否缺层、缺 block、indexer/tail cache 未传。

### 6. 失败注入

- 单层传输失败。
- Store load 失败。
- metaserver 超时。
- 确认 `kv_load_failure_policy=recompute` 或等价策略生效，D 侧 invalid block 能触发重算。

## 输出

```text
health: pass | fail
layerwise_triggered: yes | no | unknown
block_mapping: consistent | mismatch | unknown
precision: pass | fail | not-run
failure_injection: pass | fail | not-run
evidence_path: ...
next_action: closeout | troubleshooting | code-optimizer
```

## 拒绝条件

- 用户要求只凭“请求返回 200”判定精度通过。
- 用户要求写入固定机型、固定拓扑或历史性能数字。
- 用户要求跳过 block 映射一致性检查。
- 用户要求隐瞒缺块或静默重算，把异常请求当作成功。

## 边界

本 skill 只做验证和证据采集，不修改连接器、不调整业务代码。发现结构性缺陷时交 `code-optimizer`。
采集产物默认仅保留在本地工作目录；对外共享前必须按现场规范脱敏，脚本不自动删除用户文件。
