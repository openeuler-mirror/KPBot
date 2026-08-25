# 状态机 JSON Schema（状态持久化）

工作流产物 `baseline_state.json` 与 `optimization_points_state.json` 采用以下机器可读 schema。所有写入/读取必须遵循该 schema，非法状态转移需拦截并显式报错，禁止凭自然语言约定自由发挥。

## 1. baseline_state.json（滚动 baseline）

```json
{
  "initial_baseline": {
    "captured_at": "<ISO8601>",
    "binary": "<BIN_SERVER_OPT>",
    "flags": "--enable_kdnn=true --annc=true --annc_fused_matmul=true",
    "capacity_qps": 310,
    "p99_us": 5450,
    "cpu_pct": 62.4,
    "source": "阶段1 端到端极限测试"
  },
  "current_baseline": {
    "binary": "<当前最新部署 binary>",
    "flags": "...",
    "capacity_qps": 375,
    "p99_us": 5000,
    "cpu_pct": 59.0
  },
  "merged_points": [
    { "point_id": "A", "merged_at": "<ISO8601>", "commit": "<sha>" },
    { "point_id": "QKV", "merged_at": "<ISO8601>", "commit": "<sha>" }
  ]
}
```

字段语义：
- `initial_baseline`：阶段 1 测得的起点（当前已优化状态，非无加速原生版本），**只写一次**。
- `current_baseline`：每个优化点 `accepted` 后升级为最新部署状态。
- `merged_points`：已合入优化点的继承清单（`inherited_changes` 来源），禁止静默回退到 `initial_baseline`。

## 2. optimization_points_state.json（优化点状态机）

```json
{
  "points": [
    {
      "point_id": "QKV",
      "priority": 1,
      "status": "accepted",
      "history": [
        { "from": "designed", "to": "committed", "at": "<ISO8601>" },
        { "from": "committed", "to": "deployed", "at": "<ISO8601>" },
        { "from": "deployed", "to": "accepted", "at": "<ISO8601>" }
      ],
      "design": "results/design/QKV.md",
      "commit": "<sha>",
      "rounds": ["results/rounds/round_1_QKV_summary.json"],
      "result": { "op_level": "-57%", "e2e": "P99 -5.4%", "capacity": "+20%" }
    }
  ]
}
```

### 2.1 状态定义与合法转移

```
designed ──确认──▶ committed ──构建/部署成功──▶ deployed ──验证通过──▶ accepted
   │                  │                            │
   │                  └──构建失败──▶ fix(仍 committed，rebase) 
   │                                           └──验证失败──▶ 负收益根因 → 追加轮次(仍 deployed) / rejected
```

| 状态 | 含义 | 合法下一状态 |
|---|---|---|
| `designed` | 设计提案已产出，待用户确认 | `committed`、`cancelled` |
| `committed` | 代码已 commit，待外部构建 | `deployed`、`fix_required`（构建失败，仍 committed） |
| `deployed` | 已部署，待验证 | `accepted`、`rejected`、追加轮次（仍 deployed） |
| `accepted` | 验证通过，收益确认，升级为新 baseline | 终态 |
| `rejected` | 负收益不可消除，有据可查 | 终态 |
| `cancelled` | 用户中止该点 | 终态 |

**非法转移拦截**：如 `designed → accepted`（跳过 commit/deploy）、`committed → accepted`（未部署直接验收）、`accepted → designed`（回退）等一律报错，需走正确路径。

### 2.2 写回时机

| 事件 | 写回 |
|---|---|
| 设计提案落盘 | 新增 point，`status=designed` |
| 用户确认设计 | 追加 history 记录 → `committed`（code 提交后） |
| 外部构建成功+部署 | → `deployed` |
| 验证结论 | → `accepted`/`rejected`，并同步更新 `baseline_state.json` |
| 负收益追加修复轮次 | 追加 `rounds` 项，`status` 保持 `deployed` |
