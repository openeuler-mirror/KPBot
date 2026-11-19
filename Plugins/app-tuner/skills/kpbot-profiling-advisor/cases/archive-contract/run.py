#!/usr/bin/env python3
"""run.py — tier A：archive_report.py 契约用例（kpbot-profiling-advisor）

覆盖：ZIP 归档产出、markdown 链接重写为随包附件、sha256 清单、
重名去重、Windows 保留名、--attach 附件清单回写、错误路径。
运行（技能根目录）: python3 cases/archive-contract/run.py
"""
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

CASE_DIR = Path(__file__).resolve().parent
SCRIPT = CASE_DIR / "../../scripts/archive_report.py"
WORK = Path(tempfile.mkdtemp(prefix="ar-ut-"))

passed = failed = skipped = 0


def check(cond, name, actual=""):
    global passed, failed
    if cond:
        print(f"\033[32m  [PASS] {name}\033[0m")
        passed += 1
    else:
        print(f"\033[31m  [FAIL] {name} (实际: {actual})\033[0m")
        failed += 1


def run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        capture_output=True, text=True,
    )


print("===== archive_report 契约用例 =====")

print("AR-01: 基础归档——报告 + 图片引用重写 + SHA256 清单")
src = WORK / "src"; src.mkdir()
(src / "pic.png").write_bytes(b"\x89PNG-fake")
(src / "ref.md").write_text("# ref\n")
(src / "report.md").write_text("# 报告\n\n![数据](pic.png)\n\n[详见](ref.md)\n")
out = WORK / "r1.zip"
r = run("--report", src / "report.md", "--output", out, "--attach", src / "pic.png")
check(r.returncode == 0, "AR-01: rc=0", r.stderr)
with zipfile.ZipFile(out) as z:
    names = z.namelist()
    check("SHA256SUMS.txt" in names, "AR-01: 含 SHA256SUMS.txt")
    check("report.md" in names, "AR-01: 含报告")
    body = z.read("report.md").decode()
    check("](<pic.png>)" in body or "](pic.png)" in body, "AR-01: 图片链接重写为随包名", body)
    sums = z.read("SHA256SUMS.txt").decode()
    check(len(sums.strip().splitlines()) >= 1, "AR-01: sha256 行存在")

print("AR-02: 重名去重——不同目录的两个同名附件")
d2a = WORK / "d2a"; d2b = WORK / "d2b"; d2a.mkdir(); d2b.mkdir()
(d2a / "a.txt").write_text("1"); (d2b / "a.txt").write_text("2")
(d2a / "m.md").write_text("x\n")
r = run("--report", d2a / "m.md", "--output", WORK / "r2.zip", "--attach", d2a / "a.txt", "--attach", d2b / "a.txt")
with zipfile.ZipFile(WORK / "r2.zip") as z:
    names = z.namelist()
    check("a.txt" in names and "a-2.txt" in names, "AR-02: 重名自动加 -2 后缀", names)

print("AR-03: Windows 保留名")
d3 = WORK / "d3"; d3.mkdir()
(d3 / "CON.md").write_text("c\n")
r = run("--report", d3 / "CON.md", "--output", WORK / "r3.zip")
with zipfile.ZipFile(WORK / "r3.zip") as z:
    check("_CON.md" in z.namelist(), "AR-03: CON → _CON 前缀", z.namelist())

print("AR-04: 输出非 .zip → exit 1")
r = run("--report", d3 / "CON.md", "--output", WORK / "out.txt")
check(r.returncode == 1, "AR-04: 非 zip 后缀拒绝", r.returncode)

print("AR-05: 引用缺失文件 → exit 1")
d5 = WORK / "d5"; d5.mkdir()
(d5 / "m.md").write_text("[x](missing.txt)\n")
r = run("--report", d5 / "m.md", "--output", WORK / "r5.zip")
check(r.returncode == 1, "AR-05: 引用无法归档 → exit 1", r.returncode)

print("AR-06: --attach 附件清单回写报告")
d6 = WORK / "d6"; d6.mkdir()
(d6 / "m.md").write_text("# t\n")
(d6 / "att.csv").write_text("a,b\n")
r = run("--report", d6 / "m.md", "--output", WORK / "r6.zip", "--attach", d6 / "att.csv")
with zipfile.ZipFile(WORK / "r6.zip") as z:
    body = z.read("m.md").decode()
    check("随包附件" in body and "att.csv" in body, "AR-06: 报告尾部含附件清单", body)

print("AR-07: sha256 与成员内容一致")
import hashlib
with zipfile.ZipFile(WORK / "r6.zip") as z:
    digest = hashlib.sha256(z.read("att.csv")).hexdigest()
    sums = z.read("SHA256SUMS.txt").decode()
    check(digest in sums, "AR-07: 摘要与内容匹配")

print("""
============================================
  测试汇总
============================================""")
print(f"\033[32m  PASS: {passed}\033[0m")
print(f"\033[31m  FAIL: {failed}\033[0m")
print(f"\033[33m  SKIP: {skipped}\033[0m")
print(f"  总计: {passed + failed + skipped}")
print("============================================")

result_dir = CASE_DIR / "results"
result_dir.mkdir(exist_ok=True)
(result_dir / "result.json").write_text(json.dumps(
    {"success": failed == 0, "passed": passed, "checks_ran": passed + failed,
     "checks_failures": failed, "skipped": skipped},
    ensure_ascii=False, indent=2), encoding="utf-8")
sys.exit(0 if failed == 0 else 1)
