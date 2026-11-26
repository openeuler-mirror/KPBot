#!/usr/bin/env python3
"""Tier-A integrity checks for a KPBot skill.

Validates the skill stays well-formed and de-privatized as it evolves:
  1. SKILL.md frontmatter: name matches the skill directory, description present,
     no license:/metadata: keys (repo convention: frontmatter = name + description only)
  2. every references/*.md reference in SKILL.md resolves on disk
     (markdown-link form `](references/...)` or backtick form `` `references/...` ``)
  3. description carries a trigger + anti-trigger convention:
     leaf skills -> "适用" + "不适用"; hub skills -> "触发"
  4. no sensitive content (private IPs, credentials, personal home paths)
  5. every scripts/*.py reference in SKILL.md resolves on disk

Writes results/result.json; exits non-zero on any failure.
"""

import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]
SKILL_MD = SKILL_DIR / "SKILL.md"
REFS_DIR = SKILL_DIR / "references"
SCRIPTS_DIR = SKILL_DIR / "scripts"
RESULTS = Path(__file__).resolve().parent / "results" / "result.json"

failures = []
links_checked = 0
scripts_checked = 0
sensitive_findings = []


def fail(msg):
    failures.append(msg)


text = SKILL_MD.read_text(encoding="utf-8").replace("\r\n", "\n")
fm = ""

fm_match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
if not fm_match:
    fail("SKILL.md has no frontmatter block")
else:
    fm = fm_match.group(1)
    name_match = re.search(r"^name:\s*(\S+)", fm, re.MULTILINE)
    if not name_match:
        fail("frontmatter: missing name")
    elif name_match.group(1) != SKILL_DIR.name:
        fail(f"frontmatter: name {name_match.group(1)!r} != dir {SKILL_DIR.name!r}")
    if not re.search(r"^description:\s*\S", fm, re.MULTILINE | re.DOTALL):
        fail("frontmatter: missing/empty description")
    if re.search(r"^license:|^metadata:", fm, re.MULTILINE):
        fail("frontmatter: must not carry license:/metadata: keys")

# reference links resolve (markdown-link or backtick form)
link_targets = re.findall(r"\]\((references/[^)]+\.md)\)", text)
link_targets += re.findall(r"`(references/[A-Za-z0-9_.-]+\.md)`", text)
for target in link_targets:
    links_checked += 1
    if not (SKILL_DIR / target).is_file():
        fail(f"broken reference link: {target}")
if links_checked < 1:
    fail("expected at least 1 references/*.md link in SKILL.md")

# script references resolve (markdown-link or backtick form)
script_targets = re.findall(r"\]\((scripts/[^)]+\.py)\)", text)
script_targets += re.findall(r"`(scripts/[A-Za-z0-9_.-]+\.py)`", text)
for target in script_targets:
    scripts_checked += 1
    if not (SKILL_DIR / target).is_file():
        fail(f"broken script reference: {target}")

# trigger + anti-trigger convention
leaf_style = ("适用" in fm) and ("不适用" in fm)
hub_style = "触发" in fm
if not (leaf_style or hub_style):
    fail("description lacks 适用/不适用反例 (leaf) or 触发 (hub) convention")

# sensitive content scan — cover SKILL.md + references/*.md + scripts/** so that
# a future commit dropping credentials into a script (e.g. scripts/deploy.sh) is
# caught. cases/ harness is intentionally excluded (template code, self-referential
# regex literals, no project content).
# 值侧形状：引号字符串，或不含空白/引号/括号/尖括号的 3 位以上字面量。
# 后者要求后随空白、行尾或分隔符，用来排除 response[...] / config.get(...) 这类
# 调用与索引表达式，避免把普通代码赋值误判为凭据。
_VAL = r"""(?:['"][^'"\n]{1,}['"]|[^\s'"\[\](){}<>,;]{3,}(?=\s|$|[,;}\]#]))"""

SENSITIVE = [
    # (正则, 说明, 是否脱敏回显)：内部地址 / 个人路径 / 工具本身不是凭据，可原样回显
    # 便于修复；凭据类一律脱敏，避免原文落进 results/result.json（install.sh 会
    # cp -r 整个 skill 目录）与 stdout/CI 日志。
    (r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b", "private IPv4", False),
    (r"\b(?:169\.254|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\.\d{1,3}\.\d{1,3}\b", "link-local/CGNAT IPv4", False),
    (r"(?i)\b(?:f[cd][0-9a-f]{2}|fe80):[0-9a-f:]{2,}", "private IPv6", False),
    (r"/(?:home|Users)/(?!<|\$|\{)[A-Za-z0-9_.-]+/", "personal home path", False),
    (r"/root/(?!<|\$|\{)[A-Za-z0-9_.-]+/", "root home path", False),
    (r"\bsshpass\b", "ssh credential tool", False),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key block", False),

    # 键名式凭据：键名与值都可能带引号（JSON/YAML/dict），键名允许命名空间前缀。
    # 不用 \b：_ 属于 \w，`_token` / `secret_` 处 \b 不成立会整体漏检。
    # 1) 大写 ENV 风格：GITHUB_TOKEN / MEM0_API_TOKEN / AWS_SECRET_ACCESS_KEY ...
    (r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|PASSPHRASE|CREDENTIAL)\s*[:=]\s*" + _VAL,
     "credential-like literal", True),
    # 2) 显式凭据键名（可带命名空间前缀与引号）：client_secret / secret_key / db_password ...
    (r"(?i)(?<![A-Za-z0-9_])(?:[A-Za-z0-9]+[_-])*['\"]?(?:api[_-]?key|apikey|api[_-]?token|access[_-]?token|refresh[_-]?token|auth[_-]?token|id[_-]?token|bearer[_-]?token|session[_-]?token|private[_-]?token|github[_-]?token|secret[_-]?access[_-]?key|secret[_-]?key|client[_-]?secret|app[_-]?secret|password|passwd|passphrase|credential)['\"]?\s*[:=]\s*" + _VAL,
     "credential-like literal", True),
    # 3) 裸写 token / secret：值须为字面量
    (r"(?i)(?<![A-Za-z0-9_])['\"]?(?:token|secret)['\"]?\s*[:=]\s*" + _VAL,
     "credential-like literal", True),
    # 4) 裸写 pwd（值不能是 $(pwd) 这类命令替换）
    (r"(?i)(?<![A-Za-z0-9_])['\"]?pwd['\"]?\s*[:=]\s*" + _VAL,
     "credential-like literal", True),
    # 5) Authorization 头
    (r"(?i)(?<![A-Za-z0-9_])['\"]?authorization['\"]?\s*[:=]\s*['\"]?[^\s'\"]+", "authorization header", True),

    # 已知厂商前缀 / JWT 字面量：形状固定，误报极低
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id", True),
    (r"\bsk-[A-Za-z0-9_-]{20,}", "openai-style secret key", True),
    (r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}\b", "github token literal", True),
    (r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "github fine-grained token", True),
    (r"\bglpat-[A-Za-z0-9_-]{20,}", "gitlab token literal", True),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}", "slack token literal", True),
    (r"\bAIza[0-9A-Za-z_-]{30,}", "google api key literal", True),
    (r"\bhf_[A-Za-z0-9]{20,}\b", "huggingface token literal", True),
    (r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", "JWT literal", True),
]


def redact(matched, mask):
    """凭据类命中只保留键名，值以长度占位，防止原文进入结果文件与日志。"""
    if not mask:
        return matched
    m = re.match(r"\s*(.*?)\s*[:=]\s*(\S.*)$", matched, re.DOTALL)
    if not m:
        return "<redacted>"
    return f"{m.group(1)}=<redacted len={len(m.group(2))}>"


# references/ 递归扫描（reference 子目录里的文件同样算文档面）
scan_files = [SKILL_MD] + sorted(REFS_DIR.rglob("*.md"))
if SCRIPTS_DIR.is_dir():
    for ext in ("*.py", "*.sh", "*.md", "*.yaml", "*.yml", "*.json", "*.toml"):
        scan_files.extend(SCRIPTS_DIR.rglob(ext))
scan_files = sorted(set(scan_files))
seen = set()
for path in scan_files:
    body = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    rel = path.relative_to(SKILL_DIR).as_posix()
    for pattern, why, mask in SENSITIVE:
        for hit in re.finditer(pattern, body):
            line_no = body[:hit.start()].count("\n") + 1
            if (rel, line_no) in seen:  # 同一行多条规则命中同一凭据只记一次
                continue
            seen.add((rel, line_no))
            sensitive_findings.append(f"{rel}:{line_no}: {why}: {redact(hit.group(0), mask)}")
if sensitive_findings:
    fail(f"sensitive content found: {len(sensitive_findings)} hit(s)")

# ---- 规则自检：证明上面的规则与脱敏不是空转 -------------------------------
# 正样本必须至少被一条规则命中，负样本必须一条都不命中，凭据类回显必须脱敏。
# 这些假凭据只出现在 cases/ 下，而 scan_files 只覆盖 SKILL.md / references / scripts，
# 所以不会 self-trigger；自检结果写入 result.json 供 case.yaml 断言。
SELFTEST_POSITIVE = [
    "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwx1234",
    "HF_TOKEN=hf_abcdefghijklmnopqrstuvwxyz",
    "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz012345",
    'API_KEY="ak_live_abcdef"',
    '{"api_key": "ak_live_abcdef", "token": "eyJhbGciOiJIUzI1NiJ9"}',
    '"password": "s3cr3t-value"',
    "'secret': 'abc123'",
    "aws_secret_access_key=wJalrXUtnFEMIvGr5",
    "client_secret=abc123def456",
    "token=abcdefgh1234",
    "password=hunter2",
    "pwd=abc123",
    "Authorization: Bearer abcdef123456",
    "AKIAIOSFODNN7EXAMPLE",
    "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
    "glpat-abcdefghij1234567890",
    "xoxb-1234567890-abcdefghij",
    "AIzaSyA1234567890abcdefghijklmnopqrstu",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
    "10.11.12.13",
    "169.254.1.1",
    "100.64.3.4",
    "fd00::1",
    "/home/alice/project",
    "/root/.ssh/id_rsa",
    "-----BEGIN RSA PRIVATE KEY-----",
    "sshpass -p x ssh host",
]
SELFTEST_NEGATIVE = [
    "sort_key = lambda x: x",
    'next_page_token = response["nextPageToken"]',
    'primary_key = "name"',
    "cache_key = build_cache_key(item)",
    "key = cache_key(name, item, args, kwargs)",
    "keys=some_value",
    "sort_keys=True",
    'raise KeyError("missing")',
    "PWD=$(pwd)",
    'token = response["nextPageToken"]',
    'db_password = config.get("db_password")',
    'secret = os.environ.get("SECRET")',
    'return {"token": resp["token"]}',
    "kubectl get pods -n default",
    "1721.1.1.1",
    "step3: do it",
]


def _selftest_scan(sample):
    return [
        (why, redact(hit.group(0), mask), mask)
        for pattern, why, mask in SENSITIVE
        for hit in re.finditer(pattern, sample)
    ]


selftest_positive_missed = 0
selftest_negative_flagged = 0
selftest_redaction_ok = True
for sample in SELFTEST_POSITIVE:
    hits = _selftest_scan(sample)
    if not hits:
        selftest_positive_missed += 1
        fail(f"sensitive selftest: positive sample not caught: {sample!r}")
    for why, echo, mask in hits:
        if mask and "<redacted" not in echo:
            selftest_redaction_ok = False
            fail(f"sensitive selftest: credential echo not redacted: {why}: {echo!r}")
for sample in SELFTEST_NEGATIVE:
    hits = _selftest_scan(sample)
    if hits:
        selftest_negative_flagged += 1
        fail(f"sensitive selftest: negative sample flagged: {sample!r} -> {hits[0][:2]}")

# content anchors (skill-specific): the four GC 判据 must stay traceable in the docs,
# so a future edit that drops one of them without updating the case fails here.
CONTENT_ANCHORS = ["live", "next-GC goal", "GOGC", "STW", "GOMEMLIMIT"]
_doc_blob = text + "\n".join(
    p.read_text(encoding="utf-8").replace("\r\n", "\n") for p in sorted(REFS_DIR.glob("*.md"))
)
content_anchors_checked = 0
for anchor in CONTENT_ANCHORS:
    if anchor in _doc_blob:
        content_anchors_checked += 1
    else:
        fail(f"content anchor missing from SKILL.md/references: {anchor!r}")

RESULTS.parent.mkdir(parents=True, exist_ok=True)
result = {
    "success": not failures,
    "failures": len(failures),
    "failure_details": failures,
    "links_checked": links_checked,
    "scripts_checked": scripts_checked,
    "sensitive_findings": len(sensitive_findings),
    "sensitive_details": sensitive_findings,
    "sensitive_selftest_positive_total": len(SELFTEST_POSITIVE),
    "sensitive_selftest_positive_missed": selftest_positive_missed,
    "sensitive_selftest_negative_total": len(SELFTEST_NEGATIVE),
    "sensitive_selftest_negative_flagged": selftest_negative_flagged,
    "sensitive_selftest_redaction_ok": selftest_redaction_ok,
    "content_anchors_checked": content_anchors_checked,
}
RESULTS.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(result, indent=2, ensure_ascii=False))
sys.exit(0 if not failures else 1)