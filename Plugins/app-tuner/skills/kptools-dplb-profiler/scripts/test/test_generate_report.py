# -*- coding: utf-8 -*-
"""kptools-dplb-profiler 报告端回归测试（generate_report.py）

覆盖：时间戳容错、单池/对比报告关键要素（列导出说明/负queue修正/uptime警示/修正比例差异）、
硬失败（时间戳全坏/数据列全空）、主机解析（下划线与 .engine 后缀）、--print-schema、
评级逻辑（CV 工程经验分级/小样本护栏/短窗口范围说明）、新指标（失衡差/排队 LIF）。
"""
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

# ---------- 汇总 ----------
n_pass = sum(1 for _, ok, _ in results if ok)
print("\n== 报告回归结果: %d/%d ==" % (n_pass, len(results)))
sys.exit(0 if n_pass == len(results) else 1)
