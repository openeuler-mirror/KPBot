#!/usr/bin/env python3
"""Discover SIMD conversion candidates in one project or between two trees.

The scanner is deliberately heuristic: it builds a function inventory, labels
each function with the instruction set it appears to use, and proposes
same-operation pairs.  The agent still has to read both bodies and decide
whether a proposal is a real vectorisation rewrite.

Usage:
    python3 scan_simd.py project --root /path/to/project --out /tmp/scan.json
    python3 scan_simd.py different --old /path/old --new /path/new --out /tmp/scan.json

Only source files are read.  Nothing is written outside --out (or stdout).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

SOURCE_EXTS = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".s", ".S", ".asm",
}
ASM_EXTS = {".s", ".S", ".asm"}
SKIP_DIRS = {
    ".git", ".hg", ".svn", "build", "Build", "build-debug", "build-release",
    "cmake-build-debug", "cmake-build-release", "node_modules", "__pycache__",
    ".deps", ".cache", ".dsh", "dist", "out",
}
# Other SIMD backends are not C/NEON/SVE references.  Keeping them out avoids
# pairs such as RVV <-> NEON, which look like C -> NEON after suffix stripping.
OTHER_ARCH_PATH_RE = re.compile(
    r"(?:^|/)(?:riscv(?:64)?|loongarch(?:64)?|x86|x86_64|ppc|msa|wasm)(?:/|$)",
    re.IGNORECASE)
OTHER_ARCH_NAME_RE = re.compile(
    r"_(?:rvv|lasx|lsx|avx\d*|sse\d*|ssse3|msa)$",
    re.IGNORECASE)

# ---------------------------------------------------------------------------
# Instruction-set detection
# ---------------------------------------------------------------------------

NEON_PREFIXES = (
    "ld1|ld2|ld3|ld4|st1|st2|st3|st4|ld1q|st1q|"
    "add|sub|mul|mla|mls|fma|fms|abd|aba|max|min|padd|paddl|padal|"
    "qadd|qsub|qmul|qdmulh|qrdmulh|qrdmlah|qrdmlsh|qdmull|qdmlal|qdmlsl|"
    "shl|shr|sra|sri|sli|shrn|rshrn|qshrn|qrshrn|qshrun|qrshrun|"
    "dup|mov|get|set|combine|create|reinterpret|zip|uzp|trn|ext|rev|"
    "tbl|tbx|qtbl|qtbx|bsl|bit|bif|and|orr|eor|bic|not|mvn|clz|cnt|cls|rbit|"
    "abs|neg|sqrt|rsqrte|recpe|recps|rsqrts|cvt|rint|round|ceil|floor|trunc|"
    "tst|ceq|cge|cle|cgt|clt|cage|calt|cale|cagt|"
    "qmovn|movn|movl|movw|qxtn|qxtun|xtn|addhn|subhn|raddhn|rsubhn|"
    "addl|addw|subl|subw|mull|mlal|mlsl|abal|abdl|sabal|uabal|sabdl|uabdl|"
    "adalp|sadalp|uadalp|addlp|saddlp|uaddlp|addv|maxv|minv|fmaxv|fminv|"
    "fmaxnmv|fminnmv|dot|usdot|udot|sdot|bfdot|bfmmla|mmla|"
    "fmla|fmls|fmlal|fmlsl|vfma|vfms|vfnma|vfnms|"
    "paddl|padal|qrdmlah|qrdmlsh|qshl|qrshl|sqshl|uqshl|srshl|"
    "smax|umax|smin|umin|shll|ushll|sshll|saddl|uaddl|smull|umull|"
    "smlal|umlal|sadalp|uadalp|saddlp|uaddlp|sabal|uabal|sabdl|uabdl|"
    "sri|sli|sqxtn|uqxtn|sqxtun|sqshrn|uqshrn|sqrshrn|uqrshrn|"
    "sqxtnb|uqxtnb|sqxtnt|uqxtnt|"
)
NEON_CALL_RE = re.compile(
    r"\bv(?:" + NEON_PREFIXES + r")(?:q)?(?:_[a-z0-9]+)*\s*\(")
NEON_TYPE_RE = re.compile(
    r"\b(?:u?int|float)(?:8|16|32|64)x(?:1|2|4|8|16)_t\b")
NEON_ASM_REG_RE = re.compile(
    r"\bv\d+\.(?:8b|16b|4h|8h|2s|4s|1d|2d)\b")
NEON_ASM_LD_RE = re.compile(
    r"\b(?:ld[1-4]|st[1-4])\s*\{?\s*v\d+")
NEON_ASM_MNEMONIC_RE = re.compile(
    r"\b(?:add|sub|mul|mla|mls|fmla|fmls|sabd|uabd|smax|umax|smin|umin|"
    r"dup|ins|ext|tbl|tbx|zip1|zip2|uzp1|uzp2|trn1|trn2|rev64|rev32|rev16|"
    r"cmeq|cmge|cmgt|cmhi|cmhs|sshr|ushr|shl|sli|sri|sqxtn|uqxtn|sqxtun|xtn|"
    r"addv|smaxv|umaxv|sminv|uminv|faddp|addp|movi|mvni|bic|orr|eor|and|bsl|"
    r"bit|bif|fabd|fabs|fneg|fsqrt|frecpe|frsqrte|fcvt|scvtf|ucvtf|"
    r"ushll|sshll|shll|saddl|uaddl|smull|umull|smlal|umlal|sadalp|uadalp|"
    r"saddlp|uaddlp|sabal|uabal|sabdl|uabdl)\s+v\d+\.")
NEON_ASM32_LD_RE = re.compile(
    r"\bv(?:ld[1-4]|st[1-4])\.[0-9]+\s*\{?\s*[dq]\d+")
NEON_ASM32_OP_RE = re.compile(
    r"\bv[a-z0-9]+\.[a-z0-9]+\s+[dq]\d+")
NEON_INCLUDE_RE = re.compile(r"#\s*include\s*[<\"]arm_neon\.h[>\"]")

SVE_PREFIXES = (
    "ld1|ld2|ld3|ld4|st1|st2|st3|st4|ldnt1|stnt1|"
    "add|sub|mul|mla|mls|mad|msb|abd|aba|max|min|"
    "qadd|qsub|qmul|qdmulh|qrdmulh|qrdmlah|qrdmlsh|"
    "whilelt|whilele|whilelo|whilels|whilege|whilegt|"
    "ptrue|pfalse|ptest|ptest_any|ptest_all|"
    "cnt|inc|dec|index|dup|sel|mov|reinterpret|"
    "and|orr|eor|bic|not|bsl|clz|cnt|cls|rbit|"
    "abs|neg|sqrt|rsqrte|recpe|recps|rsqrts|"
    "cvt|rint|round|ceil|floor|trunc|fcvt|scvtf|ucvtf|"
    "cmp|cmeq|cmne|cmge|cmgt|cmle|cmelt|cmlt|cmhi|cmhs|"
    "tbl|tbx|zip|uzp|trn|rev|compact|splice|ext|"
    "qxtn|qxtun|xtn|qxtnb|qxtnt|uqxtnb|uqxtnt|sqxtnb|sqxtnt|"
    "addl|addw|subl|subw|mull|mlal|mlsl|abdl|abal|sabdl|uabdl|"
    "sadalp|uadalp|saddlp|uaddlp|adalp|addlp|"
    "addv|maxv|minv|fmaxv|fminv|fmaxnmv|fminnmv|"
    "dot|usdot|udot|sdot|bfdot|mmla|"
    "fma|fms|fnma|fnms|"
    "get|set|create|tuple|zip|uzp|trn|unpk|"
    "ld1_gather|ld1sb|ld1ub|ld1sh|ld1uh|ld1sw|ld1uw|"
    "st1b|st1h|st1w|st1d|prfb|prfh|prfw|prfd|"
)
SVE_CALL_RE = re.compile(
    r"\bsv(?:" + SVE_PREFIXES + r")(?:_[a-z0-9]+)*\s*\(")
SVE_TYPE_RE = re.compile(
    r"\bsv(?:bool|u?int(?:8|16|32|64)|float(?:16|32|64))_t\b")
SVE_ASM_RE = re.compile(
    r"\b(?:ptrue|pfalse|whilelt|whilele|whilelo|whilels|whilege|whilegt|"
    r"cntb|cntw|cntd|cntb|cntsb|incb|incw|incd|incp|decd|decw|decb)\b")
SVE_ASM_REG_RE = re.compile(r"\bz\d+\.(?:b|h|s|d)\b")
SVE_INCLUDE_RE = re.compile(r"#\s*include\s*[<\"]arm_sve\.h[>\"]")


def _count(pattern: re.Pattern[str], text: str) -> int:
    return len(pattern.findall(text))


@dataclass
class Function:
    name: str
    file: str               # relative to the scanned root
    line: int
    end_line: int
    body: str
    lang: str = "c"          # c | asm
    argc: int = 0
    signature: str = ""
    isa: str = "c"
    neon_score: int = 0
    sve_score: int = 0
    key: str = ""
    dispatch_keys: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {
            "name": self.name,
            "file": self.file,
            "line": self.line,
            "end_line": self.end_line,
            "lang": self.lang,
            "argc": self.argc,
            "isa": self.isa,
            "neon_score": self.neon_score,
            "sve_score": self.sve_score,
            "key": self.key,
        }


def classify_isa(text: str, lang: str = "c") -> tuple[str, int, int]:
    """Return (isa, neon_score, sve_score).

    `isa` is the dominant language of the snippet: c, neon, sve or mixed.
    Scores count independent evidence, not characters.
    """
    sve = 0
    sve += 2 * _count(SVE_INCLUDE_RE, text)
    sve += 2 * _count(SVE_TYPE_RE, text)
    sve += _count(SVE_CALL_RE, text)
    if lang == "asm":
        sve += 2 * _count(SVE_ASM_REG_RE, text)
        sve += 2 * _count(SVE_ASM_RE, text)

    neon = 0
    neon += 2 * _count(NEON_INCLUDE_RE, text)
    neon += 2 * _count(NEON_TYPE_RE, text)
    neon += _count(NEON_CALL_RE, text)
    if lang == "asm":
        neon += 2 * _count(NEON_ASM_REG_RE, text)
        neon += _count(NEON_ASM_LD_RE, text)
        neon += _count(NEON_ASM_MNEMONIC_RE, text)
        # AArch32 NEON uses d/q registers and typed mnemonics such as
        # vrhadd.u8 or vld1.32 {d0}, which the AArch64 patterns miss.
        neon += 2 * _count(NEON_ASM32_LD_RE, text)
        neon += _count(NEON_ASM32_OP_RE, text)

    if sve and neon:
        dominant = "sve" if sve >= neon else "neon"
        return "mixed", neon, sve
    if sve:
        return "sve", neon, sve
    if neon:
        return "neon", neon, sve
    return "c", neon, sve


def target_isa(rec: Function) -> str:
    """Best guess at which instruction set an implementation is written in.

    Any use of intrinsics counts: a wrapper that calls a handful of NEON
    helpers is still NEON-side code, not a scalar reference.  A handful of
    unrelated scalar functions can therefore be classified as vector code,
    but that is safer than offering them as C references.
    """
    if rec.isa == "mixed":
        return "sve" if rec.sve_score >= rec.neon_score else "neon"
    if rec.isa in ("sve", "neon"):
        return rec.isa
    if rec.sve_score and not rec.neon_score:
        return "sve"
    if rec.neon_score and not rec.sve_score:
        return "neon"
    if rec.sve_score and rec.neon_score:
        return "sve" if rec.sve_score >= rec.neon_score else "neon"
    return "c"


def pair_category(before: Function, after: Function) -> str | None:
    b, a = target_isa(before), target_isa(after)
    if b == a:
        return None
    mapping = {("c", "neon"): "C_NEON", ("c", "sve"): "C_SVE",
               ("neon", "sve"): "NEON_SVE"}
    return mapping.get((b, a))


# ---------------------------------------------------------------------------
# C/C++ function extraction
# ---------------------------------------------------------------------------

def mask_comments_and_strings(text: str) -> str:
    """Replace comments and string bodies with spaces, preserving offsets."""
    out = list(text)
    i, n = 0, len(text)
    state = "code"
    while i < n:
        c = text[i]
        if state == "code":
            if c == "/" and i + 1 < n and text[i + 1] == "/":
                out[i] = out[i + 1] = " "
                i += 2
                state = "line"
            elif c == "/" and i + 1 < n and text[i + 1] == "*":
                out[i] = out[i + 1] = " "
                i += 2
                state = "block"
            elif c == '"':
                out[i] = " "
                i += 1
                state = "string"
            elif c == "'":
                out[i] = " "
                i += 1
                state = "char"
            else:
                i += 1
        elif state == "line":
            if c == "\n":
                state = "code"
            else:
                out[i] = " "
            i += 1
        elif state == "block":
            if c == "*" and i + 1 < n and text[i + 1] == "/":
                out[i] = out[i + 1] = " "
                i += 2
                state = "code"
            else:
                if c != "\n":
                    out[i] = " "
                i += 1
        elif state in ("string", "char"):
            quote = '"' if state == "string" else "'"
            if c == "\\":
                out[i] = " "
                if i + 1 < n:
                    if text[i + 1] != "\n":
                        out[i + 1] = " "
                i += 2
            elif c == quote:
                out[i] = " "
                i += 1
                state = "code"
            else:
                if c != "\n":
                    out[i] = " "
                i += 1
    return "".join(out)


CONTROL_WORDS = {
    "if", "for", "while", "switch", "catch", "do", "else", "try", "return",
    "sizeof", "alignof", "decltype", "static_assert", "assert", "noexcept",
    "throw", "new", "delete", "case", "default", "operator",
}


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _matching_brace(text: str, open_pos: int) -> int:
    depth = 0
    for i in range(open_pos, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(text) - 1


def _count_args(params: str) -> int:
    if not params.strip() or params.strip() == "void":
        return 0
    depth = 0
    count = 1
    for ch in params:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            count += 1
    return count


def _parse_c_header(header: str) -> tuple[str, str, int] | None:
    """Return (name, signature, argc) when `header` opens a function body."""
    # Preprocessor lines before a definition are not part of its signature.
    # Dropping them also prevents the first function after an include block
    # from being rejected as a macro.
    h = re.sub(r"(?m)^\s*#.*$", "", header).strip()
    if not h or len(h) > 4000:
        return None
    if h.startswith("#") or "#define" in h:
        return None
    # Last balanced parenthesis group is the parameter list in normal code.
    close = h.rfind(")")
    if close < 0:
        return None
    depth = 0
    open_pos = -1
    for i in range(close, -1, -1):
        if h[i] == ")":
            depth += 1
        elif h[i] == "(":
            depth -= 1
            if depth == 0:
                open_pos = i
                break
    if open_pos <= 0:
        return None
    prefix = h[:open_pos]
    tail = h[close + 1:].strip()
    if tail and not re.fullmatch(
            r"(?:const|volatile|noexcept|override|final|&|&&|->.*|"
            r"__attribute__\s*\(\(.*\)\)|asm\s*\(.*\))+",
            tail, re.S):
        return None
    if "=" in prefix and "operator=" not in prefix:
        return None
    if prefix.rstrip().endswith("]") or "[]" in prefix[-4:]:
        return None
    name_match = re.search(
        r"([A-Za-z_]\w*(?:::[A-Za-z_~]\w*)*|operator\s*[^\s(]+)\s*$", prefix)
    if not name_match:
        return None
    name = name_match.group(1)
    short = name.split("::")[-1].lstrip("~")
    if short in CONTROL_WORDS or short.startswith("__attribute__"):
        return None
    if not re.match(r"^[A-Za-z_]\w*$", short):
        return None
    # A declaration like "int x[]" / "auto v = ..." never reaches here because
    # the header must end with a parameter list and no top-level '='.
    if re.search(r"\b(?:class|struct|union|enum|namespace)\b[^;{]*$", prefix):
        return None
    signature = re.sub(r"\s+", " ", h)

    # Recover the raw parameter text (between open_pos and close).
    params = h[open_pos + 1:close]
    return name, signature, _count_args(params)


def find_c_functions(text: str, rel_file: str) -> list[Function]:
    masked = mask_comments_and_strings(text)
    funcs: list[Function] = []
    i, n = 0, len(masked)
    while i < n:
        if masked[i] != "{":
            i += 1
            continue
        start = max(masked.rfind(";", 0, i), masked.rfind("}", 0, i),
                    masked.rfind("{", 0, i))
        header = masked[start + 1:i]
        parsed = _parse_c_header(header)
        if not parsed:
            i += 1
            continue
        end = _matching_brace(masked, i)
        name, signature, argc = parsed
        body = text[i:end + 1]
        body_masked = masked[i:end + 1]
        isa, neon, sve = classify_isa(body_masked, "c")
        funcs.append(Function(
            name=name.split("::")[-1],
            file=rel_file,
            line=_line_of(masked, start + 1),
            end_line=_line_of(masked, end),
            body=body,
            lang="c",
            argc=argc,
            signature=signature,
            isa=isa,
            neon_score=neon,
            sve_score=sve,
        ))
        i = end + 1
    return funcs


# ---------------------------------------------------------------------------
# Assembly function extraction
# ---------------------------------------------------------------------------

ASM_START_RE = re.compile(r"^\s*(?:function|FUNC)\s+(.+?)\s*$")
ASM_END_RE = re.compile(r"^\s*(?:endfunc|\.endfunc|ENDFUNC)\b")
ASM_TYPE_RE = re.compile(r"^\s*\.type\s+([^,]+),\s*%function\s*$")
ASM_LABEL_RE = re.compile(r"^([A-Za-z_.$][\w.$]*)\s*:")


def _asm_name(expr: str) -> str:
    expr = expr.strip()
    m = re.search(r"PFX(?:\d*)?\s*\((.+)\)", expr)
    if m:
        expr = m.group(1)
    expr = expr.replace("\\h", "").replace("\\", "")
    tokens = re.findall(r"[A-Za-z_]\w*", expr)
    if not tokens:
        return ""
    # The last token is normally the function name; macro suffixes like PFX
    # contribute earlier tokens only.
    for token in reversed(tokens):
        if token not in {"PFX", "PFX2", "PFX3", "EXTERN_ASM", "X265_NS"}:
            return token
    return tokens[-1]


def find_asm_functions(text: str, rel_file: str) -> list[Function]:
    lines = text.splitlines()
    starts: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        m = ASM_START_RE.match(line)
        if m:
            starts.append((idx, _asm_name(m.group(1))))
    funcs: list[Function] = []
    if starts:
        for pos, (idx, name) in enumerate(starts):
            end_idx = len(lines) - 1
            for j in range(idx + 1, len(lines)):
                if ASM_END_RE.match(lines[j]):
                    end_idx = j
                    break
            body = "\n".join(lines[idx:end_idx + 1])
            isa, neon, sve = classify_isa(body, "asm")
            funcs.append(Function(
                name=name, file=rel_file, line=idx + 1, end_line=end_idx + 1,
                body=body, lang="asm", isa=isa, neon_score=neon,
                sve_score=sve, argc=0,
            ))
        return funcs

    # Fallback: .type NAME, %function followed by a label.
    pending: str | None = None
    label_line = 0
    for idx, line in enumerate(lines):
        m = ASM_TYPE_RE.match(line)
        if m:
            pending = _asm_name(m.group(1))
            continue
        lm = ASM_LABEL_RE.match(line)
        if pending and lm:
            label_line = idx
            body = "\n".join(lines[label_line:])
            isa, neon, sve = classify_isa(body, "asm")
            funcs.append(Function(
                name=pending, file=rel_file, line=idx + 1,
                end_line=len(lines), body=body, lang="asm", isa=isa,
                neon_score=neon, sve_score=sve,
            ))
            pending = None
    return funcs


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

ISA_SUFFIX_RE = re.compile(
    r"_(?:neon_dotprod|neon_i8mm|neon|sve2_bitperm|sve2|sve|dotprod|i8mm|"
    r"rvv|lasx|lsx|avx512|avx2|avx|ssse3|sse41|sse4|sse2)$")
DIM_SUFFIX_RE = re.compile(r"_(?:\d+x\d+|x\d+)$")


def base_key(name: str) -> str:
    """Reduce a symbol to an operation key, removing ISA and size suffixes."""
    k = name.lower()
    k = re.sub(r"^x265_", "", k)
    k = re.sub(r"^pixel_", "", k)
    changed = True
    while changed:
        changed = False
        new = ISA_SUFFIX_RE.sub("", k)
        if new != k:
            k, changed = new, True
        if k.endswith("_c"):
            k, changed = k[:-2], True
        new = DIM_SUFFIX_RE.sub("", k)
        if new != k:
            k, changed = new, True
    k = re.sub(r"^interp\d+_", "interp_", k)
    k = re.sub(r"^filter(\d+)_", r"filter\1_", k)
    k = re.sub(r"\d+$", "", k)
    k = re.sub(r"_+", "_", k).strip("_")
    return k


def iter_files(root: Path, include: str | None, exclude: str | None) -> Iterable[Path]:
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            path = Path(dirpath) / fn
            if path.suffix not in SOURCE_EXTS:
                continue
            rel = path.relative_to(root).as_posix()
            if OTHER_ARCH_PATH_RE.search(rel):
                continue
            if include and not re.search(include, rel):
                continue
            if exclude and re.search(exclude, rel):
                continue
            yield path


def build_inventory(root: Path, include: str | None, exclude: str | None
                    ) -> tuple[list[Function], int]:
    root = root.resolve()
    funcs: list[Function] = []
    files = 0
    for path in iter_files(root, include, exclude):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\x00" in text[:4096]:
            continue
        files += 1
        rel = path.relative_to(root).as_posix()
        if path.suffix in ASM_EXTS:
            found = find_asm_functions(text, rel)
        else:
            found = find_c_functions(text, rel)
        for f in found:
            if OTHER_ARCH_NAME_RE.search(f.name):
                continue
            f.key = base_key(f.name)
            funcs.append(f)
    return funcs, files


def parse_dispatch(funcs: list[Function]) -> dict[str, list[Function]]:
    """Map primitive-field assignments (p.cu[...].sad = ...) to functions."""
    by_name: dict[str, Function] = {}
    for f in funcs:
        by_name.setdefault(f.name, f)
    field_groups: dict[str, list[Function]] = defaultdict(list)
    assign_re = re.compile(
        r"([A-Za-z_]\w*(?:\[[^\]]*\])?(?:\.\w+(?:\[[^\]]*\])?)+)\s*=\s*"
        r"(?:PFX\s*\(\s*)?([A-Za-z_]\w*)")
    for f in funcs:
        if "setup" not in f.name.lower() and "Primitives" not in f.body:
            continue
        for m in assign_re.finditer(mask_comments_and_strings(f.body)):
            lhs = re.sub(r"\[[^\]]*\]", "[]", m.group(1))
            lhs = re.sub(r"\s+", "", lhs)
            rhs_name = m.group(2)
            target = by_name.get(rhs_name)
            if target is None:
                # Assignment through a macro or a function not parsed yet.
                continue
            field_groups[lhs].append(target)
    # Deduplicate functions per field.
    for lhs, items in list(field_groups.items()):
        seen = set()
        unique = []
        for item in items:
            ident = (item.name, item.file)
            if ident not in seen:
                seen.add(ident)
                unique.append(item)
        field_groups[lhs] = unique
    return field_groups


# ---------------------------------------------------------------------------
# Pair proposal
# ---------------------------------------------------------------------------

DIM_RE = re.compile(r"\d+x\d+(?:x\d+)?")
TRAILING_NUM_RE = re.compile(r"(\d+)$")


def _dims(name: str) -> set[str]:
    lower = name.lower()
    dims = set(DIM_RE.findall(lower))
    # Names such as partialButterfly16 encode the block size as a trailing
    # number rather than WxH.
    stem = ISA_SUFFIX_RE.sub("", lower)
    stem = re.sub(r"_c$", "", stem)
    match = TRAILING_NUM_RE.search(stem)
    if match:
        dims.add(match.group(1))
    return dims


def _name_similarity(a: Function, b: Function) -> float:
    return SequenceMatcher(None, a.key, b.key).ratio()


def _dimension_penalty(a: Function, b: Function) -> tuple[float, bool]:
    """Different explicit block sizes are strong evidence of different ops."""
    da, db = _dims(a.name), _dims(b.name)
    if da and db and da != db:
        return 0.45, True
    return 1.0, False


def _candidate(prefix: str, idx: int, kind: str, category: str,
               before: Function, after: Function, confidence: float,
               reason: str, **extra) -> dict:
    item = {
        "id": f"{prefix}{idx}",
        "kind": kind,
        "isa_pair": category,
        "confidence": round(confidence, 2),
        "reason": reason,
        "before": before.public(),
        "after": after.public(),
    }
    item.update(extra)
    return item


def propose_project(funcs: list[Function]) -> list[dict]:
    groups: dict[str, list[Function]] = defaultdict(list)
    for f in funcs:
        if f.key:
            groups[f.key].append(f)

    proposals: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    idx = 0

    def add(before: Function, after: Function, confidence: float,
            reason: str, **extra) -> None:
        nonlocal idx
        cat = pair_category(before, after)
        if not cat:
            return
        ident = (before.name, after.name, cat)
        if ident in seen:
            return
        seen.add(ident)
        idx += 1
        proposals.append(_candidate(
            "p", idx, "project", cat, before, after, confidence, reason, **extra))

    for key, members in sorted(groups.items()):
        cats: dict[str, list[Function]] = defaultdict(list)
        for m in members:
            cats[target_isa(m)].append(m)
        isas = [i for i in ("c", "neon", "sve") if cats.get(i)]
        if len(isas) < 2:
            continue
        for i in range(len(isas)):
            for j in range(i + 1, len(isas)):
                left, right = isas[i], isas[j]
                a_list, b_list = cats[left], cats[right]
                best: tuple[float, Function, Function, bool] | None = None
                for a in a_list:
                    for b in b_list:
                        similarity = _name_similarity(a, b)
                        penalty, mismatch = _dimension_penalty(a, b)
                        score = similarity * penalty
                        if best is None or score > best[0]:
                            best = (score, a, b, mismatch)
                assert best is not None
                score, a, b, mismatch = best
                # C reference should be "before", vector code "after".
                if right == "c":
                    before, after = b, a
                else:
                    before, after = a, b
                add(before, after, 0.55 + 0.4 * score,
                    "base-name" + (":dimension-mismatch" if mismatch else ""),
                    group=key, alternatives=len(members))

    # Dispatch-table groups catch renamed C/NEON/SVE implementations.
    field_groups = parse_dispatch(funcs)
    for lhs, members in sorted(field_groups.items()):
        cats: dict[str, list[Function]] = defaultdict(list)
        for m in members:
            cats[target_isa(m)].append(m)
        isas = [i for i in ("c", "neon", "sve") if cats.get(i)]
        if len(isas) < 2:
            continue
        for i in range(len(isas)):
            for j in range(i + 1, len(isas)):
                left, right = isas[i], isas[j]
                a, b = cats[left][0], cats[right][0]
                if right == "c":
                    before, after = b, a
                else:
                    before, after = a, b
                add(before, after, 0.9, "dispatch-field",
                    group=lhs, alternatives=len(members))

    proposals.sort(key=lambda x: (-x["confidence"], x["after"]["file"],
                                  x["after"]["line"]))
    return proposals


def _function_index(funcs: list[Function]) -> dict[tuple[str, str], list[Function]]:
    index: dict[tuple[str, str], list[Function]] = defaultdict(list)
    for f in funcs:
        index[(f.file, f.name)].append(f)
    return index


def _same_body(a: Function, b: Function) -> bool:
    norm = lambda s: re.sub(r"\s+", " ", s).strip()
    return norm(a.body) == norm(b.body)


def _best_reference(added: Function, pool: list[Function],
                    field_groups: dict[str, list[Function]]) -> tuple[Function | None, str, float]:
    """Find a scalar/NEON implementation of the same operation."""
    want = target_isa(added)
    if want == "c":
        return None, "", 0.0
    candidates: list[tuple[float, Function, str]] = []

    for f in pool:
        if f is added:
            continue
        f_isa = target_isa(f)
        if f_isa == want:
            continue
        if f.key and f.key == added.key:
            penalty, mismatch = _dimension_penalty(added, f)
            score = (0.9 + 0.09 * _name_similarity(added, f)) * penalty
            reason = "base-name" + (":dimension-mismatch" if mismatch else "")
            candidates.append((score, f, reason))
    for lhs, members in field_groups.items():
        names = {m.name for m in members}
        if added.name not in names:
            continue
        for f in members:
            if f is added or target_isa(f) == want:
                continue
            candidates.append((0.95, f, f"dispatch-field:{lhs}"))

    if not candidates:
        return None, "", 0.0
    candidates.sort(key=lambda item: -item[0])
    score, ref, reason = candidates[0]
    return ref, reason, min(0.99, score)


def propose_different(old_funcs: list[Function], new_funcs: list[Function]
                      ) -> tuple[list[dict], list[dict]]:
    old_index = _function_index(old_funcs)
    new_index = _function_index(new_funcs)
    proposals: list[dict] = []
    unpaired: list[dict] = []
    idx = 0

    # Modified implementations: same file + name, different body.
    for key in sorted(set(old_index) & set(new_index)):
        olds = old_index[key]
        news = new_index[key]
        used_new: set[int] = set()
        for old in olds:
            match = None
            for pos, new in enumerate(news):
                if pos in used_new:
                    continue
                if old.argc == new.argc:
                    match = (pos, new)
                    break
            if match is None and news:
                match = (0, news[0])
            if match is None:
                continue
            pos, new = match
            used_new.add(pos)
            if _same_body(old, new):
                continue
            cat = pair_category(old, new)
            if not cat:
                continue
            idx += 1
            proposals.append(_candidate(
                "d", idx, "modified", cat, old, new, 0.99,
                "same-name-modified", old_file=old.file, new_file=new.file))

    # Added vector implementations: pair them with a scalar/NEON reference.
    field_groups_new = parse_dispatch(new_funcs)
    field_groups_old = parse_dispatch(old_funcs)
    pool = new_funcs + old_funcs
    field_groups: dict[str, list[Function]] = defaultdict(list)
    for source in (field_groups_new, field_groups_old):
        for k, v in source.items():
            field_groups[k].extend(v)

    for key in sorted(set(new_index) - set(old_index)):
        for new in new_index[key]:
            want = target_isa(new)
            if want not in ("neon", "sve"):
                continue
            ref, reason, confidence = _best_reference(new, pool, field_groups)
            if ref is None:
                unpaired.append({"name": new.name, "file": new.file,
                                 "line": new.line, "isa": target_isa(new)})
                continue
            cat = pair_category(ref, new)
            if not cat:
                unpaired.append({"name": new.name, "file": new.file,
                                 "line": new.line, "isa": target_isa(new)})
                continue
            idx += 1
            proposals.append(_candidate(
                "d", idx, "added", cat, ref, new, confidence,
                "vector-impl-added:" + reason,
                reference_repo="new" if ref in new_funcs else "old",
                new_file=new.file))

    proposals.sort(key=lambda x: (-x["confidence"], x["after"]["file"],
                                  x["after"]["line"]))
    return proposals, unpaired


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _stats(funcs: list[Function]) -> dict:
    by_isa: dict[str, int] = defaultdict(int)
    for f in funcs:
        by_isa[target_isa(f)] += 1
    return {
        "functions": len(funcs),
        "by_isa": dict(sorted(by_isa.items())),
        "files": len({f.file for f in funcs}),
    }


def run_project(args: argparse.Namespace) -> dict:
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"error: project root is not a directory: {root}")
    funcs, files = build_inventory(root, args.include, args.exclude)
    proposals = propose_project(funcs)
    if args.max_candidates:
        proposals = proposals[:args.max_candidates]
    by_pair: dict[str, int] = defaultdict(int)
    for p in proposals:
        by_pair[p["isa_pair"]] += 1
    return {
        "mode": "project",
        "root": str(root),
        "stats": {
            "files": files,
            **_stats(funcs),
            "candidates": len(proposals),
            "by_pair": dict(sorted(by_pair.items())),
        },
        "candidates": proposals,
        "warnings": [],
    }


def run_different(args: argparse.Namespace) -> dict:
    old_root = Path(args.old).expanduser().resolve()
    new_root = Path(args.new).expanduser().resolve()
    for label, root in (("old", old_root), ("new", new_root)):
        if not root.is_dir():
            raise SystemExit(f"error: {label} root is not a directory: {root}")
    old_funcs, old_files = build_inventory(old_root, args.include, args.exclude)
    new_funcs, new_files = build_inventory(new_root, args.include, args.exclude)
    proposals, unpaired = propose_different(old_funcs, new_funcs)
    if args.max_candidates:
        proposals = proposals[:args.max_candidates]
    by_pair: dict[str, int] = defaultdict(int)
    for p in proposals:
        by_pair[p["isa_pair"]] += 1

    old_names = {(f.file, f.name) for f in old_funcs}
    new_names = {(f.file, f.name) for f in new_funcs}
    common = old_names & new_names
    changed = 0
    old_index = _function_index(old_funcs)
    new_index = _function_index(new_funcs)
    for key in common:
        if old_index[key] and new_index[key]:
            if not _same_body(old_index[key][0], new_index[key][0]):
                changed += 1
    return {
        "mode": "different",
        "old_root": str(old_root),
        "new_root": str(new_root),
        "stats": {
            "old_files": old_files,
            "new_files": new_files,
            "old_functions": len(old_funcs),
            "new_functions": len(new_funcs),
            "common_functions": len(common),
            "modified_functions": changed,
            "added_functions": len(set(new_index) - set(old_index)),
            "candidates": len(proposals),
            "by_pair": dict(sorted(by_pair.items())),
        },
        "candidates": proposals,
        "unpaired": unpaired,
        "warnings": [
            f"未配对的新增向量实现: {item['name']} ({item['file']}:{item['line']})"
            for item in unpaired[:50]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    p = sub.add_parser("project", help="scan one project for C/NEON/SVE siblings")
    p.add_argument("--root", required=True)
    p.add_argument("--include", help="only relative paths matching this regex")
    p.add_argument("--exclude", help="skip relative paths matching this regex")
    p.add_argument("--max-candidates", type=int, default=1000)
    p.add_argument("--out")
    p.add_argument("--quiet", action="store_true")

    d = sub.add_parser("different", help="compare two trees")
    d.add_argument("--old", required=True)
    d.add_argument("--new", required=True)
    d.add_argument("--include")
    d.add_argument("--exclude")
    d.add_argument("--max-candidates", type=int, default=1000)
    d.add_argument("--out")
    d.add_argument("--quiet", action="store_true")

    args = parser.parse_args()
    result = run_project(args) if args.mode == "project" else run_different(args)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    if not args.quiet:
        print(payload)
    else:
        stats = result["stats"]
        print(json.dumps({"mode": result["mode"], **stats}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
