#!/usr/bin/env python3
"""Tier-A integrity checks for allocator-ab-trial skill.

Groups:
  static    frontmatter / link resolution / methodology anchors / sensitive scan
  contract  functional tests of allocator_ab.sh (param validation fail-fast,
            junk/negative output fail-closed; happy path when the system
            exposes an alternative allocator via ldconfig)
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]
SKILL_MD = SKILL_DIR / "SKILL.md"
SCRIPT = SKILL_DIR / "scripts" / "allocator_ab.sh"
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

# 链接检查：>=1 个 references|scripts 链接且全部可解析（不允许零链接恒真）
links = re.findall(r"\]\(((?:references|scripts)/[^)]+)\)", text)
if len(links) < 1:
    fail(f"expected >=1 references/scripts link, found {len(links)}")
else:
    ok()
for t in links:
    if (SKILL_DIR / t).is_file():
        ok()
    else:
        fail(f"broken link: {t}")

for t in ("scripts/allocator_ab.sh", "scripts/README.md"):
    if (SKILL_DIR / t).is_file():
        ok()
    else:
        fail(f"missing: {t}")

corpus = text
anchors = ["LD_PRELOAD", "glibc", "交替", "fail-closed", "负载形态"]
for a in anchors:
    if a in corpus:
        ok()
    else:
        fail(f"anchor: {a!r}")

# ---------------- sensitive scan (SKILL.md + scripts + evals + references) ----------------
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
for sub in ("references", "scripts", "evals"):
    d = SKILL_DIR / sub
    if d.is_dir():
        scan_targets += sorted(d.iterdir())
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
if len(scan_targets) >= 3:
    ok()
else:
    fail(f"sensitive scan scope too small: {len(scan_targets)} files")

# BOM 检查（evals 曾携带 U+FEFF）
bom_bad = [p.name for p in (SKILL_DIR / "evals").glob("*.json")
           if p.read_bytes().startswith(b"\xef\xbb\xbf")]
if bom_bad:
    fail(f"BOM present: {bom_bad}")
else:
    ok()

# ---------------- functional contract tests ----------------
bash = bash_exe()
if not bash:
    fail("bash not found")
else:
    def run_ab(*args):
        return subprocess.run([bash, str(SCRIPT)] + list(args),
                              capture_output=True, text=True, timeout=120,
                              encoding="utf-8", errors="replace")

    def expect(desc, want_rc, *args, contains=None):
        r = run_ab(*args)
        if r.returncode == want_rc:
            if contains and contains not in (r.stdout + r.stderr):
                fail(f"{desc}: rc ok but missing {contains!r}")
                return
            ok()
        else:
            fail(f"{desc}: expected exit {want_rc}, got {r.returncode}")

    r = run_ab()
    if r.returncode == 2:
        ok()
    else:
        fail(f"no-arg: expected 2, got {r.returncode}")

    expect("-r 0 -> 2", 2, "-c", "echo 100 ns", "-r", "0")
    expect("-r abc -> 2", 2, "-c", "echo 100 ns", "-r", "abc")
    expect("-w -1 -> 2", 2, "-c", "echo 100 ns", "-r", "2", "-w", "-1")

    # ---- mock 环境：PATH 注入假 ldconfig（报一个假 jemalloc）使契约测试全环境可跑 ----
    # （回归防护不应依赖宿主机装了什么分配器——ggboom2 意见）
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as mt:
        mockbin = Path(mt) / "mockbin"
        mockbin.mkdir()
        (mockbin / "ldconfig").write_text(
            "#!/usr/bin/env bash\ncat <<'EOF'\n\tlibjemalloc.so.2 (libc6) => /usr/bin/msys-2.0.dll\n\tlibz.so.1 (libc6) => /lib/libz.so.1\nEOF\n",
            encoding="utf-8", newline="\n")
        # 假 ldd：只对 fake_linked_* 可执行报告 libjemalloc（模拟链接期锁定）
        (mockbin / "ldd").write_text(
            '#!/usr/bin/env bash\ncase "$1" in *fake_linked*) echo "\tlibjemalloc.so.2 => /usr/fake/lib/libjemalloc.so.2 (0x7f00)";; esac\nexit 0\n',
            encoding="utf-8", newline="\n")
        # 假的"链接期锁定"被测可执行（真实文件，过 [ -x ] 检查）
        fake_linked = mockbin / "fake_linked_bench.sh"
        fake_linked.write_text("#!/usr/bin/env bash\necho 100 ns\n",
                               encoding="utf-8", newline="\n")
        fake_linked.chmod(0o755)
        mock_env = dict(os.environ)
        mock_env["PATH"] = str(mockbin) + os.pathsep + mock_env.get("PATH", "")
        # msys bash 把 /usr/bin 无条件前置，PATH 无法遮蔽系统 ldd——
        # 用脚本提供的 LDD_PROBE 注入口直接指向 mock
        mock_env["LDD_PROBE"] = (mockbin / "ldd").as_posix()

        def run_mock(*args):
            return subprocess.run([bash, str(SCRIPT)] + list(args),
                                  capture_output=True, text=True, timeout=120,
                                  encoding="utf-8", errors="replace", env=mock_env)

        # 1) 均值/百分比数值断言：jemalloc 臂逐轮 200/200/100（计数器按轮变化），
        #    glibc 恒 100 —— 均值应为 166.667 vs 100.000，变化 +66.7%
        var_bench = mockbin / "var_bench.sh"
        var_bench.write_text(
            "#!/usr/bin/env bash\n"
            'n=$(cat "$0.cnt" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$0.cnt"\n'
            'if [ -n "${LD_PRELOAD:-}" ]; then\n'
            "  case $(((n+1)/2)) in 1|2) echo 200 ns;; *) echo 100 ns;; esac\n"
            "else\n"
            "  echo 100 ns\n"
            "fi\n",
            encoding="utf-8", newline="\n")
        var_bench.chmod(0o755)
        Path(str(var_bench) + ".cnt").unlink(missing_ok=True)
        r = run_mock("-c", var_bench.as_posix(), "-r", "3", "-w", "0")
        out = r.stdout + r.stderr
        probs = []
        if r.returncode != 0:
            probs.append(f"rc={r.returncode}")
        if "166.667" not in out:
            probs.append(f"mean missing 166.667; tail={out.splitlines()[-2:]}")
        if "+66.7%" not in out:
            probs.append("delta missing +66.7%")
        if "比值" not in out:
            probs.append("ratios missing")
        if probs:
            fail("means numeric: " + "; ".join(probs))
        else:
            ok()

        # 2) 方向结论：jemalloc 恒 200 vs glibc 恒 100 —— 一致慢于可下结论
        const_bench = mockbin / "const_bench.sh"
        const_bench.write_text(
            "#!/usr/bin/env bash\n"
            'if [ -n "${LD_PRELOAD:-}" ]; then echo 200 ns; else echo 100 ns; fi\n',
            encoding="utf-8", newline="\n")
        const_bench.chmod(0o755)
        r = run_mock("-c", const_bench.as_posix(), "-r", "3", "-w", "0")
        out = r.stdout + r.stderr
        if r.returncode != 0 or "一致慢于 glibc，可下结论" not in out:
            fail(f"verdict: rc={r.returncode}, tail={out.splitlines()[-2:]}")
        else:
            ok()

        # 3) 链接期锁定：mock ldd 报告 libjemalloc -> exit 1
        r = run_mock("-c", fake_linked.as_posix() + " -r 2", "-r", "2", "-w", "0")
        out = r.stdout + r.stderr
        if r.returncode != 1 or "链接期已使用" not in out:
            fail(f"linked-alloc: rc={r.returncode}, tail={out.splitlines()[-2:]}")
        else:
            ok()

        # 4) taskset 前缀形态下链接期核实仍生效（状态机跨词回归）
        r = run_mock("-c", f"taskset -c 0 {fake_linked.as_posix()}", "-r", "2", "-w", "0")
        out = r.stdout + r.stderr
        if r.returncode != 1 or "链接期已使用" not in out:
            fail(f"taskset probe: rc={r.returncode}, tail={out.splitlines()[-2:]}")
        else:
            ok()

        # 5) 垃圾输出与负值 fail-closed（mock 环境下必跑，不依赖宿主机分配器）
        r = run_mock("-c", "echo hello world", "-r", "2", "-w", "0")
        if r.returncode == 1:
            ok()
        else:
            fail(f"junk: rc={r.returncode}")
        r = run_mock("-c", "echo -10 ns", "-r", "2", "-w", "0")
        if r.returncode == 1:
            ok()
        else:
            fail(f"negative: rc={r.returncode}")

# report
result = {"skill": SKILL_DIR.name, "case": "skill-integrity",
          "pass": not failures, "checks": checks, "failures": failures}
RESULTS.parent.mkdir(parents=True, exist_ok=True)
RESULTS.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
sys.exit(0 if not failures else 1)
