# 输入输出契约

## 文件职责

`manifest.json` 是人工核实的运行清单，不由解析器猜测。
`parsed.json` 保存文本匹配、源行、原始字段和文件哈希。
SHA-256 从原文件 Buffer 计算；逐行解析另按 UTF-8 解码，不用解码后的字符串替代原始字节计算摘要。
`selection.json` 在多窗口时指定每项指标的源行。
`quality.json` 保存规则结果和输入清单快照。
`report.md` 是可读审核底稿，最终还需补充人工诊断。
原始日志保留在独立 raw 目录，不能以摘要替换原件。

## 清单字段

从 assets/manifest-template.json 复制。
模板的 null 表示未知，不能批量改 true 以取得 pass。
每项 evidence 应写真实文件及行号或记录 ID。
脚本要求非空证据定位，但不自动认证证据内容；agent 必须阅读原件。

| 字段 | 含义 |
|---|---|
| schema_version | 当前为 1 |
| run_id | 非空运行标识 |
| identity | 目标身份是否匹配，失配为 invalid |
| scope | 采集范围是否匹配用户分析目标 |
| privilege | 实际执行级别是否符合契约 |
| steady | 窗口是否覆盖声明的目标阶段 |
| model | 当前平台、版本和指标口径是否核实 |
| window | 预期窗口是否完整 |
| execution.kind | batch 或 service |
| execution.tool_exit | 工具原始退出码 |
| execution.target_exit | batch 目标原始退出码 |
| execution.completed | batch 完成量或 service 窗口进度符合预期 |
| execution.healthy | service 健康与窗口后存活 |
| duration | 请求与实际窗口，秒 |
| warnings_reviewed | 原始警告均已解释且不破坏目标契约 |
| arithmetic | IPC 同窗口径、绝对容差及依据 |
| partition | L1 分区确认、百分点容差及依据 |
| pmu | 同会话 enabled/running 时间及接受策略 |
| work | 同窗实际完成单位数及单位名称 |

布尔断言格式：value 为 true/false/null，evidence 为证据定位，reason 为解释。
warnings_reviewed 始终检查，不依赖关键词是否命中；有证据的 false 为 limited，缺少证据为 unknown。
表中 pmu 的两项时间必须同单位；不用不同事件轮次拼成一个比值。
minimum_ratio 是本次策略值，需要依据，不提供通用准确率阈值。
若工具未暴露时间，保持 null，结果 unknown 是正常且诚实的输出。

## 行式解析适配器

支持以下独立字段行，可有空白或冒号分隔：

```text
Cycles              1,000
Instructions        2,000
IPC                 2.00
Bad Speculation     13.03
Frontend Bound      11.04
Retiring            38.62
Backend Bound       37.30
```

字段可有符号、小数、科学计数法；仅四个 L1 占比允许带 %。
Cycles、Instructions 或 IPC 带 % 时标记 unrecognized，原文保留，不参与数值复算。
千位分隔必须为三位分组，不接受 1,23 这类歧义。
N/A、not counted、not supported 记录 unavailable。
NaN/Inf 记录 nonfinite，JSON 中 value 为 null，原文仍保留。
不识别的字段值记录 unrecognized。
Cycles/Instructions 非安全整数记 unsafe_count，原文保留，不用舍入值计算。
树形子项、中文本地化、表格列布局及原始 PMU 事件不自动解析。
未知格式不能因“文件非空”视为成功。

## 多窗口选择

每条匹配有 `line-N` ID。
有多个同名项时，脚本不取 first/last，也不平均。
由 agent 阅读窗口标题后选择同一报告块，并在 arithmetic.evidence 中说明。

```json
{
  "Cycles": "line-11",
  "Instructions": "line-12",
  "IPC": "line-13",
  "Bad Speculation": "line-15",
  "Frontend Bound": "line-16",
  "Retiring": "line-17",
  "Backend Bound": "line-18"
}
```

上述 ID 仅示意，必须来自本次 parsed.json。
脚本不能证明人工选取的多行属于同窗；证据核对是交付前必做项。
字段只出现一次也不代表口径自动一致。

## 输出状态

每条 check 含 id/status/detail/evidence。
聚合优先级 invalid > unknown > limited > pass。
missing 为 unknown；已识别字段明确不可用为 invalid。
未知不等于差，表示没有足够证据支持更强判断。
有同窗工作量、非空字符串 work.unit 及证据时输出 cycles_per_unit；invalid 时抑制该派生值。
派生值单位随 cycles_per_unit_unit 输出，例如 cycles/frames；未计算时两者均为 null。
单位缺失或仅有空白时保留原始计数，不计算每单位计数。
其他状态下派生值仍需结合限制解释。
comparison_candidate 只反映本次规则是否全部通过。
comparison_eligible 始终 false，单次脚本不替代人工 A/B 审核。

## CLI 行为

全部脚本仅使用 Node.js 标准库，无 npm 安装步骤。
脚本成功生成产物退出 0，即使质量是 invalid。
参数、输入 JSON 或输出路径错误退出 2。
调用者必须读取 quality_status，不能用退出 0 判断实验成功。
输出文件已存在会失败，防止覆盖历史证据。
日志不会被当作 shell 命令执行。
原始日志可能包含环境信息，按用户指定目录保存，不上传。
