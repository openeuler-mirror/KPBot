# 优化工作流契约一致性审查（2026-10-28）

本次检查针对“生产者生成的 JSON 被同发布校验规则拒绝”和“校验器放行了违反既定门禁的结果”。以 MR !93 的 `605a41de96425c129074cf85c9b78333d6442814` 为检查起点；同时检查仓库其他插件中的同类入口。

## 已复现并修复

| 范围 | 触发条件与原行为 | 修复 |
| --- | --- | --- |
| Faiss CP0 / state schema | 非 Git 发布源码、空仓库或缺少 Git 时生成 `baseline_commit=unknown`，但 schema 拒绝该值 | 显式允许 `unknown`，记录缺失身份的含义；CP2 仍要求非占位 `baseline_id` 与可复现清单 |
| Git 提交身份 | SHA-256 Git 仓库返回 64 位提交，原 schema / 提交校验只接受 SHA-1 长度 | 源码身份与已提交候选支持 SHA-1 / SHA-256，不接受任意文本 |
| Faiss 状态机 | 两个活动阶段仍被判 PASS；CP7 完成但无 verdict 也被判 PASS | 拒绝多活动阶段与缺少终态结论 |
| Faiss 角色交接 | `review-result` 可以缺少结构化 review / verdict | schema 与无可选依赖时的校验均要求 review 对象及有效 verdict |
| 产物路径 | Linux 上 `..\\outside`、Windows 盘符与 UNC 路径可能被当成合法相对路径 | 在任一平台同时识别 POSIX / Windows 路径边界 |
| Faiss 输入结构 | JSON 根或嵌套字段类型错误时，报告 schema 错误后还会抛出异常 | 明确返回 FAIL，避免 traceback 掩盖实际错误 |
| UT/ST 报告 | 布尔值被当作整数用例计数；缺 rationale、未知字段及额外风险检查可能被脚本放行而违反 schema | 对齐结构与类型规则，并保留 READY 的实际测试门禁 |
| app-tuner 结果聚合（上游既有） | 根 JSON 非对象、动作非数组、不可散列键或 manifest 形状错误时，聚合器直接退出而未生成门禁报告 | 保留错误记录并生成受阻报告，不聚合无效角色结果 |
| app-tuner 耗时（上游既有） | Python 的数值 `1` / `1.0` 与 `True` 相等，导致一秒耗时被丢弃 | 用类型区分布尔值，保留有效数值耗时 |
| UTF-8 JSON 交换 | 非 UTF-8 系统区域设置下，Faiss state 与 app-tuner manifest / candidate pool 的默认编码可使下游读取失败 | 显式使用 UTF-8，并用真实 CLI 往返测试验证 |

非 Git 源码可以进入源码调查，但 `unknown` 不代表已建立可复现基线。内置 Faiss 构建辅助脚本需要 Git 工作树；发布归档可使用项目原生构建，按实际版本、归档摘要和产物清单记录身份。

## 检查范围与结果

- 对仓库全部 88 个 Python 文件执行语法分析，对 55 个 JSON 文件执行解析检查。
- 检查 4 个本地正式 JSON schema，以及相应的生成 / 消费入口；marketplace 的外部 schema 引用另行区分，不计作本地 schema。
- 对 Arm intrinsics 的 46 条发布记录执行 schema 校验，确认生成器内置 schema 与发布 schema 一致。
- 检查 Faiss 状态 / 交接、UT/ST、通用实验记录 / 反馈、app-tuner 结果聚合与报告、TF 状态字段约定；Markdown 字段约定与带门禁的机器校验分别核对。
- 新增 27 项契约回归通过，覆盖有 / 无 `jsonschema`、无 Git、空仓库、SHA-1 / SHA-256、非 UTF-8 区域设置、有效和无效输入。
- 原有 49 项技能 / 脚本检查通过；schema 兼容性检查仅允许本次列明的身份范围扩展与 review-result 要求，其他字段、算法冻结项和角色元数据保持兼容。
- 两平台安装回归 19 项通过；通用实验记录 14 项、反馈路由 12 项通过。
- 通用实验记录另有 2 个测试方法（5 个符号链接场景）因当前 Windows 账号缺少创建符号链接的权限而未能执行，不能计作通过。本次未改变系统权限或弱化测试。

这是一轮契约一致性和可复现错误路径审查，没有执行目标硬件上的 Faiss 性能实验，也不据此推断未测试分支均无缺陷。

## 复现命令

```bash
# 安装 jsonschema 后，额外检查正式 schema 与标准库校验器的符合关系。
python3 -m unittest discover -s tests -p 'test_*contract*.py' -v
bash tests/test_install_platform.sh
```

Faiss 状态 / 交接的用例还会调用 `python -S` 覆盖可选 `jsonschema` 不存在的模式。测试只使用临时目录和合成记录；它们不代表真实算法收益或已执行模型行为评估。
