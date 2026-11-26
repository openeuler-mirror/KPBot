#!/usr/bin/env bash
# skill-integrity 用例驱动：TerminalBench skill 静态契约看护
# 用法: bash run.sh（tier A，21 项断言；可选 SUITE_MANIFEST=<suite>/.../import-manifest.json 开启上游核验）
set -euo pipefail

case_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
results_dir="$case_dir/results"; mkdir -p "$results_dir"
SKILL_DIR="$case_dir/../.." RESULTS_DIR="$results_dir" python3 - "$case_dir" <<'EOF'
import json, os, re, sys

case_dir, skill_dir, results_dir = sys.argv[1], os.environ["SKILL_DIR"], os.environ["RESULTS_DIR"]
checks = []
def check(name, cond, detail=""):
    checks.append({"name": name, "pass": bool(cond), "detail": str(detail)[:120]})

skill_md = open(f"{skill_dir}/SKILL.md", encoding="utf-8").read()
profile = open(f"{skill_dir}/references/01-benchmark-profile.md", encoding="utf-8").read()
runbook = open(f"{skill_dir}/references/02-runbook.md", encoding="utf-8").read()
fixture = json.load(open(f"{case_dir}/fixtures/import-manifest.terminalbench.json", encoding="utf-8"))

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
check("name equals directory name", fm.get("name") == "benchmark-terminalbench-protocol")
check("description non-empty and <=1024 chars", 0 < len(desc) <= 1024, len(desc))
check("description has trigger and boundary", "TerminalBench" in desc and "回放" in desc and "不适用" in desc)

# 2) 引用资源可达与真源自含声明（成员登记归域维护者，本 skill 不改 DOMAIN.md）
for rel in ("references/01-benchmark-profile.md", "references/02-runbook.md"):
    check(f"resource exists: {rel}", os.path.isfile(f"{skill_dir}/{rel}"))
check("SKILL.md links both references",
      "references/01-benchmark-profile.md" in skill_md and "runbook" in skill_md)
check("SKILL.md self-carries source-of-truth and access precondition",
      "个人仓" in skill_md and "访问授权" in skill_md and "673be52" in skill_md)

# 3) 画像对账：4 case 的 case_id/steps/tokens/image 与 fixture 一致
fc = fixture["cases"]
check("fixture carries exactly 4 terminalbench cases",
      len(fc) == 4 and all(c["case_id"].startswith("terminalbench_") for c in fc), len(fc))
mismatch = [c["case_id"] for c in fc
            if not (f"| {c['case_id']} " in profile and f"| {c['steps']} |" in profile
                    and f"{c['tokens_total']:,}" in profile and c["image"] in profile)]
check("profile table reconciles with fixture manifest", not mismatch, mismatch)
check("profile cites source manifest and commit",
      "import-manifest.json" in profile and "673be52" in profile)

# 4) 命令纯度：replay 文件均为 terminalbench_*；导入过滤与子集目录契约
replay = set(re.findall(r"([A-Za-z0-9_\-]+\.replay\.json)", runbook))
check("runbook replay files are terminalbench-only", all(n.startswith("terminalbench_") for n in replay),
      sorted(n for n in replay if not n.startswith("terminalbench_")))
bm = re.findall(r"--benchmarks\s+([\w,]+)", runbook)
check("importer filter is terminalbench only", set(bm) == {"terminalbench"}, bm)
check("subset directory preparation present", "terminalbench_*.replay.json artifacts/traces/agbench-terminalbench" in runbook)
check("bench uses terminalbench subset dir", "--trajectory-dir artifacts/traces/agbench-terminalbench" in runbook)
# 5) 真源基准与仓库 URL（自含，不依赖 DOMAIN.md）
check("suite commit pin present in skill+runbook", all("673be52" in t for t in (skill_md, runbook)))
check("suite repo URL present in skill", "agent-workload-benchmark-suite" in skill_md)
# 6) 凭据扫描：E2B_*/API_KEY 赋值必须为占位符或环境引用
cred = [f"{p}: {mm.group(0)[:50]}" for p, t in (("SKILL.md", skill_md), ("references/02-runbook.md", runbook))
        for mm in re.finditer(r"(E2B_[A-Z_]+|API_KEY)\s*=\s*([^<\s][^\n]*)", t)
        if mm.group(2).strip().strip("'\"") and not mm.group(2).strip().strip("'\"").startswith(("${", "$", "<"))]
check("no credential literals in skill files", not cred, cred)
# 7) 上游真源比对（可选）：SUITE_MANIFEST 指向 suite 仓 import-manifest.json 时，画像与 fixture 一并对上游核验
up = os.environ.get("SUITE_MANIFEST")
if up:
    real = [c for c in json.load(open(up, encoding="utf-8"))["cases"] if c["benchmark"] == "terminalbench"]
    real_key = sorted((c["case_id"], c["steps"], c["tokens_total"], c["image"]) for c in real)
    fix_key = sorted((c["case_id"], c["steps"], c["tokens_total"], c["image"]) for c in fc)
    prof_ok = all(f"| {c['case_id']} " in profile and f"| {c['steps']} |" in profile
                  and f"{c['tokens_total']:,}" in profile and c["image"] in profile for c in real)
    check("upstream manifest reconciliation (SUITE_MANIFEST)", real_key == fix_key and prof_ok, f"upstream cases={len(real)}")
else:
    check("upstream manifest reconciliation (SUITE_MANIFEST)", True, "fixture mode; set SUITE_MANIFEST=<suite import-manifest.json> for upstream verification")
# 8) 命令契约锚点：runbook 的 flag 与环境变量 ⊆ fixture 固化的 suite 契约（源自 cli.py/__main__.py/报告脚本/README）
allowed = set(sum((fixture["contract"][k] for k in ("runner_cli", "importer_cli", "report_cli")), [])) | {"--locked", "--help"}
used = set(re.findall(r"(--[a-z][a-z0-9-]*)", runbook))
check("runbook flags within vendored suite contract", used <= allowed, sorted(used - allowed))
envs_used = set(re.findall(r"\b(E2B_[A-Z_]+|SNAP_BASE|DISKS)\b", runbook))
check("runbook env vars within vendored suite contract", envs_used <= set(fixture["contract"]["env"]),
      sorted(envs_used - set(fixture["contract"]["env"])))
passed = sum(c["pass"] for c in checks)
result = {"success": passed == len(checks), "checks_total": len(checks), "checks_passed": passed,
          "manifest_source": "upstream" if up else "fixture",
          "failed_checks": [c for c in checks if not c["pass"]]}
json.dump(result, open(f"{results_dir}/result.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"checks: {passed}/{len(checks)} passed -> {results_dir}/result.json")
sys.exit(0 if result["success"] else 1)
EOF
