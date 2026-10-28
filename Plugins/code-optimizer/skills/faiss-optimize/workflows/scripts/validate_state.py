#!/usr/bin/env python3
"""校验 faiss-optimize 运行状态（kpbot-faiss-opt-run.v1）。

用法：python3 validate_state.py <state.json>

优先用 jsonschema 做结构校验（schema 与本脚本同发布，位于 ../references/state.schema.json）；
无论 jsonschema 是否可用，都执行以下程序检查（结构校验不覆盖的语义规则）：
  1. 线性推进：completed 构成前缀；至多一个 running/blocked/failed 活动
  2. artifacts 相对路径且不含 ..
  3. blocked 阶段必须带 blocker_reason
  4. verdict 仅终态（stage 7 completed）；candidate_commit 与 verdict 一致性
  5. selected_candidate 在 stage 4 完成前必须填写
  6. CP2 前置：stage 2 completed 需有本轮基线身份（source.baseline_id 非空且非占位）；
     缺失时 CP2 应 blocked 并注明缺什么，不得虚假 completed；后续阶段遵守线性状态与各自证据要求
退出码：0=PASS，1=FAIL（打印全部违规项）。
"""
import json
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "references" / "state.schema.json"
STAGE_KEYS = [str(i) for i in range(8)]
STATUS_ENUM = {"pending", "running", "completed", "blocked", "failed"}
VERDICT_ENUM = {"ACCEPT", "ACCEPT_WITH_NOTES", "REJECT", "INCONCLUSIVE"}
COMMIT_STATUS_ENUM = {"pending", "committed", "not_authorized", "not_applicable"}
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
SOURCE_COMMIT_RE = re.compile(r"[0-9a-f]{7,64}")
COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def fail(errors, msg):
    errors.append(msg)


def check_artifacts(artifacts, errors, ctx):
    if not isinstance(artifacts, list):
        fail(errors, f"{ctx}: artifacts 必须是数组")
        return
    for a in artifacts:
        if not isinstance(a, str):
            fail(errors, f"{ctx}: artifact 非字符串：{a!r}")
            continue
        posix, windows = PurePosixPath(a), PureWindowsPath(a)
        if not a or posix.is_absolute() or windows.drive or windows.root:
            fail(errors, f"{ctx}: artifact 禁止绝对路径：{a}")
        if ".." in posix.parts or ".." in windows.parts:
            fail(errors, f"{ctx}: artifact 禁止 .. 越界：{a}")


def programmatic_checks(state, errors):
    if not isinstance(state, dict):
        fail(errors, "state 必须是对象")
        return
    if state.get("schema_version") != "kpbot-faiss-opt-run.v1":
        fail(errors, f"schema_version 应为 kpbot-faiss-opt-run.v1，实际 {state.get('schema_version')!r}")
        return
    if not isinstance(state.get("run_id"), str) or not RUN_ID_RE.fullmatch(state["run_id"]):
        fail(errors, f"run_id 非法：{state.get('run_id')!r}")

    for field in ("source", "stages", "result"):
        if not isinstance(state.get(field), dict):
            fail(errors, f"{field} 必须是对象")
            return
    baseline_commit = state["source"].get("baseline_commit")
    if not isinstance(baseline_commit, str) or (
            baseline_commit != "unknown" and not SOURCE_COMMIT_RE.fullmatch(baseline_commit)):
        fail(errors, f"source.baseline_commit 须为 Git commit 或 unknown：{baseline_commit!r}")

    stages = state.get("stages", {})
    if set(stages.keys()) != set(STAGE_KEYS):
        fail(errors, f"stages 键必须恰为 0..7，实际 {sorted(stages.keys())}")
        return

    for k in STAGE_KEYS:
        st = stages[k]
        if not isinstance(st, dict):
            fail(errors, f"stages[{k}] 必须是对象")
            return
        if not isinstance(st.get("status"), str) or st["status"] not in STATUS_ENUM:
            fail(errors, f"stages[{k}].status 非法：{st.get('status')!r}")
        if st.get("status") == "blocked" and not st.get("blocker_reason"):
            fail(errors, f"stages[{k}] blocked 必须带 blocker_reason")
        check_artifacts(st.get("artifacts", []), errors, f"stages[{k}]")

    # 线性推进：completed 前缀 + 至多一个活动（running/blocked/failed）+ pending 尾部
    # 合法形态：completed* [active] pending*（active 不得出现在 pending 之后，completed 不得出现在前缀之后）
    phase = 0  # 0=completed 前缀内, 1=已见活动, 2=已进 pending 尾部
    for k in STAGE_KEYS:
        s = stages[k]["status"]
        if s == "completed":
            if phase != 0:
                fail(errors, f"stages[{k}] completed 出现在前缀之外（违反线性推进/REWORK 应重置后续阶段）")
        elif s == "pending":
            if phase == 0:
                phase = 2
        else:  # running / blocked / failed
            if phase == 2:
                fail(errors, f"stages[{k}] 的活动状态出现在 pending 尾部之后（全局至多一个活动且先于 pending）")
            elif phase == 1:
                fail(errors, f"stages[{k}] 是第二个活动阶段（全局至多一个 running/blocked/failed）")
            phase = 1

    # selected_candidate 在 stage 4 完成前必须填写
    if stages["4"]["status"] == "completed":
        sel = state.get("selected_candidate", {})
        if not isinstance(sel, dict) or not sel.get("candidate_id"):
            fail(errors, "stages[4] completed 但 selected_candidate.candidate_id 为空")

    result = state.get("result", {})
    commit = result.get("candidate_commit", {})
    if not isinstance(commit, dict):
        fail(errors, "result.candidate_commit 必须是对象")
        return
    verdict = result.get("verdict")
    stage7_completed = stages["7"]["status"] == "completed"
    if stage7_completed and verdict is None:
        fail(errors, "stage 7 completed 必须带 result.verdict")

    if verdict is not None:
        if not stage7_completed:
            fail(errors, "result.verdict 存在但 stage 7 未 completed（verdict 仅终态）")
        if not isinstance(verdict, str) or verdict not in VERDICT_ENUM:
            fail(errors, f"result.verdict 非法：{verdict!r}")

    if not isinstance(commit.get("status"), str) or commit["status"] not in COMMIT_STATUS_ENUM:
        fail(errors, f"result.candidate_commit.status 非法：{commit.get('status')!r}")

    if verdict in ("ACCEPT", "ACCEPT_WITH_NOTES"):
        if commit.get("status") not in ("committed", "not_authorized"):
            fail(errors, f"verdict={verdict} 时 candidate_commit.status 应为 committed 或 not_authorized，实际 {commit.get('status')!r}")
    if verdict in ("REJECT", "INCONCLUSIVE"):
        if commit.get("status") != "not_applicable":
            fail(errors, f"verdict={verdict} 时 candidate_commit.status 应为 not_applicable，实际 {commit.get('status')!r}")

    if commit.get("status") == "committed":
        if not isinstance(commit.get("sha"), str) or not COMMIT_RE.fullmatch(commit["sha"]):
            fail(errors, f"candidate_commit.status=committed 但 sha 非完整 40/64 位 hex：{commit.get('sha')!r}")
        if commit.get("committed_by") != "coordinator":
            fail(errors, f"candidate_commit.committed_by 应为 coordinator，实际 {commit.get('committed_by')!r}")
        if verdict not in ("ACCEPT", "ACCEPT_WITH_NOTES"):
            fail(errors, f"candidate_commit.status=committed 但 verdict={verdict!r}（只有 ACCEPT 类可提交）")
    if commit.get("status") == "not_authorized":
        if verdict not in ("ACCEPT", "ACCEPT_WITH_NOTES"):
            fail(errors, f"candidate_commit.status=not_authorized 但 verdict={verdict!r}")

    events = result.get("rework_events", [])
    if not isinstance(events, list):
        fail(errors, "result.rework_events 必须是数组")
        events = []
    for ev in events:
        if not isinstance(ev, dict):
            fail(errors, "rework_events 条目必须是对象")
            continue
        if ev.get("return_to") not in ("CP3", "CP4", "CP5", "CP6"):
            fail(errors, f"rework_events.return_to 非法：{ev.get('return_to')!r}")
        if not ev.get("reason"):
            fail(errors, "rework_events 条目缺 reason")

    # CP2 前置：本轮基线身份（缺失时 CP2 应 blocked 并注明缺什么，
    # 不得虚假 completed；CP5/CP6/CP7 无基线身份专属前置——正常推进，性能结论由 CP7 依本轮证据判定）
    if stages["2"]["status"] == "completed":
        baseline_id = state.get("source", {}).get("baseline_id", "")
        if (not isinstance(baseline_id, str) or not baseline_id.strip()
                or baseline_id.strip().startswith("<")
                or baseline_id.strip().lower() in {"unknown", "none", "n/a", "pending", "todo", "tbd"}):
            fail(errors, f"CP2 completed 但 source.baseline_id 为空/占位（实际 {baseline_id!r}）"
                          "——基线身份缺失时 CP2 应 blocked 并注明（版本/补丁/构建标识见"
                          " faiss-benchmark-baseline/references/baseline-contract.md）")


def main() -> int:
    if len(sys.argv) != 2:
        print("用法：python3 validate_state.py <state.json>")
        return 1
    state_path = Path(sys.argv[1])
    if not state_path.is_file():
        print(f"FAIL: 文件不存在：{state_path}")
        return 1
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        print(f"FAIL: JSON 解析失败：{e}")
        return 1

    errors = []

    try:
        import jsonschema
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        for err in sorted(jsonschema.Draft202012Validator(schema).iter_errors(state),
                          key=lambda e: list(e.path)):
            fail(errors, f"schema: {'/'.join(map(str, err.path)) or '<root>'}: {err.message}")
    except ImportError:
        # jsonschema 不可用时跳过结构校验（程序语义检查仍然全量执行）
        pass

    programmatic_checks(state, errors)

    if errors:
        print(f"FAIL: {state_path}（{len(errors)} 项违规）")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"PASS: {state_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
