#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Behavioural regression checks for analyze_tpsplit_report.py + synth_samples.py.

Run: python3 evals/run_checks.py   (from the skill root)
Covers the three scenarios (clean / quality-trap / tables-only), classification
thresholds, data-quality flags, degraded mode, error paths, warmup cut,
determinism, report structure, evidence-chain traceability and HTML hygiene.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANALYZE = os.path.join(ROOT, "scripts", "analyze_tpsplit_report.py")
SYNTH = os.path.join(ROOT, "scripts", "synth_samples.py")
SECTIONS = ["## 1. 结论", "## 2. 数据与运行态", "## 3. TTFT 总览对比",
            "## 4. 分阶段拆解对比", "## 5. 收益点与证据链", "## 6. 风险与回退点",
            "## 7. 分输入长度分层对比", "## 8. 假设与未测项"]
CITE_RE = re.compile(r"`(p_engine\.log|proxy\.log):(\d+)` (before|after) (req-\d+)")

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  | " + detail) if detail else ""))


def run_py(script, args):
    return subprocess.run([sys.executable, script] + args,
                          capture_output=True, text=True)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    tmp = tempfile.mkdtemp(prefix="tpsplit-report-checks-")
    try:
        # --- 生成三套场景数据（clean 用较大样本稳定 input_len 分布） ---
        for sc, n in (("clean", 1000), ("quality-trap", 400), ("tables-only", 400)):
            r = run_py(SYNTH, ["--out", os.path.join(tmp, sc), "--scenario", sc,
                               "--num-prompts", str(n)])
            check("synth %s 生成" % sc, r.returncode == 0, r.stderr.strip()[:120])

        def analyse(sc, out="gain", extra=None):
            out_prefix = os.path.join(tmp, sc, out)
            r = run_py(ANALYZE, [
                "--before", os.path.join(tmp, sc, "before_tp32ray"),
                "--after", os.path.join(tmp, sc, "after_tp8dp"),
                "--out", out_prefix] + (extra or []))
            data = None
            jpath = out_prefix + ".json"
            if r.returncode == 0 and os.path.isfile(jpath):
                data = load_json(jpath)
            return r, data, out_prefix

        # --- clean：分类、贡献度、质量全绿 ---
        r, d, prefix = analyse("clean")
        md = open(prefix + ".md", encoding="utf-8").read()
        html = open(prefix + ".html", encoding="utf-8").read()
        check("clean 退出码 0", r.returncode == 0, r.stderr.strip()[:120])
        kinds = {p["stage"]: p["kind"] for p in (d or {}).get("points", [])}
        check("sched_qwait/fwd_pure 判收益", kinds.get("sched_qwait") == "benefit"
              and kinds.get("fwd_pure") == "benefit", json.dumps(kinds))
        check("net_lb/kv_xfer 判回退", kinds.get("net_lb") == "regression"
              and kinds.get("kv_xfer") == "regression", json.dumps(kinds))
        qwait = [p for p in d["points"] if p["stage"] == "sched_qwait"][0]
        check("sched_qwait 贡献度 ~98%", abs(qwait["contrib_pct"] - 98.0) < 2.0,
              "%.1f%%" % qwait["contrib_pct"])
        ratio = d["after"]["stats"]["ttft"]["mean"] / d["before"]["stats"]["ttft"]["mean"]
        check("TTFT mean 降 55%~75%", 0.25 < ratio < 0.45,
              "after/before=%.2f" % ratio)
        check("clean has_logs=True", d.get("has_logs") is True)
        check("clean 无混杂信号", d.get("confound_l1", 1) < 0.15,
              "L1=%.3f" % d.get("confound_l1", -1))
        check("守恒违例 0%", all(q["conservation_pct"] == 0 for q in d["quality"]))
        for sec in SECTIONS:
            check("报告含节「%s」" % sec.split(". ")[1], sec in md)
        check("HTML 自包含（无外部 http 资源）",
              "<svg" in html and not re.search(r"(src|href)='https?://", html))
        check("HTML 无 Δ%% 字面量（表头格式化残留）", "Δ%%" not in html)
        check("JSON 无 NaN 字面量", "NaN" not in open(prefix + ".json",
                                                      encoding="utf-8").read())
        # --- SVG 图表几何：所有条形落在 viewBox 内、比例尺拉开 ---
        overflow, max_w = [], 0
        for m in re.finditer(r"viewBox='0 0 (\d+) (\d+)'", html):
            W, H = int(m.group(1)), int(m.group(2))
            seg = html[m.end():html.index("</svg>", m.end())]
            for r in re.finditer(r"<rect x='(\d+)' y='(\d+)' width='(\d+)' height='(\d+)'",
                                 seg):
                x, y, w, ht = map(int, r.groups())
                max_w = max(max_w, w)
                if x < 0 or y < 0 or x + w > W or y + ht > H:
                    overflow.append((x, y, w, ht))
        check("SVG 条形无越界", not overflow, str(overflow[:3]))
        check("SVG 比例尺拉开（最长条 ≥ 380px）", max_w >= 380, "max=%dpx" % max_w)
        check("SVG 显式宽高（防老内核 150px 压扁）",
              re.search(r"<svg(?![^>]*width=)[^>]*>", html) is None,
              "存在缺 width/height 属性的 svg")
        n_cards = html.count("<div class='card'>")
        check("总览卡片等宽 grid 且结构统一（4 张，均含 k/v/sub）",
              "<div class='cards'>" in html and n_cards == 4
              and html.count("<div class='sub'>") >= n_cards,
              "cards=%d" % n_cards)

        # --- 证据链可回溯：抽引用行号回查原始日志 ---
        cites = CITE_RE.findall(md)
        check("报告含 file:line 引用", len(cites) >= 4, "%d 处" % len(cites))
        side_dir = {"before": "before_tp32ray", "after": "after_tp8dp"}
        traced = 0
        for fname, lineno, side, req in cites[:6]:
            logp = os.path.join(tmp, "clean", side_dir[side], fname)
            with open(logp, encoding="utf-8") as f:
                lines = f.readlines()
            if int(lineno) <= len(lines) and req in lines[int(lineno) - 1]:
                traced += 1
        check("抽 6 条引用回查原始日志全部命中", traced == min(6, len(cites)),
              "%d/%d" % (traced, min(6, len(cites))))

        # --- quality-trap：告警与分层 ---
        r, d, prefix = analyse("quality-trap")
        check("trap 退出码 0", r.returncode == 0)
        aq = d["quality"][1]
        check("after 守恒违例 ~8% 被标记", 5 < aq["conservation_pct"] < 12,
              "%.1f%%" % aq["conservation_pct"])
        check("input_len 混杂被标记", d.get("confound_l1", 0) > 0.15,
              "L1=%.3f" % d.get("confound_l1", -1))
        check("after 样本明显少于 before",
              aq["n"] < d["quality"][0]["n"],
              "%d vs %d" % (aq["n"], d["quality"][0]["n"]))
        mdt = open(prefix + ".md", encoding="utf-8").read()
        check("trap 报告明示混杂与分层结论为准",
              "分布不一致" in mdt and "分层结论为准" in mdt)

        # --- tables-only：降级模式 ---
        r, d, prefix = analyse("tables-only")
        mdn = open(prefix + ".md", encoding="utf-8").read()
        check("tables-only 退出码 0 且 has_logs=False",
              r.returncode == 0 and d.get("has_logs") is False)
        check("降级声明（止步表格层）", "止步于表格层" in mdn
              or "未提供原始日志" in mdn)
        check("无日志时不编造 file:line 引用", not CITE_RE.search(mdn))
        check("无日志仍给出收益结论",
              any(p["stage"] == "sched_qwait" and p["kind"] == "benefit"
                  for p in d["points"]))

        # --- 自定义 --title 生效 ---
        r, d, prefix = analyse("clean", "g_title", ["--title", "TP8 拆分收益（评审版）"])
        mdt = open(prefix + ".md", encoding="utf-8").read()
        htt = open(prefix + ".html", encoding="utf-8").read()
        check("--title 写入 md H1 与 html <title>",
              "# TP8 拆分收益（评审版）" in mdt
              and "<title>TP8 拆分收益（评审版）</title>" in htt)

        # --- 缺失阶段列：降级为缺数据并三格标注 ---
        miss = os.path.join(tmp, "misscol")
        for side in ("before_tp32ray", "after_tp8dp"):
            os.makedirs(os.path.join(miss, side), exist_ok=True)
            src = open(os.path.join(tmp, "clean", side, "ttft_table.csv"),
                       encoding="utf-8").read().splitlines()
            out = []
            for i, line in enumerate(src):
                cols = line.split(",")
                if i == 0 and side == "after_tp8dp":
                    cols = cols[:10] + cols[11:]   # 表头去掉 draft_ms
                elif side == "after_tp8dp":
                    cols = cols[:10] + cols[11:]   # 数据行去掉 draft 值
                out.append(",".join(cols))
            open(os.path.join(miss, side, "ttft_table.csv"), "w",
                 encoding="utf-8").write("\n".join(out) + "\n")
        r = run_py(ANALYZE, ["--before", os.path.join(miss, "before_tp32ray"),
                             "--after", os.path.join(miss, "after_tp8dp"),
                             "--out", os.path.join(miss, "g")])
        mdm = open(os.path.join(miss, "g.md"), encoding="utf-8").read() \
            if r.returncode == 0 else ""
        djm = load_json(os.path.join(miss, "g.json")) if r.returncode == 0 else {}
        aqm = (djm.get("quality") or [{}, {}])[1]
        check("缺 draft 列：完成分析且该阶段判缺数据",
              r.returncode == 0 and "缺失阶段列" in mdm
              and "| — | draft |" in mdm and "缺数据" in mdm,
              r.stderr.strip()[:80])
        check("缺列行不计入守恒校验（不误报 100% 违例）",
              aqm.get("conservation_checked") == 0
              and aqm.get("conservation_pct") == 0.0,
              "checked=%s pct=%s" % (aqm.get("conservation_checked"),
                                     aqm.get("conservation_pct")))

        # --- 错误路径 ---
        bad1 = os.path.join(tmp, "bad1")
        os.makedirs(os.path.join(bad1, "before_tp32ray"), exist_ok=True)
        os.makedirs(os.path.join(bad1, "after_tp8dp"), exist_ok=True)
        for side in ("before_tp32ray", "after_tp8dp"):
            src = open(os.path.join(tmp, "clean", side, "ttft_table.csv"),
                       encoding="utf-8").readlines()
            src[0] = src[0].replace("ttft_ms", "ttft2_ms")
            open(os.path.join(bad1, side, "ttft_table.csv"), "w",
                encoding="utf-8").write("".join(src))
        r = run_py(ANALYZE, ["--before", os.path.join(bad1, "before_tp32ray"),
                             "--after", os.path.join(bad1, "after_tp8dp"),
                             "--out", os.path.join(bad1, "g")])
        check("缺 ttft_ms 列报错退出", r.returncode != 0 and "ttft_ms" in r.stderr)
        empty = os.path.join(tmp, "empty")
        for side in ("before_tp32ray", "after_tp8dp"):
            os.makedirs(os.path.join(empty, side), exist_ok=True)
            open(os.path.join(empty, side, "ttft_table.csv"), "w",
                 encoding="utf-8").write("ts,request_id,input_len,ttft_ms,sched_qwait_ms\n")
        r = run_py(ANALYZE, ["--before", os.path.join(empty, "before_tp32ray"),
                             "--after", os.path.join(empty, "after_tp8dp"),
                             "--out", os.path.join(empty, "g")])
        check("空表报错退出", r.returncode != 0 and "表格为空" in r.stderr)
        r = run_py(ANALYZE, ["--before", os.path.join(tmp, "nope"),
                             "--after", os.path.join(tmp, "clean", "after_tp8dp"),
                             "--out", os.path.join(tmp, "g")])
        check("目录不存在报错退出", r.returncode != 0 and "数据目录不存在" in r.stderr)

        # --- warmup 掐头（clean 数据 N=1000，到齐约 286s，掐 60s 应减少样本） ---
        r, d0, _ = analyse("clean", "g_w0")
        r, d1, _ = analyse("clean", "g_w1", ["--warmup-seconds", "60"])
        check("warmup 掐头减少样本",
              d1 and d1["after"]["n"] < d0["after"]["n"] and d1["before"]["n"] > 0,
              "%d -> %d" % (d0["after"]["n"], (d1 or {}).get("after", {}).get("n", -1)))

        # --- 确定性：同种子两次合成均值一致 ---
        run_py(SYNTH, ["--out", os.path.join(tmp, "det1"), "--num-prompts", "200"])
        run_py(SYNTH, ["--out", os.path.join(tmp, "det2"), "--num-prompts", "200"])

        def mean_ttft(base):
            import csv as _csv
            rows = list(_csv.DictReader(open(os.path.join(base, "after_tp8dp",
                                                          "ttft_table.csv"),
                                             encoding="utf-8")))
            return sum(float(x["ttft_ms"]) for x in rows) / len(rows)
        m1, m2 = mean_ttft(os.path.join(tmp, "det1")), mean_ttft(os.path.join(tmp, "det2"))
        check("同种子合成可复现", abs(m1 - m2) < 1e-6, "%.6f vs %.6f" % (m1, m2))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("%d/%d checks passed" % (passed, total))
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
