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

- prepareProject 完整输出（必须保留 verification_commands / numeric_constraints / target_machine）：
```json
${context.prepareProject}
```
- 假设与实验引用：${context.designExperiment}
- 诊断反馈：${context.validateExperiment}
- 运行目录：${context.run_dir}

## 执行
使用 Skill tool，skill 名称为 `verify-optimization`
输入：上述完整上下文；执行用户/项目约定的全部构建、完整正确性与系统 benchmark 命令。局部性能不能替代系统验收。叶子不提交，协调者经 --verify-result 校验 Deliver 后再精确提交。

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
  "correctness_verdict": "passed",
  "local_performance_verdict": "not_run",
  "system_acceptance": "passed",
  "evidence_refs": ["candidates/<candidate_id>/raw/results.json"],
  "commands": [{"command": "<原验收命令>", "exit_code": 0}],
  "local_performance": {},
  "performance": {
    "direction": "lower",
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
    "committed": false,
    "hash": null,
    "message": "[vectorization] <function_name> - <简短描述>"
  },
  "status": "verified" | "regression" | "failed" | "unverified"
}
```

## 引用 Skill 内容
详见 `skills/verify-optimization/SKILL.md`

## 验收与路由

三个 verdict 只用 passed/failed/inconclusive/not_run。higher 指标 speedup=candidate/baseline，lower 指标 speedup=baseline/candidate；零、非有限数、缺失或不可比指标设 null、verdict=inconclusive。旧 performance 为系统结果，局部数据单列；无完整测试不能 correctness=passed，无目标系统 benchmark 不能 system_acceptance=passed。

旧 status 按优先级映射：编译/功能失败 failed；可靠系统性能退化 regression；完整正确性与系统验收均 passed 才 verified；其余 unverified。不再生成 marginal；有矛盾的旧 status 不能 Deliver。未运行的 compilation/functional_test/performance 用空对象与原因，不填假通过值或 null。

编译或正确性失败 → FixCode；性能退化/未验收 → ValidateExperiment（保留候选 patch/目录、parent_state、原始日志与新证据）；修复后仍需 fresh Verify 完整重验。目标不可用明确 awaiting_target_evidence，不在开发机替代验收。禁止全树清理或 stash；只在完整验收与 CLI Deliver 后由协调者晋升父状态。
