# 独立报告复核

`case.yaml` 的 tier A 入口只运行 `verify.py --self-test`，验证已有校验器能接受正例、拒绝反例。它不启动 agent，不评价模型输出质量，也不提供 skill 有无的 A/B 证据。

需要检查实际分析行为时，在独立工作目录使用 [prompt.md](prompt.md) 的请求和两份 fixture 生成报告。为避免泄漏预期，分析阶段不要提供本清单或校验器自测中的答案。报告完成后，在本 case 目录运行：

```sh
python3 verify.py /absolute/path/to/prefetch-policy.json
```

默认读取 JSON 旁的 `PREFETCH-POLICY.md`；也可通过 `--markdown /absolute/path/to/report.md` 指定文件。自动结果中 `success=true` 只表示结构和固定 fixture 检查通过。`requires_manual_review=true` 表示仍需下面的逐条语义核对，不能据此报告完整验收通过。

## 人工语义检查

- 对照源码逐条核对 `address`、`condition`、`consume_stage`、`prefetch_stage` 和 `notes`。字段存在不代表解释正确。
- Rust L2 指向当前 Entry，第一趟全部遍历完成后才进入第二趟消费；提前量由阶段关系描述。AArch64 空实现不能写成实际发出了 L2 提示。
- Rust L1 指向 `entries[i + 16]`，仅在 `i + 16 < entries.len()` 时执行，后续消费同一 Entry 的 `key`。检查前 16 项、尾部与短输入的覆盖边界；16 的单位是元素。
- C 读封装与写封装分别指向 `values[0]`。核对 `read_one` 的读取、`write_one` 的写入及 `__aarch64__` 条件；两平台均需覆盖。不能混淆读写提示或将 locality 直接当缓存级别。
- 核对 Markdown 与 JSON 每个 finding 的 ID、架构、地址、条件、距离单位、证据和三层状态一致。ID 出现只完成自动关联检查，不代表正文一致。
- 确认报告没有把源码提示意图写成实测缓存驻留、路径执行或性能收益。源码引用应覆盖封装、调用和消费，且两份 fixture 的身份及摘要可追溯。

记录实际复核的报告路径、自动校验结果和上述检查结论；尚未执行的部分明确标为未验证。
