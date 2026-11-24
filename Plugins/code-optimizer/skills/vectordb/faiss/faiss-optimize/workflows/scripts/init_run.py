#!/usr/bin/env python3
"""faiss-optimize CP0 初始化脚本：建立 run_dir 与 state.json（kpbot-faiss-opt-run.v1）。

用法示例：
  python3 init_run.py --algorithm rabitq --run-id rabitq-example-01 \
      --repo /path/to/faiss --run-dir /path/to/run_dir \
      --faiss-version <detected-version> --interaction-mode collaboration
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "kpbot-faiss-opt-run.v1"
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
ALGORITHM_TO_SKILL = {
    "rabitq": "faiss-rabitq-optimization",
    "turboquant": "faiss-turboquant-optimization",
    "ivfpq": "faiss-ivfpq-optimization",
    "ivfpqfs": "faiss-ivfpqfs-optimization",
    "hnsw": "faiss-hnsw-optimization",
}
STAGE_KEYS = [str(i) for i in range(8)]


def git_commit(repo: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    ap = argparse.ArgumentParser(description="Initialize faiss-optimize run state")
    ap.add_argument("--algorithm", required=True, choices=sorted(ALGORITHM_TO_SKILL))
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--repo", required=True, help="Faiss 源码仓绝对路径")
    ap.add_argument("--run-dir", required=True, help="本次任务的独立外部产物目录")
    ap.add_argument("--faiss-version", default="", help="实际检出的Faiss版本")
    ap.add_argument("--baseline-id", default="", help="本轮基线身份（上游版本 + 补丁/构建标识，如 source-build-id）")
    ap.add_argument("--interaction-mode", default="collaboration",
                    choices=["collaboration", "auto"])
    ap.add_argument("--history-source", default=None,
                    help="可选外部实验记录路径/说明；缺省为none")
    ap.add_argument("--patch", default=None, action="append", metavar="PATH",
                    help="本轮源码已带的补丁文件路径（可多次；用户提供时记录，"
                         "SHA256 由 primary 计算并记入 run_dir manifest）")
    args = ap.parse_args()

    if not RUN_ID_RE.match(args.run_id):
        print(f"FAIL: run-id 不符合规则 ^[a-z0-9][a-z0-9._-]{{2,127}}$：{args.run_id}")
        return 1

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_absolute():
        print("FAIL: run-dir 必须是绝对路径")
        return 1
    state_path = run_dir / "state.json"
    if state_path.exists():
        print(f"FAIL: 已存在 {state_path}；禁止覆盖，换 run-dir 或 run-id")
        return 1

    for patch_path in (args.patch or []):
        if not Path(patch_path).is_file():
            print(f"FAIL: 补丁文件不存在：{patch_path}")
            return 1

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "candidates").mkdir(exist_ok=True)

    baseline_commit = git_commit(args.repo)
    decisions = [{
        "at": now_iso(),
        "decision": "run-initialized",
        "rationale": f"algorithm={args.algorithm} mode={args.interaction_mode}"
                     + (f" baseline_commit={baseline_commit}" if baseline_commit != "unknown"
                        else " baseline_commit=unknown（无 git 环境，CP1 复核版本）"),
    }]
    if args.patch:
        decisions.append({
            "at": now_iso(),
            "decision": "patch-recorded",
            "rationale": "patches=" + ";".join(args.patch)
                         + "（SHA256 由 primary 计算并记入 run_dir/manifest.json）",
        })

    state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": args.run_id,
        "repo": str(Path(args.repo).resolve()),
        "run_dir": str(run_dir),
        "interaction_mode": args.interaction_mode,
        "source": {
            "baseline_commit": baseline_commit,
            "faiss_version": args.faiss_version,
            "baseline_id": args.baseline_id,
        },
        "history_policy": (
            {"mode": "external", "source": args.history_source}
            if args.history_source else {"mode": "none"}
        ),
        "contract": {
            "algorithm": args.algorithm,
            "active_algorithm_skill": ALGORITHM_TO_SKILL[args.algorithm],
        },
        "stages": {k: {"status": "pending"} for k in STAGE_KEYS},
        "selected_candidate": {},
        "result": {"candidate_commit": {"status": "pending"}},
        "decisions": decisions,
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OK: {state_path}")
    print(f"next: 校验 state（python3 validate_state.py {state_path}），随后进入 CP1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
