#!/usr/bin/env python3
"""
查询 TaiShan v110 (Kunpeng-0xd01) 指令性能数据。

用法:
  python3 query_tsv110.py <指令名>            查找指令性能
  python3 query_tsv110.py --list              列出所有指令
  python3 query_tsv110.py --category <分类>   按分类过滤
  python3 query_tsv110.py --resources         查看资源名含义

数据来源: tsv110_full.json (从 LLVM AArch64SchedTSV110.td 解析, 207 条指令模式)
重新生成:  python3 parse_td.py -o tsv110_full.json
"""

import sys
import re
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
FULL_JSON = SCRIPT_DIR / "tsv110_full.json"

RESOURCE_ALIAS = {
    'ALU':   'ALU',
    'AB':    'AB (ALU+BRU)',
    'ALUAB': 'ALUAB (ALU+AB group)',
    'MDU':   'MDU (Multi-Cycle)',
    'FSU1':  'FSU1 (FP/ASIMD)',
    'FSU2':  'FSU2 (FP/ASIMD)',
    'F':     'F (FSU1+FSU2 group)',
    'Ld0St': 'Ld0St (Load/Store)',
    'Ld1':   'Ld1 (Load)',
    'Ld':    'Ld (Ld0St+Ld1 group)',
}

RESOURCE_INFO = {
    'ALU':   'P0 — INT ALU，纯整数运算（不支持分支）',
    'AB':    'P1/P2 — ALU+BRU，整数运算 + Taken Branch',
    'ALUAB': 'P0-P2 组 — 任意 ALU 或 AB 端口',
    'MDU':   'P3 — Multi-Cycle，整数乘除/CRC',
    'FSU1':  'P4 — FP/ASIMD，浮点/SIMD/加密（FP32 ADD 专有）',
    'FSU2':  'P5 — FP/ASIMD，浮点/SIMD（FP32 MUL 专有）',
    'F':     'P4+P5 组 — 任意 FP/ASIMD 端口',
    'Ld0St': 'P7 — Load/Store',
    'Ld1':   'P6 — Load',
    'Ld':    'P6+P7 组 — 任意 LD/ST 端口',
}


def load_json():
    if not FULL_JSON.exists():
        print(f"错误: 找不到 {FULL_JSON}", file=sys.stderr)
        print("请先运行: python3 parse_td.py -o tsv110_full.json", file=sys.stderr)
        sys.exit(1)
    with open(FULL_JSON) as f:
        return json.load(f)


def expand_simple_alts(pattern):
    """将 (A|B) 或 [AB] 展开为多个候选字符串（只保留字母数字）。"""
    # 移除 ^ $ . * + ? 等，处理 (A|B) 和 [AB]
    p = pattern.strip('^$')
    # 展开 [AB] → A, B
    p = re.sub(r'\[([^\]]+)\]', lambda m: '(' + '|'.join(m.group(1)) + ')', p)
    # 展开 (A|B|C) → 多个版本
    alts = ['']
    i = 0
    while i < len(p):
        if p[i] == '(':
            end = p.index(')', i)
            opts = p[i+1:end].split('|')
            new_alts = []
            for a in alts:
                for o in opts:
                    new_alts.append(a + o)
            alts = new_alts
            i = end + 1
        elif p[i] in '{}[]?*+.\\':
            i += 1
            if i < len(p) and p[i-1] == '\\':
                alts = [a + p[i-1:i+1].replace('\\', '') for a in alts]
            # skip regex metachar content
            while i < len(p) and p[i] in '0123456789,}':
                i += 1
        else:
            alts = [a + p[i] for a in alts]
            i += 1
    # 每候选只保留字母数字
    return [''.join(c for c in a if c.isalnum()) for a in alts]


def query(name, instructions):
    """在 TD 指令列表中查找匹配项。"""
    results = []
    for inst in instructions:
        p = inst['pattern'].strip('^$')
        # 精确正则匹配
        try:
            if re.fullmatch(p, name, re.IGNORECASE):
                results.append(inst)
                continue
        except re.error:
            pass
        # 展开 (A|B) 后做子串匹配
        expanded = expand_simple_alts(inst['pattern'])
        for alpha in expanded:
            if name.lower() in alpha.lower():
                results.append(inst)
                break
    return results


def filter_by_category(category, instructions):
    return [i for i in instructions if i['category'] == category]


def format_inst(inst):
    p = inst['pattern']
    resources_str = ', '.join(RESOURCE_ALIAS.get(r, r) for r in inst['ports'])
    tp = inst['throughput']
    if tp <= 0:
        tp_str = "n/a"
    elif tp >= 1:
        tp_str = f"{tp:.1f} inst/c"
    else:
        tp_str = f"1/{1/tp:.1f} inst/c ({tp:.3f})"
    rc_parts = [f"{r}:{c:.0f}c" for r, c in inst['resource_cycles'].items()]
    rc_str = ', '.join(rc_parts) if rc_parts else '—'
    return (
        f"  {p}\n"
        f"    Category:      {inst['category']}\n"
        f"    Latency:       {inst['latency']}c\n"
        f"    Throughput:    {tp_str}\n"
        f"    uOps:          {inst['uops']}\n"
        f"    Resources:     {resources_str}\n"
        f"    ResourceUsage:  {rc_str}"
    )


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    # --resources: 显示资源名含义
    if cmd == '--resources':
        print("LLVM 资源编号 → 端口 → 功能\n")
        for r, desc in RESOURCE_INFO.items():
            print(f"  {r:8s}  {desc}")
        return

    data = load_json()
    instructions = data['instructions']

    # --list: 列出所有指令
    if cmd == '--list':
        print(f"共 {len(instructions)} 条指令模式:\n")
        for i in instructions:
            print(f"  {i['pattern']:50s} {i['category']:20s} L={i['latency']}c")
        return

    # --category: 按分类过滤
    if cmd == '--category':
        if len(sys.argv) < 3:
            print("用法: python3 query_tsv110.py --category <分类名>")
            return
        results = filter_by_category(sys.argv[2], instructions)
        print(f"分类 '{sys.argv[2]}': {len(results)} 条\n")
        for r in results:
            print(format_inst(r))
            print()
        return

    # 默认：按指令名查询
    results = query(cmd, instructions)
    print(f"匹配 '{cmd}' 的指令 ({len(results)} 条):\n")
    if not results:
        print("  未找到匹配项")
        # 建议
        alpha_map = {}
        for i in instructions:
            alpha = ''.join(c for c in i['pattern'] if c.isalnum())
            if cmd.lower() in alpha.lower():
                alpha_map[i['pattern']] = i
        if alpha_map:
            print(f"\n  可能相关:")
            for p in alpha_map:
                print(f"    {p}")
        return

    for r in results:
        print(format_inst(r))
        print()


if __name__ == '__main__':
    main()
