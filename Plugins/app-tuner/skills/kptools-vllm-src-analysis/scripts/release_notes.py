#!/usr/bin/env python3
"""读取 vllm-ascend 发布说明（更新了什么 / 性能条目 / 依赖 / 弃用）。

两种来源：
  1. 本地版本 tag（默认）：docs/source/user_guide/release_notes.md，版本绑定；
  2. 线上官方文档（--online）：raw markdown，可拿到本地 clone 还没有的新版本。

性能相关结论口径：本脚本给出的是 release note 的「宣称」(claimed)，不是实测；
幅度必须另找 benchmark，见 references/evidence-policy.md。

用法：
  python3 scripts/release_notes.py --repo /path/vllm-ascend --version v0.30.0rc1 \
      --sections Performance,Features --json rn.json
  python3 scripts/release_notes.py --online --version v0.30.0 --sections Performance
  python3 scripts/release_notes.py --online --list
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

NOTE_PATHS = ["docs/source/user_guide/release_notes.md"]
ONLINE_URL = (
    "https://raw.githubusercontent.com/vllm-project/vllm-ascend/main/"
    "docs/source/user_guide/release_notes.md"
)
PR_RE = re.compile(r"\[#(\d+)\]\(([^)]+)\)")
HEAD_RE = re.compile(r"^##\s+(v[0-9][0-9A-Za-z.\-+]*)\s*(?:-\s*([^¶]*?))?\s*¶?\s*$")
SUB_RE = re.compile(r"^###\s+(.+?)\s*¶?\s*$")

SECTION_ALIASES = {
    "亮点": "Highlights",
    "功能特性": "Features",
    "硬件与算子支持": "Hardware and Operator Support",
    "性能": "Performance",
    "依赖项": "Dependencies",
    "弃用与配置变更": "Deprecation",
    "弃用": "Deprecation",
    "弃用与破坏性变更": "Deprecation",
    "deprecation": "Deprecation",
    "deprecation & breaking changes": "Deprecation",
    "deprecation and configuration changes": "Deprecation",
    "breaking changes": "Deprecation",
    "文档": "Documentation",
    "稳定性与错误修复": "Stability and Bug Fixes",
    "已知问题": "Known Issues",
}

_SECTION_ALIAS_LOWER = {k.lower(): v for k, v in SECTION_ALIASES.items()}


FULL_VER_RE = re.compile(r"^v\d+\.\d+\.\d+(?:rc\d+)?$")


def _match(entries, wanted):
    """完整版本号必须精确匹配，避免 v0.30.0 误配到 v0.30.0rc1。"""
    cand = wanted if wanted.startswith("v") else f"v{wanted}"
    for item in entries:
        if item["version"] == cand:
            return item, "exact"
    if FULL_VER_RE.match(cand):
        return None, None
    for item in entries:
        if item["version"].startswith(cand):
            return item, "prefix"
    return None, None


def canonical_section(name):
    base = name.replace("¶", "").strip()
    return _SECTION_ALIAS_LOWER.get(base.lower(), base)


def _section_selected(name, wanted_lowers):
    """模糊/别名匹配：请求名与规范化后的小节名任一方向包含即视为命中，
    以兼容跨版本小节改名（如 Deprecation and Configuration Changes ↔
    Deprecation & Breaking Changes）。"""
    n = name.lower()
    for wanted in wanted_lowers:
        if wanted and (wanted == n or wanted in n or n in wanted):
            return True
    return False


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _is_repo(repo):
    if not repo or not Path(repo).is_dir():
        return False
    proc = _git(repo, "rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _load_local(repo, ref):
    for path in NOTE_PATHS:
        proc = _git(repo, "show", f"{ref}:{path}")
        if proc.returncode == 0:
            return path, proc.stdout, ref
    return None, None, None


def _load_any(repo, wanted):
    for ref in (wanted, "HEAD"):
        path, text, used = _load_local(repo, ref)
        if text:
            return path, text, used
    proc = _git(repo, "tag", "--list")
    tags = sorted(t for t in proc.stdout.splitlines() if t.strip().startswith("v"))
    for tag in reversed(tags):
        path, text, used = _load_local(repo, tag)
        if text:
            return path, text, used
    return None, None, None


def fetch_text(url, timeout=30, retries=2):
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "kptools-vllm-src-analysis"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # 网络抖动重试一次
            last = exc
    raise last


def parse_notes(text, wanted, sections=None, list_only=False, source="", source_kind="local"):
    entries = []
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = HEAD_RE.match(line)
        if m:
            current = {
                "version": m.group(1),
                "date": (m.group(2) or "").replace("¶", "").strip(),
                "sections": [],
                "notes": [],
            }
            entries.append(current)
            continue
        if current is None:
            continue
        stripped = line.lstrip()
        if stripped.startswith(">"):
            content = stripped[1:].strip()
            if content:
                current["notes"].append(content)
            continue
        sub = SUB_RE.match(line)
        if sub:
            current["sections"].append(
                {"name": canonical_section(sub.group(1)), "bullets": []}
            )
            continue
        if not current["sections"]:
            continue
        if stripped.startswith("- "):
            current["sections"][-1]["bullets"].append(
                {"text": stripped[2:].strip(), "prs": []}
            )
        elif line.startswith(("  ", "\t")) and current["sections"][-1]["bullets"]:
            current["sections"][-1]["bullets"][-1]["text"] += " " + stripped

    for entry in entries:
        for section in entry["sections"]:
            for bullet in section["bullets"]:
                seen = set()
                for pr, url in PR_RE.findall(bullet["text"]):
                    if pr not in seen:
                        seen.add(pr)
                        bullet["prs"].append({"pr": pr, "url": url})

    available = [e["version"] for e in entries]
    if list_only:
        return {
            "schema_version": 1,
            "found": True,
            "available_versions": available,
            "source": source,
            "source_kind": source_kind,
            "entries": len(entries),
        }

    entry, matched_by = _match(entries, wanted)
    if entry is None:
        return {
            "schema_version": 1,
            "found": False,
            "query": wanted,
            "source": source,
            "source_kind": source_kind,
            "available_versions": available[:50],
            "degradations": (
                ["该版本无发布说明条目，可改用 digest.py 看提交级变化"]
                + (["本地无该版本条目时可加 --online 读取官方最新发布说明"]
                   if source_kind == "local" else [])
            ),
        }

    wanted_sections = {canonical_section(s).lower() for s in sections} if sections else None
    out_sections = []
    for section in entry["sections"]:
        if wanted_sections and not _section_selected(section["name"], wanted_sections):
            continue
        out_sections.append(
            {
                "name": section["name"],
                "bullets": section["bullets"],
                "bullet_count": len(section["bullets"]),
            }
        )
    degradations = []
    if wanted_sections:
        got = [s["name"].lower() for s in out_sections]
        missing = sorted(
            w for w in wanted_sections
            if not any(w == g or w in g or g in w for g in got)
        )
        if not out_sections:
            available = list(dict.fromkeys(s["name"] for s in entry["sections"]))
            degradations.append(
                "请求的小节 %s 未命中；该版本可用小节: %s"
                % (",".join(sorted(wanted_sections)), ", ".join(available) or "（无）")
            )
        elif missing:
            degradations.append(
                "请求的小节 %s 未命中；已返回: %s"
                % (",".join(missing), ", ".join(s["name"] for s in out_sections))
            )
    return {
        "schema_version": 1,
        "found": True,
        "query": wanted,
        "matched_by": matched_by,
        "version": entry["version"],
        "date": entry["date"],
        "source": f"{source}@{wanted}" if source_kind == "local" else source,
        "source_kind": source_kind,
        "notes": entry["notes"],
        "sections": out_sections,
        "counts": {
            "sections": len(out_sections),
            "bullets": sum(s["bullet_count"] for s in out_sections),
            "prs": len({p["pr"] for s in out_sections for b in s["bullets"] for p in b["prs"]}),
        },
        "evidence_level": "claimed_in_release_notes",
        "degradations": degradations,
    }


def load(repo, wanted, sections=None, list_only=False, online=False, url=None):
    if online:
        source = url or ONLINE_URL
        try:
            text = fetch_text(source)
        except Exception as exc:
            raise RuntimeError(f"线上发布说明获取失败: {exc}") from exc
        return parse_notes(text, wanted, sections, list_only, source, "online")

    if not _is_repo(repo):
        raise RuntimeError(f"不是 git 仓库: {repo}")
    path, text, ref = _load_any(repo, wanted)
    if text is None:
        raise RuntimeError(f"任何 ref 下都找不到 release_notes.md: {repo}")
    result = parse_notes(text, wanted, sections, list_only, path, "local")
    result["source_ref"] = ref
    # 本地该版本没有说明时会回退到 HEAD/最新 tag；出处必须写实际读取的 ref，
    # 查询版本单独保存在 query_version，避免把 HEAD 的内容冒充成 @<请求版本>。
    result["source"] = f"{path}@{ref}" if ref else path
    result["query_version"] = wanted
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="读取 vllm-ascend 发布说明")
    ap.add_argument("--repo")
    ap.add_argument("--online", action="store_true", help="读取官方线上 raw markdown")
    ap.add_argument("--url", help="覆盖线上地址（调试/镜像用）")
    ap.add_argument("--version")
    ap.add_argument("--sections", help="逗号分隔的小节名，中英文均可")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args(argv)

    if not args.version and not args.list:
        ap.error("需要 --version 或 --list")
    if not args.online and not args.repo:
        ap.error("本地模式需要 --repo；或使用 --online")

    wanted = args.version or ""
    sections = [s.strip() for s in args.sections.split(",")] if args.sections else None
    try:
        result = load(args.repo, wanted, sections, args.list, args.online, args.url)
    except Exception as exc:
        result = {"schema_version": 1, "status": "error", "error": str(exc)}
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 3

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if args.list:
        for version in result["available_versions"]:
            print(version)
    elif not result["found"]:
        print(f"未找到 {wanted}，可用版本: {', '.join(result['available_versions'][:10])}")
    else:
        print(
            f"{result['version']} ({result['date']}) [{result['source_kind']}]: "
            f"{result['counts']['sections']} sections, "
            f"{result['counts']['bullets']} bullets, {result['counts']['prs']} PRs"
        )
        for section in result["sections"]:
            print(f"  [{section['name']}] {section['bullet_count']} 条")
        for note in result["notes"]:
            print(f"  note: {note[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
