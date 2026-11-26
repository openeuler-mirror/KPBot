#!/usr/bin/env python3
"""Enumerate architecture-specific branches and defaults in a source tree.

Tier-1 enumeration helper for the cross-arch-regression-audit skill: lists
Rust ``cfg(target_arch = ...)``, C/C++ ``#ifdef``/``#if defined`` branches and
user-supplied keyword hits so an auditor can justify-or-flag each divergence.

The script only enumerates. It never judges whether a divergence has a stated
reason or is worth optimizing; that is the agent's audit workflow (SKILL.md
steps 3-4).

Usage:
    python3 arch_diff_scan.py --root /path/to/repo
    python3 arch_diff_scan.py --root . --keyword console --format tsv
    python3 arch_diff_scan.py --root . --output scan.json

Output (json, default): {"root", "files_scanned", "findings": [{file, line,
kind, arch, snippet}]}; tsv prints one finding per line. Use --output to
write UTF-8 directly to a file on hosts whose console codepage mangles
non-ASCII snippets (e.g. Windows). Line-level heuristic, declared limits:
attributes split across multiple lines (cfg( at end of line with
target_arch on the next) are not matched; string literals containing
unbalanced parens can truncate a span. Exit 0 on a completed scan
(finding nothing is success); 2 on bad arguments or I/O errors; 3 when
0 files were scanned (wrong root / empty tree / everything skipped -
never treat that as "no divergences"). Python 3 standard library only.
"""

import argparse
import json
import re
import sys
from pathlib import Path

RUST_EXTENSIONS = {".rs"}
C_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh", ".hxx",
                ".S", ".s", ".inc", ".inl", ".asm"}
TEXT_EXTENSIONS = RUST_EXTENSIONS | C_EXTENSIONS | {
    ".txt", ".md", ".rst", ".cmake", ".mk", ".sh", ".py", ".rs", ".toml",
    ".yaml", ".yml", ".json", ".cfg", ".conf", ".dts", ".dtsi"}

# Keyword hits inside pure documentation files are noise by default (the
# scan targets code/config divergences, not prose); opt in with
# --include-docs when archaeology over docs/changelogs is wanted.
DOC_EXTENSIONS = {".md", ".txt", ".rst"}

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "target", "build",
             "build-eu", "out", "dist", "__pycache__", ".cache", "vendor"}

# Arch macro/name -> canonical name (extend as real trees demand).
ARCH_ALIASES = {
    "__x86_64__": "x86_64",
    "__amd64__": "x86_64",
    "x86_64": "x86_64",
    "__aarch64__": "aarch64",
    "__ARM64__": "aarch64",
    "aarch64": "aarch64",
    "__arm__": "arm",
    "__riscv": "riscv",
    "riscv32": "riscv",
    "riscv64": "riscv",
    "__loongarch": "loongarch",
    "loongarch32": "loongarch",
    "loongarch64": "loongarch",
    "__powerpc__": "powerpc",
    "powerpc64": "powerpc",
}

ARCH_NAME_RE = "|".join(sorted(ARCH_ALIASES, key=len, reverse=True))

# Rust: locate cfg(...) / cfg!(...) spans by balanced parens, then emit one
# finding per target_arch inside the span. This handles any/all nesting of
# any depth (e.g. cfg(all(target_os = "linux", any(target_arch = "a",
# target_arch = "b")))) and reports every arch listed, not just the first.
CFG_START_RE = re.compile(r'cfg!?\(')
TARGET_ARCH_RE = re.compile(r'target_arch\s*=\s*"([^"]+)"')

# C/C++: a conditional-compilation line is one starting with #if/#ifdef/
# #ifndef/#elif; every arch macro ON that line yields its own finding, so
# combined conditions like `#if defined(__x86_64__) || defined(__aarch64__)`
# report both sides instead of silently dropping the second one. Line-level
# heuristic: an arch macro inside a trailing comment on such a line also
# matches - enumeration only, the audit workflow justifies or flags each
# finding by reading the code.
C_COND_LINE_RE = re.compile(r'#\s*(?:el)?if(?:n?def)?\b')
ARCH_MACRO_RE = re.compile(r'\b(' + ARCH_NAME_RE + r')\b')

SNIPPET_MAX = 160


def _snippet(line):
    text = line.strip()
    if len(text) > SNIPPET_MAX:
        text = text[:SNIPPET_MAX - 3] + "..."
    return text


def _arch_canonical(token):
    token = token.strip().strip('"')
    if token in ARCH_ALIASES:
        return ARCH_ALIASES[token]
    for alias, canonical in ARCH_ALIASES.items():
        if token.startswith(alias):
            return canonical
    return "unknown"


def _cfg_spans(line):
    """Yield (start, end) spans of complete cfg(...)/cfg!(...) calls."""
    for match in CFG_START_RE.finditer(line):
        depth = 0
        index = match.end() - 1
        while index < len(line):
            if line[index] == "(":
                depth += 1
            elif line[index] == ")":
                depth -= 1
                if depth == 0:
                    yield match.start(), index + 1
                    break
            index += 1


def scan_file(path, keywords, include_docs=False):
    """Yield finding tuples for one file. Skips undecodable content safely."""
    findings = []
    suffix = path.suffix.lower()
    keyword_allowed = (include_docs or suffix not in DOC_EXTENSIONS)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, start=1):
                if suffix in RUST_EXTENSIONS:
                    for start, end in _cfg_spans(line):
                        for arch_match in \
                                TARGET_ARCH_RE.finditer(line[start:end]):
                            findings.append((lineno, "rust-cfg",
                                             arch_match.group(1),
                                             _snippet(line)))
                if suffix in C_EXTENSIONS and C_COND_LINE_RE.search(line):
                    for arch_match in ARCH_MACRO_RE.finditer(line):
                        findings.append((lineno, "c-ifdef",
                                         arch_match.group(1),
                                         _snippet(line)))
                if keyword_allowed:
                    for keyword in keywords:
                        if keyword and keyword in line:
                            findings.append((lineno, "keyword",
                                             "unknown", _snippet(line)))
                            break
    except OSError as exc:
        print("warn: cannot read %s: %s" % (path, exc), file=sys.stderr)
    return findings


def scan_tree(root, keywords, include_docs=False):
    findings = []
    files_scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        # SKIP_DIRS applies to path parts *below* root only: a root like
        # /home/build/myrepo must not have its whole tree skipped just
        # because an ancestor directory is named like a build dir.
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        files_scanned += 1
        for lineno, kind, arch_token, snippet in \
                scan_file(path, keywords, include_docs):
            findings.append({
                "file": relative.as_posix(),
                "line": lineno,
                "kind": kind,
                "arch": _arch_canonical(arch_token),
                "snippet": snippet,
            })
    return files_scanned, findings


def emit_tsv_lines(findings):
    lines = ["file\tline\tkind\tarch\tsnippet"]
    lines += ["%(file)s\t%(line)s\t%(kind)s\t%(arch)s\t%(snippet)s"
              % finding for finding in findings]
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Enumerate architecture-specific branches in a tree.")
    parser.add_argument("--root", required=True,
                        help="source tree root to scan")
    parser.add_argument("--format", choices=("json", "tsv"),
                        default="json", help="output format (default json)")
    parser.add_argument("--keyword", action="append", default=[],
                        help="extra keyword to highlight (repeatable)")
    parser.add_argument("--include-docs", action="store_true",
                        help="also match keywords in pure doc files "
                             "(.md/.txt/.rst); skipped by default")
    parser.add_argument("--output",
                        help="write output to this file as UTF-8 instead of "
                             "stdout (avoids console encoding pitfalls)")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print("error: --root is not a directory: %s" % root,
              file=sys.stderr)
        return 2

    files_scanned, findings = scan_tree(root, args.keyword,
                                         include_docs=args.include_docs)

    if files_scanned == 0:
        print("error: 0 files scanned under %s - wrong root, empty tree, or "
              "everything matched SKIP_DIRS; an empty result here is almost "
              "certainly a usage mistake, not a divergence-free tree"
              % root, file=sys.stderr)
        return 3

    if args.format == "tsv":
        payload = None
        lines = emit_tsv_lines(findings)
    else:
        payload = json.dumps({
            "root": str(root),
            "files_scanned": files_scanned,
            "findings": findings,
        }, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            if payload is not None:
                handle.write(payload)
                handle.write("\n")
            else:
                handle.write("\n".join(lines))
                handle.write("\n")
    elif payload is not None:
        print(payload)
    else:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
