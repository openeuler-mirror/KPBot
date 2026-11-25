#!/usr/bin/env python3
"""agentenv-dtlbi-pr30652 阶段 1 门禁：__tlb_switch_to_host 热点占比判定。

用法：
    python3 scripts/check_hotspot.py <perf-report-stdio.txt> [--symbol NAME] [--threshold PCT]

输入为 `perf report --stdio --sort symbol` 的文本输出（文件）。
输出 JSON：{"symbol", "overhead_pct", "threshold_pct", "decision", "matches"}
  decision = "pass"  → 占比 ≥ 阈值，可进入阶段 2（换核仍需使用者授权）
  decision = "fail"  → 占比 < 阈值或无样本：报告占比与预期收益有限，由使用者决定
退出码：0 = 解析完成（判定见 JSON）；2 = 输入缺失/无法解析。
"""

import argparse
import json
import re
import sys

DEFAULT_SYMBOL = "__tlb_switch_to_host"
DEFAULT_THRESHOLD = 1.0
PCT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s")


def parse_overhead(path, symbol):
    total = 0.0
    matches = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            fields = line.split()
            if len(fields) < 2:
                continue
            # 行尾字段形如 [k] <symbol> 或 <symbol>；符号按整词匹配（同 grep -w）
            if fields[-1] != symbol:
                continue
            m = PCT_RE.match(line)
            if not m:
                continue
            total += float(m.group(1))
            matches += 1
    return total, matches


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report", help="perf report --stdio 输出文件")
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL)
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = ap.parse_args()
    try:
        total, matches = parse_overhead(args.report, args.symbol)
    except OSError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    decision = "pass" if total >= args.threshold else "fail"
    print(json.dumps({
        "symbol": args.symbol,
        "overhead_pct": round(total, 3),
        "threshold_pct": args.threshold,
        "matches": matches,
        "decision": decision,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
