#!/usr/bin/env python3
"""Tier-A integrity checks for the kunpeng-precision-optimization skill.

Covers: frontmatter sanity, reference index <-> files consistency, content
anchors, evals/evals.json schema, and a sensitive-content scan (private IPs,
personal home paths). Standard library only; no Kunpeng machine required.

Writes cases/skill-integrity/results/result.json; exit 0 = all checks passed.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
RESULTS = Path(__file__).resolve().parent / "results"
ANCHORS = ["-ffp-compatibility", "FTZ", "Allreduce", "RMSE", "L0", "ICC"]
SENSITIVE = {
    "private_ipv4": re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"),
    "home_path": re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+/"),
}

failures = []
details = []
links_checked = anchors_found = evals_checked = eval_queries_checked = sensitive_findings = 0


def redact(s):
    """回显命中时脱敏，避免把敏感串本身写进日志/CI 输出。"""
    s = (s or "").strip()
    if len(s) <= 4:
        return "*" * len(s)
    return f"{s[:3]}{'*' * (len(s) - 3)}"


def fail(check, msg):
    failures.append(f"{check}: {msg}")
    details.append({"check": check, "ok": False, "detail": msg})


def ok(check, detail=""):
    details.append({"check": check, "ok": True, "detail": detail})


def check_frontmatter():
    skill_md = SKILL_ROOT / "SKILL.md"
    if not skill_md.is_file():
        fail("frontmatter", "SKILL.md missing")
        return
    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        fail("frontmatter", "no YAML frontmatter")
        return
    fm = m.group(1)
    name = re.search(r"^name:\s*(.+)$", fm, re.M)
    if not name or name.group(1).strip() != SKILL_ROOT.name:
        fail("frontmatter", f"name != directory name ({SKILL_ROOT.name})")
    else:
        ok("frontmatter.name", name.group(1).strip())
    body = re.search(r"^description:\s*[|>]\s*\n((?:\s{2,}.*\n?)+)", fm, re.M)
    flat = " ".join(ln.strip() for ln in body.group(1).splitlines()) if body else str(
        (re.search(r"^description:\s*(.+)$", fm, re.M) or [""])[0])
    if len(flat) < 80:
        fail("frontmatter", f"description too short ({len(flat)} chars)")
    else:
        ok("frontmatter.description", f"{len(flat)} chars")
    if not re.search(r"^license:\s*\S+", fm, re.M):
        fail("frontmatter", "license missing")
    else:
        ok("frontmatter.license")


def check_references():
    global links_checked
    skill_md = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    cited = set(re.findall(r"references/([A-Za-z0-9._-]+\.md)", skill_md))
    present = {p.name for p in (SKILL_ROOT / "references").glob("*.md")}
    links_checked = len(cited)
    for name in sorted(cited - present):
        fail("references", f"cited but missing: references/{name}")
    for name in sorted(present - cited):
        fail("references", f"present but not indexed: references/{name}")
    if not (cited - present) and not (present - cited):
        ok("references", f"{len(present)} files, all indexed and present")


def check_anchors():
    global anchors_found
    corpus = "\n".join(
        p.read_text(encoding="utf-8")
        for p in [SKILL_ROOT / "SKILL.md", *sorted((SKILL_ROOT / "references").glob("*.md"))]
    )
    missing = [a for a in ANCHORS if a not in corpus]
    anchors_found = len(ANCHORS) - len(missing)
    if missing:
        fail("anchors", f"missing anchors: {missing}")
    else:
        ok("anchors", f"{anchors_found}/{len(ANCHORS)}")


def check_evals():
    global evals_checked
    path = SKILL_ROOT / "evals" / "evals.json"
    if not path.is_file():
        fail("evals", "evals/evals.json missing")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail("evals", f"invalid JSON: {exc}")
        return
    if data.get("skill_name") != SKILL_ROOT.name:
        fail("evals", "skill_name does not match directory name")
    evals = data.get("evals") or []
    evals_checked = len(evals)
    if len(evals) < 3:
        fail("evals", f"need >= 3 evals, found {len(evals)}")
        return
    seen = set()
    before = len(failures)
    for ev in evals:
        eid = ev.get("id")
        if not isinstance(eid, int) or eid in seen:
            fail("evals", f"bad or duplicate id: {eid!r}")
            continue
        seen.add(eid)
        for field in ("prompt", "expected_output"):
            if not str(ev.get(field, "")).strip():
                fail("evals", f"eval {eid}: empty {field}")
        if len(ev.get("expectations") or []) < 3:
            fail("evals", f"eval {eid}: fewer than 3 expectations")
    if len(failures) == before:
        ok("evals", f"{len(evals)} evals valid")


def check_eval_queries():
    """触发路由矩阵必须结构完整，否则触发回归不会被门禁发现。"""
    global eval_queries_checked
    path = SKILL_ROOT / "evals" / "eval_queries.json"
    if not path.is_file():
        fail("eval_queries", "evals/eval_queries.json missing")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail("eval_queries", f"invalid JSON: {exc}")
        return
    if not isinstance(data, dict):
        fail("eval_queries", "top level must be an object {skill_name, eval_type, protocol, queries}")
        return
    if data.get("skill_name") != SKILL_ROOT.name:
        fail("eval_queries", "skill_name does not match directory name")
    if data.get("eval_type") != "trigger_rate":
        fail("eval_queries", f"eval_type must be trigger_rate, got {data.get('eval_type')!r}")
    if not str(data.get("protocol") or "").strip():
        fail("eval_queries", "protocol missing")
    queries = data.get("queries") or []
    eval_queries_checked = len(queries)
    if len(queries) < 6:
        fail("eval_queries", f"need >= 6 queries, found {len(queries)}")
        return
    seen, pos, neg = set(), 0, 0
    before = len(failures)
    for q in queries:
        qid = q.get("id")
        if not qid or qid in seen:
            fail("eval_queries", f"bad or duplicate id: {qid!r}")
            continue
        seen.add(qid)
        if not str(q.get("prompt") or "").strip():
            fail("eval_queries", f"{qid}: empty prompt")
        if not isinstance(q.get("should_trigger"), bool):
            fail("eval_queries", f"{qid}: should_trigger must be boolean")
        elif q["should_trigger"]:
            pos += 1
        else:
            neg += 1
    if pos < 3 or neg < 3:
        fail("eval_queries", f"need >= 3 positive / >= 3 negative, got pos={pos} neg={neg}")
    if len(failures) == before:
        ok("eval_queries", f"{len(queries)} queries (pos={pos} neg={neg})")


def check_sensitive():
    global sensitive_findings
    for path in sorted(SKILL_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in {".md", ".json", ".py", ".sh", ".yaml"}:
            continue
        if "results" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pat in SENSITIVE.items():
            for hit in pat.finditer(text):
                sensitive_findings += 1
                fail("sensitive", f"{path.relative_to(SKILL_ROOT)}: {label} -> {redact(hit.group(0))}")
    if not sensitive_findings:
        ok("sensitive", "no private IPs or personal home paths")


def main():
    check_frontmatter()
    check_references()
    check_anchors()
    check_evals()
    check_eval_queries()
    check_sensitive()
    result = {
        "skill": SKILL_ROOT.name,
        "success": not failures,
        "failures": len(failures),
        "skills_checked": 1,
        "links_checked": links_checked,
        "anchors_found": anchors_found,
        "evals_checked": evals_checked,
        "eval_queries_checked": eval_queries_checked,
        "sensitive_findings": sensitive_findings,
        "messages": failures,
        "details": details,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in
                      ("skill", "success", "failures", "links_checked",
                       "anchors_found", "evals_checked", "eval_queries_checked",
                       "sensitive_findings")},
                     ensure_ascii=False))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
