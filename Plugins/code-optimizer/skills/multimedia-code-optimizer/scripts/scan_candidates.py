#!/usr/bin/env python3
"""扫描项目里的可向量化候选循环，按启发式打分排序。

**输出是候选，不是结论。** 这里没有任何数据流分析：能不能向量化必须由人读代码确认
（跨迭代依赖、别名、浮点语义、溢出）。

用法:
    python3 scan_candidates.py --project /path/to/project --top 40
    python3 scan_candidates.py --project . --include src --exclude 'test|bench'

输出 JSON:
    {"success": true, "files_scanned": n, "candidates": [...], "notes": ...}
"""

import argparse
import json
import os
import re
import sys

SOURCE_EXT = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inc"}

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".dsh", "node_modules", "__pycache__",
    "simd_optimization_result", "CMakeFiles", ".cache",
}
# 构建产物的常见名字（前缀匹配）
SKIP_PREFIX = {"build", "out", "cmake-build", "dist", "target"}

FUNC_RE = re.compile(
    r"^[A-Za-z_][\w\s\*&:<>,]*?\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*$")
FUNC_INLINE_RE = re.compile(
    r"^[A-Za-z_][\w\s\*&:<>,]*?\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{")
LOOP_RE = re.compile(r"\b(for|while)\s*\(")
INDEX_RE = re.compile(r"\w+\s*\[[^\]]*\]")
INTRINSIC_RE = re.compile(r"\bsv[a-z][a-z0-9_]*_?(?:s|u|f)\d+|"
                          r"\bv(?:ld|st|add|mul|sub|zip|uzp)\d|"
                          r"__asm__|\basm\s+volatile")
SMALL_BLOCK_RE = re.compile(r"(\d+)x(\d+)")
CARRY_RE = re.compile(r"(?:\+=|-=|\*=|/=|\|=|&=|\^=)\s*\w+\s*;")
PREV_RE = re.compile(r"\[\s*\w+\s*-\s*\d+\s*\]")
CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")

# 生命周期/内存管理类函数：内部循环再多也不是热点，降权避免它们霸榜。
COLD_NAME_RE = re.compile(
    r"^(?:create|destroy|alloc|dealloc|free|init|deinit|setup|teardown|"
    r"cleanup|reset|clear|reserve|resize|open|close)\b", re.I)
COLD_BODY_RE = re.compile(
    r"\b(?:malloc|calloc|realloc|free|new|delete)\b|"
    r"\b\w*MALLOC\w*\b|\b\w*FREE\w*\b")

# 派发表注册：`p.pu[LUMA_8x8].sad = sad<8,8>;`。编码器/解码器的热点函数基本都是
# 通过这种表注册的，函数体本身没有被直接调用，只看调用点会漏掉它们。
DISPATCH_RE = re.compile(
    r"=\s*(?:&\s*)?([A-Za-z_]\w*)\s*(?:<[^;<>]*>)?\s*;")

# DSP/多媒体里常见的 kernel 词汇，只作为弱正向信号。
KERNEL_NAME_RE = re.compile(
    r"(?:blit|copy|add|sub|mul|filter|conv|transform|quant|pred|interp|"
    r"sad|ssd|satd|dct|sum|avg|diff|scale|dot|gemm|axpy|pixel)", re.I)

NOT_A_CALL = {
    "if", "for", "while", "switch", "return", "sizeof", "do", "else",
    "case", "goto", "defined", "static_cast", "reinterpret_cast", "const_cast",
}


def is_skipped(rel_parts):
    for part in rel_parts:
        if part in SKIP_DIRS:
            return True
        if any(part == p or part.startswith(p + "-") or part.startswith(p + "_")
               for p in SKIP_PREFIX):
            return True
    return False


def collect_files(root, include, exclude):
    inc_re = re.compile(include) if include else None
    exc_re = re.compile(exclude) if exclude else None
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        parts = [] if rel == "." else rel.split(os.sep)
        if is_skipped(parts):
            dirnames[:] = []
            continue
        for name in sorted(filenames):
            ext = os.path.splitext(name)[1].lower()
            if ext not in SOURCE_EXT:
                continue
            rel_path = os.path.join(rel, name) if rel != "." else name
            if inc_re and not inc_re.search(rel_path):
                continue
            if exc_re and exc_re.search(rel_path):
                continue
            out.append(os.path.join(dirpath, name))
    return out


def find_functions(lines):
    """返回 [(name, start_idx, end_idx)]，按花括号配对界定函数体。"""
    funcs = []
    depth = 0
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip("\n")
        stripped = line.strip()
        if depth == 0 and stripped and not stripped.startswith(("//", "/*", "*")):
            m = FUNC_INLINE_RE.match(stripped) or FUNC_RE.match(stripped)
            if m:
                name = m.group(1)
                if name not in NOT_A_CALL:
                    start = i
                    d = 0
                    found = False
                    j = i
                    while j < n:
                        d += lines[j].count("{") - lines[j].count("}")
                        if "{" in lines[j]:
                            found = True
                        if found and d <= 0:
                            break
                        j += 1
                    if j < n:
                        funcs.append((name, start, j))
                        i = j + 1
                        continue
        depth += line.count("{") - line.count("}")
        i += 1
    return funcs


def loop_body(lines, start, end):
    """从循环起始行找到配对的右括号，返回 (body_lines, close_idx)。"""
    depth = 0
    started = False
    for j in range(start, end + 1):
        depth += lines[j].count("{") - lines[j].count("}")
        if "{" in lines[j]:
            started = True
        if started and depth <= 0:
            return lines[start:j + 1], j
    return lines[start:end + 1], end


def has_inner_loop(body_text):
    return bool(re.search(r"\b(for|while)\s*\(", body_text))


def score_loop(func_name, body, nesting, call_sites, rel_path="",
               dispatched=False):
    signals = []
    score = 0.0

    if not has_inner_loop(body):
        score += 3.0
        signals.append("最内层循环")
    else:
        score -= 2.0
        signals.append("外层循环（内部还有循环）")

    idx_hits = len(INDEX_RE.findall(body))
    if idx_hits >= 2:
        score += 2.0
        signals.append("多次数组下标访问(%d)" % idx_hits)
    elif idx_hits == 1:
        score += 0.5
        signals.append("单次数组下标访问")

    if CARRY_RE.search(body):
        score -= 1.5
        signals.append("存在复合赋值(可能是跨迭代累加,需人工确认)")
    if PREV_RE.search(body):
        score -= 3.0
        signals.append("出现 [i-k] 回看(可能是跨迭代依赖)")

    if INTRINSIC_RE.search(body):
        score -= 4.0
        signals.append("已含 intrinsic/asm(可能已优化过)")

    if SMALL_BLOCK_RE.search(func_name):
        score += 2.0
        signals.append("固定小尺寸块命名")

    if re.search(r"\b(uint8_t|int8_t|uint16_t|int16_t|pixel|Pel)\b", body):
        score += 1.0
        signals.append("窄整型(常见向量化机会)")

    if COLD_NAME_RE.search(func_name):
        score -= 5.0
        signals.append("生命周期/管理类函数(通常不是热点)")
    if COLD_BODY_RE.search(body):
        score -= 4.0
        signals.append("含内存分配/释放")
    if KERNEL_NAME_RE.search(func_name):
        score += 1.5
        signals.append("函数名像计算内核")
    if dispatched:
        score += 3.0
        signals.append("在派发表中被注册(典型热点形态)")

    calls = {c for c in CALL_RE.findall(body) if c not in NOT_A_CALL}
    if calls:
        score -= min(2.0, 0.5 * len(calls))
        signals.append("调用外部函数: " + ",".join(sorted(calls)[:3]))

    if len(body) <= 40:
        score += 1.0
        signals.append("循环体较短")

    if nesting <= 2:
        score += 0.5

    score += min(5.0, 0.5 * call_sites)

    # 测试/示例代码不是热点，但不隐藏——降权后仍可能进候选列表。
    if re.search(r"(^|/)(tests?|examples?|unittest)/", rel_path):
        score -= 3.0
        signals.append("位于测试/示例目录")

    return score, signals


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--include", default="", help="只扫描路径匹配该正则的文件")
    ap.add_argument("--exclude", default="", help="跳过路径匹配该正则的文件")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    root = os.path.realpath(os.path.expanduser(args.project))
    if not os.path.isdir(root):
        print(json.dumps({"success": False, "error_message": "project_not_a_directory",
                          "project": root}, ensure_ascii=False, indent=2))
        return 1

    files = collect_files(root, args.include, args.exclude)
    if not files:
        print(json.dumps({"success": False, "error_message": "no_source_files",
                          "project": root}, ensure_ascii=False, indent=2))
        return 1

    # 全项目调用点计数，用来粗略衡量"多久被调用一次"
    call_counts = {}
    dispatched = set()
    texts = {}
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        texts[path] = text
        for name in CALL_RE.findall(text):
            if name in NOT_A_CALL:
                continue
            call_counts[name] = call_counts.get(name, 0) + 1
        for name in DISPATCH_RE.findall(text):
            if name in NOT_A_CALL:
                continue
            dispatched.add(name)

    candidates = []
    for path, text in texts.items():
        lines = text.splitlines()
        rel = os.path.relpath(path, root)
        for name, start, end in find_functions(lines):
            body_lines = lines[start:end + 1]
            nesting = 0
            j = start
            while j <= end:
                if LOOP_RE.search(lines[j]):
                    loop_text, close = loop_body(lines, j, end)
                    body_text = "\n".join(loop_text[1:])
                    score, signals = score_loop(
                        name, body_text, nesting,
                        call_counts.get(name, 0), rel,
                        name in dispatched)
                    candidates.append({
                        "function": name,
                        "file": rel,
                        "abs_path": path,
                        "function_line": start + 1,
                        "loop_line": j + 1,
                        "nesting": nesting,
                        "score": round(score, 2),
                        "signals": signals,
                    })
                    j = close + 1
                    nesting += 1
                else:
                    j += 1

    candidates.sort(key=lambda c: (-c["score"], c["file"], c["loop_line"]))
    top = candidates[: args.top]

    result = {
        "success": True,
        "project": root,
        "files_scanned": len(files),
        "loop_candidates": len(candidates),
        "returned": len(top),
        "candidates": top,
        "notes": ("启发式打分，未做数据流分析。必须逐个读函数体确认跨迭代依赖、"
                  "别名、浮点语义和溢出；score 只用于决定阅读顺序。"),
        "error_message": "",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
