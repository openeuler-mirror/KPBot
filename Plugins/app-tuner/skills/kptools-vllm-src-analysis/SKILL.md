---
name: kptools-vllm-src-analysis
description: |
  vLLM / vLLM-Ascend 源码侧版本知识：回答“某个特性/架构（如 ModelRunner V1、ModelRunner V2）
  是哪版引入、哪版默认开启、哪版移除”，提取每个成熟版本（vllm-ascend 有 tag 的版本）的
  源码锚点（文件:行号、类/方法、版本间漂移）供 profiling 分析 skill 做调用栈定位，
  并给出各版本更新内容与性能条目的证据等级。
  适用于用户问 vLLM 版本演进、ModelRunner v1/v2 引入版本、某函数在某版本的位置、
  调用栈对应源码、版本间代码漂移、某版本改了什么及对 NPU/CPU 性能的影响。
   不适用：trace 本体分析走 kptools-ascend-profiling-analysis / -analysis-nostack；
   两份 trace 对比走 kptools-ascend-profiling-compare-hub；运行中进程采集走
   kptools-proc-collector / kptools-py-spy；源码打点走
   kptools-vllm-ascend-ttft-instrumentation。本 skill 只读源码，不修改、不执行、不打点。
license: MulanPSL-2.0
compatibility: Linux；只读本地 git 仓库，无 NPU 依赖
metadata:
  skill_type: analysis
  category: profiling
  domain: vllm_source_analysis
  version: "1.0.0"
---

# vLLM 版本演进与源码锚点

## 适用

- 问“ModelRunner V1 / V2 是哪版引入的、哪版默认、哪版移除”。
- 问某个版本相比之前改了什么、有哪些性能条目、对 NPU/CPU 可能有什么影响。
- 需要某版本某个函数/类的 `文件:行号`，用于解释 profiling trace 的调用栈帧。
- 需要两份版本之间的源码漂移（新增/删除/行号移动）来解释 trace 差异。

不适用：

- trace 本体分析（P1/P2/P3、Free 去向、模型结构）→ `kptools-ascend-profiling-analysis` / `-analysis-nostack`。
- 两份 trace 的整体/专项对比 → `kptools-ascend-profiling-compare-hub`。
- 运行中服务的 topdown / 指令 mix / 线程热点 → `kptools-proc-collector` / `kptools-py-spy`。
- 给源码打点 / 改源码 → `kptools-vllm-ascend-ttft-instrumentation`（本 skill 只输出位置，不改代码）。

## 与 `ascend-vllm-src` 的关系

仓库内已有配套子 skill `ascend-vllm-src`
（`Plugins/app-tuner/skills/ascend_kunpeng/kptools-ascend-profiling-analysis/vllm-src/`），
二者都做“版本 → 源码锚点 → 行号 → 漂移”，但分工不同：

| 维度 | `ascend-vllm-src`（已合入） | 本 skill `kptools-vllm-src-analysis` |
|---|---|---|
| 定位 | 单版本源码解读子 skill，服务 profiling 调用栈 | 版本演进 / 四态时间线 / 兼容矩阵 |
| 版本知识 | 预训练好的 `knowledge/<版本>.md` | 用 `git tag/grep/log` 现场取证 |
| 覆盖 | 已训练版本（目前 v0.23.0rc1） | vllm-ascend 所有发布 tag |
| 输出 | 函数语义、行号表、部署补丁映射 | 四态、逐版本锚点、漂移、release-note 证据 |

trace 帧语义解读优先用 `ascend-vllm-src`；需要“哪版引入/默认/移除、跨版本漂移、
兼容矩阵、release-note 证据”时用本 skill。

## 输入与数据源

| 输入 | 必需 | 说明 |
|---|---|---|
| vllm-ascend git 仓库 | 是* | 版本主仓；成熟版本口径 = 有发布 tag 的版本 |
| vllm git 仓库 | 否 | 用于配对与 vllm 侧锚点；缺省时配对降级 |
| 版本 tag | 视问题 | 如 `v0.30.0rc1`；不传时可先列版本 |
| 官方线上发布说明 | 否 | `--online`；用于本地 tag 还没有的新版本（如 v0.30.0 稳定版） |

\* 仅用 `--online` 查发布说明时可以不提供本地仓库；回答版本四态/源码锚点仍需仓库。

默认只读本地仓库（`VLLM_ASCEND_REPO` / `VLLM_REPO` 或 `--repo`），
用 `git show` / `git tag` / `git grep` 读取，**不 checkout、不 fetch、不执行源码**。

## 工作流

```bash
S=Plugins/app-tuner/skills/kptools-vllm-src-analysis/scripts
ASC=/path/to/vllm-ascend
VLL=/path/to/vllm

# 1. 列成熟版本 + 配对 vllm/CANN/torch_npu + ModelRunner v1/v2 是否存在
#    加 --online 可对比官方发布说明，标出本地 tag 缺失的版本
python3 $S/versions.py --ascend-repo "$ASC" --vllm-repo "$VLL" --online --json versions.json

# 2. 某东西“哪版引入/默认/移除”（四态）
python3 $S/timeline.py --repo "$ASC" --path vllm_ascend/worker/v2/model_runner.py --json tl.json
#    默认开关的默认值定义在 vllm 仓库 envs.py（vllm_ascend 侧只读取），
#    default_on 必须扫默认值所在仓库；扫 vllm_ascend 会永远 unknown。
python3 $S/timeline.py --repo "$VLL" --symbol VLLM_USE_V2_MODEL_RUNNER \
  --scope vllm/envs.py --default-regex 'VLLM_USE_V2_MODEL_RUNNER[[:space:]]*[:=][[:space:]]*.*True' --json tl.json

# 3. 某版本的源码锚点（供 call stack 定位）
python3 $S/anchors.py --repo "$ASC" --version v0.30.0rc1 --json anchors_030.json
#    两版本漂移
python3 $S/anchors.py --repo "$ASC" --version v0.30.0rc1 --diff v0.19.1rc1 --json drift.json

# 4. 版本更新了啥（release notes，含 Performance 小节与 PR 证据）
python3 $S/release_notes.py --repo "$ASC" --version v0.30.0rc1 \
  --sections Highlights,Features,Performance,Deprecation --json rn.json
#    本地 tag 没有的新版本（官方 docs / main 分支），走线上 raw markdown
python3 $S/release_notes.py --online --version v0.30.0 --sections Performance --json rn30.json
python3 $S/release_notes.py --online --list

# 5. 该版本区间提交级归类（release notes 缺失时兜底）
python3 $S/digest.py --repo "$ASC" --from v0.19.1rc1 --to v0.30.0rc1 --json digest.json
```

回答顺序：`versions.py` 定版本与配对 → `timeline.py` 定四态 → `anchors.py` 给源码位置
→ `release_notes.py` 给更新与性能条目 → 需要更细时用 `digest.py`。**禁止用模型记忆直接答版本号。**

## 四态口径（回答“哪版引入”必须全给）

| 状态 | 含义 |
|---|---|
| `introduced_code` | 代码提交引入（commit + 日期） |
| `first_release` | 首个包含它的发布 tag |
| `default_on` | 首个默认开启的 tag（需提供 `--default-regex`，否则 unknown） |
| `removed` | 首个移除它的 tag（未移除为 null） |

只报一个版本号视为错误回答；`unknown` 必须显式写出，不得猜测。

## 证据等级

| 等级 | 来源 | 用法 |
|---|---|---|
| `measured` | 实测 benchmark / profiling | 唯一可用于“提升了 X%”的证据 |
| `claimed_in_release_notes` | release notes 的 Performance 条目 | 只能说“官方宣称”，附 PR 链接 |
| `inferred_from_commit_subject` | `digest.py` 提交标题归类 | 只能说“动了哪块”，不能给幅度 |

趋势结论必须基于 ≥3 个版本的数据点，并标注为推断，不预测未发布版本。

## 输出

- `versions.json`：版本、日期、commit、配对 vllm/CANN/torch_npu、`model_runner.v1/v2`。
- `tl.json`：四态 + 每版本存在性 + gaps + confidence。
- `anchors_<ver>.json`：`files[].classes[].methods[]` 的 `line/end_line/bases`，可 `--diff` 出
  `added_symbols` / `removed_symbols` / `moved_symbols`（行号漂移）。
- `rn.json`：release notes 小节与 bullets，每条含 PR 链接。
- `digest.json`：提交区间按 subsystem / perf_dim 归类。

分析 skill 消费锚点时，以 `anchors_<ver>.json` 的 `version` + `commit` 为版本凭证；
trace 所在版本不明时，先用 `profiler_info_*.json` 或用户确认版本，不允许跨版本套行号。

## 降级路径

| 情况 | 行为 |
|---|---|
| 无 vllm 仓库 | 配对退化为 nearest-lower（`pairing_basis` 标注），confidence 降级 |
| 版本无对应 tag | `timeline` 全 false，`release_notes` 返回 `found=false` + 可用版本列表 |
| release notes 无该版本条目 | 返回 `found=false`，改用 `digest.py` 看提交 |
| 线上获取失败 | 返回 `status=error`，改用本地 tag 的 release notes 并在报告注明 |
| 本地无 tag、线上有发布说明 | 只给 release-note 级事实（claimed），源码锚点标 unknown；报告注明需补 tag/部署版源码 |
| 兼容矩阵缺失 | `compat=null`，配对回退到 tag/commit |
| 文件 AST 解析失败 | 记入 `parse_errors`，其余文件继续，不中断 |
| 输出目录不可写 | 非零退出并在 stderr 给原因，不静默成功 |

所有脚本失败时都输出 `{"schema_version":1,"status":"error",...}`，不会伪装成空结果。

## 口径与限制

- 成熟版本口径：**vllm-ascend 有发布 tag 的版本都算**（用户确认）。
- **本地 tag vs 线上文档**：本地 tag 是版本绑定事实；`--online` 读官方 main 最新发布说明，
  可能领先本地 clone，等级是 `claimed_official_docs`。两者版本不一致时，报告必须写
  “本地未含、线上已发布”，不能把线上新版本当作本地可复核的源码事实。
- **本地 tag 集可能不完整**：回答版本清单前先跑 `versions.py --online` 对比；对缺 tag 的
  版本只能给发布说明结论，源码锚点必须标 unknown。
- `first_release` 是 tag 维度，不等于“代码合入时间”；两者同时给出。
- `default_on` 只在 `--default-regex` 命中的仓库/范围内有效；默认值往往定义在
  `vllm` 仓库（如 `vllm/envs.py`），扫 `vllm_ascend` 会永远是 `unknown`，此时必须显式写 unknown 并说明扫错了仓库。
- release notes 的 Performance 是官方宣称，不是实测；没有实测数据时禁止写百分比收益。
- `digest.py` 的分类是关键词启发式，只作线索，不作结论。
- 锚点行号只对指定 tag 成立；生产环境若有内部补丁会整体漂移，需另给部署版源码或 offset。
- 输出确定：同一仓库/同一 tag/同一参数重复运行结果一致。

## 参考资料

- [references/glossary.md](references/glossary.md) — ModelRunner V1/V2、引擎版本与术语。
- [references/evidence-policy.md](references/evidence-policy.md) — 四态、证据等级、趋势口径。
- [references/version-registry.md](references/version-registry.md) — 已登记版本与 v1/v2 时间线。
- [references/upstream-release-notes.md](references/upstream-release-notes.md) — 官方线上发布说明要点（v0.30.0 默认 MRV1、MRV1 移除计划、依赖差异）。

## 开发校验

```bash
python3 -m unittest tests.test_vllm_src_analysis -v
```
