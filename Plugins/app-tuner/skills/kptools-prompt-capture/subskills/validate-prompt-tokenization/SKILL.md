---
name: validate-prompt-tokenization
description: >
  使用已有后端 golden 与本地 tokenizer 核对 prompt 分词一致性，报告比对范围和不一致项。
---

# Prompt 分词一致性检验

## 操作流程

1. 确认已提供 golden 语料或含 golden 的采集分段，以及本地 tokenizer 目录。golden 指后端实际返回的模板后文本与 token ID，不用离线重算值替代。没有 golden 时，只能说明离线计算口径，不能判定线上一致。
2. 优先使用已有语料。使用采集分段时，先确认导出原文的权限，写入受限目录；若只核验评估集，可增加 `--split eval`。以下 `$S` 为父 skill 目录：

```bash
python3 "$S/scripts/export_corpus.py" "$DATA" --format gold --out "$GOLD"
python3 "$S/scripts/check_tokenizer.py" --corpus "$GOLD" --tokenizer "$MODEL" --show 0
```

3. 区分“对 golden 的模板后文本做分词核验”和“从原始请求离线重建模板”。后一种还需部署时的模板及参数，不能只凭前者宣称模板一致。默认不加载自定义 tokenizer 代码；得到明确授权后才使用 `--trust-remote-code`。

## 输出

报告核验的 tokenizer、实际比对条数和不一致条数，并说明未纳入的样本。退出码 1 伴随不一致统计时表示有差异；空语料或加载失败不等于发现不一致。token 数相同不等于 ID 序列相同；正文被脱敏改写后，原 golden 不再是该正文的一致性依据。对外只给统计与限制，原语料留在受限目录。
