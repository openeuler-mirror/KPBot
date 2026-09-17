# KPBot 用例框架规范（case-spec）

> 定义 agent 级用例的目录结构、契约与运行方式，便于任何人按此新增用例。
>
> 配套：`docs/STANDARDS.md`（Skills 规范）· `docs/roadmap.md`（评估/evaluator 规划）

---

## 1. 目标与分层

用例用于看护 Skill 的端到端效果。按依赖分层，四类共享同一套 `case.yaml` 骨架：

| tier | 含义 | 依赖 | 断言 |
| --- | --- | --- | --- |
| `A` | 纯逻辑 / mock 单测 | python/bash 标准库 | 退出码 + 结果字段 |
| `B` | 需工具链 | gcc/binutils 等进镜像 | 退出码 + 编译/结果字段 |
| `C` | 真机 / ISA | BIOS/内核/性能基线（声明式前置） | 退出码 + 结果字段（真机项可 SKIP） |
| `agent` | agent 端到端 | opencode + 模型 + skill + 编译链 | 退出码 + 正确性 + 性能 + skill 调用证据 |

本文档重点约束 `agent` 层。

---

## 2. 目录结构

用例放在被测 skill 目录下，与 skill 同级归属：

```text
skills/<skill>/cases/<case>/
├── case.yaml            # 用例元数据 + 断言（唯一事实来源）
├── Dockerfile           # 环境：工具链 + skill 装入 /staging/skill
├── entrypoint.sh        # 驱动 opencode → 检查产物 → 解析 trace → verify
├── verify.sh            # 编译 baseline/optimized → 跑分 → 写 result.json
├── run_ab.sh            # A/B 对照运行器（with / without）
├── prompts/
│   ├── with-skill.md    # 显式要求调用 skill
│   └── without-skill.md # 同一任务，不提 skill，其余完全一致
├── src/
│   ├── kernel.h         # 待优化函数接口（固定不变）
│   └── kernel.c         # baseline 实现（naive 版）
├── bench/
│   └── main.c           # 正确性参照 + 计时 harness（不信任被测函数）
└── opt/                 # 运行时 agent 产物（kernel_opt.c），不入库
```

构建上下文统一为插件根（`Plugins/code-optimizer/`），`case.yaml` 用相对路径指回。

---

## 3. case.yaml 契约

```yaml
name: agent-optimize-dotproduct
skill: apply-vectorization
tier: agent

image: kpbot/<...>:1.0-openeuler24.03   # 用例镜像
model: deepseek/deepseek-v4-flash       # agent 模型
auth: runtime_mount                     # 密钥运行时注入，绝不进镜像

ab:                                    # A/B 对照（消融）
  conditions: [with, without]
  metric: speedup
  uplift: speedup_with / speedup_without

build:
  context: ../../../..                  # 指向插件根
  dockerfile: skills/<skill>/cases/<case>/Dockerfile

entrypoint: ["/workspace/entrypoint.sh"]

env_requires: []                        # tier C 才填 BIOS/内核/ISA 前置
env_vars:
  MODEL: deepseek/deepseek-v4-flash
  SKILL_MODE: with                      # with | without
  N: 1000000
  ITERS: 200

assert:
  - type: exit_code
    expect: 0
  - type: json_field
    file: results/result.json
    field: success
    expect: true
  - type: json_field
    file: results/result.json
    field: optimized_correct
    expect: 1
  - type: json_field
    file: results/result.json
    field: speedup
    op: gte
    expect: 1.0
```

---

## 4. 运行时契约（容器内 /workspace）

### 4.1 目录约定

| 路径 | 内容 | 谁写 |
| --- | --- | --- |
| `.opencode/skills/` | with 模式运行时接入 skill（from `/staging/skill`） | entrypoint |
| `prompts/` | with/without 两套提示词 | 镜像内只读 |
| `src/` `bench/` | 待优化源码 + 基准 | 镜像内只读（agent 不得改） |
| `opt/kernel_opt.c` | agent 产物 | agent 写 |
| `results/` | 结果证据 | entrypoint / verify |

### 4.2 产物文件

| 文件 | 内容 |
| --- | --- |
| `results/result.json` | 最终结论：success / optimized_correct / 耗时 / speedup / skills_invoked / tools_used |
| `results/trace.jsonl` | opencode `--format json` 原始事件流（机器可读 trace） |
| `results/trace_summary.json` | trace 解析摘要：`tools_used` / `skills_invoked` / `tool_calls` |
| `results/opencode.log` | opencode stderr |
| `results/baseline.out` / `optimized.out` | 两份 C 程序的原始输出 |

### 4.3 result.json 字段（agent 层）

```json
{
  "success": true,
  "skill_mode": "with",
  "optimized_correct": 1,
  "baseline_time_ms": 0.0967,
  "optimized_time_ms": 0.0963,
  "speedup": 1.0041,
  "skills_invoked": ["apply-vectorization"],
  "tools_used": ["skill", "bash", "read", "edit"]
}
```

---

## 5. A/B 对照（消融）

- 同一任务、同一模型、同一判定，唯一变量是「skill 有无 + prompt 是否提 skill」。
- `with`：把 `/staging/skill` 接入 `.opencode/skills/`，prompt 显式要求调用。
- `without`：不接入任何 skill，prompt 只描述任务。
- 运行器：`./run_ab.sh [--save <目录>] [<image>]`，输出客观对照表；`--save` 留存
  `result.json / trace.jsonl / trace_summary.json / opencode.log / kernel_opt.c`。
- 不做统计加工与阈值解读，只展示客观数据；测量可靠性由用例设计者负责。

---

## 6. trace 与证据

- opencode `run --format json` 输出 JSONL 事件流，含 `tool_use` 事件：
  - `tool == "skill"` 且 `input.name` 为 skill 名 → 判定「skill 被调用」。
  - 其余 `tool` 值（bash/read/edit/glob/grep…）反映真实执行流程。
- `entrypoint.sh` 解析后写 `trace_summary.json`，并把 `skills_invoked` / `tools_used`
  并入 `result.json`，供后续 evaluator / 看板 / 归因直接消费（见 `docs/roadmap.md` 10 月评估体系）。

---

## 7. 新增一个 agent 用例的步骤

1. 复制一个现有 case 目录到 `skills/<skill>/cases/<新 case>/`。
2. 改 `src/kernel.{h,c}`（待优化函数）+ `bench/main.c`（参照 + 计时）。
3. 改两份 `prompts/*.md` 的目标函数与优化要点（with/without 只差 skill 提及）。
4. 改 `verify.sh`：baseline 编译 flag、正确性判据、N/ITERS 默认值。
5. 改 `case.yaml`：name/skill/image/env_vars/assert；`run_ab.sh` 顶部 `DEFAULT_IMAGE`。
6. `docker build` 验证，再 `./run_ab.sh` 跑 A/B。

---

## 8. 现状与待办

- [x] 目录结构、case.yaml schema、result.json/trace 契约（本文档）
- [x] A/B 对照（SKILL_MODE + run_ab.sh）
- [x] trace 采集与 skill 调用证据（trace.jsonl / trace_summary.json）
- [ ] 测量可靠性（绑核/预热/多次取中位数）—— 由用例设计者按需在 bench/verify 实现
- [ ] 上层统一执行器（跨用例批量跑 A/B 并汇总）—— 底层稳定后再抽
