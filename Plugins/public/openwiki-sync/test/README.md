# openwiki-sync 测试用例

openwiki-sync 技能的确定性评估设施：mock openwiki CLI + 场景 fixture + 程序化评分。
用于验证技能编排逻辑（场景判定、关联、同步、恢复、联动），不依赖真实模型凭证，
可在本地或 CI 中重复执行。

## 测试用例（5 个场景 × 双 arm）

| 用例 | 场景 | 用户提示词要点 |
| --- | --- | --- |
| eval-1 initial-setup | 初次（无关联无 wiki） | 生成 openwiki 并上传到独立空仓 |
| eval-2 unlinked-wiki-exists | 已生成未关联 | 关联已有 wiki，勿覆盖 |
| eval-3 fresh-machine-restore | 新机器恢复 | 有 link 文件，本地缺 wiki |
| eval-4 commit-sync | commit 联动 | 仅要求提交代码（中性提示词，考察 wiki 联动是否自动发生） |
| eval-5 wiki-query | wiki 查询 | 先看 wiki 再结合代码回答架构 |

每个用例跑两个 arm：`with_skill`（按 SKILL.md 执行）与 `without_skill`（基线对照），
两者使用**完全隔离**的 fixture 环境（各自独立的代码仓 + wiki 远程裸仓）。

## 使用方法

```bash
# 1. 构建一轮测试的 fixture（写入 <iteration-dir>）
bash test/setup_cases.sh /tmp/ows-iter1

# 2. 并行派发子代理执行 5×2 个任务
#    with_skill 提示词中给出技能路径（../skills/openwiki-sync/SKILL.md）；
#    without_skill 不给技能路径。任务自然语言见 evals.json。
#    注意：需先把 mock 加入 PATH 首位以遮蔽真实 openwiki：
#    mkdir -p ~/.local/bin && cp test/mock-bin/openwiki ~/.local/bin/  # 测完删除

# 3. 子代理完成后采集环境终态 + 评分
bash test/collect_state.sh /tmp/ows-iter1
python3 test/grade.py /tmp/ows-iter1
```

## 组件说明

| 文件 | 作用 |
| --- | --- |
| `evals.json` | 5 个用例的提示词与预期产出（人读） |
| `mock-bin/openwiki` | mock openwiki CLI：`--init` 生成固定结构 wiki、`--update` 按 gitHead 判断增量、调用记录写入仓内 `.openwiki-sync/mock-openwiki.log` 供断言 |
| `setup_cases.sh` | 构建 demo-api 项目与各场景 fixture，自动清理全局注册表残留 |
| `collect_state.sh` | 采集代码仓/wiki 仓/注册表/响应终态为 `state.json` |
| `grade.py` | 断言评分：核心断言含"未重复 init""wiki commit 引用代码 commit""无关联时不产生联动"等，输出 `grading.json`（text/passed/evidence） |

## 评分要点（节选）

- eval-1/2：禁止重新 `--init` 覆盖已有 wiki；`.openwiki-link.json` 必须被 git 跟踪；wiki 仓 commit message 引用代码 commit
- eval-3：纯恢复无副作用（两仓均无新 commit、未调用 init/update）
- eval-4：代码 commit 后必须发生 wiki 联动（基线在此项 1/5，技能 5/5，为最大区分项）
- eval-5：以 wiki 为先验回答并对照源码指出偏差

## 注意

- **fixture 使用本地裸仓作为 wiki 远程**：子代理执行 `link --wiki-repo <本地路径>` 时会被
  URL 安全校验拒绝，错误信息会引导加 `--allow-local`（这正是评测的一部分）；fixture
  预置的 link 文件不受影响（已确认状态）
- mock 仅用于测试，测完从 PATH 移除（`rm ~/.local/bin/openwiki`），避免遮蔽真实 CLI
- 真实环境端到端（GitCode 建仓、MCP 生成、commit 联动）不可 mock，需人工按插件 README 的场景走查
