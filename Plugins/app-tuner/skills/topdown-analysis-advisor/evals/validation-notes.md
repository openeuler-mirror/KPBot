# 本次验证记录

验证日期：2026-11-20。
环境：Windows，Node.js v24.22.1。
本记录区分已执行检查与尚未执行的硬件验收。

## 第一轮：脚本行为

执行 node --check 与 node --test。
初始 20 项测试通过。
覆盖缺测、多窗口、异常百分比、重复指标、大 PMU 计数和参数检查。
覆盖输出拒绝覆盖、manifest 不一致、非零退出、缺失程序和进程超时。
所有采集行为测试使用模拟进程，没有访问生产目标。

## 第二轮：现有指南样本

只读提取 KPBot-new-new 中 kptools-devkit/references/topdown-guide.md 的指标树示例。
首次检查发现树形前缀中的空格被误认为数值列分隔符。
修正列匹配后新增深层树前缀回归测试。
复测 21 项通过。
现有指南示例识别到 36 条指标。
示例中的省略行“...”产生 unparsed_metric_line，整体为 needs_review。
这是预期行为：省略的指标不能被当作完整采集数据。

## 第三轮：结构与语义

检查 frontmatter 必需字段、名称、描述长度和未完成占位符。
检查入口与 references 中六个实际资源链接均可解析。
检查八个行为评估用例 JSON 可读取且标识不重复。
检查总行数处于用户要求的 1000..2000 行范围。
将兼容性说明放入 metadata，避免标准校验器的顶层字段白名单冲突。
复核不自动推断分母、不把缺测补零、不把采集成功等同根因成立。

## 工具限制

已尝试执行 skill-creator/scripts/quick_validate.py。
环境缺少 PyYAML，标准校验器未能启动；没有将其记为通过。
另行检查了本文件实际使用的 frontmatter 子集及结构。
未安装额外依赖或修改全局 Python 环境。
行为评估 JSON 是后续人工/Agent 验收场景，不宣称已自动执行这些场景。

## 尚需真机验收

- Linux aarch64 上实际 DevKit 版本与输出格式。
- 目标 PID/CPU 范围、PMU 权限和事件可用性。
- 采集开销、真实业务窗口与多 profile 的可比性。
- 当前版本帮助、指标公式及不同平台的差异。

没有真机验收前，不承诺所有 DevKit 版本均兼容。

## PR 审核修订验证（2026-11-24）

- 增加 SIGINT/SIGTERM 处理，等待采集退出并返回 interrupted 状态供 manifest 保存。
- 保留 interrupted_by，区分操作者中断信号与子进程最终终止信号。
- 全部指标缺测时返回 unusable，同时保留原始行及警告。
- 拒绝显式空 PID/CPU 参数，选项与参数值使用相同的目标存在性判据。
- eval_queries.json 改为 skill_name / queries 对象包装，保留八条行为评估场景。
- 补全文档中的 manifest 字段及负退出码、error 的含义。
- node --check 通过，node --test 共 26 项全部通过。
- 信号回归测试在 Windows 使用 process.emit 触发处理器并终止模拟子进程。
- 本次未执行 Linux 内核信号和真实 DevKit 进程组验收，不将模拟测试等同真机验证。
