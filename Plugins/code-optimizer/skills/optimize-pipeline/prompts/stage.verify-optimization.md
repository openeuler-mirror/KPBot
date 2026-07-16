# ${stage_name}

## 任务
验证优化结果：编译、测试、性能对比。

## 你的角色
verification expert

## 上下文
- applyOptimization 输出：
```json
${context.applyOptimization}
```
- prepareProject repo + baseline：
```json
${context.prepareProject.repo}
```
```json
${context.prepareProject.baseline}
```
- decideOptimization plan（用于获取 strategy 信息）：
```json
${context.decideOptimization}
```
- fixCode 输出（仅重新验证时有值）：
```json
${context.fixCode}
```

## 执行
使用 Skill tool，skill 名称为 `verify-optimization`
输入：applyOptimization 输出 + baseline 数据

若 strategy 为 `autovec-source-transform`，验证仍只使用现有编译、完整测试、hidden checks、性能对比和反汇编/编译器反馈证据；不要引入 IR equivalence、Alive2、长轮次 fuzz 或 optional deep mode。

## 输出格式
返回 JSON 契约：
```json
{
  "function": "<function_name>",
  "optimization_point_id": "<opt_point_id>",
  "compilation": {
    "ok": true,
    "warnings": 0,
    "error": null
  },
  "functional_test": {
    "passed": true,
    "details": "12/12 cases passed"
  },
  "performance": {
    "baseline_ns": 125000,
    "optimized_ns": 42000,
    "speedup": 2.98,
    "regression": false
  },
  "debug_process": {
    "compilation_quick_fix_attempted": false,
    "functional_quick_fix_attempted": false,
    "quick_fix_succeeded": false,
    "fixes_applied": [],
    "final_outcome": "pass|fixed|failed"
  },
  "git": {
    "committed": true,
    "hash": "<commit_hash>",
    "message": "[vectorization] <function_name> - <简短描述>"
  },
  "status": "verified" | "marginal" | "regression" | "failed" | "unverified"
}
```

## 引用 Skill 内容
详见 `skills/verify-optimization/SKILL.md`

## 修复路由说明

当 verify-optimization 输出 `status: "failed"` 时：
- 编译错误或功能测试错误会通过 `compilation.error` 和 `functional_test.details` 传递给下游 fix-code 阶段
- verify-optimization **不执行 git stash**（保留工作区变更供 fix-code 修复）
- fix-code 修复成功后会重新分发 verify-optimization subagent 进行独立验证
