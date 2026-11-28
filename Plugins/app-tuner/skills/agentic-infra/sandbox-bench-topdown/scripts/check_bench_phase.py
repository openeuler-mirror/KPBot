#!/usr/bin/env python3
"""bench 计量期门禁判定（唯一实现，capture_topdown.sh 模板直接调用）。

用法：
    python3 scripts/check_bench_phase.py <bench-run.log> --bench NAME

判据（顺序解析，防两类误判）：
  逐行跟踪"当前项"：Benchmarking <X> 行（行尾锚定，防 snapshot_creation 前缀
  匹配 _1gdisk 变体）把当前项切为 X；"Collecting N samples" 只在当前项==目标
  项时计入——防止日志里他项的 Collecting 先行出现而目标项仅 warm-up 的误判。
  命中目标项 Benchmarking 且其后出现其 Collecting → measuring
  仅目标项 Benchmarking → warmup；否则 → not_started
  同时解析 "Collecting N samples in estimated <T> s" 提供计量窗 est_sec。

退出码契约：0 = measuring（gate pass）；1 = 尚未进入计量期（gate fail，
等待方应继续轮询）；2 = 输入缺失/不可读。"""

import argparse
import json
import re
import sys

BENCH_RE = re.compile(r"Benchmarking\s+(\S+)\s*$")
COLLECT_RE = re.compile(r"Collecting (\d+) samples in estimated (\d+)")


def classify(path, bench):
    current = None
    bench_seen = False
    gate = False
    est_sec = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = BENCH_RE.search(line)
            if m:
                current = m.group(1)
                if current == bench:
                    bench_seen = True
                continue
            cm = COLLECT_RE.search(line)
            if cm:
                if current == bench:
                    gate = True
                    est_sec = int(cm.group(2))
    if bench_seen and gate:
        state = "measuring"
    elif bench_seen:
        state = "warmup"
    else:
        state = "not_started"
    return state, est_sec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log", help="bench-run.log 路径")
    ap.add_argument("--bench", required=True, help="单项名（精确匹配）")
    args = ap.parse_args()
    try:
        state, est_sec = classify(args.log, args.bench)
    except OSError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"bench": args.bench, "state": state,
                      "est_sec": est_sec,
                      "gate": "pass" if state == "measuring" else "fail"},
                     ensure_ascii=False))
    return 0 if state == "measuring" else 1


if __name__ == "__main__":
    sys.exit(main())
