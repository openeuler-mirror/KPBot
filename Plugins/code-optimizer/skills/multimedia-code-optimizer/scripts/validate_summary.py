#!/usr/bin/env python3
"""Check the summary tree for structure, dangling links and forbidden content.

Usage:
    python3 validate_summary.py [--skill-root /path/to/multimedia-code-optimizer]

Exit code 0 means no errors.  Warnings (for example an empty mode) do not fail.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from merge_summary import MODES, parse_summary  # noqa: E402

PROVENANCE_PATTERNS = [
    (re.compile(r"(?:^|[\s(`'\"/])[\w./-]+\.(?:c|cc|cpp|cxx|h|hh|hpp|S):\d+"),
     "source-file:line reference"),
    (re.compile(r"第\s*\d+\s*行"), "line number reference"),
    (re.compile(r"\bline\s+\d+\b", re.IGNORECASE), "line number reference"),
    (re.compile(r"(?:文件|来源|仓库|库名|路径)[:：]"), "provenance label"),
    (re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/"), "absolute user path"),
    (re.compile(r"\b[A-Za-z]:\\\\"), "absolute Windows path"),
]
BENCHMARK_PATTERNS = [
    (re.compile(r"\bbenchmark\b", re.IGNORECASE), "benchmark content"),
    (re.compile(r"性能测试|加速比|吞吐量|耗时|火焰图|perf\s+stat|speedup|"
                r"\bcycles\b|\bprofiling\b", re.IGNORECASE),
     "performance measurement content"),
]


def scan_text(path: Path, text: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for pattern, label in PROVENANCE_PATTERNS:
        match = pattern.search(text)
        if match:
            snippet = text[max(0, match.start() - 30):match.end() + 30]
            errors.append(f"{path}: forbidden {label}: ...{snippet.strip()}...")
    for pattern, label in BENCHMARK_PATTERNS:
        match = pattern.search(text)
        if match:
            snippet = text[max(0, match.start() - 30):match.end() + 30]
            errors.append(f"{path}: forbidden {label}: ...{snippet.strip()}...")
    return errors, warnings


def validate(skill_root: Path) -> dict:
    summary = skill_root / "summary"
    errors: list[str] = []
    warnings: list[str] = []
    methods_total = 0

    if not summary.is_dir():
        return {"success": False, "errors": [f"missing {summary}"],
                "warnings": [], "methods": 0}
    if not (summary / "manual").is_dir():
        errors.append("missing summary/manual/")
    if not (summary / "README.md").is_file():
        errors.append("missing summary/README.md")

    for mode in MODES:
        mode_dir = summary / "auto" / mode
        summary_md = mode_dir / "summary.md"
        patterns = mode_dir / "patterns"
        if not summary_md.is_file():
            errors.append(f"missing {summary_md.relative_to(skill_root)}")
            continue
        if not patterns.is_dir():
            errors.append(f"missing {patterns.relative_to(skill_root)}")
            continue
        text = summary_md.read_text(encoding="utf-8")
        methods = parse_summary(text)
        methods_total += len(methods)
        if not methods:
            warnings.append(f"{summary_md.relative_to(skill_root)} has no methods yet")
        for method in methods:
            title, slug, scene = method["title"], method["slug"], method["scene"]
            if not scene:
                errors.append(f"{summary_md}: method '{title}' missing 场景")
            if not slug:
                errors.append(f"{summary_md}: method '{title}' missing 手法 link")
                continue
            pattern = patterns / f"{slug}.md"
            if not pattern.is_file():
                errors.append(f"{summary_md}: dangling link patterns/{slug}.md")
                continue
            ptext = pattern.read_text(encoding="utf-8")
            if "# 修改要点" not in ptext:
                errors.append(f"{pattern}: missing '# 修改要点'")
            if "# 修改代码示例" not in ptext:
                errors.append(f"{pattern}: missing '# 修改代码示例'")
            if not re.search(r"```", ptext):
                warnings.append(f"{pattern}: no code fence")

    for path in sorted(summary.rglob("*.md")):
        if "manual" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        text_errors, text_warnings = scan_text(path.relative_to(skill_root), text)
        errors.extend(text_errors)
        warnings.extend(text_warnings)

    return {
        "success": not errors,
        "errors": errors,
        "warnings": warnings,
        "methods": methods_total,
        "summary_paths": [f"summary/auto/{m}/summary.md" for m in MODES],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-root",
                        default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args()
    result = validate(Path(args.skill_root).resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
