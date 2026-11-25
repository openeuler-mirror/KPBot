#!/usr/bin/env python3
"""Tier-A integrity checks for the five pgGraph app-tuner skills.

Checks frontmatter, local links, content anchors, evals structure and sensitive
paths/IPs. Writes results/result.json; exit code 0 only when failures == 0.
"""

import json
import re
import sys
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = CASE_DIR / "results"
SUITE_ROOT = Path(__file__).resolve().parents[3]
ROOT = SUITE_ROOT.parents[3]

SKILLS = {
    "pggraph-architecture": [
        "pgrx",
        "CSR",
        "ResolutionIndex",
        "FilterIndex",
        ".pggraph",
        "mutable_overlay",
    ],
    "pggraph-build-deployment": [
        "cargo pgrx",
        "graph.so",
        "CREATE EXTENSION graph",
        "clean-graph",
        "bidirectional",
    ],
    "pggraph-test-verification": [
        "result_checksum",
        "SQL JOIN",
        "throughput",
        "A/B",
        "GQL row cap",
    ],
    "pggraph-hotspot-analysis": [
        "UnionFind",
        "Topdown",
        "pg_stat_activity",
        "反向 CSR",
        "GIL",
    ],
    "pggraph-performance-optimization": [
        "UnionFind",
        "node_deleted",
        "component_sizes",
        "target-cpu",
        "反向 CSR",
        "回滚",
    ],
}

# 链接看护两类引用：
# 1) skill 内相对链接：references/<file> 或 scripts/<file>（可多级子目录）；
# 2) 跨 skill 相对链接：../pggraph-<skill>/<path>——兄弟 skill 间共享契约的入口
#    （各姊妹 SKILL.md 的完整性用例调用 ../pggraph-architecture/cases/... 即此类）。
# 段模式要求首字符为字母/数字/下划线、末字符不为点，避免把散文省略号（...）或
# 构建上下文路径（如 ldbc-driver 的 CLASSPATH ../common/target/…）误当链接。
_SEG = r"[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?"
LINK_RE = re.compile(
    r"(?<![/\w-])"
    r"(?:(?:references|scripts)/" + _SEG + r"(?:/" + _SEG + r")*"
    r"|\.\./pggraph-[a-z0-9-]+(?:/" + _SEG + r")+)"
)
PRIVATE_IP_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)
PERSONAL_PATH_RE = re.compile(r"(/home/[A-Za-z]|/ssd2?/)")

FAILURES = []
PENDING = []
COUNTS = {
    "skills_checked": 0,
    "links_checked": 0,
    "scripts_checked": 0,
    "anchors_found": 0,
    "evals_checked": 0,
    "sensitive_findings": 0,
}


def fail(message):
    FAILURES.append(message)


def frontmatter(text):
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return match.group(1) if match else None


def field(block, name):
    match = re.search(rf"^{name}:\s*(.*)$", block, re.M)
    if not match:
        return ""
    value = match.group(1).strip()
    if value in {">", "|"}:
        lines = []
        rest = block.split(f"{name}:", 1)[1].splitlines()[1:]
        for line in rest:
            if line.startswith("  ") or not line.strip():
                lines.append(line.strip())
            elif lines and lines[-1]:
                break
        value = " ".join(lines).strip()
    return value


def check_skill(name, anchors):
    skill_dir = SUITE_ROOT / name
    if not skill_dir.is_dir():
        # 姊妹 skill 随系列 PR 交付：缺失记 pending（不算失败），便于中间状态校验
        PENDING.append(f"missing skill dir: {name} (pending series PR)")
        return
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        fail(f"missing SKILL.md: {name}")
        return

    text = skill_md.read_text(encoding="utf-8")
    block = frontmatter(text)
    if block is None:
        fail(f"invalid frontmatter: {name}")
    else:
        if field(block, "name") != name:
            fail(f"frontmatter name mismatch: {name}")
        description = field(block, "description")
        if not description:
            fail(f"empty description: {name}")
        elif len(description) > 1024:
            fail(f"description too long: {name} ({len(description)})")
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
        fail(f"invalid skill name: {name}")

    for anchor in anchors:
        if anchor in text:
            COUNTS["anchors_found"] += 1
        else:
            fail(f"{name} missing anchor: {anchor}")

    for md in [skill_md, *sorted((skill_dir / "references").glob("*.md"))]:
        if not md.is_file():
            continue
        content = md.read_text(encoding="utf-8")
        for rel in LINK_RE.findall(content):
            COUNTS["links_checked"] += 1
            target = skill_dir / rel
            if rel.startswith("../"):
                sibling = skill_dir.parent / rel.split("/")[1]
                if not sibling.is_dir():
                    # 目标姊妹 skill 尚未合入（系列 PR 待交付）——记 pending 而非 failure
                    PENDING.append(f"{md.relative_to(ROOT)} -> cross-skill ref pending (skill absent): {rel}")
                    continue
            if not target.is_file():
                fail(f"{md.relative_to(ROOT)} -> missing {rel}")

    scripts = skill_dir / "scripts"
    if scripts.is_dir():
        for script in scripts.iterdir():
            if not script.is_file():
                continue
            COUNTS["scripts_checked"] += 1
            first = script.read_text(encoding="utf-8").splitlines()[0]
            if not first.startswith("#!"):
                fail(f"no shebang: {script.relative_to(ROOT)}")

    evals_path = skill_dir / "evals/evals.json"
    if not evals_path.is_file():
        fail(f"missing evals: {name}")
    else:
        try:
            data = json.loads(evals_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            fail(f"invalid evals JSON: {name}: {exc}")
            data = {}
        if data.get("skill_name") != name:
            fail(f"evals skill_name mismatch: {name}")
        evals = data.get("evals") or []
        COUNTS["evals_checked"] += len(evals)
        if len(evals) < 3:
            fail(f"too few evals: {name}")
        ids = [item.get("id") for item in evals]
        if len(set(ids)) != len(ids):
            fail(f"duplicate eval ids: {name}")
        for item in evals:
            if not item.get("prompt"):
                fail(f"{name} eval missing prompt")
            if len(item.get("expectations") or []) < 3:
                fail(f"{name} eval has <3 expectations")

    for path in skill_dir.rglob("*"):
        if not path.is_file() or path.suffix not in {".md", ".json", ".sh", ".py"}:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        if PRIVATE_IP_RE.search(content):
            COUNTS["sensitive_findings"] += 1
            fail(f"private IP in {path.relative_to(ROOT)}")
        if PERSONAL_PATH_RE.search(content):
            COUNTS["sensitive_findings"] += 1
            fail(f"personal path in {path.relative_to(ROOT)}")


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    for name, anchors in SKILLS.items():
        skill_dir = SUITE_ROOT / name
        if not skill_dir.is_dir():
            # 未合入的姊妹 skill：记入 pending（不算失败），result.json 据此区分“系列还缺谁”；
            # 不计入 skills_checked——该指标只统计树内实际存在的成员
            PENDING.append(f"missing skill dir: {name} (pending series PR)")
            continue
        COUNTS["skills_checked"] += 1
        check_skill(name, anchors)

    result = {
        "success": not FAILURES,
        "failures": len(FAILURES),
        "pending": PENDING,
        "messages": FAILURES,
        **COUNTS,
    }
    (RESULTS_DIR / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
