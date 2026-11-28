#!/usr/bin/env python3
"""topdown-phase-contract：计量期门禁（唯一实现 scripts/check_bench_phase.py）契约回归。

覆盖（tier A）：成功/误判/前缀变体/跨项顺序/CRLF/输入缺失 + 发布路径断言
（SKILL.md 采集模板必须调用本脚本——防门禁回归到旁路实现）。
"""

import json
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
CHECK = SKILL_ROOT / "scripts" / "check_bench_phase.py"
EVID = Path(__file__).resolve().parent / "evidence"
SKILL_MD = SKILL_ROOT / "SKILL.md"


def run(log, bench="snapshot_creation"):
    return subprocess.run([sys.executable, str(CHECK), str(log), "--bench", bench],
                          capture_output=True, text=True, check=False)


def expect(name, log, state, rc, bench="snapshot_creation"):
    proc = run(log, bench)
    assert proc.returncode == rc, (name, "rc", proc.returncode, "!=", rc)
    data = json.loads(proc.stdout)
    assert data["state"] == state, (name, data)
    if state == "measuring":
        assert data["est_sec"] == 120, (name, data)
    print("PASS %-14s -> %-11s rc=%d" % (name, state, rc))


def main():
    expect("measuring", EVID / "in-measurement.log", "measuring", 0)
    expect("warmup", EVID / "warmup-only.log", "warmup", 1)
    expect("empty", EVID / "empty.log", "not_started", 1)

    variant = run(EVID / "prefix-variant.log", bench="snapshot_creation")
    vdata = json.loads(variant.stdout)
    assert vdata["state"] == "not_started" and variant.returncode == 1, ("prefix-variant", vdata)

    cross = run(EVID / "cross-item-order.log")
    cdata = json.loads(cross.stdout)
    assert cdata["state"] == "warmup" and cross.returncode == 1, ("cross-item-order", cdata)

    crlf = run(EVID / "crlf-measuring.log")
    ldata = json.loads(crlf.stdout)
    assert ldata["state"] == "measuring" and crlf.returncode == 0, ("crlf", ldata)

    miss = run(EVID / "no-such-file.log")
    assert miss.returncode == 2, ("missing rc", miss.returncode)

    # 发布路径断言：SKILL.md 采集模板必须调用门禁脚本（唯一实现，防旁路漂移）
    skill = SKILL_MD.read_text(encoding="utf-8")
    assert 'python3 "$CHECKER" "$LOG" --bench "$BENCH"' in skill, "published-path: template must call checker"
    assert 'CHECKER="${CHECK_BENCH_PHASE:-}"' in skill, "published-path: CHECKER wiring missing"

    print("OK: phase contract 9 assertions passed (incl. published-path)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
