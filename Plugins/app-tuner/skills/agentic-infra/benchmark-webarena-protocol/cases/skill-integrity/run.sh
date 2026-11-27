#!/usr/bin/env bash
# skill-integrity 用例驱动：WebArena skill 静态契约看护
# 用法: bash run.sh（tier A，21 项断言；可选 SUITE_MANIFEST 开启上游核验、SUITE_REPO 实证真源 commit）
set -euo pipefail

case_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
results_dir="$case_dir/results"; mkdir -p "$results_dir"
SKILL_DIR="$case_dir/../.." RESULTS_DIR="$results_dir" python3 - "$case_dir" <<'EOF'
import json, os, re, subprocess, sys

case_dir, skill_dir, results_dir = sys.argv[1], os.environ["SKILL_DIR"], os.environ["RESULTS_DIR"]
checks = []
def check(name, cond, detail=""):
    checks.append({"name": name, "pass": bool(cond), "detail": str(detail)[:120]})

skill_md = open(f"{skill_dir}/SKILL.md", encoding="utf-8").read()
profile = open(f"{skill_dir}/references/01-benchmark-profile.md", encoding="utf-8").read()
runbook = open(f"{skill_dir}/references/02-runbook.md", encoding="utf-8").read()
fixture = json.load(open(f"{case_dir}/fixtures/import-manifest.webarena.json", encoding="utf-8"))

# 1) frontmatter：可解析、name=目录名、description 非空且 <=1024、含触发与不适用边界
m = re.match(r"^---\n(.*?)\n---\n", skill_md, re.S)
lines = m.group(1).splitlines() if m else []
fm, desc, i = {}, "", 0
while i < len(lines):
    line = lines[i]
    if line.startswith("name:"):
        fm["name"] = line.split(":", 1)[1].strip()
    if line.startswith("description:"):
        rest = line.split(":", 1)[1].strip()
        if rest in (">", "|", ">-", "|-"):
            folded = []
            for cont in lines[i + 1:]:
                if not cont.startswith((" ", "\t")) or not cont.strip():
                    break
                folded.append(cont.strip()); i += 1
            desc = " ".join(folded)
        else:
            desc = rest.strip("\"'")
    i += 1
check("frontmatter parses with name", bool(m and fm.get("name")))
check("name equals directory name", fm.get("name") == "benchmark-webarena-protocol")
check("description non-empty and <=1024 chars", 0 < len(desc) <= 1024, len(desc))
check("description has trigger and boundary", "webarena" in desc and "回放" in desc and "不适用" in desc)

# 2) 引用资源可达与真源自含声明（成员登记归域维护者，本 skill 不改 DOMAIN.md）
for rel in ("references/01-benchmark-profile.md", "references/02-runbook.md"):
    check(f"resource exists: {rel}", os.path.isfile(f"{skill_dir}/{rel}"))
check("SKILL.md links both references",
      "references/01-benchmark-profile.md" in skill_md and "runbook" in skill_md)
check("SKILL.md self-carries source-of-truth and access precondition",
      "个人仓" in skill_md and "访问授权" in skill_md and "673be52" in skill_md)

# 3) 画像对账（行级）：按 case_id 定位表格行，与 fixture 逐字段全等比对
fc = fixture["cases"]
check("fixture carries exactly 4 webarena cases with unique ids",
      len(fc) == 4 and len({c["case_id"] for c in fc}) == 4
      and all(c["case_id"].startswith("webarena_") for c in fc), len(fc))
rows = {}
dup = False
table_lines = [ln for ln in profile.splitlines() if ln.startswith("| webarena_")]
for ln in table_lines:
    cols = [x.strip() for x in ln.strip().strip("|").split("|")]
    if len(cols) == 7:
        if cols[0] in rows:
            dup = True
        rows[cols[0]] = cols
def row_key(c):
    lab = [c.get(k, 0) for k in ("exec", "read", "write", "edit", "apply_patch")]
    return (c["case_id"], str(c["steps"]), "/".join(str(x) for x in lab),
            f"{c['tokens_total']:,}", str(c["think_time"]), c["skipped"], c["image"])
fix_rows = {row_key(c)[0]: row_key(c)[1:] for c in fc}
mismatch = []
for c in fc:
    k = row_key(c)
    r = rows.get(c["case_id"])
    if r is None or tuple(r[1:]) != k[1:]:
        mismatch.append(c["case_id"])
extra = [cid for cid in rows if cid not in fix_rows]
check("profile table row-level reconciles with fixture", not mismatch and not extra
      and not dup and len(table_lines) == 4 and len(rows) == 4,
      f"mismatch={mismatch[:3]} extra={extra[:3]} dup={dup} lines={len(table_lines)}")
check("profile cites source manifest and commit",
      "import-manifest.json" in profile and "673be52" in profile)

# 4) 命令纯度：导入命令必含 --benchmarks webarena；replay 文件均为 webarena_*；子集目录契约
imp_lines = [l for l in runbook.splitlines() if "trajectory_importer" in l and " import" in l]
bm = re.findall(r"--benchmarks\s+([\w,]+)", runbook)
check("importer filter is webarena only", bool(imp_lines)
      and all("--benchmarks webarena" in l for l in imp_lines)
      and set(bm) == {"webarena"}, imp_lines)
replay = {x for x in re.findall(r"([A-Za-z0-9_\-\*]+\.replay\.json)", runbook) if not x.startswith("*")}
concrete = {x for x in replay if "*" not in x}
expected_files = {c["case_id"] + ".replay.json" for c in fc}
check("runbook replay files == fixture case ids and webarena-only",
      all(x.startswith("webarena_") for x in replay) and concrete == expected_files,
      f"missing={sorted(expected_files - concrete)[:3]} extra={sorted(concrete - expected_files)[:3]}")
check("subset dir per-run mktemp + canonical expected list",
      "mktemp -d artifacts/traces/agbench-webarena." in runbook and "expected = {" in runbook)
check("bench covers 3 backends + pause-resume and uses per-run subset dir",
      "--backend docker" in runbook and "--backend aenv" in runbook and "--backend e2b" in runbook
      and "--mode pause-resume --backend aenv" in runbook
      and '--trajectory-dir "$SUBSET_DIR"' in runbook)

# 5) 真源 commit 钉扎（设 SUITE_REPO 时实证存在，否则字面钉扎）
sr = os.environ.get("SUITE_REPO")
if sr:
    r = subprocess.run(["git", "-C", sr, "cat-file", "-e", "673be52^{commit}"],
                       capture_output=True)
    check("suite commit pin (SUITE_REPO verified)", r.returncode == 0, sr)
else:
    check("suite commit pin (literal; set SUITE_REPO to verify)",
          all("673be52" in t for t in (skill_md, runbook)))
check("suite repo URL present in skill", "agent-workload-benchmark-suite" in skill_md)

# 6) 凭据扫描：命中时详情只含变量名/文件/行号（脱敏），不含被检值
cred = []
for p, t in (("SKILL.md", skill_md), ("references/02-runbook.md", runbook)):
    for mm in re.finditer(r"(E2B_[A-Z_]+|API_KEY)\s*=\s*([^<\s][^\n]*)", t):
        val = mm.group(2).strip().strip("'\"")
        if val and not val.startswith(("${", "$", "<")):
            cred.append(f"{p}:{t[:mm.start()].count(chr(10)) + 1} {mm.group(1)}=<redacted>")
check("no credential literals in skill files", not cred, cred)

# 7) 上游真源比对（可选）：SUITE_MANIFEST 指向 suite 仓 import-manifest.json 时行级核验
up = os.environ.get("SUITE_MANIFEST")
if up:
    real = [c for c in json.load(open(up, encoding="utf-8"))["cases"] if c["benchmark"] == "webarena"]
    def upstream_key(c):
        lab = c["steps_by_label"]
        return (c["case_id"], c["steps"], c["tokens_total"], c["image"],
                lab.get("exec", 0), lab.get("read", 0), lab.get("write", 0),
                lab.get("edit", 0), lab.get("apply_patch", 0))
    def fixture_key(c):
        return (c["case_id"], c["steps"], c["tokens_total"], c["image"],
                c["exec"], c["read"], c["write"], c["edit"], c["apply_patch"])
    rk = sorted(upstream_key(c) for c in real)
    fk = sorted(fixture_key(c) for c in fc)
    ok_rows = all(rows.get(c["case_id"]) is not None for c in real)
    check("upstream manifest row-level reconciliation (SUITE_MANIFEST)",
          rk == fk and ok_rows and len(real) == 4, f"upstream cases={len(real)}")
else:
    check("upstream manifest reconciliation (SUITE_MANIFEST)", True,
          "fixture mode; set SUITE_MANIFEST=<suite import-manifest.json> for upstream verification")

# 8) 命令契约锚点：runbook 的 flag 与环境变量 ⊆ fixture 固化的 suite 契约
allowed = set(sum((fixture["contract"][k] for k in ("runner_cli", "importer_cli", "report_cli", "build_cli")), [])) | {"--locked", "--help"}
used = set(re.findall(r"(--[a-z][a-z0-9-]*)", runbook))
check("runbook flags within vendored suite contract", used <= allowed, sorted(used - allowed))
envs_used = set(re.findall(r"\b(E2B_[A-Z_]+|SNAP_BASE|DISKS)\b", runbook))
check("runbook env vars within vendored suite contract", envs_used <= set(fixture["contract"]["env"]),
      sorted(envs_used - set(fixture["contract"]["env"])))

passed = sum(c["pass"] for c in checks)
result = {"success": passed == len(checks), "checks_total": len(checks), "checks_passed": passed,
          "manifest_source": "upstream" if up else "fixture",
          "commit_pin": "verified" if sr else "literal",
          "failed_checks": [c for c in checks if not c["pass"]]}
json.dump(result, open(f"{results_dir}/result.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"checks: {passed}/{len(checks)} passed -> {results_dir}/result.json")
sys.exit(0 if result["success"] else 1)
EOF
