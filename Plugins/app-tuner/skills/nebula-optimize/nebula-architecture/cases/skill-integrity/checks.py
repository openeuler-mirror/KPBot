#!/usr/bin/env python3
"""Tier-A integrity checks for the five NebulaGraph app-tuner skills.

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
    "nebula-architecture": [
        "MetaClient",
        "RocksDB",
        "Key",
        "Thrift",
        "Raft",
    ],
    "nebula-build-deployment": [
        "ENABLE_STANDALONE_VERSION",
        "third-party",
        "cluster.id",
        "--reflink=never",
        "SHOW HOSTS",
    ],
    "nebula-test-verification": [
        "FIND SHORTEST PATH",
        "iterations/s",
        "checks",
        "latency",
        "A/B",
    ],
    "nebula-hotspot-analysis": [
        "futex",
        "Topdown",
        "graph-netio",
        "reader-pool",
        "/stats",
    ],
    "nebula-performance-optimization": [
        "LSE",
        "Value",
        "flat_map",
        "Block Cache",
        "compaction",
        "回滚",
    ],
}

LINK_RE = re.compile(r"(?<![/\w-])(?:references|scripts)/[A-Za-z0-9_.-]+")
PRIVATE_IP_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)
PERSONAL_PATH_RE = re.compile(r"(/home/[A-Za-z]|/ssd2?/)")

FAILURES = []
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
        fail(f"missing skill dir: {name}")
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
            if not (skill_dir / rel).is_file():
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
        COUNTS["skills_checked"] += 1
        check_skill(name, anchors)

    result = {
        "success": not FAILURES,
        "failures": len(FAILURES),
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
