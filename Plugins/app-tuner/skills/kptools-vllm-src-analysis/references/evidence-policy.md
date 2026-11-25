# 证据与口径规则

本 skill 的输出面向“版本问题”，最容易犯的错是**用一个版本号概括引入过程**，
以及**把 release note 的宣称当成实测**。以下规则必须遵守。

## 1. “哪版引入”必须四态

| 状态 | 字段 | 说明 |
|---|---|---|
| 代码引入 | `introduced_code` | 提交级：commit + 日期 + subject |
| 首个发布包含 | `first_release` | tag 维度；与代码合入时间可能不同 |
| 默认开启 | `default_on` | 需要显式给 `--default-regex`；否则 unknown |
| 移除 | `removed` | 首个不再包含它的 tag；未移除为 null |

- 只回答其中一个状态 = 不完整回答。
- `default_on` 不能靠“开关存在”推断，必须看到默认值或决策逻辑。
- 状态之间可以有长间隔（例：ModelRunner V2 代码引入 2025-12-18，
  首个 release v0.13.0rc1 2025-12-27，至 v0.30.0rc1 仍未默认开启）。

## 2. 证据分级

| 等级 | 允许的表述 | 禁止 |
|---|---|---|
| `measured` | “实测提升 X%”（附 benchmark 路径/报告） | 无 |
| `claimed_in_release_notes` | “该版本 release notes 宣称…”（附 PR） | 写成“实测提升” |
| `inferred_from_commit_subject` | “提交显示改动了 MoE/NPU 路径” | 推断幅度、推断因果 |

- release notes 的 `Performance` 只是条目，不等于数字；没有实测不要写百分比。
- `digest.py` 的 subsystem / perf_dim 是关键词分类，只作线索。
- 无法归类的提交计入 `unclassified`，不要硬塞。

## 3. 绑定版本

- 源码锚点（`anchors.py`）只对指定 tag 成立；回答时必须带 `version` + `commit`。
- 生产 trace 若来自内部补丁版，行号会整体漂移；需用户提供部署版源码或 offset，
  不允许把社区 tag 的行号直接套到补丁版 trace 上。
- trace 版本不明时，先看 `profiler_info_*.json` 或向用户确认，再选锚点。

## 4. 趋势

- 至少 3 个版本的数据点，且每个点有证据，才能给趋势。
- 趋势必须标注为推断，并区分“已发生”（版本事实）与“可能继续”（推断）。
- 不预测未发布版本的功能或性能；官方 roadmap 只作引用，不作结论。

## 5. 失败与未知

- 输入仓库不可读、版本不存在、release notes 缺条目：输出 `unknown` / `found=false`
  + 可用版本列表 + 降级原因，不静默失败。
- 任何脚本错误都输出 `{"schema_version":1,"status":"error","error":"..."}`。

## 6. 与 profiling 分析的接口

分析 skill 需要源码位置时，消费 `anchors_<version>.json` 的：

```json
{"path": "...", "classes": [{"name": "...", "line": 0,
  "bases": ["..."], "methods": [{"name": "...", "line": 0}]}]}
```

若调用栈里的函数在锚点表中找不到，报 `unresolved`，不要根据名字猜文件。
