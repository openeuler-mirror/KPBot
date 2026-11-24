#!/usr/bin/env python3
"""Merge one analysis bundle into summary/auto/<MODE>/.

The bundle is produced by the agent and contains method-level knowledge, not
per-file provenance.  Merging is append-only from the user's point of view:
existing scenarios, points and code examples are preserved, new material is
deduplicated, and the table of contents plus summary/README.md are rebuilt.

Usage:
    python3 merge_summary.py --bundle /tmp/bundle.json
    python3 merge_summary.py --bundle /tmp/bundle.json --skill-root /path/to/skill
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

MODES = {
    "C_NEON": ("C", "NEON"),
    "C_SVE": ("C", "SVE"),
    "NEON_SVE": ("NEON", "SVE"),
}
MODE_LABEL = {
    "C_NEON": "C → NEON",
    "C_SVE": "C → SVE",
    "NEON_SVE": "NEON → SVE",
}
TOC_START = "<!-- simd-methods:toc:start -->"
TOC_END = "<!-- simd-methods:toc:end -->"
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
H1_RE = re.compile(r"^#\s+(.+?)\s*$")
SCENE_RE = re.compile(r"^- 场景[:：]\s*(.*)$")
PATTERN_LINK_RE = re.compile(r"\(patterns/([A-Za-z0-9_.-]+\.md)\)")
EXAMPLE_RE = re.compile(r"^##\s+示例.*$", re.M)
FENCE_RE = re.compile(r"```[A-Za-z0-9_+-]*\n(.*?)```", re.S)


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip()


def anchor(title: str) -> str:
    value = title.strip().lower()
    value = re.sub(r"[^\w\u4e00-\u9fff\- ]", "", value, flags=re.UNICODE)
    return value.replace(" ", "-")


def ensure_skeleton(skill_root: Path) -> Path:
    summary = skill_root / "summary"
    manual = summary / "manual"
    auto = summary / "auto"
    summary.mkdir(parents=True, exist_ok=True)
    # The user owns summary/manual: create it once, never touch it again.
    if not manual.exists():
        manual.mkdir(parents=True)
    for mode in MODES:
        mode_dir = auto / mode
        (mode_dir / "patterns").mkdir(parents=True, exist_ok=True)
        summary_md = mode_dir / "summary.md"
        if not summary_md.exists():
            summary_md.write_text(
                TOC_START + "\n## 方法目录\n\n（暂无方法，运行 summary 模式后自动生成）\n"
                + TOC_END + "\n",
                encoding="utf-8",
            )
    return summary


def split_toc(text: str) -> tuple[str, str, str]:
    start = text.find(TOC_START)
    end = text.find(TOC_END)
    if start < 0 or end < 0:
        return "", text, ""
    return text[:start], text[end + len(TOC_END):], text[start:end + len(TOC_END)]


def parse_summary(text: str) -> list[dict]:
    """Return method blocks in document order.

    Each block keeps its raw lines so unknown prose survives the rewrite.
    """
    prefix, body, _ = split_toc(text)
    lines = body.splitlines()
    methods: list[dict] = []
    current: dict | None = None
    for line in lines:
        match = H1_RE.match(line)
        if match:
            if current:
                methods.append(current)
            current = {"title": match.group(1).strip(), "lines": [line],
                       "scene": "", "slug": ""}
            continue
        if current is None:
            continue
        current["lines"].append(line)
        scene = SCENE_RE.match(line)
        if scene and not current["scene"]:
            current["scene"] = scene.group(1).strip()
        link = PATTERN_LINK_RE.search(line)
        if link and not current["slug"]:
            current["slug"] = link.group(1)[:-3]
    if current:
        methods.append(current)
    return methods


def method_block(title: str, scene: str, slug: str) -> list[str]:
    return [
        f"# {title}",
        f"- 场景：{scene}",
        f"- 手法：[patterns/{slug}.md](patterns/{slug}.md)",
        "",
    ]


def rebuild_summary(text: str, methods: list[dict]) -> str:
    prefix, _, _ = split_toc(text)
    prefix = prefix.rstrip()
    toc_lines = [TOC_START, "## 方法目录", ""]
    if methods:
        for index, method in enumerate(methods, 1):
            toc_lines.append(
                f"{index}. [{method['title']}](#{anchor(method['title'])})")
    else:
        toc_lines.append("（暂无方法，运行 summary 模式后自动生成）")
    toc_lines += ["", TOC_END, ""]

    body_lines: list[str] = []
    for method in methods:
        block = list(method["lines"])
        # Drop trailing blank lines, keep one separator.
        while block and not block[-1].strip():
            block.pop()
        body_lines.extend(block)
        body_lines.append("")

    pieces = []
    if prefix:
        pieces.append(prefix + "\n")
    pieces.append("\n".join(toc_lines) + "\n")
    if body_lines:
        pieces.append("\n".join(body_lines).rstrip() + "\n")
    return "".join(pieces)


def update_scene_line(block: dict, new_scenarios: list[str]) -> tuple[list[str], int]:
    existing = [s.strip() for s in re.split(r"[；;]", block["scene"]) if s.strip()]
    added = 0
    for scene in new_scenarios:
        scene = scene.strip()
        if not scene:
            continue
        if any(norm(scene) == norm(old) or norm(scene) in norm(old) for old in existing):
            continue
        existing.append(scene)
        added += 1
    if not added:
        return block["lines"], 0
    merged = "；".join(existing)
    lines = []
    replaced = False
    for line in block["lines"]:
        if not replaced and SCENE_RE.match(line):
            lines.append(f"- 场景：{merged}")
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.insert(2, f"- 场景：{merged}")
    block["scene"] = merged
    return lines, added


def parse_pattern(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    points: list[str] = []
    in_points = False
    for line in text.splitlines():
        if line.strip() == "# 修改要点":
            in_points = True
            continue
        if in_points and line.startswith("# "):
            break
        if in_points and line.strip().startswith("- "):
            points.append(line.strip()[2:].strip())
    examples: list[tuple[str, str]] = []
    chunks = EXAMPLE_RE.split(text)[1:]
    for chunk in chunks:
        fences = FENCE_RE.findall(chunk)
        if len(fences) >= 2:
            examples.append((norm(fences[0]), norm(fences[1])))
    return points, examples


def pattern_text(title: str, points: list[str], examples: list[dict]) -> str:
    out = ["# 修改要点", ""]
    for point in points:
        out.append(f"- {point.strip()}")
    out += ["", "# 修改代码示例", ""]
    for index, example in enumerate(examples, 1):
        note = (example.get("note") or "").strip()
        heading = f"## 示例 {index}" + (f"：{note}" if note else "")
        before_lang = example.get("before_lang") or example.get("lang") or "c"
        after_lang = example.get("after_lang") or example.get("lang") or "c"
        out += [heading, "", "### 修改前", "", f"```{before_lang}",
                example["before"].strip("\n"), "```", "",
                "### 修改后", "", f"```{after_lang}",
                example["after"].strip("\n"), "```", ""]
    return "\n".join(out).rstrip() + "\n"


def append_points(text: str, points: list[str]) -> tuple[str, int]:
    existing, _ = parse_pattern(text)
    new = [p for p in points if norm(p) and norm(p) not in {norm(e) for e in existing}]
    if not new:
        return text, 0
    marker = "# 修改代码示例"
    lines = text.splitlines()
    try:
        index = lines.index(marker)
    except ValueError:
        lines += ["", marker, ""]
        index = lines.index(marker)
    bullets = [f"- {p.strip()}" for p in new]
    if index > 0 and not lines[index - 1].strip():
        # Reuse the existing blank line as the separator before the heading.
        lines[index - 1:index - 1] = bullets
    else:
        lines[index:index] = bullets + [""]
    return "\n".join(lines).rstrip() + "\n", len(new)


def append_examples(text: str, examples: list[dict]) -> tuple[str, int]:
    _, existing = parse_pattern(text)
    count = len(re.findall(r"^##\s+示例", text, re.M))
    out = text.rstrip() + "\n"
    added = 0
    for example in examples:
        key = (norm(example["before"]), norm(example["after"]))
        if key in existing:
            continue
        existing.append(key)
        count += 1
        note = (example.get("note") or "").strip()
        heading = f"## 示例 {count}" + (f"：{note}" if note else "")
        before_lang = example.get("before_lang") or example.get("lang") or "c"
        after_lang = example.get("after_lang") or example.get("lang") or "c"
        out += (f"\n{heading}\n\n### 修改前\n\n```{before_lang}\n"
                + example["before"].strip("\n")
                + "\n```\n\n### 修改后\n\n```" + after_lang + "\n"
                + example["after"].strip("\n")
                + "\n```\n")
        added += 1
    return out, added


def merge_pattern(mode_dir: Path, method: dict) -> tuple[int, int, bool]:
    slug = method["slug"]
    path = mode_dir / "patterns" / f"{slug}.md"
    points = method["points"]
    examples = method["examples"]
    if not path.exists():
        path.write_text(pattern_text(method["title"], points, examples),
                        encoding="utf-8")
        return len(points), len(examples), True
    text = path.read_text(encoding="utf-8")
    text, points_added = append_points(text, points)
    text, examples_added = append_examples(text, examples)
    path.write_text(text, encoding="utf-8")
    return points_added, examples_added, False


def validate_method(method: dict, index: int) -> list[str]:
    errors = []
    slug = str(method.get("slug", "")).strip()
    if not slug or not SLUG_RE.match(slug):
        errors.append(f"method[{index}] slug must be kebab-case")
    if not str(method.get("title", "")).strip():
        errors.append(f"method[{index}] missing title")
    scenarios = method.get("scenario") or method.get("scenarios") or []
    if isinstance(scenarios, str):
        scenarios = [scenarios]
    if not [s for s in scenarios if str(s).strip()]:
        errors.append(f"method[{index}] missing scenario")
    points = method.get("points") or []
    if not [p for p in points if str(p).strip()]:
        errors.append(f"method[{index}] missing points")
    examples = method.get("examples") or []
    if not examples:
        errors.append(f"method[{index}] missing examples")
    for j, example in enumerate(examples):
        if not str(example.get("before", "")).strip() or not str(
                example.get("after", "")).strip():
            errors.append(f"method[{index}].examples[{j}] needs before and after")
    return errors


def rebuild_readme(summary: Path) -> None:
    rows = []
    for mode in MODES:
        summary_md = summary / "auto" / mode / "summary.md"
        title = MODE_LABEL[mode]
        count = 0
        if summary_md.exists():
            count = len(parse_summary(summary_md.read_text(encoding="utf-8")))
        rows.append(f"| {title} | {count} | [summary.md](auto/{mode}/summary.md) |")

    manual_files = []
    manual = summary / "manual"
    if manual.exists():
        manual_files = sorted(p.name for p in manual.glob("*.md"))
    manual_lines = ["- [manual/](manual/)：人工补充的总结，技能不会修改。"]
    manual_lines += [f"- [manual/{name}](manual/{name})" for name in manual_files]

    readme = "\n".join([
        "# SIMD 向量化方法总结",
        "",
        "本目录沉淀可跨项目复用的 SIMD 向量化改写方法。所有自动总结只保留场景、",
        "修改要点和代码切片，不记录来源仓库、文件路径、行号、作者或性能数据。",
        "方法库是增量积累的：每次扫描只追加新方法或新示例，不表示已经覆盖某个项目的全部函数。",
        "",
        "## 自动总结",
        "",
        "| 方向 | 方法数 | 导航 |",
        "| --- | --- | --- |",
        *rows,
        "",
        "## 手动总结",
        "",
        *manual_lines,
        "",
        "## 目录约定",
        "",
        "```text",
        "summary/",
        "├── README.md              # 本导航",
        "├── manual/                # 人工维护，技能只创建、不修改",
        "└── auto/",
        "    ├── C_NEON/",
        "    │   ├── summary.md     # 场景与规则索引",
        "    │   └── patterns/      # 每个方法的要点与代码示例",
        "    ├── C_SVE/",
        "    └── NEON_SVE/",
        "```",
        "",
    ])
    (summary / "README.md").write_text(readme, encoding="utf-8")


def merge_bundle(skill_root: Path, bundle: dict) -> dict:
    summary = ensure_skeleton(skill_root)
    mode = str(bundle.get("mode", "")).strip()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    methods = bundle.get("methods") or []
    if not isinstance(methods, list) or not methods:
        raise ValueError("bundle.methods must be a non-empty list")

    errors: list[str] = []
    for index, method in enumerate(methods):
        errors.extend(validate_method(method, index))
    if errors:
        raise ValueError("; ".join(errors))

    mode_dir = summary / "auto" / mode
    summary_md = mode_dir / "summary.md"
    text = summary_md.read_text(encoding="utf-8") if summary_md.exists() else ""
    existing = parse_summary(text)
    by_slug = {m["slug"]: m for m in existing if m["slug"]}

    added_methods = merged_methods = 0
    points_added = examples_added = scenarios_added = 0

    for method in methods:
        slug = method["slug"]
        scenarios = method.get("scenario") or method.get("scenarios") or []
        if isinstance(scenarios, str):
            scenarios = [scenarios]
        points_added_i, examples_added_i, created = merge_pattern(mode_dir, method)
        points_added += points_added_i
        examples_added += examples_added_i

        if slug in by_slug:
            merged_methods += 1
            block = by_slug[slug]
            block["lines"], scene_added = update_scene_line(block, scenarios)
            scenarios_added += scene_added
        else:
            added_methods += 1
            scene = "；".join(s.strip() for s in scenarios if str(s).strip())
            block = {"title": method["title"], "scene": scene,
                     "slug": slug, "lines": method_block(method["title"], scene, slug)}
            existing.append(block)
            by_slug[slug] = block
            scenarios_added += len([s for s in scenarios if str(s).strip()])

    summary_md.write_text(rebuild_summary(text, existing), encoding="utf-8")
    rebuild_readme(summary)
    return {
        "success": True,
        "mode": mode,
        "methods_added": added_methods,
        "methods_merged": merged_methods,
        "scenarios_added": scenarios_added,
        "points_added": points_added,
        "examples_added": examples_added,
        "summary": str(summary_md),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--skill-root",
                        default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args()
    try:
        bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
        result = merge_bundle(Path(args.skill_root), bundle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"success": False, "error_message": str(exc)}
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
