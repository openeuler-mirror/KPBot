#!/usr/bin/env python3
"""ARM SPE LD/ST CSV 聚合分析：按 PC 统计高延迟 load/store。

输入为 `perf record -e arm_spe/.../` + `perf script -F cpu,op,pc,event,issue_lat,total_lat,vaddr,paddr,data_source` 的 CSV。
用法:
  spe_ldst.py <csv> [--bin BIN] [--op LD|ST|ALL] [--top N]
              [--lat-key total_lat|issue_lat] [--min-count N]

输出: 每 PC 的 count / mean / p99 / max 延迟；给出 --bin 时用 addr2line 映射符号与源码行。
高 total_lat 的 LD 且其附近有批量 NEON store 的 PC，是 store->load forwarding failure 的候选。
"""
import argparse
import csv
import os
import statistics
import subprocess
import sys

PASS = 0
FAIL = 0


def addr2line(binary, pc):
    try:
        out = subprocess.run(
            ["addr2line", "-f", "-C", "-e", binary, hex(int(pc, 16))],
            capture_output=True, text=True, timeout=30,
        ).stdout.splitlines()
        if len(out) >= 2:
            return f"{out[0].strip()} @ {out[1].strip()}"
    except Exception:
        pass
    return ""


def p99(vals):
    if not vals:
        return 0.0
    sv = sorted(vals)
    idx = max(0, int(round(len(sv) * 0.99)) - 1)
    return sv[idx]


def main():
    global PASS, FAIL
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv", help="SPE ld/st CSV 文件")
    ap.add_argument("--bin", help="二进制路径 (addr2line 符号化)")
    ap.add_argument("--op", default="LD", choices=["LD", "ST", "ALL"])
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--lat-key", default="total_lat", choices=["total_lat", "issue_lat"])
    ap.add_argument("--min-count", type=int, default=0)
    args = ap.parse_args()

    if not os.path.isfile(args.csv):
        print(f"错误: CSV 不存在: {args.csv}", file=sys.stderr)
        return 2
    if args.bin and not os.path.isfile(args.bin):
        print(f"错误: 二进制不存在: {args.bin}", file=sys.stderr)
        return 2

    agg = {}  # pc -> {count, lat: []}
    header = None
    with open(args.csv, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            if header is None:
                low = [c.strip('"') for c in row]
                if "op" in low and "pc" in low:
                    header = {name: i for i, name in enumerate(low)}
                    continue
                # 无表头：按 perf script 固定列顺序
                header = {"op": 0, "pc": 1, "issue_lat": 11, "total_lat": 12}
            try:
                op = row[header["op"]].strip('"')
                pc = row[header["pc"]].strip('"')
                lat = int(float(row[header[args.lat_key]].strip('"')))
            except (ValueError, IndexError, KeyError):
                continue
            if args.op != "ALL" and op != args.op:
                continue
            ent = agg.setdefault(pc, {"count": 0, "lat": []})
            ent["count"] += 1
            ent["lat"].append(lat)

    if not agg:
        print("无匹配记录", file=sys.stderr)
        return 1

    rows = []
    for pc, ent in agg.items():
        if ent["count"] < args.min_count:
            continue
        rows.append((pc, ent["count"], statistics.mean(ent["lat"]),
                     p99(ent["lat"]), max(ent["lat"])))
    rows.sort(key=lambda r: -r[3])  # 按 p99 降序

    print(f"=== SPE {args.op} 高延迟聚合 (lat-key={args.lat_key}) top {args.top} ===")
    print(f"{'PC':<18} {'count':>7} {'mean':>8} {'p99':>8} {'max':>8}  symbol @ source")
    for pc, cnt, mean, p99v, mx in rows[: args.top]:
        loc = addr2line(args.bin, pc) if args.bin else ""
        print(f"0x{int(pc,16):016x} {cnt:>7} {mean:>8.1f} {p99v:>8} {mx:>8}  {loc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
