#!/usr/bin/env python3
"""Tier-A integrity checks for the variant-build-safety skill.

Groups:
  static    frontmatter / link resolution / methodology anchors
  template  安全模板契约断言（基线校验先于 trap 注册 / 快照独立目录 /
            per-file cp 还原 / 禁止整目录 mv 还原写法 / marker 双向校验）
  sensitive 敏感内容看护（SKILL.md + references + evals 全覆盖，凭据命中
            脱敏）+ BOM 检查
"""
import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]
SKILL_MD = SKILL_DIR / "SKILL.md"
REFS_DIR = SKILL_DIR / "references"
RESULTS = Path(__file__).resolve().parent / "results" / "result.json"

failures = []
checks = 0


def ok():
    global checks
    checks += 1


def fail(msg):
    failures.append(msg)


def redact(s):
    # 键值形态：保留键名、值按长度占位（值原文不进入 failures/result.json/CI 日志）
    s = re.sub(r"(?i)(password|token|secret|api_key|client_secret)[\s]*[:=][\s]*(\S+)",
               lambda m: f"{m.group(1)}=<redacted len={len(m.group(2))}>", s)
    s = re.sub(r"(?i)(password|token|secret|api_key|client_secret)[^,\s]*",
              r"\1=***", s)
    # Authorization 头 / 私钥块：整串掩码
    s = re.sub(r"(?i)authorization\s*[:=]\s*(bearer|basic)\s+\S+",
              r"authorization: \1 <redacted>", s)
    s = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
              "<redacted private key block>", s)
    return s


text = SKILL_MD.read_text(encoding="utf-8")

# ---- 1. frontmatter ----
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

# ---- 2. links ----
links = re.findall(r"\]\(((?:references|scripts)/[^)]+)\)", text)
if len(links) < 1:
    fail(f"expected >=1 references link, found {len(links)}")
else:
    ok()
for target in links:
    if (SKILL_DIR / target).is_file():
        ok()
    else:
        fail(f"broken link: {target}")

# ---- 3. methodology anchors ----
corpus = text + "\n" + "\n".join(
    p.read_text(encoding="utf-8") for p in sorted(REFS_DIR.glob("*.md")))
for a in ["链接指纹", "binary-compile-forensics", "CMAKE_PREFIX_PATH", "trap",
          "链接方式选择", "生成记录"]:
    if a in corpus:
        ok()
    else:
        fail(f"anchor: {a!r}")

# ---- 4. 安全模板契约 ----
# 4a. 基线校验必须先于 trap 注册（文本顺序：grep 基线块出现在 trap 行之前）
baseline_pos = text.find("FATAL: tree not BASE")
trap_pos = text.find("trap 'for f in $FILES")
if baseline_pos < 0:
    fail("template: baseline check (tree not BASE) not documented")
else:
    ok()
if 0 <= baseline_pos < trap_pos:
    ok()
else:
    fail("template: baseline check must precede trap registration")

# 4b. 快照独立目录 + per-file cp 还原
if "SNAP=" in text and "cp -f \"$SNAP/$f\" \"$f\"" in text:
    ok()
else:
    fail("template: independent snapshot dir + per-file cp restore missing")

# 4c. 模板 trap 行不得回归整目录 mv 还原写法（反面案例披露区之外）——
#     反向断言：SKILL.md 中出现的 mv 还原写法必须只在「陷阱」警示段内
mv_restore = "mv original.bak/ original"
if mv_restore in text:
    # 找 mv 写法出现的每一处，要求 40 字符内可见「陷阱」字样（近邻性）
    idxs = [m.start() for m in re.finditer(re.escape(mv_restore), text)]
    near = all("陷阱" in text[max(0, i - 60):i + 120] for i in idxs)
    if near:
        ok()  # 仅在陷阱警示语境内披露
    else:
        fail("template: mv-into-dir restore appears OUTSIDE trap-warning context (template regression?)")
else:
    ok()

# 4d. marker 双向校验（前置 + 后置）
if text.count("MARKER MISSING") >= 1 and "tree not BASE" in text:
    ok()
else:
    fail("template: two-sided marker verification missing")

# ---- 5. sensitive scan（SKILL.md + references + evals）----
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

# ---- 正样本规则自检（防规则空转）与脱敏回显自检 ----
# 正样本必须命中（规则灭活时整体假绿）；负样本必须不命中（规则过宽误报）
SELFTEST_POS = [
    "机房 192.168.1.100 服务器",
    "password: hunter2",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.fakejwt",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEpA\n-----END RSA PRIVATE KEY-----",
]
SELFTEST_NEG = ["192.168 网段说明文档", "记不住 password 这个词", "公开白皮书"]
pos_ok = neg_ok = True
for s in SELFTEST_POS:
    if not any(pat.search(s) for pat, _label in SENSITIVE):
        pos_ok = False
        fail(f"selftest: positive sample no rule fires: {redact(s)[:40]}")
for s in SELFTEST_NEG:
    if any(pat.search(s) for pat, _label in SENSITIVE):
        neg_ok = False
        fail(f"selftest: negative sample false-fires: {s[:40]}")
if pos_ok and neg_ok:
    ok()
# 脱敏回显自检：redact 后的消息不得含原始凭据值
redact_samples = ["password: hunter2", "api_key=sk-1234567890abcdef",
                  "Authorization: Bearer eyJhbGciOi.fake"]
redact_ok = all(
    ("hunter2" not in redact(s) and "sk-1234567890abcdef" not in redact(s)
     and "eyJhbGciOi.fake" not in redact(s))
    for s in redact_samples)
if redact_ok:
    ok()
else:
    fail("selftest: redact leaks original credential values")
result_selftest = {"rules_pos": pos_ok, "rules_neg": neg_ok, "redact": redact_ok}

# BOM 检查
bom_bad = [p.name for p in scan_targets if p.is_file()
           and p.read_bytes().startswith(b"\xef\xbb\xbf")]
if bom_bad:
    fail(f"BOM present: {bom_bad}")
else:
    ok()

# ---- report ----
result = {"skill": SKILL_DIR.name, "case": "skill-integrity",
          "pass": not failures, "checks": checks, "failures": failures,
          "selftest": result_selftest}
RESULTS.parent.mkdir(parents=True, exist_ok=True)
RESULTS.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
sys.exit(0 if not failures else 1)
