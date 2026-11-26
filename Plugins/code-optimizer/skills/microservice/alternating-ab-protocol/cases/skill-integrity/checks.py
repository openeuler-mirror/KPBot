#!/usr/bin/env python3
"""Tier-A integrity checks for the alternating-ab-protocol skill.

Groups:
  static    frontmatter / assets / methodology anchors / sensitive scan
  contract  functional tests of alternating_ab.sh exit-code & verdict contract
            (param validation, trend-only rounds, symmetric direction, means
            output, fail-closed on negative/unparseable time, scientific
            notation, noise zone, cross-round CV gate)
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]
SKILL_MD = SKILL_DIR / "SKILL.md"
REFS_DIR = SKILL_DIR / "references"
SCRIPT = SKILL_DIR / "scripts" / "alternating_ab.sh"
RESULTS = Path(__file__).resolve().parent / "results" / "result.json"

failures = []
checks = 0


def ok():
    global checks
    checks += 1


def fail(msg):
    failures.append(msg)


def bash_exe():
    exe = shutil.which("bash")
    if exe:
        return exe
    for c in (r"C:\Program Files\Git\bin\bash.exe",
              r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(c).is_file():
            return c
    return None


def redact(s):
    return re.sub(r"(password|token|secret|api_key|client_secret)[^,\s]*",
                  r"\1=***", s, flags=re.I)


# ---------------- static ----------------
text = SKILL_MD.read_text(encoding="utf-8")
fm = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
if not fm:
    fail("no frontmatter")
else:
    name = re.search(r"^name:\s*(\S+)", fm.group(1), re.M)
    if not name or name.group(1) != SKILL_DIR.name:
        fail("name mismatch")
    else:
        ok()
    if re.search(r"^description:\s*\S", fm.group(1), re.M | re.DOTALL):
        ok()
    else:
        fail("missing description")

links = re.findall(r"\]\(((?:references|scripts)/[^)]+)\)", text)
if len(links) < 2:
    fail(f"expected >=2 links, found {len(links)}")
else:
    ok()
for t in links:
    if (SKILL_DIR / t).is_file():
        ok()
    else:
        fail(f"broken link: {t}")
for t in ("references/protocol-guide.md", "scripts/alternating_ab.sh"):
    if (SKILL_DIR / t).is_file():
        ok()
    else:
        fail(f"missing: {t}")

corpus = text + "\n" + "\n".join(
    p.read_text(encoding="utf-8") for p in sorted(REFS_DIR.glob("*.md")))
ANCHORS = [
    "交替",
    "三级健康度",
    "跨轮离散度",
    "fail-closed",
    "方向一致",
    "噪声区",
    "单调漂移",
]
for a in ANCHORS:
    if a in corpus:
        ok()
    else:
        fail(f"anchor missing: {a!r}")

# ---------------- sensitive scan (SKILL.md + references + scripts + evals) ----------------
SENSITIVE = [
    (re.compile(r"\b(10|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"\b192\.168\.\d+\.\d+\b"), "private IP"),
    (re.compile(r"\b169\.254\.\d+\.\d+\b"), "link-local IP"),
    (re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b"), "CGNAT IP"),
    (re.compile(r"\b(?:f[cd][0-9a-f]{2}|fe[89ab])[0-9a-f:]+\b", re.I), "private IPv6"),
    (re.compile(r"(?i)password\s*[=:]\s*\S+"), "credential"),
    (re.compile(r"(?i)(api_?key|secret_?key|client_secret|access_token)\s*[=:]\s*\S+"), "credential"),
    (re.compile(r"(?i)authorization\s*[:=]\s*(bearer|basic)\s+\S+"), "auth header"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"/home/[a-z][a-z0-9_-]+/"), "home path"),
    (re.compile(r"/root/[a-z][a-z0-9_-]*/"), "root path"),
    (re.compile(r"[A-Z]:[/\\]Users[/\\]"), "Windows user path"),
    (re.compile(r"(?i)\bsshpass\b"), "sshpass"),
]
scan_targets = [SKILL_MD]
scan_targets += sorted(REFS_DIR.glob("*.md"))
scan_targets += sorted((SKILL_DIR / "scripts").glob("*")) if (SKILL_DIR / "scripts").is_dir() else []
scan_targets += sorted((SKILL_DIR / "evals").glob("*.json")) if (SKILL_DIR / "evals").is_dir() else []
sensitive_hits = 0
for path in scan_targets:
    if not path.is_file():
        continue
    try:
        body = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        fail(f"not utf-8: {path.name}")
        continue
    for pat, label in SENSITIVE:
        for m in pat.finditer(body):
            fail(f"{label}: {path.name}: {redact(m.group(0))!r}")
            sensitive_hits += 1
if sensitive_hits == 0:
    ok()
# scan-scope self-test: rules must actually be able to fire (non-empty corpus)
if len(scan_targets) >= 3:
    ok()
else:
    fail(f"sensitive scan scope too small: {len(scan_targets)} files")

# ---------------- functional contract tests ----------------
bash = bash_exe()
if not bash:
    fail("bash not found")
else:
    def run_ab(*args, cwd=None):
        return subprocess.run([bash, str(SCRIPT)] + list(args),
                              capture_output=True, text=True, timeout=120,
                              encoding="utf-8", errors="replace")

    def expect(desc, want_rc, *args, contains=None, not_contains=None):
        global checks
        r = run_ab(*args)
        if r.returncode == want_rc:
            out = r.stdout + r.stderr
            if contains and contains not in out:
                fail(f"{desc}: rc ok but output missing {contains!r}")
                return
            if not_contains and not_contains in out:
                fail(f"{desc}: rc ok but output contains {not_contains!r}")
                return
            ok()
        else:
            fail(f"{desc}: expected exit {want_rc}, got {r.returncode}; "
                 f"tail: {(r.stdout + r.stderr).splitlines()[-1:] }")

    def mkbench(tmp, name, lines):
        f = Path(tmp) / name
        f.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        return f

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        base_fixed = mkbench(tmp, "base.sh", ["echo 100 ns"])
        opt_faster = mkbench(tmp, "fast.sh", ["echo 50 ns"])
        opt_slower = mkbench(tmp, "slow.sh", ["echo 200 ns"])
        opt_equal = mkbench(tmp, "equal.sh", ["echo 100 ns"])
        base_neg = mkbench(tmp, "neg.sh", ["echo -10 ns"])
        base_junk = mkbench(tmp, "junk.sh", ["echo hello"])
        base_sci = mkbench(tmp, "sci.sh", ["echo 1e-3 s"])
        opt_sci = mkbench(tmp, "scihalf.sh", ["echo 5e-4 s"])
        # counter-based: alternating values -> mixed directions
        mixed = mkbench(tmp, "mixed.sh", [
            'c=$(cat "$0.cnt" 2>/dev/null || echo 0)',
            'c=$((c+1))',
            'echo $c > "$0.cnt"',
            'if [ $((c % 2)) -eq 0 ]; then echo 50 ns; else echo 150 ns; fi'])
        # counter-based: wild base values -> high cross-round CV
        hcv = mkbench(tmp, "hcv.sh", [
            'c=$(cat "$0.cnt" 2>/dev/null || echo 0)',
            'c=$((c+1))',
            'echo $c > "$0.cnt"',
            'if [ $((c % 2)) -eq 0 ]; then echo 500 ns; else echo 100 ns; fi'])

        B = f"bash {base_fixed.as_posix()}"
        r = run_ab()
        if r.returncode == 2:
            ok()
        else:
            fail(f"no-arg: expected exit 2, got {r.returncode}")

        expect("-r 0 -> 2", 2, "-b", B, "-o", f"bash {opt_faster.as_posix()}", "-r", "0")
        expect("-r abc -> 2", 2, "-b", B, "-o", f"bash {opt_faster.as_posix()}", "-r", "abc")
        expect("-r 2 -> 2", 2, "-b", B, "-o", f"bash {opt_faster.as_posix()}", "-r", "2")
        expect("-w -1 -> 2", 2, "-b", B, "-o", f"bash {opt_faster.as_posix()}", "-r", "5", "-w", "-1")
        expect("-r 3 trend-only -> 3", 3, "-b", B, "-o", f"bash {opt_faster.as_posix()}", "-r", "3",
               contains="不可下结论")
        expect("5/5 faster -> 0", 0, "-b", B, "-o", f"bash {opt_faster.as_posix()}",
               contains="代表值")
        expect("5/5 slower -> 0", 0, "-b", B, "-o", f"bash {opt_slower.as_posix()}",
               contains="一致变慢")
        expect("5/5 equal -> 0", 0, "-b", B, "-o", f"bash {opt_equal.as_posix()}",
               contains="无差异")
        expect("negative time -> 1", 1, "-b", f"bash {base_neg.as_posix()}",
               "-o", f"bash {opt_faster.as_posix()}")
        expect("junk output -> 1", 1, "-b", f"bash {base_junk.as_posix()}",
               "-o", f"bash {opt_faster.as_posix()}")

        r = run_ab("-b", f"bash {base_sci.as_posix()}", "-o", f"bash {opt_sci.as_posix()}", "-r", "5")
        if r.returncode != 0 or "base=1000000.000ns" not in r.stdout:
            fail(f"scientific notation: rc={r.returncode}, first round: "
                 f"{r.stdout.splitlines()[:1]}")
        else:
            ok()

        Path(str(mixed) + ".cnt").unlink(missing_ok=True)
        expect("mixed -> 1 noise", 1, "-b", B, "-o", f"bash {mixed.as_posix()}", "-r", "5", "-w", "0",
               contains="噪声区")
        Path(str(hcv) + ".cnt").unlink(missing_ok=True)
        expect("high CV -> 1", 1, "-b", f"bash {hcv.as_posix()}",
               "-o", f"bash {opt_faster.as_posix()}", "-r", "5", "-w", "0",
               contains="离散度")

        # 第 2 级健康度：opt 逐轮递减（90/80/70/60/50ns）base 恒 100ns —— 逐轮比值
        # 持续单调下降，drift >= 3 -> exit 1 且报「单调漂移」
        decr = mkbench(tmp, "decr.sh", [
            'c=$(cat "$0.cnt" 2>/dev/null || echo 0)',
            'c=$((c+1))',
            'echo $c > "$0.cnt"',
            'echo $((100 - c * 10)) ns'])
        Path(str(decr) + ".cnt").unlink(missing_ok=True)
        expect("monotonic drift -> 1", 1, "-b", B, "-o", f"bash {decr.as_posix()}",
               "-r", "5", "-w", "0", contains="单调漂移")

# report
result = {
    "skill": SKILL_DIR.name,
    "case": "skill-integrity",
    "pass": not failures,
    "checks": checks,
    "failures": failures,
}
RESULTS.parent.mkdir(parents=True, exist_ok=True)
RESULTS.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
sys.exit(0 if not failures else 1)
