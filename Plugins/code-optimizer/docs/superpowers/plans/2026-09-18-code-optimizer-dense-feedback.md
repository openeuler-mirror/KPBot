# Code Optimizer Dense Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 为 code-optimizer 增加“假设与实验设计”“实验验证与反馈”两个环节及实验记录，让优化失败能够修正假设。

**Architecture:** 保留现有阶段与叶子 skills，新增两个阶段模板；在现有 orchestrator 下添加记录与路由辅助脚本。Agent 在目标环境使用现有执行工具运行实验，辅助脚本校验记录及决定下一阶段，不建设远程执行服务或新 benchmark 框架。

**Tech Stack:** Markdown 阶段模板、Python 3 标准库、unittest、现有 Shell 工具和 Claude/OpenCode 安装机制；不新增第三方 Python 依赖。

**Spec:** [已确认的设计范围 v3](../../self-evolution-plan.md)。执行时先读设计范围，再按本计划开发。

## Global Constraints

- 本期仅修改 code-optimizer，不改 app-tuner 和 TF 专项。
- dotproduct 仅为已有示例，不作为本期测试入口或主要验收案例。
- 不使用、不改造 evolve-skill；优化方法库另立后续计划。
- 正式运行时由 Agent 在目标环境调用实验工具并读取结果；手工提供历史结果仅用于开发和回放。
- 当前开发机器不承担鲲鹏性能验收；本机检查只证明契约、记录和路由正确。
- 局部实验不能替代项目完整正确性与用户约定工作负载的最终验收。
- 保留现有编排与叶子 skills，通过阶段模板和小型辅助脚本接入，不建设新运行时平台。
- 主源与已存在的 OpenCode 覆盖文件保持语义一致。
- 原始证据、模型解释与经实验支持的结论分别记录；采样缺失不能证明未执行，理论估算不能冒充实测。
- 目标环境不可用时返回 awaiting_target_evidence，不在本机用替代性能结果宣告成功。

---

## 先看任务清单

| 任务 | 交付 | 修改重点 |
| --- | --- | --- |
| 1 | 实验记录与证据校验 | 新增 record 脚本、契约和逻辑测试 |
| 2 | 假设与实验设计 | 新阶段模板；准备、分析阶段提供必要信息 |
| 3 | 实验验证与反馈 | 新阶段模板；证据检查、预算与路由 |
| 4 | 接入优化流程 | 编排、优化决策、验证和失败回路 |
| 5 | 两平台交付及回放验收 | 安装产物、记录回放、真实场景验收指引 |

依赖：1 → 2 → 3 → 4 → 5。已于 2026-09-20 完成本期实现、本机检查及独立审查，详见 [实施记录](../../implementation-results.md)。真实历史回放和鲲鹏实测保持 pending。

## 路径和文件职责

工作仓库为 `/Users/moxiujuan/Code/openEuler/features_dev/KPBot_self_evolve/KPBot`，当前分支 `self_evolve`，检查基线 `ac60a29`。下文所有文件路径、Python 测试命令均相对 `Plugins/code-optimizer/`，除明确写出仓库根目录的命令。

新增：

- `skills/kpbot-code-optimizer/references/experiment-contract.md`：共享字段、证据规则、恢复及路由表。
- `skills/kpbot-code-optimizer/scripts/experiment_record.py`：原始记录校验、路径/身份检查、追加保存。
- `skills/kpbot-code-optimizer/scripts/experiment_feedback.py`：反馈状态校验、确定性路由及 CLI。
- `skills/kpbot-code-optimizer/prompts/stage.design-experiment.md`：假设与实验设计阶段。
- `skills/kpbot-code-optimizer/prompts/stage.validate-experiment.md`：目标环境实验执行和反馈阶段。
- `tests/feedback/test_record.py`、`test_feedback.py`、`test_platform.py`：与 ISA 无关的验证。
- `docs/feedback-replay.md`：历史证据回放与目标验收步骤。

不新增顶层 skill，不调整 skill 数量，不自动修改 `evolve-skill`，不创建方法库。

## Task 1：实现最小实验记录

**Files:** 创建 `references/experiment-contract.md`、`scripts/experiment_record.py`（均位于 `skills/kpbot-code-optimizer/`）；创建 `tests/feedback/test_record.py`。

**Interfaces:**

- `validate_record(record: dict, evidence_root: pathlib.Path) -> None`：失败抛 `ValueError`。
- `write_record(record: dict, run_dir: pathlib.Path) -> pathlib.Path`：校验后写入 `experiments/<experiment_id>/record.json`，已有记录拒绝覆盖。
- 后续阶段 CLI：`python3 scripts/experiment_record.py --record INPUT --run-dir RUN`；成功打印保存路径，错误打印原因并退出非零。INPUT 引用的原始证据已经存放在 RUN 下。

- [x] **Step 1 — 写契约及独立的最小测试记录。** 测试模块定义以下辅助函数；它是明确标记的逻辑 fixture，不是目标性能证据。

```python
from pathlib import Path

def sample(root: Path) -> dict:
    (root / 'raw.txt').write_text('fixture: path observation\n')
    return {
        'schema_version': 1, 'run_id': 'r1', 'point_id': 'p1',
        'experiment_id': 'e1', 'hypothesis_id': 'h1-v1',
        'parent_state': 'source-digest-fixture',
        'execution_context': {'target': 'fixture', 'build': 'fixture',
                              'workload': 'fixture'},
        'hypothesis': {'claim': '运行时选择了变体 A',
                       'alternative': '运行时选择了变体 B',
                       'support_if': '直接执行记录包含 A',
                       'refute_if': '直接执行记录包含 B'},
        'experiment': {'command': ['fixture-only'],
                       'controlled': ['相同输入与构建'],
                       'changed': '仅观察实际分派', 'timeout_seconds': 600},
        'evidence': [{'path': 'raw.txt', 'scope': 'request',
                      'kind': 'observation', 'source': 'fixture'}],
    }
```

契约补充精确定义：ID 仅允许字母、数字、下划线和连字符；正文字符串不得空；执行条件含目标型号标识、实际构建参数和工作负载身份；parent_state 必须代表实际源码（含未提交修改），不能默认只用 HEAD。`kind` 只允许 observation/estimate；每条证据标注 scope 与 source（target/fixture/historical）。历史证据只有条件可核验时可供当前判断，fixture 永远不能作为目标实测。

- [x] **Step 2 — 写失败测试并运行。** 将上述 helper 与下列测试写入 `test_record.py`，测试引用尚未存在的实现。

```python
import tempfile
import unittest
from experiment_record import validate_record, write_record

class RecordTests(unittest.TestCase):
    def test_duplicate_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            r = sample(root)
            self.assertTrue(write_record(r, root).is_file())
            with self.assertRaises(ValueError):
                write_record(r, root)

    def test_missing_or_outside_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for path in ('missing.txt', '../outside.txt', '/etc/hosts'):
                r = sample(root)
                r['evidence'][0]['path'] = path
                with self.assertRaises(ValueError):
                    validate_record(r, root)

    def test_hypothesis_requires_refutation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            r = sample(root)
            del r['hypothesis']['refute_if']
            with self.assertRaises(ValueError):
                validate_record(r, root)
```

Run: `PYTHONPATH=skills/kpbot-code-optimizer/scripts python3 -m unittest discover -s tests/feedback -p test_record.py -v`。
Expected：首次为模块缺失；完成实现后全部 PASS。

- [x] **Step 3 — 实现字段校验、证据校验和保存。** 使用显式字段表校验上面的所有必填字段与枚举，错误统一为 ValueError；未知 schema_version 拒绝；timeout 必须为正整数且拒绝 bool。`evidence` 可以为空以保存尚未执行的设计记录，但后续不能据此判断 supported/refuted。路径校验核心如下：

```python
root = evidence_root.resolve()
for item in record['evidence']:
    raw = Path(item['path'])
    resolved = (root / raw).resolve()
    if raw.is_absolute() or root not in resolved.parents or not resolved.is_file():
        raise ValueError('invalid evidence path')
```

保存核心（在 validate_record 成功后使用）：

```python
folder = run_dir / 'experiments' / record['experiment_id']
folder.parent.mkdir(parents=True, exist_ok=True)
try:
    folder.mkdir()
except FileExistsError as exc:
    raise ValueError('experiment already exists') from exc
path = folder / 'record.json'
path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
return path
```

加入 `argparse` 的 `--record/--run-dir` 入口；JSON 解析、目录和写入异常返回非零；不执行 record 中的 command。失败写入目录保留并报告为不完整记录，不将其用于恢复或采纳。

- [x] **Step 4 — 运行 Step 2 命令，补充 unknown schema、非法 ID、空条件、符号链接逃逸测试后再次运行。** 用不同实验 ID 引用同一父状态应通过，重复 ID 必须失败。
- [x] **Step 5 — 审查并提交本任务文件。** 建议提交信息：`feat(code-optimizer): record diagnostic experiments`。只暂存本任务文件，不提交用户其他改动。

## Task 2：增加“假设与实验设计”阶段

**Files:** 新建 `skills/kpbot-code-optimizer/prompts/stage.design-experiment.md`。修改 `skills/prepare-project/SKILL.md`、`skills/analyze-hotspot/SKILL.md`、`skills/analyze-caller-context/SKILL.md`。同步已有覆盖 `opencode/prepare-project/SKILL.md`、`opencode/analyze-hotspot/SKILL.md`。

**Interfaces:** 输入 `context.prepareProject`、`context.analyzeHotspot`、可选 `context.analyzeCallerContext`、`context.current_optimization_point`；输出 `context.designExperiment`，其 record 结构严格采用 Task 1，执行前 evidence 可为空。准备阶段增加 `diagnostic_budget={max_experiments:3, command_timeout_seconds:600}`，可以由任务目标明确覆盖。

- [x] **Step 1 — 将 Task 1 的 refutation 测试扩展为缺少 controlled/build/workload 时拒绝。** 使用 `del r['experiment']['controlled']`、`del r['execution_context']['build']`、`del r['execution_context']['workload']` 各一项；运行 Task 1 命令。缺项已被 Task 1 覆盖则应保持绿，不制造假红。
- [x] **Step 2 — 写新模板，使用下列正文作为核心要求。** 模板引用同级 `../references/experiment-contract.md`；由协调者把目标 run_dir 一起传入。

```markdown
## 任务
对当前优化点提出可反驳假设并设计最小区分实验。不要直接实施最终优化。

## 输出要求
按 experiment-contract.md 输出记录：观察依据、claim、alternative、support_if、refute_if；明确目标执行路径、输入与构建条件、controlled、changed、命令及超时。
每个实验回答一个问题；可仅观察或做受控候选变更。无需为每个问题采集全部工具。
已有可比原始证据时引用它，避免重复执行。理论估算标为 estimate，不能写成实测。
临时探针或候选变更须在独立目录进行，记录来源和恢复方式，不污染已验收父状态。
```

- [x] **Step 3 — 更新准备/分析阶段的输出与推断规则。** PrepareProject 输出目标机器、验证命令、数值约束和诊断预算；AnalyzeHotspot 输出 observations/possible_causes/missing_evidence，并保留 optimization_points 兼容。caller 输出实际可确认的输入/分派条件和未覆盖条件。

将“采样缺失证明未调用”“命令级计数器归属于当前函数”“理论周期等于实测”改为需后续验证的假设；保留已存在的分析工具和策略。无目标机器时只完成静态分析，不请求用户为了流程固定补齐一切信息。

- [x] **Step 4 — 用两个固定记录检查模板输出。** 记录一仅含理论估算，结果只能为待验证假设；记录二包含相同运行条件下已有直接观测，允许复用但必须保留引用。人工核对输出契约，记录这是模板审查，不冒充真实 Agent 性能测试。
- [x] **Step 5 — 提交以上文件。** 建议：`feat(code-optimizer): design falsifiable experiments`。

## Task 3：增加“实验验证与反馈”阶段及路由工具

**Files:** 新建 `skills/kpbot-code-optimizer/prompts/stage.validate-experiment.md`、`scripts/experiment_feedback.py`（同 skill）；新增 `tests/feedback/test_feedback.py`；扩展 experiment-contract。

**Interfaces:**

- `choose_next(feedback: dict, remaining: int) -> str`。
- feedback 必含 `execution_status`（completed/failed/awaiting_target_evidence）、`hypothesis_verdict`（supported/refuted/inconclusive/invalid）、`evidence_checked: bool`、`target_checked: bool`、`explanation: str`、`record_path: str`。布尔字段由校验过程生成，不能仅信任 Agent 填入。
- CLI：`python3 scripts/experiment_feedback.py --record RECORD --feedback FEEDBACK --run-dir RUN --remaining N`；读取并调用 Task 1 校验；按记录核验 source、目标与当前配置可比性，缺项不设置 checked；输出 next_stage/reason。completed 下 supported/refuted 至少有一条有效 observation，estimate/fixture 不足以建立目标验证。

- [x] **Step 1 — 写路由测试并运行。** 测试记录为纯逻辑 fixture，不是性能入口。

```python
import unittest
from experiment_feedback import choose_next

class FeedbackTests(unittest.TestCase):
    def test_routes(self):
        def f(verdict, status='completed'):
            return dict(execution_status=status, hypothesis_verdict=verdict,
                        evidence_checked=True, target_checked=True)
        self.assertEqual(choose_next(f('supported'), 0), 'DecideOptimization')
        self.assertEqual(choose_next(f('refuted'), 2), 'AnalyzeHotspot')
        self.assertEqual(choose_next(f('inconclusive'), 2), 'DesignExperiment')
        self.assertEqual(choose_next(f('inconclusive'), 0), 'stop_budget')
        self.assertEqual(choose_next(f('invalid'), 2), 'blocked_evidence')
        self.assertEqual(choose_next(f('inconclusive', 'awaiting_target_evidence'), 2),
                         'awaiting_target_evidence')
        bad = f('supported'); bad['evidence_checked'] = False
        self.assertEqual(choose_next(bad, 2), 'blocked_evidence')
```

Run: `PYTHONPATH=skills/kpbot-code-optimizer/scripts python3 -m unittest discover -s tests/feedback -p test_feedback.py -v`。
Expected：首次模块缺失；实现后 PASS。

- [x] **Step 2 — 实现严格枚举/类型检查和核心分支。** 非法枚举、remaining 负数或 bool 抛 ValueError；以下为校验后的路由核心，不能把该函数视为自动因果证明器。

```python
if feedback['execution_status'] == 'awaiting_target_evidence':
    return 'awaiting_target_evidence'
if feedback['execution_status'] == 'failed':
    return 'blocked_execution'
if not feedback['evidence_checked'] or not feedback['target_checked']:
    return 'blocked_evidence'
v = feedback['hypothesis_verdict']
if v == 'invalid':
    return 'blocked_evidence'
if v == 'supported':
    return 'DecideOptimization'
if remaining == 0:
    return 'stop_budget'
return 'AnalyzeHotspot' if v == 'refuted' else 'DesignExperiment'
```

诊断命令执行失败不直接转 FixCode：先区分工具/环境故障与优化代码错误。只有后续 VerifyOptimization 明确报告候选编译/功能失败时才转 FixCode。

- [x] **Step 3 — 写验证阶段模板。** 核心内容如下：

```markdown
确认目标机器、源码父状态、构建参数和输入条件与设计一致。
目标环境不匹配或工具不可用时返回 awaiting_target_evidence/failed，不在当前机器测替代性能。
执行已设计的命令；执行前扣减实验额度，使用规定超时；保存原始结果、实际条件与命令退出码。
复用已有结果时核对身份和条件，不消耗新的执行额度。
对照预先声明的 support_if/refute_if 给出 supported/refuted/inconclusive/invalid，并逐项引用原始证据。
描述观察与解释的区别；相关性只支持提出假设，不能单独确认原因。
写入实验记录，调用反馈 CLI 得到下一阶段。不要提交代码或修改默认 skill。
```

- [x] **Step 4 — 补充集成验证。** 原始文件不存在、fixture 冒充 target、错误目标标识、unknown verdict、命令失败、负预算分别用临时文件调用 CLI，断言不能输出 DecideOptimization。执行状态缺失时返回错误，不隐式认为 completed。
- [x] **Step 5 — 提交本任务文件。** 建议：`feat(code-optimizer): validate experiments and route feedback`。

## Task 4：接入现有流程并保留完整验收

**Files:** 扩展 `skills/kpbot-code-optimizer/scripts/experiment_feedback.py` 和 `tests/feedback/test_feedback.py`；修改 `skills/kpbot-code-optimizer/SKILL.md`；`skills/decide-optimization/SKILL.md`、`skills/verify-optimization/SKILL.md`、`skills/fix-code/SKILL.md`；`skills/kpbot-code-optimizer/prompts/stage.decide-optimization.md`、`stage.verify-optimization.md`、`stage.fix-code.md`、`partials/final-summary.md`。同步 `opencode/` 下这四个 SKILL.md 的实际覆盖版本。

**Interfaces:** 新状态 `context.designExperiment`、`context.validateExperiment`、`context.diagnosticAttempts`；保留既有 context 与旧输出字段。优化验证增加 `correctness_verdict`、`local_performance_verdict`、`system_acceptance`，值为 passed/failed/inconclusive/not_run。新增 `route_verification(result: dict) -> str`，将候选验证结果映射到 FixCode/ValidateExperiment/Deliver。

- [x] **Step 1 — 写路由表驱动用例。** 为同一优化点连续给出 inconclusive→refuted→supported，断言先设计、再重分析、最后才 Decide；追加性能退化结果必须回 ValidateExperiment；编译或功能失败必须进入 FixCode。诊断部分调用 Task 3 的 choose_next；候选验证路由使用下面的新函数测试，不检查无意义的正文关键词：

```python
from experiment_feedback import route_verification

class VerificationRouteTests(unittest.TestCase):
    def test_candidate_routes(self):
        self.assertEqual(route_verification({'compilation': {'ok': False}}), 'FixCode')
        self.assertEqual(route_verification({'functional_test': {'passed': False}}), 'FixCode')
        self.assertEqual(route_verification({'status': 'regression'}), 'ValidateExperiment')
        self.assertEqual(route_verification({'local_performance_verdict': 'passed',
                                            'system_acceptance': 'not_run'}), 'ValidateExperiment')
        self.assertEqual(route_verification({'correctness_verdict': 'passed',
                                            'system_acceptance': 'passed'}), 'Deliver')
```

按 Task 3 命令先确认新函数缺失导致失败，然后实现：

```python
def route_verification(result: dict) -> str:
    if (result.get('compilation') or {}).get('ok') is False:
        return 'FixCode'
    if (result.get('functional_test') or {}).get('passed') is False:
        return 'FixCode'
    if result.get('correctness_verdict') == 'failed':
        return 'FixCode'
    if (result.get('correctness_verdict') == 'passed'
            and result.get('system_acceptance') == 'passed'):
        return 'Deliver'
    return 'ValidateExperiment'
```

该函数仅消费 VerifyOptimization 已校验的结果，冲突字段和非法枚举须在输入校验中拒绝。CLI 用 `--verify-result FILE` 进入候选结果路由，与 Task 3 的 `--record/--feedback/--run-dir/--remaining` 模式互斥，缺失对应模式参数返回错误。
- [x] **Step 2 — 在主源和覆盖编排的“优化点级各阶段执行”前插入两个阶段。** 使用既有平台工具差异（Claude Agent / OpenCode task），共享新增模板；每个点初始化诊断预算，回到分析不得重置已消费次数。

```text
AnalyzeHotspot → DesignExperiment → ValidateExperiment
next_stage == DecideOptimization → 既有 Decide → AdversarialReview → Apply → Verify
next_stage == AnalyzeHotspot/DesignExperiment → 返回对应阶段并携带反证
next_stage == awaiting_target_evidence → 保存状态和缺项，停止依赖该证据的操作
next_stage == stop_budget/blocked_* → 记录原因，禁止强行 apply
```

模板填充列表、stage_versions、reviewCriteria、任务创建、最终报告和 batch_result 均增加两个阶段的映射，不能仅修改顶部示意图。

- [x] **Step 3 — 修改决策和验证。** Decide 消费假设/实验引用，可保留策略或因证据变化返回诊断；未验证预测不能输出确定收益。Verify 保留完整正确性与用户既定 benchmark，分开输出局部与系统结果；system_acceptance 未通过时不因局部提速而写 verified。

指标方向显式记录 higher/lower：speedup 分别用 candidate/baseline 与 baseline/candidate；零、非有限值及缺失指标返回未决。旧接口的最终 status 从完整验收映射，编译/功能失败为 failed，可靠性能退化为 regression，系统未验收为 unverified。

- [x] **Step 4 — 更新失败恢复规则。** 性能失败携带新证据回 ValidateExperiment，不能自动全部落到 FixCode。保留失败候选 patch/目录和 parent_state；下一候选前确认当前状态已恢复或隔离，无法确认则 blocked。不得用清理整个 working tree 的方式丢弃用户变更。
- [x] **Step 5 — 更新汇总，运行 Task 1/3 的全部测试并审查两个平台流程。** 报告只列假设、实验次数、关键证据、转向原因和最终状态；实验日志保留在文件中。删除主流程中的 evolve-skill 进化入口提示，以“本期保存实验记录，方法库后续建设”替代，不改 evolve-skill 自身。
- [x] **Step 6 — 提交本任务文件。** 建议：`feat(code-optimizer): integrate diagnostic feedback loop`。

## Task 5：安装一致性、回放与真实验收交付

**Files:** 新增 `tests/feedback/test_platform.py`、`docs/feedback-replay.md`；修改 `Plugins/code-optimizer/README.md`（此处为仓库根相对路径）。

**Interfaces:** 两个平台安装后都能访问 orchestrator 的新增 prompts/references/scripts；不依赖插件根 docs 在安装时保留。真实记录使用 Task 1 结构，回放不执行其中 command。

- [x] **Step 1 — 写安装测试。** 在 TemporaryDirectory 中分别执行仓库根的 `install.sh project claude TMP` 和 `install.sh project opencode TMP`，捕获退出码。使用 `subprocess.run([...], check=True, capture_output=True, text=True)`，所有变更限临时目录。

对两个目标的 `.claude/skills/` 或 `.opencode/skills/` 检查以下文件实际存在：

```python
required = [
 'kpbot-code-optimizer/prompts/stage.design-experiment.md',
 'kpbot-code-optimizer/prompts/stage.validate-experiment.md',
 'kpbot-code-optimizer/references/experiment-contract.md',
 'kpbot-code-optimizer/scripts/experiment_record.py',
 'kpbot-code-optimizer/scripts/experiment_feedback.py',
]
for relative in required:
    self.assertTrue((installed_skills / relative).is_file(), relative)
```

测试函数中根据每个 tool 构造 installed_skills；用安装后的 scripts 路径作为 PYTHONPATH 再运行 Task 1/3 测试，避免仅验证源文件而漏掉安装产物。

- [x] **Step 2 — 运行安装和逻辑测试。** 在插件根运行 `PYTHONPATH=skills/kpbot-code-optimizer/scripts python3 -m unittest discover -s tests/feedback -v`；另在仓库根运行 `bash tests/test_install_platform.sh`。前者应同时覆盖两个平台，后者保持已有排除规则通过。
- [x] **Step 3 — 写逐轮回放指引。** 首先仅开放用户案例初始条件和第一轮观测，记录假设及下一步实验；再开放下一轮结果，核对转向，不提前加载最终答案。记录来源为 historical，回放通过只说明诊断流程，不声明新性能收益。
- [x] **Step 4 — 写目标验收清单。** 使用用户提供的真实局部实验：固定目标机器/输入/编译条件，执行一轮诊断和候选变更，验证正确性与原始数据，最终按项目验收方法测试。必须覆盖假设被反驳和局部结果不足以判断系统收益的分支；若真实案例不含该分支，另用明确标记的逻辑回放覆盖，不制造实测案例。
- [x] **Step 5 — README 增加两个阶段和短流程图。** 标明“已通过本机逻辑与安装测试”“历史回放结果”“鲲鹏实测结果”三种独立状态。目标机器或案例未提供时，将实测列为 pending，不能写整体验收通过。
- [x] **Step 6 — 提交交付文件。** 建议：`test(code-optimizer): verify feedback installation and replay`。

## 计划自检与交接

- [x] 设计中的两个新增环节分别由 Task 2/3 实现，Task 4 接回主流程。
- [x] 实验记录由 Task 1 实现；局部/最终验收和证据边界由 Task 2/4 实现。
- [x] Agent 直接执行目标实验；没有强制人工导入的新流程。
- [x] 不包含 dotproduct 改造、evolve-skill 扩展、方法库或远程运行平台。
- [x] 新字段、函数和 CLI 在跨任务调用时保持一致；主源和覆盖层都包含修改。
- [x] 计划文件不是运行证据；本机实现与测试已完成，真实历史回放和鲲鹏实测仍待验收。

执行方式可选：使用 Superpowers 的 subagent-driven-development 逐任务实施并审查，或使用 executing-plans 在当前任务内顺序实施。无论采用哪种方式，首个任务都是实验记录与证据契约，而非启动目标性能测试。
