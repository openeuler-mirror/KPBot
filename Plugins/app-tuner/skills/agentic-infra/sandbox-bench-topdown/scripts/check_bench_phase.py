#!/usr/bin/env python3
"""bench 计量期门禁判定：解析 bench-run.log，判定是否已进入计量阶段。

用法：
    python3 scripts/check_bench_phase.py <bench-run.log> [--bench NAME]

判据（与 capture_topdown.sh 一致，可回归；Benchmarking 行须行尾锚定，
防 snapshot_creation 误匹配 snapshot_creation_1gdisk 前缀变体）：
  Benchmarking <BENCH> 行出现 + "Collecting N samples" 进度行出现 → measuring
  仅 Benchmarking 行（warm-up 进行中）→ warmup
  两者皆无 → not_started
退出码：0 = 解析完成（状态见 JSON）；2 = 输入缺失。
"""

import argparse
import json
import re
import sys

COLLECT_RE = re.compile(r"Collecting \d+ samples")


BENCH_RE_TMPL = r"Benchmarking {bench}[ \t]*$"


def classify(path, bench):
    has_bench_line = False
    has_collecting = False
    bench_re = re.compile(BENCH_RE_TMPL.format(bench=re.escape(bench)))
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if bench_re.search(line):          # 行尾锚定：防 snapshot_creation 前缀匹配 _1gdisk 变体
                has_bench_line = True
            if COLLECT_RE.search(line):
                has_collecting = True
    if has_bench_line and has_collecting:
        state = "measuring"
    elif has_bench_line:
        state = "warmup"
    else:
        state = "not_started"
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log", help="bench-run.log 路径")
    ap.add_argument("--bench", required=True, help="单项名（精确匹配）")
    args = ap.parse_args()
    try:
        state = classify(args.log, args.bench)
    except OSError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"bench": args.bench, "state": state,
                      "gate": "pass" if state == "measuring" else "fail"},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
