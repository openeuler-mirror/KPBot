# 输出契约 prefetch-policy/v1

`PREFETCH-POLICY.md` 与 JSON 使用相同 finding ID。报告先给出已回答的范围，再列架构对照、调用时序和缺项。所有路径指向本次实际读取的文件；行号从 1 开始。外部历史记录注明时间、来源和未复验状态。

## JSON 字段

| 字段 | 内容 |
|---|---|
| `schema_version` | 固定 `prefetch-policy/v1` |
| `status` | `ok` / `partial` / `blocked`，语义见 SKILL.md |
| `target` | `source_root`、`scope`、`architectures`、`commit`（未知为 null）、`dirty`（未知为 null）、`build_context` |
| `artifacts` | 数组：`path`、`sha256`、`architecture`、`build_link`（与源码/构建的关联说明）；没有则空数组 |
| `source_files` | 数组项为 `path`（可解析的真实路径）、`sha256`（64 位十六进制摘要）；列出所有引用的源文件。纯产物调查为 [] |
| `findings` | 下面定义的策略数组；按架构与调用位置区分 |
| `limitations` | 本次证据可回答和不能回答的范围 |
| `required_evidence` | 待补的具体文件、配置或运行证据；完整源码调查可为空 |

每个 finding 至少包含 `id`、`architecture`、`level_hint`、`access`、`source_state`、`binary_state`、`runtime_state`、`distance`、`evidence`。建议同时记录 `instruction`、`retention`、`condition`、`address`、`prefetch_stage`、`consume_stage`、`notes`。

- `level_hint`：`L1` / `L2` / `L3` / `other` / `unknown`。是提示意图，不代表驻留状态。空实现可以保留封装原本的 L2 意图，但在 notes 指明没有指令；builtin 无目标映射时为 unknown。
- `access`：`read` / `write` / `unknown`。指令预取另记 notes，不能归为数据读策略。
- `source_state`：`implemented`（在给定条件下有有效预取操作）、`noop`（该分支显式为空）、`not_found_in_scope`（限定搜索范围内未发现）、`unknown`（缺源码/配置，不能判定）。
- `binary_state`：`verified`（已识别产物或带出处的反汇编中，目标路径的相关位置有预取指令）、`not_found_in_scope`（已完整解码并检查的明确范围中未见预取指令）、`unavailable`（无适用产物/工具）、`unlinked`（有命中但产物身份或目标路径无法关联）。源码对应关系单独记录在 `artifacts[].build_link`，未知时明确写“源码关联未知”，不影响对已知产物指令的判读。
- `runtime_state`：`observed`（同构建运行证据确认该路径/指令执行）、`unverified`。这不表示测量到了缓存填充或收益。
- `distance`：`kind` 为 `fixed` / `phase` / `unknown`，`value` 为数值或 null，`unit` 为 `items` / `spans` / `bytes` / `iterations` 等明确单位。`phase` 的 `value` 必须为 null，推荐 `unit=phase`；`noop` 的距离不适用，用 `kind=unknown,value=null,unit=unknown` 并在 notes 说明。
- `evidence`：数组项为 `path`、`lines: [start,end]`、`symbol`。每项指向一个实际符号及其范围；常量、类型和函数分别引用，不把多个符号合写成一个名称。源码 finding 引用定义、调用及消费点，另在 `source_files` 保存涉及文件的 SHA-256。产物 finding 引用归档的反汇编文本行；命中的预取指令以 `instruction_address` 记录该产物内指令地址（例如 `0x1040`）。

产物未命中时不要求 `instruction_address`，也不填虚构地址。以已检查的 `symbol`、反汇编 `lines` 及 `address_range: [start,end]` 作为证据；地址为该产物中的十六进制字符串，范围含起始、不含结束，end 为末条已检查指令结束后的第一个字节地址。保留产物身份和摘要，notes 说明检查覆盖范围。若反汇编截断、存在未解码字节或无法确认目标范围完整性，不能将该范围判为 `not_found_in_scope`；记录缺项，按已有可用证据返回 `partial` 或 `blocked`。

空实现仍可在 `prefetch_stage` 或 notes 中记录封装调用发生的阶段，但不能将调用到消费之间的间隔填成有效预取距离；`value=0` 也不能替代不适用的 null。

示意（合成输入，不是实测）：

```json
{
  "schema_version": "prefetch-policy/v1",
  "status": "partial",
  "target": {
    "source_root": "/analysis/demo", "scope": "probe", "architectures": ["aarch64"],
    "commit": null, "dirty": null, "build_context": null
  },
  "artifacts": [],
  "source_files": [],
  "findings": [{
    "id": "probe-l1-arm", "architecture": "aarch64", "level_hint": "L1",
    "access": "read", "instruction": "prfm pldl1keep", "retention": "KEEP",
    "source_state": "implemented", "binary_state": "unavailable", "runtime_state": "unverified",
    "distance": {"value": 16, "unit": "items", "kind": "fixed"},
    "condition": "i + 16 < n", "address": "items[i+16]",
    "prefetch_stage": "probe look-ahead", "consume_stage": "probe load",
    "evidence": [{"path": "demo.rs", "lines": [20, 25], "symbol": "probe"}]
  }],
  "limitations": ["仅分析源码；尚未核对目标产物"],
  "required_evidence": ["相同源码与构建参数生成的 AArch64 产物"]
}
```

示例中的 `source_files` 为空是为了省略虚构摘要；真实报告应填引用文件的摘要。无证据时不要沿用示例行号或示例结论。

## 仅产物与受阻输入的判定

以下为契约验收情形，独立审查时从输入事实推导结果，不沿用源码示例的结论：

| 输入与请求 | 预期状态 |
|---|---|
| 已绑定摘要、架构、来源的反汇编，在用户指定的 `probe` 符号 `0x1040` 处有 `prfm pldl2keep`；仅问该产物提示 | `status=ok`、`source_state=unknown`、`binary_state=verified`、`runtime_state=unverified`，`source_files=[]`，`target.source_root/commit/dirty/build_context=null`，`build_link` 说明源码未知；evidence 引实际文本行和 `instruction_address=0x1040` |
| 已绑定摘要、架构、来源的反汇编完整覆盖 `probe` 的 `[0x1000,0x1048)`，对应归档文本第 12–29 行，所有指令均成功解码且无预取；仅询问该函数范围 | `status=ok`、`source_state=unknown`、`binary_state=not_found_in_scope`、`runtime_state=unverified`；evidence 引归档文本的 `path`、`symbol=probe`、`lines=[12,29]`、`address_range=["0x1000","0x1048"]`，省略 `instruction_address`。`level_hint/access=unknown`，距离为 `unknown/null/unknown`；保留 artifacts 身份及源码关联未知说明，不扩大为整个程序没有预取 |
| 只有二进制，所有适用解码器都不可用，也无可读源码/归档反汇编 | `status=blocked`、`findings=[]`，缺项说明需要适配解码器或对应反汇编；不能写“未发现预取” |
| 只有无出处的 PRFM 文本，无法关联目标产物或请求函数 | 若仍能说明该条指令语义，`status=partial`、`binary_state=unlinked`；不能声称目标程序已生成或执行该指令 |

若用户要求核对当前源码与运行产物是否一致，即使已确认产物指令，缺 `build_link` 仍须将总体状态设为 `partial`。反汇编文本本身的摘要不能代替缺失的二进制摘要；在 artifacts 中按实际可得文件登记，并说明原二进制未取得。

## 一致性检查

逐条核对人读表格与 JSON 的架构、单位和三层状态一致；确认每个引用行范围存在且支持相应事实。`blocked` 不填虚构 findings；`noop` 不得在缺运行证据时声称硬件关闭。源码出现多个调用点时，至少说明哪些已覆盖、哪些未覆盖。性能结论另需可比数据，不能由这些状态字段推导。
