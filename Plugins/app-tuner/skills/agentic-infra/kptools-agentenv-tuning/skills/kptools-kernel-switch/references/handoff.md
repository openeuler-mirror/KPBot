# 交回一次内核构建、试启动或回退

每个交付点使用单独的结果文件。调用方提供本轮 `run_id`、目标层及实例 ID、目标和预期身份。执行者将观察填入 `switch-result.json`，所有 `evidence_sources` 路径均相对于该文件的目录。把需要引用的日志或摘录放入本轮私有目录。对成功后静默的命令也要保存被比较的值和退出码。归档核验用实际采集记录关联目标和时间窗口。

| `goal` | 完成时的 `phase` | 验收材料 |
|---|---|---|
| `build` | `built` | `evidence_sources.kernelrelease` 为构建生成的文本，`artifact` 为实际交付的主镜像或包。`findings.release`、`artifact_sha256` 分别与它们核对。另附最终配置、工具链和配套模块清单。 |
| `trial` | `verified` | `kernel` 指向本轮 `check_kernel.py boot` 的 JSON。记录运行 release、下次启动项或镜像及其 `next_boot` 读回记录。Host 需要上一次的 release 和 boot ID。Guest 需要 `cold_start` 探针记录新沙箱及镜像。服务和业务各需一次成功探针。 |
| `restore` | `restored` | 使用原内核、原默认项或原 Guest 镜像作为本次期望值，复用试启动的检查。从切回后的状态重新采集，另存回退结果。 |
| `runtime` | `verified` | 至少记录 `mechanism` 的实际读回和 `workload` 的业务结果。在 `findings` 中注明原值、试验值及最终有效值。 |

每个已选探针在 `findings.probe_exit_codes` 中填写实际退出码，在 `evidence_sources` 下用同名键引用其日志。涉及机制、生效依赖或暂停恢复时，将 `mechanism`、`pause_resume` 等本次指定项也放入两处。只有本轮要求的探针全部通过才填写 `completed`。新旧 release 相同时填写 `findings.previous_release` 和预期的 `build_id`，用候选 `vmlinux` 的 GNU build ID 核对运行身份。`next_boot` 读回文件只保存一行实际生效的下次启动项 ID 或配置中的镜像路径。纯运行时改动把参数原值、试验值和最终读数分别写成 `previous_value`、`trial_value`、`effective_value` 三个字符串。业务保持同一沙箱、版本和负载，耗时或内存收益由父任务另做同条件 A/B。

以下是合成的 Guest 试启动交回示例。实际执行时记录本次的沙箱 ID，并使用该实例冷启动时导出的同一份 `/proc` 归档生成 `boot-check.json`。

```sh
python3 scripts/check_kernel.py boot --root "$RUN/capture" --run-id "$RUN_ID" \
  --target-id "$SANDBOX_ID" --release "$EXPECTED_RELEASE" > "$RUN/boot-check.json"
```

```json
{
  "contract": "agentenv-kernel-switch/v1",
  "run_id": "guest-net-01",
  "target": {"layer": "guest", "id": "sandbox-demo"},
  "goal": "trial", "status": "completed", "phase": "verified",
  "findings": {
    "release": "6.12-agentenv-net",
    "next_boot": "guest-net.img",
    "probe_exit_codes": {"cold_start": 0, "service": 0, "workload": 0}
  },
  "evidence_sources": {
    "kernel": "boot-check.json", "next_boot": "selected-image.log",
    "cold_start": "sandbox-create.log", "service": "envd.log", "workload": "guest-io.log"
  },
  "required_evidence": []
}
```

若选用的镜像仍从旧快照恢复，保留实际 `boot-check.json`，写 `status=blocked` 和最后实际到达的 `phase=requested`。在 `required_evidence` 写明缺项，如“提供从候选镜像冷启的沙箱 ID 与内部 release”。缺权限、依赖、回退通道或连接时同样可提前交回，引用已有材料，在 `findings` 记录最近观察与可行的恢复动作。若随后回退成功，另存 `goal=restore` 的结果，并保留原来的受阻记录。

直接调用时返回结果文件、当前内核、下次启动目标和遗留项。App Tuner 委托时，在执行前从任务包提取 `current_run_id`、目标标识、交付目标和本轮要求的额外探针，保存为 `request.json`。预先约定的 release 填入 `release`。同 release 镜像试启动或回退时，将目标 `vmlinux` 的 GNU build ID 固定在原始请求的 `build_id`，采集时传给 `check_kernel.py boot --build-id`。构建交付已有独立预期摘要时，将它填入原始请求的 `artifact_sha256`。校验器会将这些预期值与实际启动记录或交付文件逐一核对。下面的示例使用可区分的 release。

```json
{"contract":"agentenv-kernel-switch-request/v1","run_id":"guest-net-01","target":{"layer":"guest","id":"sandbox-demo"},"goal":"trial","release":"6.12-agentenv-net","probes":["workload"]}
```

只读复核使用原始请求和结果两个输入。在父任务 `required_output_path` 要求的原格式中引用复核结果、证据路径及缺项，由父任务更新状态。

```sh
python3 scripts/verify_handoff.py "$RUN/switch-result.json" --request "$RUN/request.json"
```

标准输出是 `agentenv-kernel-switch-verification/v1` 的 JSON。退出码 0 表示已核对所声明的完成结果，1 表示有效的受阻交回并列出缺项，2 表示契约、证据引用或身份有误。校验器读取已保存的证据，服务和负载的结论来自实际运行探针时保存的日志与退出码。
