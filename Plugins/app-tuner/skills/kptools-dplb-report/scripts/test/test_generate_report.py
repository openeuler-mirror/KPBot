# -*- coding: utf-8 -*-
"""kptools-dplb-report 报告端回归测试（generate_report.py）

覆盖：时间戳容错、单池/对比报告关键要素（列导出说明/负queue修正/uptime警示/修正比例差异）、
硬失败（时间戳全坏/数据列全空）、主机解析（下划线与 .engine 后缀）、--print-schema、
评级逻辑（CV 工程经验分级/小样本护栏/短窗口范围说明）、新指标（失衡差/排队 LIF）、
评审修复（路径冲突防护/失效引擎剔除/乱序时间戳/窗口平均配对区间/方向措辞/零流量/证据不足/
引擎集合/胜场计数），自检修复（重复时间戳去重/N̄ 有效引擎口径/process_start 生命周期/
对比表保留增量缺失引擎），复核修复（硬链接 samefile/process_start 亚秒级精确比对/
集合不一致时平局判定一并暂停），以及对比版主机名转义（HTML 注入防护）、
frontmatter license/metadata、README/文档口径同步。
"""
import json
import os
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True  # 不在 scripts/ 下生成 __pycache__，保持工作区干净
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(TEST_DIR, ".."))
GEN = os.path.join(SCRIPTS, "generate_report.py")
TMP = tempfile.mkdtemp(prefix="lbprof_report_test_")

HEADER = ("timestamp,ip_port,num_requests_running,num_requests_waiting,num_requests_swapped,"
          "request_success_total,request_failure_total,num_preemptions_total,prompt_tokens_total,"
          "generation_tokens_total,request_validation_time_total,kv_cache_usage_perc,"
          "cpu_cache_usage_perc,prefix_cache_hit_rate,pending_prefix_cache_hit_rate,"
          "external_prefix_cache_hit_rate,process_start_time_seconds,ttft_avg,queue_avg,"
          "prefill_avg,tpot_avg,e2e_avg,ttft_sum,ttft_count,queue_sum,queue_count,prefill_sum,"
          "prefill_count,tpot_sum,tpot_count,e2e_sum,e2e_count,queue_fixed")


def make_v2_csv(path, ts_mode="readable", base_counter=1000, neg_queue_engines=(),
                absent_cols=("num_requests_swapped", "request_failure_total",
                             "request_validation_time_total", "cpu_cache_usage_perc",
                             "pending_prefix_cache_hit_rate"),
                process_start=1793797588.0, n_points=10, engines=None, fix_col=True):
    """构造 v2 CSV。engines: [(tag, share)]"""
    if engines is None:
        engines = [("10.0.0.1:9001", 1.0), ("10.0.0.1:9002", 0.5),
                   ("10.0.0.2:9001", 0.8), ("10.0.0.2:9002", 0.3)]
    rows = [HEADER]
    t0 = 1793900000
    for i in range(n_points):
        t = t0 + i * 10
        ts = ("2026-11-12 %02d:%02d:%02d" % (10 + i // 60, i % 60, (i * 7) % 60)) if ts_mode == "readable" else str(t)
        for tag, share in engines:
            succ = base_counter + int(i * 100 * share)
            prompt = succ * 120
            gen = succ * 60
            ttft_s = 50.0 + 10 * i * share
            ttft_c = succ
            q_s = -55.5 if tag in neg_queue_engines else 2.0 * share + i * 0.1
            q_c = succ
            p_s = (80.0 + ttft_s) if tag in neg_queue_engines else 30.0 * share + i * 0.2
            p_c = succ
            tp_s = 29.6 + i
            tp_c = succ * 8
            e2e_s = ttft_s + p_s
            e2e_c = succ

            def V(col):
                if col in absent_cols:
                    return "-1"
                return {
                    "num_requests_running": "3",
                    "num_requests_waiting": "1" if i % 3 == 0 else "0",
                    "request_success_total": str(succ),
                    "num_preemptions_total": "0",
                    "prompt_tokens_total": str(prompt),
                    "generation_tokens_total": str(gen),
                    "kv_cache_usage_perc": "0.42",
                    "prefix_cache_hit_rate": "0.85",
                    "external_prefix_cache_hit_rate": "1.0",
                    "process_start_time_seconds": str(process_start),
                    "ttft_avg": "%.3f" % (ttft_s / ttft_c),
                    "queue_avg": "%.3f" % (q_s / q_c),
                    "prefill_avg": "%.3f" % (p_s / p_c),
                    "tpot_avg": "%.4f" % (tp_s / tp_c),
                    "e2e_avg": "%.3f" % (e2e_s / e2e_c),
                    "ttft_sum": "%.1f" % ttft_s,
                    "ttft_count": str(ttft_c),
                    "queue_sum": "%.1f" % q_s,
                    "queue_count": str(q_c),
                    "prefill_sum": "%.1f" % p_s,
                    "prefill_count": str(p_c),
                    "tpot_sum": "%.1f" % tp_s,
                    "tpot_count": str(tp_c),
                    "e2e_sum": "%.1f" % e2e_s,
                    "e2e_count": str(e2e_c),
                }[col]
            vals = [ts, tag] + [V("num_requests_running"), V("num_requests_waiting"), V("num_requests_swapped"),
                                V("request_success_total"), V("request_failure_total"), V("num_preemptions_total"),
                                V("prompt_tokens_total"), V("generation_tokens_total"), V("request_validation_time_total"),
                                V("kv_cache_usage_perc"), V("cpu_cache_usage_perc"), V("prefix_cache_hit_rate"),
                                V("pending_prefix_cache_hit_rate"), V("external_prefix_cache_hit_rate"),
                                V("process_start_time_seconds"), V("ttft_avg"), V("queue_avg"), V("prefill_avg"),
                                V("tpot_avg"), V("e2e_avg"), V("ttft_sum"), V("ttft_count"), V("queue_sum"),
                                V("queue_count"), V("prefill_sum"), V("prefill_count"), V("tpot_sum"), V("tpot_count"),
                                V("e2e_sum"), V("e2e_count")]
            if fix_col:
                vals.append("1" if tag in neg_queue_engines else "0")
            rows.append(",".join(vals))
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")


results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL") + " - " + name + ((" | " + str(detail)) if detail and not cond else ""))


# ---------- 1. parse_ts 单元测试 ----------
sys.path.insert(0, SCRIPTS)
import importlib
gr = importlib.import_module("generate_report")
pt = gr.parse_ts
check("parse_ts epoch", pt("1793900000") == 1793900000)
check("parse_ts readable", pt("2026-11-12 10:00:00") is not None)
check("parse_ts iso-tz", pt("2026-11-12T10:00:00+08:00") is not None)
check("parse_ts iso", pt("2026-11-12T10:00:00") is not None)
check("parse_ts junk -> None", pt("not-a-time") is None)

# ---------- 2. 单池报告：可读时间戳 + queue_fixed + 列导出说明 ----------
a_csv = os.path.join(TMP, "dp_load_metrics_A_test.csv")
make_v2_csv(a_csv, ts_mode="readable", neg_queue_engines=("10.0.0.1:9001",))
out_html = os.path.join(TMP, "report_single.html")
r = subprocess.run([sys.executable, GEN, "--csv", a_csv, "--title", "回归-单池",
                    "-o", out_html, "--summary-out", os.path.join(TMP, "summary.json")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("single: exit 0", r.returncode == 0, r.stderr[-300:])
html = open(out_html, encoding="utf-8").read() if r.returncode == 0 else ""
check("single: 列导出说明脚注", "列导出说明" in html and "采集丢点" in html)
check("single: 负 queue 修正脚注", "负 queue 修正" in html and "1/4" in html)
check("single: 无 >30% 缺失误报崩溃", "存在缺失率" not in html)  # absent 列剔除后不应触发
check("single: 时间窗口可读", "2026-11-12" in html)
check("single: summary 含 queue_fixed", '"queue_fixed"' in open(os.path.join(TMP, "summary.json"), encoding="utf-8").read())

# ---------- 3. 对比报告：epoch + uptime 不一致 + 修正比例差异 ----------
b_csv = os.path.join(TMP, "dp_load_metrics_B_test.csv")
make_v2_csv(b_csv, ts_mode="epoch", base_counter=2000, process_start=1794576116.0,
            neg_queue_engines=("10.0.0.2:9001", "10.0.0.2:9002"))
out_cmp = os.path.join(TMP, "report_compare.html")
r = subprocess.run([sys.executable, GEN, "--csv", a_csv, b_csv, "--label-a", "A策略",
                    "--label-b", "B策略", "--title", "回归-对比", "-o", out_cmp],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("compare: exit 0", r.returncode == 0, r.stderr[-300:])
html2 = open(out_cmp, encoding="utf-8").read() if r.returncode == 0 else ""
check("compare: uptime 不一致警示", "两段 uptime 不一致" in html2)
check("compare: 修正比例差异警示", "被负 queue 修正的引擎比例差异较大" in html2)

# ---------- 4. 硬失败：时间戳全坏 ----------
bad_csv = os.path.join(TMP, "bad_ts.csv")
make_v2_csv(bad_csv, ts_mode="epoch")
with open(bad_csv, encoding="utf-8") as f:
    lines = f.read().splitlines()
with open(bad_csv, "w", encoding="utf-8") as f:
    f.write(lines[0] + "\n")
    for ln in lines[1:]:
        parts = ln.split(",")
        parts[0] = "garbage-time"
        f.write(",".join(parts) + "\n")
r = subprocess.run([sys.executable, GEN, "--csv", bad_csv, "-o", os.path.join(TMP, "bad.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("hardfail: 非零退出", r.returncode != 0)
check("hardfail: 提示含时间戳格式", "时间戳格式不支持" in (r.stdout + r.stderr))

# ---------- 5. 全数据列空 ----------
empty_csv = os.path.join(TMP, "all_empty.csv")
make_v2_csv(empty_csv, ts_mode="epoch",
            absent_cols=("num_requests_running", "num_requests_waiting", "num_requests_swapped",
                         "request_success_total", "request_failure_total", "num_preemptions_total",
                         "prompt_tokens_total", "generation_tokens_total", "request_validation_time_total",
                         "kv_cache_usage_perc", "cpu_cache_usage_perc", "prefix_cache_hit_rate",
                         "pending_prefix_cache_hit_rate", "external_prefix_cache_hit_rate",
                         "process_start_time_seconds", "ttft_avg", "queue_avg", "prefill_avg",
                         "tpot_avg", "e2e_avg", "ttft_sum", "ttft_count", "queue_sum", "queue_count",
                         "prefill_sum", "prefill_count", "tpot_sum", "tpot_count", "e2e_sum", "e2e_count"))
r = subprocess.run([sys.executable, GEN, "--csv", empty_csv, "-o", os.path.join(TMP, "empty.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("all-empty: 非零退出", r.returncode != 0)
check("all-empty: 提示数据列全空", "数据列均无有效值" in (r.stdout + r.stderr))

# ---------- 6. host_port 下划线兼容 ----------
us_csv = os.path.join(TMP, "underscore.csv")
make_v2_csv(us_csv, ts_mode="epoch",
            engines=[("10.0.0.1_9001", 1.0), ("10.0.0.1_9002", 0.6), ("10.0.0.2_9001", 0.9)])
r = subprocess.run([sys.executable, GEN, "--csv", us_csv, "-o", os.path.join(TMP, "us.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("underscore: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    h = open(os.path.join(TMP, "us.html"), encoding="utf-8").read()
    check("underscore: 主机分组正确(10.0.0.1 内 CV)", "10.0.0.1" in h)

# ---------- 7. --print-schema（免 -o） ----------
r = subprocess.run([sys.executable, GEN, "--csv", a_csv, "--print-schema"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("print-schema: exit 0", r.returncode == 0)
check("print-schema: 输出 engines/points", "engines:" in r.stdout and "points" in r.stdout)
# 无 -o 且非 print-schema -> 应非零退出并给出指引
r = subprocess.run([sys.executable, GEN, "--csv", a_csv],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("no-out: 非零退出且有指引", r.returncode != 0 and "--print-schema" in (r.stdout + r.stderr))

# ---------- 7b. ip:port.engine 主机解析 ----------
eng_csv = os.path.join(TMP, "dp_load_metrics_dotengine.csv")
make_v2_csv(eng_csv, ts_mode="epoch",
            engines=[("10.0.0.1:9100.0", 1.0), ("10.0.0.1:9100.1", 0.5),
                     ("10.0.0.2:9100.0", 0.8), ("10.0.0.2:9100.1", 0.3)])
r = subprocess.run([sys.executable, GEN, "--csv", eng_csv, "-o", os.path.join(TMP, "eng.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("dot-engine: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    he = open(os.path.join(TMP, "eng.html"), encoding="utf-8").read()
    check("dot-engine: 主机分组正确（2 台主机而非 4 台）", "10.0.0.1" in he and "10.0.0.2" in he)
    check("dot-engine: 主机内 CV 可计算（非 n/a）", "主机内端口" in he and "n/a" not in he.split("主机内")[1][:200])
# Engine 单元
for tag, exp_host, exp_port, exp_eng in (("10.0.0.1:9100.0", "10.0.0.1", "9100", "0"),
                                          ("10.0.0.1_9100.3", "10.0.0.1", "9100", "3"),
                                          ("10.0.0.1:9100", "10.0.0.1", "9100", "")):
    e = gr.Engine(tag)
    check("Engine parse %s" % tag, (e.host, e.port, e.engine) == (exp_host, exp_port, exp_eng),
          (e.host, e.port, e.engine))

# ---------- 7c. 新指标：失衡差 max-min / 排队 LIF ----------
check("metric: 失衡差 max-min 行", "失衡差 max-min" in html)
check("metric: 排队 LIF 卡片", "排队 LIF" in html)
# 工业对照（路由器内部参数）不应出现在报告中，只留在技能内部文档
check("metric: 报告不含工业对照细节", "affinity-load-factor" not in html and "sgl-router" not in html)
# 计分卡行名带指标定义悬停
check("scorecard: 行名带 title 定义", "title=" in html and "变异系数" in html)

# ---------- 8. 高缺失引擎分支不再崩溃（>30% 告警可渲染） ----------
hm_csv = os.path.join(TMP, "dp_load_metrics_highmiss.csv")
make_v2_csv(hm_csv, ts_mode="epoch",
            engines=[("10.0.0.1:9001", 1.0), ("10.0.0.1:9002", 0.5), ("10.0.0.2:9009", 0.7)])
# 把第三个引擎大部分列改成 -1（模拟高缺失）
with open(hm_csv, encoding="utf-8") as f:
    lines = f.read().splitlines()
out_lines = [lines[0]]
for ln in lines[1:]:
    p = ln.split(",")
    if p[1] == "10.0.0.2:9009":
        p = [p[0], p[1]] + ["-1"] * (len(p) - 2)
    out_lines.append(",".join(p))
with open(hm_csv, "w", encoding="utf-8") as f:
    f.write("\n".join(out_lines) + "\n")
r = subprocess.run([sys.executable, GEN, "--csv", hm_csv, "-o", os.path.join(TMP, "hm.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("high-miss: exit 0（不再 %-format 崩溃）", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    h = open(os.path.join(TMP, "hm.html"), encoding="utf-8").read()
    check("high-miss: 告警渲染", "存在缺失率" in h)

# ---------- 9. 评级逻辑：效应量固定档 + CV 工程经验分级 ----------
h = html if html else ""
check("scorecard: 噪声下限列已移除", "噪声下限" not in h and "CV÷噪声" not in h)
check("scorecard: 含失衡比 max/min 行", "失衡比 max/min" in h)
# a_csv CV=0.21（N̄≈383>200，护栏未激活）→ 请求分配 CV = 轻度倾斜
check("scorecard: 请求分配 CV 分级（轻度倾斜）", "轻度倾斜" in h)
check("scorecard: CV 分级说明含工程经验区间", "工程经验参考区间" in h)
check("scorecard: Token CV 仍为参考", "Token 负载 CV（参考）" in h)
check("scorecard: LIF 有评级（非参考）", "尾部失衡 LIF" in h)
check("大样本: 无样本护栏提示", "每引擎平均仅" not in h)

# ---------- 10. 小样本护栏：低请求数触发 ----------
ss_csv = os.path.join(TMP, "dp_load_metrics_smallsample.csv")
# 每引擎窗口增量只有 ~15 个请求 -> N̄ << 200
make_v2_csv(ss_csv, ts_mode="epoch", base_counter=1000, n_points=5,
            engines=[("10.0.0.1:9001", 1.0), ("10.0.0.2:9001", 0.6)])
with open(ss_csv, encoding="utf-8") as f:
    lines = f.read().splitlines()
out_lines = [lines[0]]
per_tag = {}
for ln in lines[1:]:
    p = ln.split(",")
    k = per_tag.get(p[1], 0)
    per_tag[p[1]] = k + 1
    p[5] = str(1000 + k * 3)  # request_success_total: 窗口增量仅 12/引擎
    out_lines.append(",".join(p))
with open(ss_csv, "w", encoding="utf-8") as f:
    f.write("\n".join(out_lines) + "\n")
r = subprocess.run([sys.executable, GEN, "--csv", ss_csv, "-o", os.path.join(TMP, "ss.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("small-sample: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    hss = open(os.path.join(TMP, "ss.html"), encoding="utf-8").read()
    check("small-sample: 护栏提示出现", "每引擎平均仅" in hss and "样本护栏" in hss)
    check("small-sample: 提及随机底噪 1/√N̄", "1/√N̄" in hss)
    check("small-sample: 提示效应量优先", "LIF/失衡比" in hss)
    check("small-sample: CV 分级仍展示但注明不参与判定", "只展示、不参与横幅判定" in hss)

# ---------- 11. 短窗口范围说明：短窗 + 判定非均衡时提示 ----------
# ss 数据两个引擎增量相同 -> 判定"基本均衡" -> 即使窗口只有 40s 也不应提示（反例）
check("short-window: 均衡短窗不提示（反例）", "窗口范围说明" not in hss)
# a_csv 窗口 90s 且数据有倾斜（shares 1.0/0.5/0.8/0.3）→ 应触发窗口范围说明
check("short-window: 倾斜短窗触发说明", "窗口范围说明" in html)
check("short-window: 说明时段性现象", "时段性现象" in html)
# 对比报告（两份均 40~90s 短窗）应有对比版窗口说明
check("short-window: 对比版说明", "两个采样窗口均较短" in html2)

# ---------- 12. 短窗口时长显示：10s 窗口应显示"10 秒"而非"0 分钟" ----------
f15_csv = os.path.join(TMP, "dp_load_metrics_10s.csv")
make_v2_csv(f15_csv, ts_mode="epoch", n_points=2,
            engines=[("10.0.0.1:9001", 1.0), ("10.0.0.1:9002", 0.4)])
r = subprocess.run([sys.executable, GEN, "--csv", f15_csv, "-o", os.path.join(TMP, "f15.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("dur-fmt: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    hf = open(os.path.join(TMP, "f15.html"), encoding="utf-8").read()
    check("dur-fmt: 10s 窗口显示\"10 秒\"", "10 秒" in hf and "0 分钟" not in hf)
    check("dur-fmt: 窗口范围说明已触发", "窗口范围说明" in hf)

# ---------- 13. md_to_html：标题独占一块不再吞掉前文（评审修复回归） ----------
md = gr.md_to_html("## H\n\n正文\n\n- a\n- b")
check("md: 标题独占块输出独立 <h3>", "<h3>H</h3>" in md)
check("md: 段落保留", "<p>正文</p>" in md)
check("md: 列表正常渲染", "<ul><li>a</li><li>b</li></ul>" in md)
check("md: 未把整体 HTML 塞进 <ul>", "<ul><h3>" not in md)
md2 = gr.md_to_html("前置段落\n\n## 只有标题")
check("md: 标题前段落不被吞", md2.startswith("<p>前置段落</p>") and "<h3>只有标题</h3>" in md2, md2)
check("md: 标题-only 块不产生 <ul>", "<ul>" not in md2)
# 端到端：--extra-analysis 用「## 标题 + 空行 + 正文」（SKILL.md 流程 B3 的真实结构）
extra_md = os.path.join(TMP, "extra.md")
with open(extra_md, "w", encoding="utf-8") as f:
    f.write("## 深度分析\n\n引擎间差异主要来自长序列聚集。\n\n- 端口 9001 持续偏热\n- 建议复查哈希键\n")
out_extra = os.path.join(TMP, "report_extra.html")
r = subprocess.run([sys.executable, GEN, "--csv", a_csv, "--title", "回归-附加分析",
                    "-o", out_extra, "--extra-analysis", extra_md],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("extra: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    hx = open(out_extra, encoding="utf-8").read()
    check("extra: 标题渲染为独立 <h3>", "<h3>深度分析</h3>" in hx)
    check("extra: 无 <ul><h3> 结构错乱", "<ul><h3>" not in hx and "<ul><p>" not in hx)
    check("extra: 列表与段落按序渲染", "<p>引擎间差异主要来自长序列聚集。</p>" in hx
          and "<ul><li>端口 9001 持续偏热</li><li>建议复查哈希键</li></ul>" in hx)

# ---------- 14. 排队集中度/top1 剔除毛刺引擎（评审修复回归） ----------
# eng1 仅 1/30 采样点 waiting 非零（非零占比 3.3% < 5% -> 毛刺），但瞬时值大（cum=100）；
# eng2 每点 waiting=2（cum=60）、eng3 每点 waiting=1（cum=30）——真实排队集中在 eng2。
# 修复前：毛刺引擎被计入集中度分母并被点名为 top1；修复后：top1/集中度均只看非毛刺引擎。
spike_csv = os.path.join(TMP, "dp_load_metrics_spike.csv")
make_v2_csv(spike_csv, ts_mode="epoch", n_points=30,
            engines=[("10.0.0.1:9001", 1.0), ("10.0.0.2:9001", 1.0), ("10.0.0.2:9002", 1.0)])
with open(spike_csv, encoding="utf-8") as f:
    lines = f.read().splitlines()
first_ts = lines[1].split(",")[0]
out_lines = [lines[0]]
for ln in lines[1:]:
    p = ln.split(",")
    if p[1] == "10.0.0.1:9001":
        p[3] = "100" if p[0] == first_ts else "0"
    elif p[1] == "10.0.0.2:9001":
        p[3] = "2"
    else:
        p[3] = "1"
    out_lines.append(",".join(p))
with open(spike_csv, "w", encoding="utf-8") as f:
    f.write("\n".join(out_lines) + "\n")
spike_summary = os.path.join(TMP, "spike_summary.json")
r = subprocess.run([sys.executable, GEN, "--csv", spike_csv, "--title", "回归-毛刺",
                    "-o", os.path.join(TMP, "spike.html"), "--summary-out", spike_summary],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("spike: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    import json as _json
    sj = _json.load(open(spike_summary, encoding="utf-8"))
    d0 = sj["datasets"][0]
    check("spike: 毛刺清单正确", d0["queue_spikes"] == ["10.0.0.1:9001"], d0["queue_spikes"])
    check("spike: top1 不是毛刺引擎", d0["queue_top"] == "10.0.0.2:9001", d0["queue_top"])
    check("spike: 集中度按非毛刺口径 60/90", d0["queue_conc"] is not None and abs(d0["queue_conc"] - 60.0 / 90.0) < 1e-6, d0["queue_conc"])
    check("spike: 毛刺 waiting 占比单独成字段 100/190",
          abs((d0["queue_spike_waiting_share"] or 0) - 100.0 / 190.0) < 1e-6, d0.get("queue_spike_waiting_share"))
    hsp = open(os.path.join(TMP, "spike.html"), encoding="utf-8").read()
    check("spike: 毛刺降级脚注", "毛刺说明" in hsp and "不参与集中度" in hsp)
    check("spike: 建议点名真实集中引擎", "排队集中于引擎 10.0.0.2:9001" in hsp)
    check("spike: 不再把毛刺引擎当集中点", "排队集中于引擎 10.0.0.1:9001" not in hsp)

# ---------- 15. A/B 对比小样本护栏：CV 差值不再直接定胜负（评审修复回归） ----------
# 两池均小样本（N̄<200）、CV 差 0.33 vs 0.05 >> 0.02：横幅不得宣布胜负
gA = os.path.join(TMP, "dp_guard_A.csv")
gB = os.path.join(TMP, "dp_guard_B.csv")
make_v2_csv(gA, ts_mode="epoch", base_counter=1000, n_points=2,
            engines=[("10.0.0.1:9001", 1.0), ("10.0.0.1:9002", 0.5)])
make_v2_csv(gB, ts_mode="epoch", base_counter=1000, n_points=2,
            engines=[("10.0.0.1:9001", 1.0), ("10.0.0.1:9002", 0.9)])
out_g = os.path.join(TMP, "report_guard.html")
r = subprocess.run([sys.executable, GEN, "--csv", gA, gB, "--label-a", "A策略", "--label-b", "B策略",
                    "--title", "回归-小样本对比", "-o", out_g],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("guard: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    hg = open(out_g, encoding="utf-8").read()
    check("guard: 横幅不作胜负判定", "不作胜负判定" in hg and "请求分布更均衡" not in hg)
    check("guard: 总结同样不作胜负", "本报告不作胜负判定" in hg)
    check("guard: 提示效应量优先", "LIF/失衡比等效应量" in hg)
# 大样本（N̄>200）：差异 >= 0.02 时仍应宣布占优方（护栏不误伤正常判定）
gA2 = os.path.join(TMP, "dp_big_A.csv")
gB2 = os.path.join(TMP, "dp_big_B.csv")
make_v2_csv(gA2, ts_mode="epoch", base_counter=1000, n_points=20,
            engines=[("10.0.0.1:9001", 1.0), ("10.0.0.1:9002", 0.5)])
make_v2_csv(gB2, ts_mode="epoch", base_counter=1000, n_points=20,
            engines=[("10.0.0.1:9001", 1.0), ("10.0.0.1:9002", 0.9)])
r = subprocess.run([sys.executable, GEN, "--csv", gA2, gB2, "--label-a", "A策略", "--label-b", "B策略",
                    "--title", "回归-大样本对比", "-o", os.path.join(TMP, "report_big.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("big: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    hb = open(os.path.join(TMP, "report_big.html"), encoding="utf-8").read()
    check("big: 大样本仍宣布占优方", "B策略 请求分布更均衡" in hb)
    check("big: 不误挂护栏横幅", "不作胜负判定" not in hb)

# ---------- 16. 自检修复：列导出说明脚注无残留引号 ----------
check("single: absent 脚注无残留引号", "</p>\"" not in html)

# ---------- 17. 引擎标签 HTML 转义（自检修复回归） ----------
# CSV 的 ip_port 列来自远端采集机，含 HTML 字符的标签必须转义后进 HTML
evil_csv = os.path.join(TMP, "dp_load_metrics_evil.csv")
make_v2_csv(evil_csv, ts_mode="epoch",
            engines=[("<b>10.0.0.1</b>:9001", 1.0), ("<b>10.0.0.1</b>:9002", 0.5)])
r = subprocess.run([sys.executable, GEN, "--csv", evil_csv, "-o", os.path.join(TMP, "evil.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("escape: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    he = open(os.path.join(TMP, "evil.html"), encoding="utf-8").read()
    check("escape: 原始标签不进 HTML", "<b>10.0.0.1</b>" not in he)
    check("escape: 标签已转义输出", "&lt;b&gt;10.0.0.1&lt;/b&gt;" in he)

# ---------- 18. 评审修复：输出路径冲突防护（-o/--summary-out 不得指向输入或彼此） ----------
def mk_v2_row(ts, tag, running, waiting, succ, prompt, gen, tts, ttc, qfix=0):
    """手工构造 33 列 v2 行：缺省列按“未导出/常量”填，方便局部控制"""
    ttavg = (tts / ttc) if (ttc is not None and ttc > 0 and tts is not None and tts >= 0) else -1
    vals = {
        "timestamp": ts, "ip_port": tag,
        "num_requests_running": running, "num_requests_waiting": waiting,
        "num_requests_swapped": -1, "request_success_total": succ,
        "request_failure_total": -1, "num_preemptions_total": 0,
        "prompt_tokens_total": prompt, "generation_tokens_total": gen,
        "request_validation_time_total": -1, "kv_cache_usage_perc": 0.42,
        "cpu_cache_usage_perc": -1, "prefix_cache_hit_rate": 0.5,
        "pending_prefix_cache_hit_rate": -1, "external_prefix_cache_hit_rate": 0.0,
        "process_start_time_seconds": 1793797588,
        "ttft_avg": ttavg, "queue_avg": 0.0, "prefill_avg": 0.1,
        "tpot_avg": 0.02, "e2e_avg": 0.5,
        "ttft_sum": tts, "ttft_count": ttc,
        "queue_sum": 0.5, "queue_count": ttc if (ttc is not None and ttc >= 0) else -1,
        "prefill_sum": 10.0 if (tts is not None and tts >= 0) else -1,
        "prefill_count": ttc if (ttc is not None and ttc >= 0) else -1,
        "tpot_sum": 20.0 if (tts is not None and tts >= 0) else -1,
        "tpot_count": (ttc * 8) if (ttc is not None and ttc >= 0) else -1,
        "e2e_sum": 50.0 if (tts is not None and tts >= 0) else -1,
        "e2e_count": ttc if (ttc is not None and ttc >= 0) else -1,
        "queue_fixed": qfix,
    }
    return ",".join(str(vals[c]) for c in HEADER.split(","))


def write_v2_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(HEADER + "\n")
        f.write("\n".join(rows) + "\n")


RT_CSV = evil_csv  # 复用 17 节的输入做冲突测试（防护在解析前执行，任意输入均可）
r = subprocess.run([sys.executable, GEN, "--csv", RT_CSV, "-o", RT_CSV],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("clash: -o 指向输入 CSV 时非零退出", r.returncode != 0)
check("clash: 提示输出路径冲突", "冲突" in (r.stderr + r.stdout))
rt_before = open(RT_CSV, encoding="utf-8").read()
check("clash: 输入 CSV 未被破坏", rt_before.startswith("timestamp,ip_port"))
clash_html = os.path.join(TMP, "clash_out.html")
r = subprocess.run([sys.executable, GEN, "--csv", RT_CSV, "-o", clash_html, "--summary-out", clash_html],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("clash: -o 与 --summary-out 相同时非零退出", r.returncode != 0)
check("clash: 相同路径时未生成输出文件", not os.path.exists(clash_html))

# ---------- 19. 评审修复：request counter 有效点不足的引擎不参与均衡性计算 ----------
rows19 = []
ts19 = ["2026-11-12 10:%02d:%02d" % (i, (i * 7) % 60) for i in range(10)]
for i, ts in enumerate(ts19):
    s1 = 1000 + 90 * i
    s2 = 1000 + 110 * i
    rows19.append(mk_v2_row(ts, "10.0.0.1:9001", 5, 0, s1, s1 * 120, s1 * 60, 0.5 * s1, s1))
    rows19.append(mk_v2_row(ts, "10.0.0.1:9002", 5, 0, s2, s2 * 120, s2 * 60, 0.5 * s2, s2))
    # 失效引擎：request counter 全缺失（gauge 正常），不应被当作真实零流量
    rows19.append(mk_v2_row(ts, "10.0.0.2:9001", 5, 0, -1, -1, -1, -1, -1))
dead_csv = os.path.join(TMP, "dp_dead_engine.csv")
write_v2_csv(dead_csv, rows19)
dead_out = os.path.join(TMP, "report_dead.html")
dead_sum = os.path.join(TMP, "summary_dead.json")
r = subprocess.run([sys.executable, GEN, "--csv", dead_csv, "-o", dead_out, "--summary-out", dead_sum],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("dead-engine: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    hd = open(dead_out, encoding="utf-8").read()
    sj19 = json.load(open(dead_sum, encoding="utf-8"))
    d0 = sj19["datasets"][0]
    check("dead-engine: 数据口径展示 有效引擎数", "3（有效 2）" in hd)
    check("dead-engine: 失效引擎排除出均衡性计算", "有效引擎 2/3" in hd)
    check("dead-engine: summary valid_engines=2", d0.get("valid_engines") == 2)
    check("dead-engine: summary invalid_tags 命中",
          d0.get("invalid_tags") == ["10.0.0.2:9001"])
    # 若把失效引擎当零流量：CV≈0.72 -> 横幅判「显著失衡」；
    # 正确排除后 CV=0.10 -> 横幅判「基本均衡」（分级图例文字不参与断言）
    check("dead-engine: 失效引擎未拉高 CV（横幅判 基本均衡）", "综合结论：基本均衡" in hd)
    check("dead-engine: 未被误判为显著失衡", "综合结论：显著失衡" not in hd)

# ---------- 20. 评审修复：CSV 行序乱序时按时间排序后差分，不误判重启/重复计数 ----------
rows20 = []
t20 = [1793900000 + i * 10 for i in range(4)]
for i, t in enumerate(t20):
    s = 1000 + 50 * i
    rows20.append(mk_v2_row(str(t), "10.0.0.1:9001", 5, 0, s, s * 120, s * 60, 0.5 * s, s))
rows20 = [rows20[i] for i in (0, 2, 1, 3)]  # 行序 t0,t2,t1,t3 乱序
oo_csv = os.path.join(TMP, "dp_out_of_order.csv")
write_v2_csv(oo_csv, rows20)
oo_out = os.path.join(TMP, "report_oo.html")
oo_sum = os.path.join(TMP, "summary_oo.json")
r = subprocess.run([sys.executable, GEN, "--csv", oo_csv, "-o", oo_out, "--summary-out", oo_sum],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("out-of-order: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    sj20 = json.load(open(oo_sum, encoding="utf-8"))
    ho = open(oo_out, encoding="utf-8").read()
    em20 = sj20["datasets"][0]["engines"]["10.0.0.1:9001"]
    check("out-of-order: 按时间排序后增量正确（150 而非重复计入的 200）", em20["req"] == 150)
    check("out-of-order: 正常增长未被误判为重启", em20["restarts"] == 0)
    check("out-of-order: 报告未挂重启警示块", "窗口内存在疑似重启" not in ho)

# ---------- 21. 评审修复：窗口平均只用两端同时有 sum&count 的区间 ----------
rows21 = []
for i, ts in enumerate(ts19):
    s = 1000 + 100 * i
    tts = 0.5 * s
    if i == 9:
        tts = -1  # 末端 sum 缺失：不得用更早的 sum 点配全量 Δcount
    rows21.append(mk_v2_row(ts, "10.0.0.1:9001", 5, 0, s, s * 120, s * 60, tts, s))
wavg_csv = os.path.join(TMP, "dp_wavg_end.csv")
write_v2_csv(wavg_csv, rows21)
rows22 = []
for i, ts in enumerate(ts19):
    s = 1000 + 100 * i
    tts = 0.5 * s if i == 0 else -1  # 只有一个有效 sum 点：应判不可用而非零延迟
    rows22.append(mk_v2_row(ts, "10.0.0.1:9002", 5, 0, s, s * 120, s * 60, tts, s))
wavg2_csv = os.path.join(TMP, "dp_wavg_single.csv")
write_v2_csv(wavg2_csv, rows22)
w_sum = os.path.join(TMP, "summary_wavg.json")
r = subprocess.run([sys.executable, GEN, "--csv", wavg_csv, wavg2_csv,
                    "--label-a", "末端缺失", "--label-b", "单点缺失",
                    "-o", os.path.join(TMP, "report_wavg.html"), "--summary-out", w_sum],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("wavg: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    sj21 = json.load(open(w_sum, encoding="utf-8"))
    tt_end = sj21["datasets"][0]["engines"]["10.0.0.1:9001"]["ttft"]
    tt_one = sj21["datasets"][1]["engines"]["10.0.0.1:9002"]["ttft"]
    check("wavg: 末端 sum 缺失时按配对区间计算（恒 0.5s）",
          tt_end is not None and abs(tt_end - 0.5) < 1e-6)
    check("wavg: 仅单点 sum 时判不可用（None 而非零延迟）", tt_one is None)

# ---------- 22. 评审修复：A/B 失衡结构结论按实际方向措辞（不套固定模板） ----------
rowsA, rowsB = [], []
for i, ts in enumerate(ts19):
    for tag, step in (("10.0.0.1:9001", 45), ("10.0.0.1:9002", 54),
                      ("10.0.0.2:9001", 45), ("10.0.0.2:9002", 54)):
        s = 1000 + step * i
        rowsA.append(mk_v2_row(ts, tag, 5, 0, s, s * 120, s * 60, 0.5 * s, s))
    for tag, step in (("10.0.0.1:9001", 63), ("10.0.0.1:9002", 68),
                      ("10.0.0.2:9001", 31), ("10.0.0.2:9002", 36)):
        s = 1000 + step * i
        rowsB.append(mk_v2_row(ts, tag, 5, 0, s, s * 120, s * 60, 0.5 * s, s))
dirA = os.path.join(TMP, "dp_dir_A.csv")
dirB = os.path.join(TMP, "dp_dir_B.csv")
write_v2_csv(dirA, rowsA)
write_v2_csv(dirB, rowsB)
# A：主机间均衡、主机内失衡（within 占比≈1.0），请求 CV≈0.09 -> 占优方
# B：主机间失衡为主（within 占比≈2.4%），请求 CV≈0.33
dir_out = os.path.join(TMP, "report_dir.html")
r = subprocess.run([sys.executable, GEN, "--csv", dirA, dirB, "--label-a", "A", "--label-b", "B",
                    "--title", "方向-失衡结构对比", "-o", dir_out],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("dir: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    hdir = open(dir_out, encoding="utf-8").read()
    check("dir: 不再用固定方向模板（无 已由…转为 表述）", "已由" not in hdir)
    check("dir: 高 within 一侧（A）被描述为主机内分流", "A 的失衡更多来自主机内端口级分流" in hdir)
    check("dir: 低 within 一侧（B）被描述为主机间调度", "B 更多来自主机间调度" in hdir)
    check("dir: 微弱残余 只修饰低 within 一侧（B）", "B 侧主机内仅" in hdir)
    check("dir: 未把高 within 的 A 说成微弱残余", "A 侧主机内仅" not in hdir)

# ---------- 23. 评审修复：一侧零流量（ratio=0 除零防护 + 横幅/总结标明不可比） ----------
rowsZA, rowsZB = [], []
for i, ts in enumerate(ts19):
    # A：counter 全程恒定 -> 窗口增量 0（合法 CSV）；B：正常增长
    rowsZA.append(mk_v2_row(ts, "10.0.0.1:9001", 5, 0, 1000, 120000, 60000, 500.0, 1000))
    rowsZA.append(mk_v2_row(ts, "10.0.0.1:9002", 5, 0, 1000, 120000, 60000, 500.0, 1000))
    rowsZB.append(mk_v2_row(ts, "10.0.0.1:9001", 5, 0, 1000 + 100 * i, (1000 + 100 * i) * 120, (1000 + 100 * i) * 60, 0.5 * (1000 + 100 * i), 1000 + 100 * i))
    rowsZB.append(mk_v2_row(ts, "10.0.0.1:9002", 5, 0, 1000 + 110 * i, (1000 + 110 * i) * 120, (1000 + 110 * i) * 60, 0.5 * (1000 + 110 * i), 1000 + 110 * i))
zA = os.path.join(TMP, "dp_zero_A.csv")
zB = os.path.join(TMP, "dp_zero_B.csv")
write_v2_csv(zA, rowsZA)
write_v2_csv(zB, rowsZB)
r = subprocess.run([sys.executable, GEN, "--csv", zA, zB, "--label-a", "A", "--label-b", "B",
                    "-o", os.path.join(TMP, "report_zero.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("zero-side: exit 0（不因 1/ratio 除零崩溃）", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    hz = open(os.path.join(TMP, "report_zero.html"), encoding="utf-8").read()
    check("zero-side: 横幅标明一侧零流量不可比", "零流量" in hz and "不可比" in hz)
    check("zero-side: 未输出胜负判定", "请求分布更均衡" not in hz)
    check("zero-side: 综合总结同步标明", "对比不可比" in hz or "零流量" in hz)

# ---------- 24. 评审修复：全部评级项缺失时横幅判「证据不足」而非「基本均衡」 ----------
rows24 = [mk_v2_row("2026-11-12 10:00:00", "10.0.0.1:9001", 5, 0, -1, -1, -1, -1, -1),
          mk_v2_row("2026-11-12 10:00:00", "10.0.0.1:9002", 5, 0, -1, -1, -1, -1, -1)]
ev_csv = os.path.join(TMP, "dp_no_evidence.csv")
write_v2_csv(ev_csv, rows24)
r = subprocess.run([sys.executable, GEN, "--csv", ev_csv, "-o", os.path.join(TMP, "report_ev.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("no-evidence: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    he = open(os.path.join(TMP, "report_ev.html"), encoding="utf-8").read()
    check("no-evidence: 横幅判证据不足", "综合结论：证据不足" in he)
    check("no-evidence: 提示补采", "补采" in he)
    check("no-evidence: 不再默认基本均衡", "综合结论：基本均衡" not in he)

# ---------- 25. 评审修复：引擎集合不一致时暂停胜负判定（不按各自全池 CV 判优劣） ----------
rowsSA, rowsSB = [], []
for i, ts in enumerate(ts19):
    for tag, step in (("10.0.0.1:9001", 100), ("10.0.0.2:9001", 200), ("10.0.0.3:9001", 300)):
        s = 1000 + step * i
        rowsSA.append(mk_v2_row(ts, tag, 5, 0, s, s * 120, s * 60, 0.5 * s, s))
        rowsSB.append(mk_v2_row(ts, tag, 5, 0, s, s * 120, s * 60, 0.5 * s, s))
    rowsSB.append(mk_v2_row(ts, "10.0.0.4:9001", 5, 0, 1000, 120000, 60000, 500.0, 1000))  # B 独有闲置引擎
sA = os.path.join(TMP, "dp_set_A.csv")
sB = os.path.join(TMP, "dp_set_B.csv")
write_v2_csv(sA, rowsSA)
write_v2_csv(sB, rowsSB)
r = subprocess.run([sys.executable, GEN, "--csv", sA, sB, "--label-a", "A", "--label-b", "B",
                    "-o", os.path.join(TMP, "report_set.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("set-mismatch: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    hs = open(os.path.join(TMP, "report_set.html"), encoding="utf-8").read()
    check("set-mismatch: 横幅不作胜负与平局判定", "不作胜负与平局判定" in hs and "引擎集合不一致" in hs)
    check("set-mismatch: 独有闲置引擎未让 A 胜出", "请求分布更均衡" not in hs)
    check("set-mismatch: 综合总结同步暂停判定", "一并不作" in hs.split("</h2>")[-1] or "引擎集合不一致" in hs)

# ---------- 26. 评审修复：胜场计数归属胜方（max(wins_a,wins_b) 可能把对方胜场写给胜方） ----------
rowsWA, rowsWB = [], []
for i, ts in enumerate(ts19):
    for j in range(1, 9):
        tag = "10.0.0.1:900%d" % j
        # A：7 台紧致 + 1 台略高（请求 CV 0.034 胜出）；TTFT 分两组 0.4/0.6（TTFT CV 0.2 输给 B）
        stepA = 51 if j == 8 else 46
        sA_ = 1000 + stepA * i
        ttftA = 0.4 * sA_ if j <= 4 else 0.6 * sA_
        waitA = 5 if j == 1 else 0          # A 排队集中于 e1（集中度 1.0）
        rowsWA.append(mk_v2_row(ts, tag, 5, waitA, sA_, sA_ * 120, sA_ * 60, ttftA, sA_))
        # B：7 台正常 + 1 台极低（请求 CV 0.132 落败；LIF/失衡比/最热占比 优于 A）；TTFT 恒 0.5
        stepB = 30 if j == 8 else 50
        sB_ = 1000 + stepB * i
        rowsWB.append(mk_v2_row(ts, tag, 5, 1, sB_, sB_ * 120, sB_ * 60, 0.5 * sB_, sB_))
wA = os.path.join(TMP, "dp_win_A.csv")
wB = os.path.join(TMP, "dp_win_B.csv")
write_v2_csv(wA, rowsWA)
write_v2_csv(wB, rowsWB)
r = subprocess.run([sys.executable, GEN, "--csv", wA, wB, "--label-a", "A", "--label-b", "B",
                    "-o", os.path.join(TMP, "report_win.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("win-count: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    hw = open(os.path.join(TMP, "report_win.html"), encoding="utf-8").read()
    # A 按请求 CV 胜出（0.035 vs 0.139），占优 3 项（CV/失衡比/主机内 CV）；B 占优 4 项
    # （LIF/最热占比/TTFT/集中度）。旧代码 max(wins_a,wins_b)=4 会把 B 的 4 项胜场写给 A
    check("win-count: 胜方 A 占优项数取自身计数（3 项）", "占优 3 项" in hw)
    check("win-count: 不再把 B 的 4 项胜场写给 A", "占优 4 项" not in hw)
    check("win-count: 另一方略好项数正确（4 项）", "另有 4 项指标另一方略好" in hw)

# ---------- 27. 自检修复：重复时间戳后写覆盖，gauge/cum 不重复加权 ----------
rows27 = []
t27 = [1793900000 + i * 10 for i in range(4)]
# E1 的 t0 有两行（重复时间戳）：按“后写覆盖”取 succ=1000、waiting=9；
# 按原始行序差分（1005→1000）会被误判为重启，waiting 均值/累计也会双计
rows27.append(mk_v2_row(str(t27[0]), "10.0.0.1:9001", 5, 5, 1005, 1005 * 120, 1005 * 60, 0.5 * 1005, 1005))
rows27.append(mk_v2_row(str(t27[0]), "10.0.0.1:9001", 5, 9, 1000, 1000 * 120, 1000 * 60, 0.5 * 1000, 1000))
for i in (1, 2, 3):
    s = 1000 + 10 * i
    rows27.append(mk_v2_row(str(t27[i]), "10.0.0.1:9001", 5, 1, s, s * 120, s * 60, 0.5 * s, s))
for i in range(4):
    s = 2000 + 10 * i
    rows27.append(mk_v2_row(str(t27[i]), "10.0.0.1:9002", 5, 2, s, s * 120, s * 60, 0.5 * s, s))
dup_csv = os.path.join(TMP, "dp_dup_ts.csv")
write_v2_csv(dup_csv, rows27)
dup_sum = os.path.join(TMP, "summary_dup.json")
r = subprocess.run([sys.executable, GEN, "--csv", dup_csv, "-o", os.path.join(TMP, "dup.html"),
                    "--summary-out", dup_sum],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("dup-ts: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    sj27 = json.load(open(dup_sum, encoding="utf-8"))
    d27 = sj27["datasets"][0]
    e1 = d27["engines"]["10.0.0.1:9001"]
    check("dup-ts: 后写覆盖取值（req=30 而非跨 1005 重复计入）", e1["req"] == 30, e1["req"])
    check("dup-ts: 重复时间戳不误判重启", e1["restarts"] == 0, e1["restarts"])
    check("dup-ts: waiting 均值去重（(9+1+1+1)/4=3.0）",
          e1["waiting_mean"] is not None and abs(e1["waiting_mean"] - 3.0) < 1e-9, e1["waiting_mean"])
    check("dup-ts: waiting 累计去重（集中度 12/20 而非 17/25）",
          d27["queue_conc"] is not None and abs(d27["queue_conc"] - 12.0 / 20.0) < 1e-9, d27["queue_conc"])

# ---------- 28. 自检修复：样本护栏 N̄ 按有效引擎数计（被剔除引擎不进分母） ----------
rows28 = []
for i, ts in enumerate(ts19):
    s = 1000 + 25 * i
    rows28.append(mk_v2_row(ts, "10.0.0.1:9001", 5, 0, s, s * 120, s * 60, 0.5 * s, s))
    rows28.append(mk_v2_row(ts, "10.0.0.1:9002", 5, 0, s, s * 120, s * 60, 0.5 * s, s))
    rows28.append(mk_v2_row(ts, "10.0.0.2:9001", 5, 0, -1, -1, -1, -1, -1))  # 失效引擎：增量缺失
nv_csv = os.path.join(TMP, "dp_nvalid_guard.csv")
write_v2_csv(nv_csv, rows28)
r = subprocess.run([sys.executable, GEN, "--csv", nv_csv, "-o", os.path.join(TMP, "nv.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("nvalid-guard: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    hv = open(os.path.join(TMP, "nv.html"), encoding="utf-8").read()
    # 有效引擎 2 个、总请求 450 -> N̄=225>=200，护栏不应触发；
    # 若误按全部引擎（3 个）计：450/3=150<200 -> 误触发样本护栏
    check("nvalid-guard: N̄ 按有效引擎数计（护栏不误触发）", "每引擎平均仅" not in hv)
    check("nvalid-guard: 失效引擎剔除标注仍在", "有效引擎 2/3" in hv)

# ---------- 29. 自检修复：window_avg 弃用 process_start 变化的跨生命周期区间 ----------
rows29 = []
sums29 = [(50.0, 100), (60.0, 110), (90.0, 115), (100.0, 125)]
ps29 = [1793900000, 1793900000, 1793900500, 1793900500]
for i in range(4):
    s = 1000 + 10 * i
    row = mk_v2_row(str(t27[i]), "10.0.0.1:9001", 5, 0, s, s * 120, s * 60, sums29[i][0], sums29[i][1])
    p = row.split(",")
    p[16] = str(ps29[i])  # process_start_time_seconds 列
    rows29.append(",".join(p))
psx_csv = os.path.join(TMP, "dp_ps_change.csv")
write_v2_csv(psx_csv, rows29)
psx_sum = os.path.join(TMP, "summary_psx.json")
r = subprocess.run([sys.executable, GEN, "--csv", psx_csv, "-o", os.path.join(TMP, "psx.html"),
                    "--summary-out", psx_sum],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("ps-change: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    e29 = json.load(open(psx_sum, encoding="utf-8"))["datasets"][0]["engines"]["10.0.0.1:9001"]
    # (t0,t1)=10s/10req、(t2,t3)=10s/10req -> ttft=20/20=1.0；
    # (t1,t2) 跨 process_start 变化（sum +30/count +5 的错配区间）应弃用，
    # 否则会算成 50/25=2.0
    check("ps-change: 跨生命周期区间弃用（ttft=1.0 而非 2.0）",
          e29["ttft"] is not None and abs(e29["ttft"] - 1.0) < 1e-9, e29["ttft"])
    check("ps-change: 重启识别仍正常触发（ps 变化）", e29["restarts"] == 1, e29["restarts"])

# ---------- 30. 自检修复：对比表不静默丢弃增量缺失引擎（保留 n/a 行） ----------
rows30A, rows30B = [], []
for i, ts in enumerate(ts19):
    s = 1000 + 50 * i
    rows30A.append(mk_v2_row(ts, "10.0.0.1:9001", 5, 0, s, s * 120, s * 60, 0.5 * s, s))
    rows30A.append(mk_v2_row(ts, "10.0.0.1:9002", 5, 0, -1, -1, -1, -1, -1))  # A 侧失效引擎
    sB1, sB2 = 1000 + 40 * i, 1000 + 30 * i
    rows30B.append(mk_v2_row(ts, "10.0.0.1:9001", 5, 0, sB1, sB1 * 120, sB1 * 60, 0.5 * sB1, sB1))
    rows30B.append(mk_v2_row(ts, "10.0.0.1:9002", 5, 0, sB2, sB2 * 120, sB2 * 60, 0.5 * sB2, sB2))
mzA, mzB = os.path.join(TMP, "dp_mvnone_A.csv"), os.path.join(TMP, "dp_mvnone_B.csv")
write_v2_csv(mzA, rows30A)
write_v2_csv(mzB, rows30B)
r = subprocess.run([sys.executable, GEN, "--csv", mzA, mzB, "--label-a", "A", "--label-b", "B",
                    "-o", os.path.join(TMP, "report_mvnone.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("mv-none: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    hm = open(os.path.join(TMP, "report_mvnone.html"), encoding="utf-8").read()
    # 修复前 A 侧失效引擎被 continue 掉、从「三、请求分配对比」整表消失；
    # 修复后保留行：A 增量 n/a、B 增量 270（窗口增量 = 30*9）
    check("mv-none: 失效引擎保留在请求分配对比（A 侧 n/a、B 侧有值）",
          "10.0.0.1:9002</td><td>n/a</td><td>270</td>" in hm)
    check("mv-none: 失效引擎行排表尾（有效引擎 9001 在前）",
          hm.find("10.0.0.1:9001</td><td>450</td><td>360</td>")
          < hm.find("10.0.0.1:9002</td><td>n/a</td><td>270</td>"))
    check("mv-none: 表尾口径说明", "列在表尾" in hm)

# ---------- 31. 复核修复：已存在的输出与输入互为硬链接（同 inode）时同样拒绝 ----------
# realpath 只解析符号链接，识别不了硬链接——字符串比对会漏过，samefile 按 inode 比对
hl_src = os.path.join(TMP, "dp_hardlink_src.csv")
with open(hl_src, "w", newline="", encoding="utf-8") as f:
    f.write(open(a_csv, encoding="utf-8").read())
hl_out = os.path.join(TMP, "hl_out.html")
try:
    os.link(hl_src, hl_out)
    _linked = True
except OSError:
    _linked = False  # 文件系统不支持硬链接（FAT/exFAT 等）时跳过
if _linked:
    r = subprocess.run([sys.executable, GEN, "--csv", hl_src, "-o", hl_out],
                      capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("hardlink: -o 为输入的硬链接时非零退出", r.returncode != 0)
    check("hardlink: 提示路径冲突", "冲突" in (r.stderr + r.stdout))
    check("hardlink: 原始采样未被改写", open(hl_src, encoding="utf-8").read().startswith("timestamp,ip_port"))
else:
    check("hardlink: 文件系统不支持硬链接，跳过（字符串比对已由 18 节覆盖）", True)

# ---------- 32. 复核修复：process_start 亚秒级变化也判跨生命周期（精确比对无容差） ----------
rows32 = []
sums32 = [(50.0, 100), (60.0, 110), (90.0, 115), (100.0, 125)]
ps32 = [1793900000, 1793900000, 1793900000.5, 1793900000.5]  # t2 处新进程仅晚启动 0.5s
for i in range(4):
    s = 1000 + 10 * i
    row = mk_v2_row(str(t27[i]), "10.0.0.1:9001", 5, 0, s, s * 120, s * 60, sums32[i][0], sums32[i][1])
    p = row.split(",")
    p[16] = str(ps32[i])  # process_start_time_seconds 列
    rows32.append(",".join(p))
subps_csv = os.path.join(TMP, "dp_subsec_ps.csv")
write_v2_csv(subps_csv, rows32)
subps_sum = os.path.join(TMP, "summary_subps.json")
r = subprocess.run([sys.executable, GEN, "--csv", subps_csv, "-o", os.path.join(TMP, "subps.html"),
                    "--summary-out", subps_sum],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("subsec-ps: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    e32 = json.load(open(subps_sum, encoding="utf-8"))["datasets"][0]["engines"]["10.0.0.1:9001"]
    # (t0,t1)=10s/10req、(t2,t3)=10s/10req -> ttft=20/20=1.0；旧实现的 1s 容差会放行
    # (t1,t2)（新进程晚启动 0.5s、两列计数均已追上）→ 错算成 50/25=2.0
    check("subsec-ps: 亚秒级 ps 变化弃用区间（ttft=1.0 而非 2.0）",
          e32["ttft"] is not None and abs(e32["ttft"] - 1.0) < 1e-9, e32["ttft"])
    check("subsec-ps: 亚秒级 ps 变化同样触发重启识别", e32["restarts"] == 1, e32["restarts"])

# ---------- 33. 复核修复：集合不一致时平局判定也暂停（先于 CV 差值门槛） ----------
rows33A, rows33B = [], []
tags33 = ["10.0.0.1:9001", "10.0.0.1:9002", "10.0.0.2:9001"]
for i, ts in enumerate(ts19):
    for tag in tags33:
        s = 1000 + 100 * i
        rows33A.append(mk_v2_row(ts, tag, 5, 0, s, s * 120, s * 60, 0.5 * s, s))
        rows33B.append(mk_v2_row(ts, tag, 5, 0, s, s * 120, s * 60, 0.5 * s, s))
    sB = 1000 + 100 * i  # B 独有引擎增量与共有引擎相同 -> 两池请求 CV 均 0（差值 < 0.02）
    rows33B.append(mk_v2_row(ts, "10.0.0.2:9002", 5, 0, sB, sB * 120, sB * 60, 0.5 * sB, sB))
tieA, tieB = os.path.join(TMP, "dp_tie_A.csv"), os.path.join(TMP, "dp_tie_B.csv")
write_v2_csv(tieA, rows33A)
write_v2_csv(tieB, rows33B)
r = subprocess.run([sys.executable, GEN, "--csv", tieA, tieB, "--label-a", "A", "--label-b", "B",
                    "-o", os.path.join(TMP, "report_tie.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("tie-mismatch: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    htie = open(os.path.join(TMP, "report_tie.html"), encoding="utf-8").read()
    # 旧实现：集合不同但 CV 差 <0.02 -> 输出「两者均衡程度相当」——平局同样是基于
    # 不同引擎全集的结论；修复后集合差异先于平局/胜负分支处理，一并不作
    check("tie-mismatch: 横幅不作胜负与平局判定", "不作胜负与平局判定" in htie)
    check("tie-mismatch: 不再输出基于不同全集的平局结论",
          "均衡程度相当" not in htie and "基本相当" not in htie)
    check("tie-mismatch: 综合总结同步暂停", "一并不作" in htie.split("</h2>")[-1] or "胜负与平局判定" in htie)

# ---------- 34. 评审修复：对比版「主机内端口级 CV 明细」主机名转义（HTML 注入防护） ----------
# 主机名来自 CSV 的 ip_port（远端可控数据）：对比版 section_three_compare 的明细行
# 此前未 esc()，恶意主机标签可把 <img onerror>/<script> 注入自包含报告。
EVIL = "<img src=x onerror=alert(1)>"
rows34A, rows34B = [], []
for i, ts in enumerate(ts19):
    s1, s2 = 1000 + 45 * i, 1000 + 60 * i  # 同主机两引擎增量不同 -> 主机内 CV 可算，明细行渲染
    rows34A.append(mk_v2_row(ts, EVIL + ":9001", 5, 0, s1, s1 * 120, s1 * 60, 0.5 * s1, s1))
    rows34A.append(mk_v2_row(ts, EVIL + ":9002", 5, 0, s2, s2 * 120, s2 * 60, 0.5 * s2, s2))
    sB1, sB2 = 1000 + 50 * i, 1000 + 55 * i
    rows34B.append(mk_v2_row(ts, EVIL + ":9001", 5, 0, sB1, sB1 * 120, sB1 * 60, 0.5 * sB1, sB1))
    rows34B.append(mk_v2_row(ts, EVIL + ":9002", 5, 0, sB2, sB2 * 120, sB2 * 60, 0.5 * sB2, sB2))
evA, evB = os.path.join(TMP, "dp_evilhost_A.csv"), os.path.join(TMP, "dp_evilhost_B.csv")
write_v2_csv(evA, rows34A)
write_v2_csv(evB, rows34B)
r = subprocess.run([sys.executable, GEN, "--csv", evA, evB, "--label-a", "A", "--label-b", "B",
                    "-o", os.path.join(TMP, "report_evilhost.html")],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
check("evil-host: exit 0", r.returncode == 0, r.stderr[-300:])
if r.returncode == 0:
    hev = open(os.path.join(TMP, "report_evilhost.html"), encoding="utf-8").read()
    check("evil-host: 报告无未转义注入标记（<img 原始形态缺席）", "<img src=x" not in hev)
    check("evil-host: 明细行主机名为转义形态", "A 明细：&lt;img src=x" in hev and "B 明细：&lt;img src=x" in hev)
    check("evil-host: 对比版主机内端口级 CV 明细已渲染", "主机内端口级 CV（加权）" in hev)

# ---------- 汇总 ----------
n_pass = sum(1 for _, ok, _ in results if ok)
print("\n== 报告回归结果: %d/%d ==" % (n_pass, len(results)))
sys.exit(0 if n_pass == len(results) else 1)
