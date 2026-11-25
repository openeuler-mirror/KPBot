#!/usr/bin/env python3
"""topdown-phase-contract：计量期门禁逻辑回归（tier A）。

成功路径：Benchmarking + Collecting 行齐备 → measuring/pass。
误判路径：仅 warm-up → warmup/fail；空日志 → not_started/fail。
输入缺失 → rc=2。验收：本脚本退出码 0。
"""

import json
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
CHECK = SKILL_ROOT / "scripts" / "check_bench_phase.py"
EVID = Path(__file__).resolve().parent / "evidence"


def run(log, bench="snapshot_creation"):
    return subprocess.run([sys.executable, str(CHECK), str(log), "--bench", bench],
                          capture_output=True, text=True, check=False)


def expect(name, log, state, bench="snapshot_creation"):
    proc = run(log, bench)
    assert proc.returncode == 0, (name, proc.returncode, proc.stderr)
    data = json.loads(proc.stdout)
    assert data["state"] == state, (name, data)
    print("PASS %-14s -> %s" % (name, state))


def main():
    expect("measuring", EVID / "in-measurement.log", "measuring")
    expect("warmup", EVID / "warmup-only.log", "warmup")
    expect("empty", EVID / "empty.log", "not_started")

    other = run(EVID / "in-measurement.log", bench="snapshot_resume")
    data = json.loads(other.stdout)
    assert data["state"] == "not_started", ("exact-match", data)

    miss = run(EVID / "no-such-file.log")
    assert miss.returncode == 2, ("missing rc", miss.returncode)

    print("OK: phase contract 5 assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
