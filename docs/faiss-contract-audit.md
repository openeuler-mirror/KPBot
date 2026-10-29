# Faiss 技能契约一致性审查（2026-10-29）

审查范围为本次 MR 新增的 15 个 Faiss 相关技能，包括随包的 `vector-experiment-methodology` 和 `vector-ut-st-validation`，以及必要的路由、安装回归和集成文档。技能目录共 95 个文件：80 个 Markdown、5 个 Python、1 个 Bash、9 个 JSON。

## 已复现并修复

| 范围 | 触发条件与原行为 | 修复 |
| --- | --- | --- |
| CP0 / state schema | 非 Git 发布源码、空仓库或缺少 Git 时生成 `baseline_commit=unknown`，但 schema 拒绝该值 | 显式允许 `unknown`；CP2 仍要求非占位 `baseline_id` 与可复现清单 |
| Git 提交身份 | SHA-256 Git 仓库返回 64 位提交，原 schema / 校验只接受 SHA-1 长度 | 支持 SHA-1 / SHA-256 身份，不接受任意文本 |
| 状态机 | 两个活动阶段被判 PASS；CP7 完成但无 verdict 也被判 PASS | 拒绝多活动阶段与缺少终态结论 |
| 角色交接 | `review-result` 可以缺少结构化 review / verdict | schema 与无可选依赖时的校验均要求有效检视结论 |
| 产物路径 | 不同平台对反斜杠、盘符、UNC 和 `..` 的解释不同 | 在任一平台同时识别 POSIX / Windows 路径边界 |
| 输入结构 | JSON 根或嵌套字段类型错误时，校验器可能抛出异常 | 返回明确 FAIL，避免 traceback 掩盖输入错误 |
| UT/ST 报告 | 布尔值被当作整数用例计数；缺 rationale、未知字段和额外风险项可能被放行 | 对齐正式 schema，保留 READY 的实际测试门禁 |
| UTF-8 状态交换 | 非 UTF-8 系统区域设置下，state 写入与下游读取的编码可能不一致 | 显式使用 UTF-8，并用真实 CLI 往返验证 |
| 性能比较 | 布尔值被当作数字、缺失样本被静默忽略，或输出 NaN / Infinity | 每条性能样本都需为有限数值；不完整正确性字段明确标记不可用；无法输出合法数值时拒绝生成报告 |
| 构建清单 | 额外参数或工程强制赋值改变 Release / generic / 显式 KRL，但 manifest 仍写固定值 | 配置后核对实际 CMake cache；不一致时停止构建，保留配置日志，不发布虚假 manifest |

非 Git 源码可以进入调查，但 `unknown` 不是完整基线身份。内置构建辅助脚本使用 Git 工作树及固定的 Release / generic 形状；其他情况使用项目原生构建命令并记录实际版本、源码摘要和构建配置。

## 验证结果

- 检查全部 95 个技能文件的契约与调用关系；5 个运行脚本通过语法检查，9 个 JSON 可解析，3 个正式 schema 有效。
- 30 项 Faiss 相关契约回归通过：13 项状态 / 交接、7 项 UT/ST、7 项性能比较、3 项构建清单。用例覆盖有 / 无 jsonschema、无 Git、空仓库、SHA-1 / SHA-256、编码、路径和无效输入。
- 新增的性能与构建回归在旧实现上可复现失败，修复后通过。构建回归只使用模拟编译器输出，不声称完成真实 Faiss 编译。
- 原有 49 项技能 / 脚本检查与 19 项安装回归通过；冻结字段、角色元数据保持兼容，schema 变化仅包含列明的身份范围与检视结论要求。
- 原始技能目录与 KPBot 中的 95 个文件逐字节一致；MR 最终差异通过 Faiss 范围白名单检查。

## 复现命令

```bash
python3 -m unittest discover -s tests -p 'test_faiss_*contract*.py' -v
python3 -m unittest discover -s tests -p 'test_vector_report_contract.py' -v
bash tests/test_install_platform.sh
```

安装 `jsonschema` 后会执行正式 schema 的一致性检查；状态 / 交接测试另以 `python -S` 覆盖缺少可选依赖的模式。21 个 eval 场景仍为规格，本次未运行模型行为评测或目标硬件性能实验。
