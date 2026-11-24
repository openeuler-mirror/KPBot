# -*- coding: utf-8 -*-
"""kptools-dplb-profiler 回采端单元测试（extract_metric_prom.py 纯函数）

覆盖：幽灵 tag 合并（process_start 无 engine 标签）、job 模糊建议（改名残留）、
负 queue 修正（返回修正时间集合）、health_check（fnum/未导出列剔除/process_start 一致性）、
build_rows（queue_fixed 标记列/可读时间戳）。
"""
import contextlib
import importlib
import io
import os
import sys

sys.dont_write_bytecode = True  # 不在 scripts/ 下生成 __pycache__，保持工作区干净
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(TEST_DIR, ".."))
sys.path.insert(0, SCRIPTS)
prom = importlib.import_module("extract_metric_prom")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + " - " + name + ((" | " + str(detail)) if not cond else ""))


# ---------- merge_ghost_engine_tags ----------
engines = {
    "10.0.0.1:9100": {"process_start_time_seconds": {100: 1793797588.0, 110: 1793797588.0}},
    "10.0.0.1:9100.0": {"request_success_total": {100: 10, 110: 20}},
    "10.0.0.1:9101": {"request_success_total": {100: 5, 110: 7},
                      "process_start_time_seconds": {100: 1793797589.0}},
}
merged = prom.merge_ghost_engine_tags(engines)
check("ghost: 检出 1 个幽灵 tag", len(merged) == 1)
check("ghost: 合并目标为 .0 后缀", merged and merged[0][0] == "10.0.0.1:9100" and merged[0][1] == ["10.0.0.1:9100.0"])
check("ghost: 幽灵 tag 已删除", "10.0.0.1:9100" not in engines)
check("ghost: process_start 并入真实 tag", engines["10.0.0.1:9100.0"]["process_start_time_seconds"][100] == 1793797588.0)
check("ghost: 引擎数不翻倍（2 个）", len(engines) == 2)
# 无幽灵时不动作
engines2 = {"a:1": {"request_success_total": {1: 2}}}
check("ghost: 无幽灵时原样返回", prom.merge_ghost_engine_tags(engines2) == [])

# ---------- suggest_jobs ----------
live = ["glm53_0_prefill_0", "glm53_0_prefill_1", "glm53_1_prefill_0", "glm53_1_decode_0",
        "glm53_2_prefill_0", "dsv4_1_1_flash_decode_0"]
sug = prom.suggest_jobs("glm53_prefill_.*", live)
check("suggest: 提示改名后的前缀", sug == ["glm53_0_prefill_0", "glm53_0_prefill_1", "glm53_1_prefill_0", "glm53_2_prefill_0"], sug)
sug2 = prom.suggest_jobs("foo_bar", live)
check("suggest: 无相近时为空", sug2 == [])

# ---------- fix_negative_hist 新签名 + build_rows 标记 ----------
eng = {
    "e1": {"queue_sum": {1: -10.0, 2: 3.0}, "prefill_sum": {1: 20.0, 2: 5.0}},
    "e2": {"queue_sum": {1: 4.0}, "prefill_sum": {1: 6.0}},
}
fixed, corrected = prom.fix_negative_hist(eng)
check("fixneg: 返回清单+修正时间", fixed and corrected.get("e1") == {1})
check("fixneg: queue 归零", eng["e1"]["queue_sum"][1] == 0.0)
check("fixneg: prefill 吸收", abs(eng["e1"]["prefill_sum"][1] - 10.0) < 1e-9)
check("fixneg: 正常引擎不动", eng["e2"]["queue_sum"][1] == 4.0)

# ---------- health_check：fnum 可用 + 未导出列剔除 + process_start 一致性 ----------
eng_hc = {
    "a:1": {"process_start_time_seconds": {1: 1793797588.0}, "request_success_total": {1: 5}},
    "a:2": {"process_start_time_seconds": {1: 1793797588.0}, "request_success_total": {1: 7}},
}
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    prom.health_check(["a:1", "a:2"], eng_hc, [1], [], 10)
out_hc = buf.getvalue()
check("health_check: fnum 不再 NameError", "process_start 一致" in out_hc, out_hc[-200:])
check("health_check: 未导出列剔除提示", "未导出" in out_hc)
check("health_check: 缺失率分母排除未导出列（25 列中仅 2 列在采）", "数据列: 2" in out_hc,
      [l for l in out_hc.splitlines() if "数据列" in l])

# build_rows 末列 queue_fixed
roweng = {}
allcols = [c[0] for c in prom.BASE_COLS] + [h[0] + s for h in prom.HISTS for s in ("_sum", "_count")]
for e in ("e1", "e2"):
    roweng[e] = {}
    for c in allcols:
        roweng[e][c] = {1: 1.0}
order, rows = prom.build_rows(roweng, [1, 2], 8 * 3600, {"e1": {1}})
check("buildrows: 列数 = header 长度", all(len(r) == len(prom.CSV_HEADER) for r in rows))
check("buildrows: header 含 queue_fixed", prom.CSV_HEADER[-1] == "queue_fixed")
r_e1_t1 = next(r for r in rows if r[1] == "e1" and r[0].endswith("08:00:01"))
r_e1_t2 = next(r for r in rows if r[1] == "e1" and r[0].endswith("08:00:02"))
r_e2_t1 = next(r for r in rows if r[1] == "e2" and r[0].endswith("08:00:01"))
check("buildrows: 被修正点标记 1", r_e1_t1[-1] == "1")
check("buildrows: 未修正点标记 0", r_e1_t2[-1] == "0")
check("buildrows: 未修正引擎标记 0", r_e2_t1[-1] == "0")
check("buildrows: 时间戳可读格式", __import__("re").match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", r_e1_t1[0]) is not None, r_e1_t1[0])

# ---------- --model 与 --job/--match 组合：selector 无双逗号 ----------
class _CapClient(object):
    def __init__(self):
        self.queries = []

    def query_range(self, q, s, e, st):
        self.queries.append(q)
        return {}


cap = _CapClient()
try:
    prom.find_model_jobs(cap, "glm53-2", 'job=~"glm53_2_.*"', 1, 2, 1, "service_role")
except SystemExit:
    pass  # 空结果集会 SystemExit，但 promql 已被捕获
check("model+job: selector 无双逗号", cap.queries and ",," not in cap.queries[0]
      and 'model_name="glm53-2",job=~"glm53_2_.*"' in cap.queries[0], cap.queries[:1])

# ---------- --model 输出文件名前缀去重 ----------
check("fname: role==pref 去重",
      prom.model_out_fname("glm53_2_decode", "glm53_2_decode", "20261119") ==
      "dp_load_metrics_glm53_2_decode_20261119.csv")
check("fname: role 以 pref 为前缀时去重",
      prom.model_out_fname("glm53_2", "glm53_2_decode", "20261119") ==
      "dp_load_metrics_glm53_2_decode_20261119.csv")
check("fname: role 与 pref 无重叠时保留两者",
      prom.model_out_fname("glm53", "dsv4_role", "20261119") ==
      "dp_load_metrics_glm53_dsv4_role_20261119.csv")
check("fname: all 角色用 pref",
      prom.model_out_fname("glm53_2", "all", "20261119") ==
      "dp_load_metrics_glm53_2_20261119.csv")

# ---------- 输出文件名消毒（远端 job 标签值含 / 或 .. 不再拼成路径） ----------
check("fname: job 名含 / 被消毒为 _（无路径穿越）",
      prom.model_out_fname("../evil", "all", "t") == "dp_load_metrics_.._evil_t.csv"
      and "/" not in prom.model_out_fname("a/b/c", "all", "t"),
      prom.model_out_fname("../evil", "all", "t"))
check("fname: 去重逻辑在消毒后仍成立",
      prom.model_out_fname("glm53_2", "glm53_2_decode", "20261119") ==
      "dp_load_metrics_glm53_2_decode_20261119.csv")
check("fname: safe_fname 空值/特殊字符",
      prom.safe_fname("") == "" and prom.safe_fname("a b;c") == "a_b_c")

# ---------- --start 缺省处理（评审修复回归：--list-jobs 可省略 --start） ----------
import argparse
import subprocess
import tempfile

PROM = os.path.join(SCRIPTS, "extract_metric_prom.py")

check("start: list-jobs 缺省为 now-1h", prom.resolve_start(
    argparse.Namespace(start=None, list_jobs=True)) == "now-1h")
check("start: 显式 start 原样返回", prom.resolve_start(
    argparse.Namespace(start="30m", list_jobs=False)) == "30m")
try:
    prom.resolve_start(argparse.Namespace(start=None, list_jobs=False))
    check("start: 非 list-jobs 缺 --start 应报错", False)
except SystemExit as e:
    check("start: 非 list-jobs 缺 --start 应报错", "--start 为必填" in str(e), str(e))
# CLI 冒烟：缺 --start 且非 --list-jobs 时，在发起任何网络请求前退出（不再被 argparse 拦死）
r = subprocess.run([sys.executable, PROM, "--prom-url", "http://127.0.0.1:9", "--job", "x"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
check("cli: 缺 --start 非零退出并指引", r.returncode != 0 and "--start 为必填" in (r.stdout + r.stderr),
      (r.stdout + r.stderr)[-120:])

# ---------- Grafana 凭证入口与 URL 校验（评审修复回归） ----------
def _ns(**kw):
    base = dict(prom_url=None, grafana_url=None, user=None, password=None,
                password_file=None, datasource_uid=None, allow_insecure_http=False)
    base.update(kw)
    return argparse.Namespace(**base)

os.environ["GRAFANA_PASSWORD"] = "env-secret"
check("pwd: 环境变量入口", prom.resolve_grafana_password(_ns()) == "env-secret")
check("pwd: --password 优先", prom.resolve_grafana_password(_ns(password="argv-secret")) == "argv-secret")
_fd, pfile = tempfile.mkstemp(suffix=".txt")
with os.fdopen(_fd, "w", encoding="utf-8") as f:
    f.write("  file-secret  \n")
try:
    check("pwd: 密码文件入口并去两端空白", prom.resolve_grafana_password(_ns(password_file=pfile)) == "file-secret")
    try:
        prom.resolve_grafana_password(_ns(password_file=TEST_DIR))
        check("pwd: 密码文件不可读时报错", False)
    except SystemExit as e:
        check("pwd: 密码文件不可读时报错", "--password-file" in str(e), str(e))
finally:
    os.remove(pfile)
os.environ.pop("GRAFANA_PASSWORD", None)
check("pwd: 无任何来源时返回空串", prom.resolve_grafana_password(_ns()) == "")

# prom 与 grafana 互斥（防止凭证据发往另一端点）
try:
    prom.Client(_ns(prom_url="http://p:9090", grafana_url="https://g:3000", user="u", datasource_uid="d"))
    check("client: prom+grafana 互斥报错", False)
except SystemExit as e:
    check("client: prom+grafana 互斥报错", "互斥" in str(e), str(e))
# http:// Grafana + 凭证默认拒绝（联网前退出）
try:
    prom.Client(_ns(grafana_url="http://g:3000", user="u", datasource_uid="d", password="x"))
    check("client: http+凭证默认拒绝", False)
except SystemExit as e:
    check("client: http+凭证默认拒绝", "拒绝向 http" in str(e), str(e))
# 非 http(s) scheme 拒绝
try:
    prom.Client(_ns(grafana_url="ftp://g:3000", user="u", datasource_uid="d"))
    check("client: 非 http(s) scheme 拒绝", False)
except SystemExit as e:
    check("client: 非 http(s) scheme 拒绝", "非法" in str(e), str(e))
# https 正常构造：凭证可来自环境变量；err_label 脱敏（不含 datasource uid）
import base64 as _b64
os.environ["GRAFANA_PASSWORD"] = "s"
c_https = prom.Client(_ns(grafana_url="https://g:3000", user="u", datasource_uid="secret-uid"))
check("client: https 构造成功（env 密码入 Basic 头）",
      c_https.auth == "Basic " + _b64.b64encode(b"u:s").decode())
check("client: err_label 脱敏不含 uid",
      c_https.err_label == "https://g:3000/api/v1" and "secret-uid" not in c_https.err_label)
os.environ.pop("GRAFANA_PASSWORD", None)
# --allow-insecure-http 显式放行 http（仅构造，不联网）
c_http = prom.Client(_ns(grafana_url="http://g:3000", user="u", datasource_uid="d",
                         allow_insecure_http=True, password="x"))
check("client: --allow-insecure-http 放行", c_http.auth is not None and c_http.err_label == "http://g:3000/api/v1")
# prom 模式 err_label（失败时的错误信息不带查询参数）
c_prom = prom.Client(_ns(prom_url="http://p:9090"))
check("client: prom err_label 为脱敏端点", c_prom.err_label == "http://p:9090/api/v1")
# CLI 冒烟：http grafana 在联网前被拒
r = subprocess.run([sys.executable, PROM, "--grafana-url", "http://g:3000", "--user", "u",
                    "--datasource-uid", "d", "--start", "now-1h"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
check("cli: http grafana 被拒", r.returncode != 0 and "拒绝向 http" in (r.stdout + r.stderr))

n = sum(1 for _, ok in results if ok)
print("\n== 回采单测结果: %d/%d ==" % (n, len(results)))
sys.exit(0 if n == len(results) else 1)
