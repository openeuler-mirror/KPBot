# 输入契约与用户交互

## 信息来源

`authorization.user_request` 必须来自用户主动 PGO 指令；`approval.evidence` 记录用户对实验输入的明确说明或对完整方案的接受。已有明确上下文直接沿用，不逐阶段索取确认。二者是审计记录，不是由程序认证的授权令牌。

程序只校验字段和文件；Agent 必须判断数据是否对应用户业务、调用命令是否真的使用所列数据，以及 correctness 是否足以检测错误。不能用 `confirmed:true` 代替真实确认。

## 缺失项的报告方式

一次列出所有当前已知缺口：缺失字段、已发现候选、影响、需用户指定的内容。例如“找到两个 benchmark，但不知道目标是压缩还是解压；请指定数据目录、压缩等级、操作比例、训练轮数”。不得只说“缺少参数”，也不问用户已经明确的信息。

可只读发现，不能运行未知输入的 benchmark。用户没有答复时保持 awaiting_input，不能采用候选或默认业务选择。

## 完整字段

执行器契约统一由 `scripts/lib/contract.mjs` 校验，避免维护重复的 schema。

| 字段 | 类型/说明 |
|---|---|
| version | 固定 1 |
| authorization | explicit_pgo=true，user_request=原始用户指令 |
| approval | confirmed=true，evidence=必要条件已明确的上下文依据 |
| source_dir / run_dir | 绝对路径且互不包含；run_dir 初始化前不能存在 |
| environment | platform=linux，arch=arm64，hostname=选定节点，cpu_scope=部署范围，isolation=实验隔离说明 |
| tools | cc/cxx/cmake/gcov_tool/gcov_dump 的绝对路径，GCC 工具版本一致 |
| build | generator=Unix Makefiles；type=Release/RelWithDebInfo；targets=插桩编译目标；link_targets=最终链接目标；configure_args=明确额外 -D 参数 |
| workload | process_model=single/joined_threads；description=业务与并发说明；env=明确固定环境变量对象 |
| datasets | id/path/sha256/description/user_evidence 数组；每项是文件，不隐式展开目录 |
| training | id/argv/dataset_ids/description/runs 数组；批准次数决定采集工作量 |
| validation | id/argv/dataset_ids/description/weight/max_regression_pct 数组 |
| correctness | argv/description；自校验程序必须失败时非零退出或 correct:false |
| measurement | metric/direction/pairs/warmups/min_gain_pct/max_cv_pct/bootstrap_samples/seed/confidence |
| resources | jobs/timeout_seconds/total_seconds/min_free_bytes，全为批准的正整数 |
| artifacts | build 内 driver 和库的相对路径数组 |
| libraries | target/path/kind=static或shared；静态库额外 link_target=最终链接目标 |

`total_seconds` 从 init 开始按墙钟计时，包括阶段间间隔。长时间搁置后需创建新实验，不偷偷延长预算。CPU 绑定可通过明确的 argv 包装（如已配置的 taskset）实现，但不要擅自更改系统 governor 或 NUMA 策略。

## 命令与输出协议

argv 为数组，支持 `{source}`、`{build}`、`{run}`、`{data:数据id}`。不通过 shell 解释；需要脚本时把脚本保存为已审查的独立文件并显式调用。参数占位在运行时展开。

每个训练、正确性、验证命令 stdout 只能输出一个 JSON 对象：

```json
{"correct":true,"metrics":{"elapsed_ns":123456},"checksum":42}
```

指标名称由用户指定，必须是正有限值。其他日志写 stderr。训练和正确性只要求 correct:true；性能测量还要求 metrics 对应字段。现有 benchmark 输出不兼容时，需要审查一个转换 wrapper，不能伪造 correct:true 或从不明字段猜指标。

`datasets` 中的参数文件也算数据：若运行合成测试，文件应明确随机种子、分布、尺寸等；其业务适用性必须获用户确认。训练/验证不能使用同一文件或仅改名的相同内容。字节不同仍不证明独立性，语义重复或泄漏需 Agent 审查。

## 不提供通用业务默认值

不得把开发测试中的数据或阈值用于用户项目。首版算法要求至少 6 对测量、bootstrap 至少 1000 次、95% 区间；具体值仍需在计划中明确。
