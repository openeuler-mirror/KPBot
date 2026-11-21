#!/usr/bin/env python3
"""ARM SPE LD/ST 逐指令延迟聚合：按 PC 统计高延迟 load/store。

输入契约（脚本自动识别，三选一）：
  1. `perf script -F cpu,op,pc,event,issue_lat,total_lat,vaddr,paddr,data_source` 的空格分隔文本；
     字段顺序即上面 -F 的顺序（cpu op pc event issue_lat total_lat vaddr paddr data_source）。
     perf script 开头的 '#' 横幅/注释行会被跳过。
  2. 带表头的逗号 CSV（如 spe-parser 产物），表头行含 op 与 pc 字段名（列顺序可任意）。
  3. 无表头 CSV/文本但列顺序已知：用 --columns 显式给出列名（逗号分隔），避免错误映射。

用法:
  spe_ldst.py <csv> [--bin BIN] [--op LD|ST|ALL] [--top N]
              [--lat-key total_lat|issue_lat] [--min-count N]
              [--columns cpu,op,pc,event,issue_lat,total_lat,vaddr,paddr,data_source]

输出: 每 PC 的 count / mean / p99 / max 延迟；给出 --bin 时用 addr2line 映射符号与源码行。
高 total_lat 的 LD 且其附近有批量 NEON store 的 PC，是 store->load forwarding failure 的候选。
"""
import argparse
import csv
import os
import statistics
import subprocess
import sys

# perf script -F cpu,op,pc,event,issue_lat,total_lat,vaddr,paddr,data_source 的字段顺序
DOC_COLUMNS = ["cpu", "op", "pc", "event", "issue_lat", "total_lat",
               "vaddr", "paddr", "data_source"]


def addr2line(binary, pc_int):
    try:
        out = subprocess.run(
            ["addr2line", "-f", "-C", "-e", binary, hex(pc_int)],
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


def split_fields(line, dialect):
    if dialect == "csv":
        return [c.strip().strip('"') for c in next(csv.reader([line]))]
    return line.split()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="SPE ld/st 文本或 CSV 文件")
    ap.add_argument("--bin", help="二进制路径 (addr2line 符号化)")
    ap.add_argument("--op", default="LD", choices=["LD", "ST", "ALL"])
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--lat-key", default="total_lat", choices=["total_lat", "issue_lat"])
    ap.add_argument("--min-count", type=int, default=0)
    ap.add_argument("--columns", help="无表头时的列名顺序（逗号分隔）")
    args = ap.parse_args()

    if not os.path.isfile(args.csv):
        print(f"错误: 输入不存在: {args.csv}", file=sys.stderr)
        return 2
    if args.bin and not os.path.isfile(args.bin):
        print(f"错误: 二进制不存在: {args.bin}", file=sys.stderr)
        return 2
    if args.lat_key not in ("total_lat", "issue_lat"):
        print(f"错误: 不支持的 --lat-key: {args.lat_key}", file=sys.stderr)
        return 2

    required = ("op", "pc", args.lat_key)
    col_map = None
    if args.columns:
        names = [n.strip().lower() for n in args.columns.split(",")]
        col_map = {n: i for i, n in enumerate(names)}
        missing = set(required) - set(col_map)
        if missing:
            print(f"错误: --columns 缺少必需列: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    agg = {}  # pc_int -> {count, lat: []}
    dialect = None
    seen = 0
    skipped = 0
    with open(args.csv, newline="", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            # 跳过 perf script 的 '#' 横幅 / 注释
            if line.lstrip().startswith("#"):
                continue
            if dialect is None:
                dialect = "csv" if "," in line else "text"
            fields = split_fields(line, dialect)

            if col_map is None:
                low = [x.lower() for x in fields]
                if "op" in low and "pc" in low:
                    col_map = {n: i for i, n in enumerate(low)}
                    missing = set(required) - set(col_map)
                    if missing:
                        print(f"错误: 表头缺少必需列: {', '.join(sorted(missing))}"
                              f"（可用 --columns 指定列顺序）", file=sys.stderr)
                        return 2
                    continue  # 表头行，不作为数据
                # 无表头：回退到文档 9 列契约
                col_map = {n: i for i, n in enumerate(DOC_COLUMNS)}

            seen += 1
            try:
                op = fields[col_map["op"]]
                lat = int(float(fields[col_map[args.lat_key]]))
                pc = int(fields[col_map["pc"]], 16)
            except (ValueError, IndexError, KeyError):
                skipped += 1
                continue
            if args.op != "ALL" and op.upper() != args.op:
                continue
            ent = agg.setdefault(pc, {"count": 0, "lat": []})
            ent["count"] += 1
            ent["lat"].append(lat)

    if col_map is None:
        print("错误: 输入为空或全部为注释/横幅行", file=sys.stderr)
        return 2
    if not agg:
        print("无匹配记录", file=sys.stderr)
        if skipped:
            print(f"提示: 跳过 {skipped}/{seen} 行（列映射不匹配）；若列顺序不是文档 9 列，"
                  f"请用 --columns 指定。", file=sys.stderr)
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
        print(f"0x{pc:016x} {cnt:>7} {mean:>8.1f} {p99v:>8} {mx:>8}  {loc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
