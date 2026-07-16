# apply-vectorization 与其他技能的交互说明

## 1. 角色定位

`apply-vectorization` 只负责：

- 先探测本地环境
- 校验 request
- 判断待优化循环是否真的可安全向量化
- 查询结构化 ISA 知识库与手册
- 在通过前置检查后，选择正确的 `NEON`、`SVE` 或 `SME` 代码形态
- 生成 canonical `vectorization_result`

它不负责：

- Git 提交
- 项目级回归
- 在仓库脚本里直接起 subagent
- 对不安全或不支持的场景强行给出“优化后代码”

## 2. 上游输入

典型上游会提供：

- 目标函数名
- 循环位置
- 依赖关系提示
- `target_arch`

这些信息会先整理成 canonical request JSON，再交给 `apply-vectorization`。

## 3. 下游消费

典型下游会消费：

- `vectorization_result.success`
- `vectorized_code`
- `replacement_kind`
- `application_mode`
- `artifacts`
- `intrinsics_used`
- `safety_checks`
- `error_message`

如果要做本地物化和 benchmark，下游不是另一个 skill，而是仓库脚本链：

1. `materialize_vectorization_result.py`
2. `benchmark_real_source.sh`
3. `benchmark_before_after.sh`

## 4. Codex / subagent 边界

在 Codex 中可以：

- 主会话准备 request JSON
- 主会话用 `query_arm_intrinsics.py` 查条目或校验候选代码片段
- subagent 使用 `$apply-vectorization` 生成 canonical response JSON
- 主会话保存 response JSON
- 主会话把这份 response JSON 交给 `benchmark_model_response.sh`

仓库脚本不能：

- 直接起 Codex subagent
- 在 shell 内假设自己能调用模型

## 5. 职责分工

职责划分是：

- 用户或上游流程提供：待优化源码、目标函数和循环位置
- Codex / subagent 临时生成：response JSON
- benchmark 脚本运行时生成：request JSON、generated C 和 assembly artifacts
- 仓库不保存：优化后快照、参考性能快照

## 6. 替换契约

下游不能把 `vectorized_code` 一律当作循环行段替换。必须先看 `replacement_kind`：

- `translation_unit` 或 `full_function`：使用物化链路写入 `generate/`，或由 pipeline 根据目标函数边界替换完整函数。
- `function_body`：复用原始函数签名，只替换函数体内部。
- `loop_body`：才允许按 `loop_info.start_line/end_line` 替换循环行段。

`application_mode` 默认是 `materialize_to_generate`，表示不覆盖用户源码。只有上层明确要求原地修改，且替换边界与 `replacement_kind` 匹配时，才使用 `inplace_replace`。

`artifacts` 是 pipeline 输出的一部分，不能只停留在 apply-vectorization 内部。特别是 `assembly` 形态，C wrapper 和 `.S/.s/.asm` artifact 必须一起进入编译、链接、产物记录和最终报告。

## 7. 什么时候该停

出现这些情况时，`apply-vectorization` 自己就该停：

- 环境探测失败
- request 不合法
- 间接寻址或 scatter/gather，除非 SVE 路径已经通过 `semantic_contract` 证明索引只读、边界内、scatter 无重复且无别名冲突
- 跨迭代依赖
- 已存在 intrinsics、inline asm 或 standalone assembly 但无法证明约束、clobber、ABI、尾处理或符号边界
- 显式 `codegen_style` 与源码形态冲突
- `SME ZA/tile` 语义不明确
