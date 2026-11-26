#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# ARM SPE 记录 → 标准 CSV 转换器
# arm-spe-analysis skill - scripts/spe-to-csv.py
# 用途：将 `perf script` 输出的 SPE 采样记录逐行解析为统一 schema 的 CSV，
#       供 spe-latency-report.py / 其他开源 spe-parser 消费。
# 用法：
#   perf script -i spe.data | python3 spe-to-csv.py > samples.csv
#   python3 spe-to-csv.py spe-script.txt > samples.csv      # 从文件读
#   python3 spe-to-csv.py --validate samples.csv            # 校验 schema
#   python3 spe-to-csv.py --schema                         # 打印 schema 说明
# =============================================================================

import sys
import re
import csv
import argparse
import json

SCHEMA = [
    "comm", "pid", "tid", "cpu", "time", "period", "event",
    "ip", "sym", "dso", "weight", "data_src", "addr",
]

# perf script 基础行：
#   COMM PID [CPU] TIME: PERIOD EVENT: IP SYMBOL (DSO) [trail...]
# 兼容字段缺失（无 tid、无 symbol、无 dso）与尾部扩展字段
# （weight/data_src/addr，来自 perf script -F +weight,+data_src,+addr）。
LINE_RE = re.compile(
    r'^\s*(?P<comm>\S+)\s+'
    r'(?P<pid>\d+)\s+'
    r'(?:(?P<tid>\d+)\s+)?'
    r'\[(?P<cpu>\d+)\]\s+'
    r'(?P<time>\d+(?:\.\d+)?):\s+'
    r'(?P<period>\d+)\s+'
    r'(?P<event>[A-Za-z0-9._\-]+)\s*:\s+'
    r'(?P<ip>(?:0x)?[0-9a-fA-F]+)'
    r'(?:\s+(?P<sym>(?!\()[^\s]+(?:\+0x[0-9a-fA-F]+)?))?'
    r'(?:\s+\((?P<dso>[^)]*)\))?'
    r'(?P<trail>.*)$'
)


_SRC_EXACT = {"na", "unknown", "none", "-"}
_SRC_HINTS = ("l1", "l2", "l3", "llc", "ram", "dram", "remote", "peer",
              "mem", "snoop")


def _looks_like_src(tok):
    t = tok.lower()
    if t in _SRC_EXACT:
        return True
    if tok.isdigit() and 0 <= int(tok) <= 7:
        return True
    return any(k in t for k in _SRC_HINTS)


def parse_trail(trail):
    """从行尾扩展字段中提取 weight/data_src/addr。

    兼容两种行尾布局：
      - 关键字形式:  weight:120 data_src:L1_or_0x0 addr:0x...
      - 裸字段形式:  ``120 L1_or_0x0 0x...`` 或 ``120 1 0x...``
    """
    weight = data_src = addr = ""
    if not trail:
        return weight, data_src, addr

    m = re.search(r'(?:weight|latency|lat)\s*[=:]\s*(\d+)', trail, re.I)
    if m:
        weight = m.group(1)
    m = re.search(r'(?:data_src|mem|source|dsrc)\s*[=:]\s*(\S+)', trail, re.I)
    if m:
        data_src = m.group(1)
    m = re.search(r'(?:addr|pa|phys_addr|daddr)\s*[=:]\s*(0x[0-9a-fA-F]+)',
                  trail, re.I)
    if m:
        addr = m.group(1)

    # 清理掉已用关键字消费的 token，剩余 token 按裸字段布局处理
    clean = []
    for tok in trail.split():
        if re.match(r'(?:weight|latency|lat|data_src|mem|source|dsrc|'
                    r'addr|pa|phys_addr|daddr)\s*[=:]', tok, re.I):
            continue
        clean.append(tok)

    if not weight:
        for i, tok in enumerate(clean):
            if tok.isdigit():
                weight = tok
                clean.pop(i)
                break
    if not data_src:
        for tok in clean:
            if tok.startswith("0x"):
                continue
            if _looks_like_src(tok):
                data_src = tok
                break
    if not addr:
        for tok in clean:
            if re.match(r'0x[0-9a-fA-F]+$', tok):
                addr = tok
                break

    return weight, data_src, addr


def parse_line(line):
    m = LINE_RE.match(line)
    if not m:
        return None
    d = m.groupdict()
    weight, data_src, addr = parse_trail(d.get("trail") or "")
    return {
        "comm": (d.get("comm") or "").strip(),
        "pid": (d.get("pid") or "").strip(),
        "tid": (d.get("tid") or "").strip(),
        "cpu": (d.get("cpu") or "").strip(),
        "time": (d.get("time") or "").strip(),
        "period": (d.get("period") or "").strip(),
        "event": (d.get("event") or "").strip(),
        "ip": (d.get("ip") or "").strip(),
        "sym": (d.get("sym") or "").strip(),
        "dso": (d.get("dso") or "").strip(),
        "weight": weight,
        "data_src": data_src,
        "addr": addr,
    }


def convert(fh, out):
    writer = csv.DictWriter(out, fieldnames=SCHEMA)
    writer.writeheader()
    n = 0
    skipped = 0
    for raw in fh:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        row = parse_line(line)
        if row is None:
            skipped += 1
            continue
        writer.writerow(row)
        n += 1
    return n, skipped


def validate(path, out):
    problems = []
    n = 0
    with open(path, "r", newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            problems.append("空文件：无表头")
            header = []
        if header != SCHEMA:
            problems.append("表头不匹配：期望 %s，实际 %s" % (SCHEMA, header))
        for idx, row in enumerate(reader, start=2):
            n += 1
            if len(row) != len(SCHEMA):
                problems.append("第 %d 行列数 %d != %d" % (idx, len(row), len(SCHEMA)))
                continue
            if not row[1].isdigit():
                problems.append("第 %d 行 pid 非数字: %r" % (idx, row[1]))
    print("样本行数: %d" % n)
    if problems:
        for p in problems[:20]:
            print("  [FAIL] %s" % p, file=sys.stderr)
        print("校验失败: %d 个问题" % len(problems), file=sys.stderr)
        return 1
    print("校验通过: 表头与 %d 行记录均符合 schema" % n)
    return 0


def main():
    ap = argparse.ArgumentParser(description="ARM SPE 记录 → 标准 CSV")
    ap.add_argument("input", nargs="?", default=None,
                    help="perf script 输出文件；缺省读 stdin")
    ap.add_argument("--validate", metavar="CSV", default=None,
                    help="校验已有 CSV 是否符合 schema")
    ap.add_argument("--schema", action="store_true",
                    help="打印 CSV schema 说明")
    args = ap.parse_args()

    if args.schema:
        print("SPE 样本 CSV schema（%d 列）:" % len(SCHEMA))
        print(",".join(SCHEMA))
        print()
        print("字段说明:")
        desc = {
            "comm": "进程名/线程名", "pid": "进程 PID", "tid": "线程 TID（可缺省）",
            "cpu": "采样 CPU 编号", "time": "采样时间戳（秒）",
            "period": "采样周期计数值", "event": "SPE 事件类型（如 l1d-miss/llc-miss/tlb-miss/branch-miss）",
            "ip": "指令地址 PC", "sym": "符号（函数+偏移，可缺省）",
            "dso": "动态库/可执行文件（可缺省）",
            "weight": "指令延迟（周期数，来自 perf 的 weight 字段；不可用时为空）",
            "data_src": "数据来源层级（如 L1/L2/L3/DRAM/REMOTE；不可用时为空）",
            "addr": "数据地址（访存指令有效；不可用时为空）",
        }
        for k in SCHEMA:
            print("  %-10s %s" % (k, desc[k]))
        return 0

    if args.validate:
        return validate(args.validate, sys.stdout)

    src = sys.stdin
    if args.input:
        src = open(args.input, "r", encoding="utf-8", errors="replace")
    n, skipped = convert(src, sys.stdout)
    if args.input:
        src.close()
    if skipped:
        print("[warn] %d 行无法解析（非 SPE 采样行）" % skipped, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
