---
title: 归档与运行连续性
type: skill-reference
---

# 归档与运行连续性

## 上下文定位

- 先遵守当前 Vault 的 `AGENTS.md`。默认使用当前工作区，用户指定 Vault 时使用指定位置。
- 调度器提供 Automation ID 时先读 `$CODEX_HOME/automations/<id>/memory.md`，缺失可创建。`CODEX_HOME` 未设置时可定位 `~/.codex`。不要因为本技能来源于某自动化便在普通调用中使用固定 ID。
- 手动运行无自动化 ID 时，读取本目录索引与最新成功报告；连续性记录写在该报告frontmatter/证据台账。不得擅自推进不相关自动化的水位。
- 配置/范围变更、技能创建、commit/push不是报告运行，不修改 `last_success_report_generated_at`。

## 时间与去重

建议记录这些字段，兼容已有命名：

```yaml
generated_at: 'YYYY-MM-DDTHH:mm:ss+08:00'
coverage_start: 'YYYY-MM-DDTHH:mm:ss+08:00'
coverage_end: 'YYYY-MM-DDTHH:mm:ss+08:00'
retrieved_from: 'YYYY-MM-DDTHH:mm:ss+08:00'
retrieved_to: 'YYYY-MM-DDTHH:mm:ss+08:00'
timezone: Asia/Shanghai
```

正式窗口 `(上次成功生成时间, 本次启动时间]`；用较宽查询抓取边界，再按精确事件时间过滤并去重。上期 `coverage_end→generated_at` 单列补漏，避免研究制作期间丢事件。提交/PR/release/公告各自记录ID，release累计说明不重复计旧合入。

保留不完整分页、默认分支外refs、日期精度等覆盖缺口。报告内容截止与实际检索时间分开；检索晚于截止不让未来事件混入本期。增补版继承原窗口和生成水位，注明增补时间。

## 产物路径

默认沿用现有归档布局，避免技能改名产生第二套目录：

```text
20_Knowledge/AI Infra/Community Insights/<YYYY>/
  <YYYY-MM-DD-HHmm>-inference-community-insights.md
  <YYYY-MM-DD-HHmm>-inference-community-insights.html
  <YYYY-MM-DD-HHmm>-inference-community-insights-evidence.md
```

同一批产物使用共同stem。任何目标已存在时，为新批次选择 `-v2`、`-v3` 等共同stem，不覆盖旧报告。MD先完成，再派生HTML；需要补充已有报告时保留原文并新建增补版本。

正文、证据与索引使用 YAML frontmatter。证据台账存 Markdown，保留URL、SHA、时间与核验结论；不要将运行临时JSON转成另一套权威事实源。

更新 `Community Insights Index.md`：Obsidian双链到MD、HTML、证据，日期、窗口及3–5条主要发现；纳入有实质变化的Mooncake/3FS和三家模型生态。核根或实际 `Knowledge Index.md` 已有 `[[Community Insights Index]]`，缺失才增补；不重写已有内容、不清理无关改动。

## 成功水位与失败处理

成功意味着MD/HTML/证据可读、归档和索引完成且必要验证通过。之后记录运行时间、成功生成时间、覆盖窗、产物绝对路径、结论、采集范围/缺口、下一期问题与来源。

- 写入失败：不推进水位；保留已生成文件并说明缺少步骤，重跑先查这些文件。
- 部分来源不可用：仍可归档有明确限制的报告，记未完成来源/分页以供追补；不能把缺口记为0事件。
- 全网不可用：可保存失败记录或离线草稿，但不标记成功研究报告，不跳过未覆盖窗口。
- 无实质更新但已执行检查：正常生成简短成功报告。
- 只在确有写入/全网/授权阻塞时提醒用户处理；普通证据缺口作为报告限制呈现。

## 后续维护

按六社区及三家×五类保存来源、最近事件ID、版本、未决状态、上次核验和下一步。稳定事实进入专题知识页时遵守Vault模板和双链规则；不要把每期重复基线包装成新发现。
