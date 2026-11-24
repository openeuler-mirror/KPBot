#!/usr/bin/env python3
"""dtlbi-gate-contract：阶段 1 门禁逻辑回归（tier A）。

成功路径：含 __tlb_switch_to_host 5.23% 的合成 perf report → pass。
异常路径：无样本 → fail；低于阈值 0.43% → fail（不得误判为可继续）。
输入缺失 → 退出码 2。验收：本脚本退出码 0。
"""

import json
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
CHECK = SKILL_ROOT / "scripts" / "check_hotspot.py"
EVID = Path(__file__).resolve().parent / "evidence"


def run(report, *extra):
    proc = subprocess.run(
        [sys.executable, str(CHECK), str(report), *extra],
        capture_output=True, text=True, check=False)
    return proc


def expect_case(name, report, decision, overhead, matches):
    proc = run(report)
    assert proc.returncode == 0, (name, proc.returncode, proc.stderr)
    data = json.loads(proc.stdout)
    assert data["decision"] == decision, (name, data)
    assert abs(data["overhead_pct"] - overhead) < 1e-6, (name, data)
    assert data["matches"] == matches, (name, data)
    print("PASS %-8s overhead=%.2f%% -> %s" % (name, data["overhead_pct"], decision))


def main():
    expect_case("hotspot", EVID / "perf-report-hotspot.txt", "pass", 5.23, 1)
    expect_case("none", EVID / "perf-report-none.txt", "fail", 0.0, 0)
    expect_case("low", EVID / "perf-report-low.txt", "fail", 0.43, 1)

    proc = run(EVID / "no-such-file.txt")
    assert proc.returncode == 2, ("missing-file rc", proc.returncode)

    proc = run(EVID / "perf-report-low.txt", "--threshold", "0.4")
    data = json.loads(proc.stdout)
    assert data["decision"] == "pass", ("threshold override", data)

    print("OK: gate contract 5 assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
