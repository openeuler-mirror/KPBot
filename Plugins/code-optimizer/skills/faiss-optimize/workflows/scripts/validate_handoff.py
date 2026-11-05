#!/usr/bin/env python3
"""校验 faiss-optimize 角色 handoff（kpbot-faiss-handoff.v1）。

用法：python3 validate_handoff.py <handoff.json>

优先用 jsonschema 结构校验（schema 位于 ../references/handoff.schema.json，与本脚本同发布）；
无论 jsonschema 是否可用，都执行程序语义检查：
  1. handoff_type=blocker 必须带 blocker
  2. review.verdict=REWORK 必须带 return_to（CP3/CP4/CP5/CP6）+ reason + required_actions
  3. artifacts 相对路径且不含 ..
  4. intended_for 与 handoff_type 的默认目标角色不符时告警（不判 FAIL）
退出码：0=PASS（可含 warning），1=FAIL。
"""
import json
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "references" / "handoff.schema.json"
TYPE_TO_AGENT = {
    "analysis-request": "faiss-analyzer",
    "analysis-result": "primary",
    "baseline-request": "faiss-profiler",
    "profile-request": "faiss-profiler",
    "profile-result": "primary",
    "plan-request": "faiss-candidate-planner",
    "candidate-plan": "primary",
    "implement-request": "faiss-implementer",
    "implement-result": "primary",
    "abba-request": "faiss-profiler",
    "abba-result": "primary",
    "review-request": "faiss-reviewer",
    "review-result": "primary",
    "blocker": "primary",
}
PRODUCED_BY_ENUM = set(TYPE_TO_AGENT.values()) | {"primary"}


def programmatic_checks(h, errors, warnings):
    if not isinstance(h, dict):
        fail(errors, "handoff 必须是对象")
        return
    if h.get("schema_version") != "kpbot-faiss-handoff.v1":
        fail(errors, f"schema_version 应为 kpbot-faiss-handoff.v1，实际 {h.get('schema_version')!r}")
    ht = h.get("handoff_type")
    if not isinstance(ht, str) or ht not in TYPE_TO_AGENT:
        fail(errors, f"handoff_type 非法：{ht!r}")
        return
    if not isinstance(h.get("produced_by"), str) or h["produced_by"] not in PRODUCED_BY_ENUM:
        fail(errors, f"produced_by 非法：{h.get('produced_by')!r}")
    if not isinstance(h.get("run_id"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,127}", h["run_id"]):
        fail(errors, f"run_id 非法：{h.get('run_id')!r}")

    payload = h.get("payload", {})
    if not isinstance(payload, dict):
        fail(errors, "payload 必须是对象")
        return
    if not isinstance(payload.get("summary"), str) or not payload["summary"].strip():
        fail(errors, "payload.summary 必填且非空")

    if ht == "blocker":
        b = h.get("blocker")
        if not isinstance(b, dict):
            fail(errors, "handoff_type=blocker 必须带 blocker 对象")
        else:
            if b.get("blocker_type") not in ("env", "source", "dataset", "build", "test",
                                             "evidence", "authorization"):
                fail(errors, f"blocker.blocker_type 非法：{b.get('blocker_type')!r}")
            if not b.get("description"):
                fail(errors, "blocker.description 必填")

    review = h.get("review")
    if (ht == "review-result" or "review" in h) and not isinstance(review, dict):
        fail(errors, "review-result 必须带 review 对象及 verdict")
    if isinstance(review, dict):
        v = review.get("verdict")
        if v == "REWORK":
            if review.get("return_to") not in ("CP3", "CP4", "CP5", "CP6"):
                fail(errors, f"REWORK 必须带合法 return_to（CP3/CP4/CP5/CP6），实际 {review.get('return_to')!r}")
            if not review.get("reason"):
                fail(errors, "REWORK 必须带 reason")
            if not review.get("required_actions"):
                fail(errors, "REWORK 必须带非空 required_actions")
        elif v not in ("ACCEPT", "ACCEPT_WITH_NOTES", "REJECT", "INCONCLUSIVE"):
            fail(errors, f"review.verdict 非法：{v!r}")

    artifacts = payload.get("artifacts", [])
    if not isinstance(artifacts, list):
        fail(errors, "payload.artifacts 必须是数组")
        artifacts = []
    for a in artifacts:
        if (not isinstance(a, str) or not a or PurePosixPath(a).is_absolute()
                or PureWindowsPath(a).drive or PureWindowsPath(a).root
                or ".." in PurePosixPath(a).parts or ".." in PureWindowsPath(a).parts):
            fail(errors, f"payload.artifacts 非法（须相对路径且无 ..）：{a!r}")

    intended = h.get("intended_for")
    if isinstance(intended, str) and intended in (set(TYPE_TO_AGENT.values()) | {"user"}) \
            and intended != TYPE_TO_AGENT[ht]:
        warnings.append(f"intended_for={intended} 与 handoff_type={ht} 默认目标 {TYPE_TO_AGENT[ht]} 不符（请确认）")


def fail(errors, msg):
    errors.append(msg)


def main() -> int:
    if len(sys.argv) != 2:
        print("用法：python3 validate_handoff.py <handoff.json>")
        return 1
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"FAIL: 文件不存在：{path}")
        return 1
    try:
        h = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        print(f"FAIL: JSON 解析失败：{e}")
        return 1

    errors, warnings = [], []
    try:
        import jsonschema
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        for err in sorted(jsonschema.Draft202012Validator(schema).iter_errors(h),
                          key=lambda e: list(e.path)):
            fail(errors, f"schema: {'/'.join(map(str, err.path)) or '<root>'}: {err.message}")
    except ImportError:
        pass

    programmatic_checks(h, errors, warnings)

    for w in warnings:
        print(f"WARNING: {w}")
    if errors:
        print(f"FAIL: {path}（{len(errors)} 项违规）")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"PASS: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
