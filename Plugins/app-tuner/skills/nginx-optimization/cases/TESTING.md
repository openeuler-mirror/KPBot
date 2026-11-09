# nginx-optimization 用例运行指南

本目录是 `nginx-optimization` skill 的 agent 级 A/B 用例集，结构与断言协议遵循
`docs/case-spec.md`；构建/运行自包含，不改动 KPBot 核心。

## 用例清单

| 用例 | tier | 验证目标 |
|---|---|---|
| `agent-scene-config` | agent | 场景四要素识别与 nginx.conf 关键决策（worker 数、session 缓存、keepalive_requests、reuseport、ECDSA、二进制绑核掩码、压测纪律） |
| `agent-tuning-loop` | agent | 调优循环纪律：baseline → 严格单变量 → 复测 → before/after → 保留/回退（mock 压测工具 + 结构化调用日志） |

## 运行方式

在 KPBot 插件根目录（包含 `skills/` 的目录）：

```bash
# 一键全部用例（发现 cases/*/case.yaml → build → run_ab → 断言评估）
bash skills/nginx-optimization/cases/run_all.sh

# 常用选项
bash skills/nginx-optimization/cases/run_all.sh --list          # 列出用例
bash skills/nginx-optimization/cases/run_all.sh --case agent-scene-config
bash skills/nginx-optimization/cases/run_all.sh --strict        # 门禁：with 成功 + 真实调用 skill + with>without
bash skills/nginx-optimization/cases/run_all.sh --dry-run       # 只打印命令
bash skills/nginx-optimization/cases/run_all.sh --lenient       # 汇总但总退出 0
```

单个用例手动双臂：

```bash
bash skills/nginx-optimization/cases/agent-scene-config/run_ab.sh --save results-ab
```

## 环境要求

- docker（build 上下文为插件根目录；基础镜像 `openeuler/openeuler:24.03-lts`）
- opencode 二进制与 auth.json（运行时挂载进容器，见 `run_ab.sh` 的 `OPENCODE_BIN`/`OPENCODE_AUTH`）
- 容器可访问模型 API（必要时 `--network host`）
- **python3 + PyYAML 为硬依赖**：缺失时 `run_all.sh` 明确报错退出，绝不静默跳过断言

## 断言语义（case.yaml）

- `exit_code`：with 臂容器退出码
- `json_field`（file 固定为 with 臂 `results/result.json`，字段由 verify.sh 写入）：
  - `success` / `one_variable`：verify 检查结论
  - `skills_invoked` + `op: contains`：**硬门禁**——with 臂必须真实调用过
    `nginx-optimization`（证据来自 entrypoint 对 trace 的解析并合并进 result.json），
    未调用时该 case 判 `invalid` 而非 `pass`，不产生 uplift 证据
- `ab.metric: assertion_pass_rate`、`ab.uplift: pass_rate_with / pass_rate_without`：
  `--strict` 据此真实比较 `with > without`（帮助文本与实现一致）

## 产物

每个用例双臂运行后保存：

- `<out>/<case>/with.json` / `without.json`：各臂 result.json（含 skills_invoked）
- `<out>/<case>/with.decisions.md`（或 `with.report.md`）：agent 产出物
- `<out>/<case>/with.trace.json`：工具调用与 skill 调用证据
- `<out>/<case>/summary.json`：双臂对照与 pass_rate/uplift

旧产物目录改为 `<dir>.bak.<时间戳>` 备份（清理动作可见），不会 `rm -rf` 未校验路径。

## 内置检视加固（相对早期 skill 用例的教训）

1. **评分材料与被测对象分离**：Dockerfile 只 `COPY SKILL.md + references/`，
   `evals/`（评分答案）与 `cases/` 不进镜像、不进 agent 工作区。
2. **mock 协议健壮**：`mock_wrk.sh` 每次调用写一行结构化 JSON
   （`{"argv":[...], "parsed":{...}, "unparsed":[...], "config_lines":[...]}`）；
   无参 baseline 记录空数组而非空行；`--key=value` 与 `--key value` 均支持；
   无法识别的参数记入 `unparsed` 并告警，不静默忽略。
3. **PyYAML 硬依赖** + 断言总数为 0 视为 fail（`unknown` ≠ `pass`）。
4. **--strict 真比较**：with 成功 + skill 真实被调用 + with assertion_pass_rate >
   without；`skills_invoked` 为空时判 `invalid`。
5. **输出目录安全**：`--out` 必须绝对路径；case `name` 缺失跳过并报错（不会拼出
   `OUT/` 空名导致误删输出根）；旧目录时间戳备份。

## 已知边界

- 两个用例均为 agent 级，需要 opencode + 模型 API 才能真正执行；CI 中可先跑
  `--dry-run` 验证结构。
- `agent-tuning-loop` 的 mock 指标是确定性合成值（由 nginx.conf 关键项决定），
  只验证"纪律"而非真实性能；真实收益数据见 SKILL.md 引用的鲲鹏实测记录。
- `agent-scene-config` 的决策检查项均来自经真机验证的 skill 内容；若 skill 决策表
  更新，需同步 verify.sh 的检查清单。
