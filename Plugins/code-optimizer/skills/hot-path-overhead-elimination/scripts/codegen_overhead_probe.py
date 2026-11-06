#!/usr/bin/env python3
"""Code-generation probe for hot-path call overhead.

解析 AArch64（鲲鹏）的 ``objdump -d`` 输出，逐符号报告：
instruction count, direct and indirect call sites, stack-frame size,
callee-saved register usage, whether the symbol is a leaf, and whether it has a
cheap conditional early exit near the entry.

``--compare`` diffs two dumps so a rewrite can be checked mechanically against
the ``codegen_expectation`` field of hot-path-overhead-elimination: the callee
should disappear from hot call sites, the predicate should show up in the
caller, and the outlined body should keep a real frame.

Usage
-----
  python3 codegen_overhead_probe.py --dump dump.txt --symbol evbuffer_add
  python3 codegen_overhead_probe.py --dump base.txt --compare cand.txt \
      --callee notify_impl --symbol append --offset 0x78
"""

import argparse
import json
import re
import sys

FUNC_RE = re.compile(r"^([0-9a-fA-F]+)\s+<([^>]+)>:$")
INSN_RE = re.compile(r"^\s+([0-9a-fA-F]+):\t(.+?)\s*$")

A64_CALLEE_SAVED_RE = re.compile(
    r"\b(x(?:19|2[0-8])|(?:d|q|v)(?:[89]|1[0-5]))\b"
)


def parse_objdump(path):
    """Return {symbol: [asm_line, ...]} preserving order."""
    functions = {}
    current = None
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            header = FUNC_RE.match(line.strip())
            if header:
                current = header.group(2)
                functions.setdefault(current, [])
                continue
            if current is None:
                continue
            insn = INSN_RE.match(line)
            if insn:
                functions[current].append(insn.group(2))
    return functions


def split_mnemonic(asm):
    parts = asm.split(None, 1)
    mnemonic = parts[0] if parts else ""
    operands = parts[1] if len(parts) > 1 else ""
    return mnemonic, operands


SYMBOL_RE = re.compile(r"<([^>]+)>")


def _to_symbol(operand):
    """Turn an AArch64 objdump call operand into a stable callee name.

    ``bl 407bc0 <evbuffer_run_callbacks>``   -> evbuffer_run_callbacks
    ``bl 400afc <evbuffer_add+0xf9>``        -> evbuffer_add (local tail call)
    """
    match = SYMBOL_RE.search(operand)
    if match:
        return match.group(1).split("+")[0].strip()
    token = operand.split()[0] if operand else ""
    return token or None


def call_targets(asm):
    """Return (is_call, target_or_None, indirect)."""
    mnemonic, operands = split_mnemonic(asm)
    if mnemonic == "bl":
        operand = operands.split("#")[0].strip()
        if re.fullmatch(r"x\d+", operand):
            return True, None, True          # bl xN: indirect through a register
        return True, _to_symbol(operand), False
    if mnemonic in ("blr", "br", "blraa", "blrab", "blraaz", "blrabz"):
        return True, None, True
    return False, None, False


def is_return(asm):
    return split_mnemonic(asm)[0] == "ret"


def is_conditional_branch(asm):
    mnemonic = split_mnemonic(asm)[0]
    return mnemonic.startswith("b.") or mnemonic in ("cbz", "cbnz", "tbz", "tbnz")


def branch_target(asm):
    operands = split_mnemonic(asm)[1]
    operand = operands.split(",")[-1].strip()
    match = re.match(r"^([0-9a-fA-F]+)\b", operand)
    if match:
        return match.group(1).lower()
    return None


def stack_frame_bytes(lines):
    total = 0
    for line in lines:
        mnemonic, operands = split_mnemonic(line)
        stp = re.search(r"\[sp,\s*#-(0x[0-9a-fA-F]+|\d+)\]!", operands)
        if mnemonic in ("stp", "str") and stp:
            value = stp.group(1)
            total += int(value, 16) if value.startswith("0x") else int(value)
        sub = re.search(r"sub\s+sp,\s*sp,\s*#(0x[0-9a-fA-F]+|\d+)", line)
        if sub:
            value = sub.group(1)
            total += int(value, 16) if value.startswith("0x") else int(value)
    return total


def callee_saved_usage(lines):
    used = set()
    for line in lines:
        mnemonic, operands = split_mnemonic(line)
        if mnemonic in ("stp", "str") and "sp" in operands:
            for match in A64_CALLEE_SAVED_RE.findall(operands.split("[")[0]):
                used.add(match)
    return sorted(used)


def early_exit(lines, addrs, window=24):
    """Detect a conditional branch in the first `window` insns jumping to a ret."""
    ret_addrs = {addr.lower() for addr, line in zip(addrs, lines) if is_return(line)}
    for index, line in enumerate(lines[:window]):
        if not is_conditional_branch(line):
            continue
        target = branch_target(line)
        if target and target in ret_addrs:
            return {
                "present": True,
                "branch_index": index,
                "branch": line,
                "target": target,
            }
    return {"present": False, "branch_index": None, "branch": None, "target": None}


def analyse_symbol(name, entries):
    addrs = [addr for addr, _ in entries]
    lines = [line for _, line in entries]
    direct, indirect, targets = 0, 0, []
    for line in lines:
        called, target, is_indirect = call_targets(line)
        if not called:
            continue
        if is_indirect:
            indirect += 1
        else:
            direct += 1
            if target:
                targets.append(target)
    frame = stack_frame_bytes(lines)
    callee_saved = callee_saved_usage(lines)
    return {
        "symbol": name,
        "instructions": len(lines),
        "direct_calls": direct,
        "indirect_calls": indirect,
        "call_total": direct + indirect,
        "call_targets": targets,
        "stack_bytes": frame,
        "callee_saved": callee_saved,
        "callee_saved_count": len(callee_saved),
        "leaf": direct + indirect == 0,
        "early_exit": early_exit(lines, addrs),
    }


def callers_of(functions, callee):
    """Count direct call sites to `callee` across every function."""
    count = 0
    for name, lines in functions.items():
        for line in lines:
            called, target, is_indirect = call_targets(line)
            if called and not is_indirect and target:
                base = target.split("+")[0].strip()
                if base == callee:
                    count += 1
    return count


def offset_operand_count(lines, offset):
    """Count loads/stores that reference a given byte offset.

    AArch64 objdump prints the immediate as ``#120`` or ``#0x78`` depending on
    the binutils version, so both forms are accepted; ``--offset`` may be given
    either way (``120`` or ``0x78``).
    """
    if offset.lower().startswith("0x"):
        value = int(offset, 16)
        needles = {f"#0x{value:x}", f"#{value}"}
    else:
        value = int(offset, 10)
        needles = {f"#{value}", f"#0x{value:x}"}
    return sum(1 for line in lines if any(needle in line for needle in needles))


def probe(args):
    functions = parse_objdump(args.dump)
    addrs_map = {}
    report = {"dump": args.dump, "function_count": len(functions)}
    if args.symbol:
        if args.symbol not in functions:
            raise SystemExit(f"symbol not found in {args.dump}: {args.symbol}")
        entries = collect_with_addrs(args.dump, args.symbol)
        symbol_report = analyse_symbol(args.symbol, entries)
        if args.offset:
            symbol_report["offset_operand_count"] = offset_operand_count(
                [line for _, line in entries], args.offset
            )
            symbol_report["offset"] = args.offset
        report["symbol"] = symbol_report
    if args.callee:
        report["callee"] = args.callee
        report["callee_call_sites"] = callers_of(functions, args.callee)
    if args.top:
        ranked = sorted(
            (
                (name, analyse_symbol(name, entries))
                for name, entries in ((n, collect_with_addrs(args.dump, n)) for n in functions)
            ),
            key=lambda item: (-item[1]["instructions"], item[0]),
        )
        report["largest"] = [
            {"symbol": name, "instructions": info["instructions"],
             "stack_bytes": info["stack_bytes"], "leaf": info["leaf"]}
            for name, info in ranked[: args.top]
        ]
    return report


def collect_with_addrs(path, symbol):
    """Re-parse for one symbol keeping (address, asm) pairs."""
    entries = []
    current = None
    capture = False
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            header = FUNC_RE.match(line.strip())
            if header:
                capture = header.group(2) == symbol
                continue
            if not capture:
                continue
            insn = INSN_RE.match(line)
            if insn:
                entries.append((insn.group(1), insn.group(2)))
    return entries


def compare(args):
    base_functions = parse_objdump(args.dump)
    cand_functions = parse_objdump(args.compare)
    result = {
        "baseline": args.dump,
        "candidate": args.compare,
        "checks": [],
        "verdict": "unknown",
    }

    if args.callee:
        before = callers_of(base_functions, args.callee)
        after = callers_of(cand_functions, args.callee)
        check = {
            "check": "callee_call_sites",
            "callee": args.callee,
            "before": before,
            "after": after,
            "expected": "after < before",
            "pass": after < before,
        }
        result["checks"].append(check)

    if args.symbol and args.symbol in base_functions:
        base = analyse_symbol(args.symbol, collect_with_addrs(args.dump, args.symbol))
        if args.symbol not in cand_functions:
            result["checks"].append({
                "check": "symbol_present",
                "symbol": args.symbol,
                "expected": "present in candidate",
                "pass": False,
            })
        else:
            cand = analyse_symbol(args.symbol, collect_with_addrs(args.compare, args.symbol))
            if args.offset:
                base["offset_operand_count"] = offset_operand_count(
                    [line for _, line in collect_with_addrs(args.dump, args.symbol)], args.offset)
                cand["offset_operand_count"] = offset_operand_count(
                    [line for _, line in collect_with_addrs(args.compare, args.symbol)], args.offset)
            merged = {}
            for key in ("instructions", "stack_bytes", "callee_saved_count",
                        "call_total", "offset_operand_count"):
                if key in base and key in cand:
                    merged[key] = {"before": base[key], "after": cand[key]}
            result["symbol"] = merged

    if args.outlined:
        present = args.outlined in cand_functions
        outlined_info = (
            analyse_symbol(args.outlined, collect_with_addrs(args.compare, args.outlined))
            if present else None
        )
        result["checks"].append({
            "check": "outlined_body_present",
            "symbol": args.outlined,
            "expected": "present in candidate, non-leaf",
            "pass": bool(present and outlined_info["call_total"] >= 0),
            "detail": outlined_info,
        })

    failed = [c for c in result["checks"] if not c.get("pass", True)]
    result["verdict"] = "pass" if not failed else "fail"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dump", required=True, help="objdump -d output (baseline)")
    parser.add_argument("--symbol", default=None, help="symbol to analyse")
    parser.add_argument("--callee", default=None,
                        help="callee symbol whose call sites should shrink")
    parser.add_argument("--outlined", default=None,
                        help="symbol expected to appear only in the candidate (outlined body)")
    parser.add_argument("--offset", default=None,
                        help="memory operand offset, e.g. 0x78, to count in the symbol")
    parser.add_argument("--top", type=int, default=0, help="report the N largest symbols")
    parser.add_argument("--compare", default=None, help="second dump to diff against")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args(argv)

    report = compare(args) if args.compare else probe(args)

    if args.json:
        json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    if report.get("verdict") == "fail":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
