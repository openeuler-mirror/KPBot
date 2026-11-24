#!/usr/bin/env python3
"""kptools-tpsplit-report：TP 拆分前后收益对比分析（TTFT 表格 + 打点日志 → 收益报告）。

输入：两套数据目录（before/after），每套含：
  ttft_table.csv   —— kptools-tpsplit-log-analysis 解析出的每请求 TTFT 分阶段表
  *.log            —— 打点原始日志（[TTFT][BREAKDOWN] v3 / [LB_DISPATCH] / [TTFT_KVXFER]
                       / [TTFT_DRAFT] / start_load_kv / [SHM_DEQUEUE_BATCH]），可选
  config.json      —— 运行态（tp/dp/模型/压测参数），可选

输出（--out 前缀）：PREFIX.md（报告）、PREFIX.json（机器可读）、PREFIX.html（自包含网页版，
含图表与可折叠证据链）。

口径与术语见 references/stage-glossary.md。纯 Python 标准库，本地运行（Windows/Linux 均可）。

用法示例：
  python analyze_tpsplit_report.py \
      --before data/before_tp32ray --after data/after_tp8dp \
      --out report/tpsplit_gain [--title "TP32→TP8+DP 收益对比"]
"""
import argparse
import csv
import html
import json
import math
import os
import re
import sys
import time

STAGES = ["net_lb", "fe_inputq", "fe_addreq", "sched_qwait", "sched_total",
          "fwd_pure", "draft", "kv_xfer", "shm_dq", "other"]
STAGE_CN = {
    "net_lb": "网络与 LB 分发", "fe_inputq": "前端输入队列", "fe_addreq": "add_request 处理",
    "sched_qwait": "调度排队（waiting queue）", "sched_total": "调度本体（组 batch）",
    "fwd_pure": "纯前向计算", "draft": "MTP/投机 draft 段", "kv_xfer": "KV 传输（P→D）",
    "shm_dq": "shm 队列出队", "other": "其他（兜底）",
}
STAGE_GROUP = {
    "net_lb": "网络与入口", "fe_inputq": "网络与入口", "fe_addreq": "网络与入口",
    "sched_qwait": "调度", "sched_total": "调度", "fwd_pure": "计算", "draft": "计算",
    "kv_xfer": "数据搬运", "shm_dq": "数据搬运", "other": "其他",
}
# 阶段 → 专属打点行（证据链 L3 的补充引用；无专属行的阶段以 BREAKDOWN 行为原始证据）
STAGE_SUPPORT_TAG = {"net_lb": "LB_DISPATCH", "kv_xfer": "KVXFER", "draft": "TTFT_DRAFT"}
INLEN_BUCKETS = [(0, 1024, "<1024"), (1024, 2048, "1024-2048"),
                 (2048, 4096, "2048-4096"), (4096, 10 ** 9, ">=4096")]


def log(msg):
    print(msg)


def sanitize_nan(obj):
    """NaN/Inf 不是合法 JSON，递归换成 None（对应“缺数据”）。"""
    if isinstance(obj, dict):
        return {k: sanitize_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_nan(v) for v in obj]
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return None
    return obj


# ---------------- 数据加载 ----------------

def load_config(d):
    p = os.path.join(d, "config.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def load_table(d, warmup_s=0.0):
    """读 ttft_table.csv（容忍缺列/多列；缺的阶段按缺失处理）。"""
    path = None
    for cand in ("ttft_table.csv", "ttft_table.CSV"):
        if os.path.isfile(os.path.join(d, cand)):
            path = os.path.join(d, cand)
            break
    if not path:
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(".csv"):
                path = os.path.join(d, fn)
                break
    if not path:
        raise FileNotFoundError("在 %s 下未找到 ttft_table.csv（或任何 .csv）" % d)
    with open(path, encoding="utf-8-sig", newline="") as f:
        rdr = csv.DictReader(f)
        base = os.path.basename(path)
        if not rdr.fieldnames or "ttft_ms" not in rdr.fieldnames:
            raise ValueError("%s 缺 ttft_ms 列（表头：%s）——不是本 skill 的口径"
                             % (path, ",".join(rdr.fieldnames or [])))
        rows = []
        for r in rdr:
            try:
                req = (r.get("request_id") or "").strip()
                if not req or r.get("ttft_ms") in (None, ""):
                    continue
                row = {"request_id": req,
                       "input_len": int(float(r.get("input_len") or 0)),
                       "ttft": float(r["ttft_ms"])}
                for s in STAGES:
                    key = s + "_ms"
                    v = r.get(key)
                    if v is None or v == "":
                        row[s] = None
                    else:
                        row[s] = float(v)
                row["ts_epoch"] = parse_ts(r.get("ts") or "")
                rows.append(row)
            except (ValueError, TypeError):
                continue
        if not rows:
            raise ValueError("%s 表格为空（只有表头或没有可解析的数据行）" % path)
        present = [s for s in STAGES if any(row[s] is not None for row in rows)]
        missing = [s for s in STAGES if s not in present]
        if "sched_qwait" in missing:
            raise ValueError("%s 缺 sched_qwait_ms 列（收益主战场无法分析）" % path)
    if warmup_s > 0 and rows:
        t0 = min(r["ts_epoch"] for r in rows if r["ts_epoch"]) if any(
            r["ts_epoch"] for r in rows) else None
        if t0 is not None:
            rows = [r for r in rows if r["ts_epoch"] - t0 >= warmup_s]
    return rows, {"path": base, "missing_stages": missing, "warmup_s": warmup_s}


def parse_ts(s):
    s = s.strip().replace(",", ".")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            continue
    return 0.0


LOG_PATTERNS = {
    "BREAKDOWN": re.compile(r"\[TTFT\]\[BREAKDOWN\] v3 .*?\breq=(\S+)"),
    "LB_DISPATCH": re.compile(r"\[LB_DISPATCH\] .*?\breq=(\S+)"),
    "KVXFER": re.compile(r"\[TTFT_KVXFER\]\[put\] .*?\breq=(\S+)"),
    "TTFT_DRAFT": re.compile(r"\[TTFT_DRAFT\] .*?\breq=(\S+)"),
    "LOAD_KV": re.compile(r"start_load_kv .*?\breq=(\S+)"),
}
SHM_BATCH_RE = re.compile(r"\[SHM_DEQUEUE_BATCH\]")


def scan_logs(d):
    """扫描目录下全部 .log，按 req 建索引（BREAKDOWN 优先；每 req 每类留一条）。"""
    idx = {tag: {} for tag in LOG_PATTERNS}
    shm_lines = []  # (lineno, file, text)
    files = [fn for fn in sorted(os.listdir(d)) if fn.lower().endswith(".log")]
    for fn in files:
        with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                line = line.rstrip("\n")
                for tag, pat in LOG_PATTERNS.items():
                    m = pat.search(line)
                    if m:
                        idx[tag].setdefault(m.group(1), (fn, lineno, line))
                        break
                else:
                    if SHM_BATCH_RE.search(line):
                        shm_lines.append((fn, lineno, line))
    return {"files": files, "by_req": idx, "shm_lines": shm_lines}


# ---------------- 统计 ----------------

def _pct(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    i = max(0, min(len(sorted_vals) - 1, int(math.ceil(p / 100.0 * len(sorted_vals))) - 1))
    return sorted_vals[i]


def stats_for(rows, key):
    vals = sorted(r[key] for r in rows if r.get(key) is not None)
    n = len(vals)
    if not n:
        return {"n": 0, "mean": float("nan"), "p50": float("nan"),
                "p90": float("nan"), "p99": float("nan"), "max": float("nan")}
    return {"n": n, "mean": sum(vals) / n, "p50": _pct(vals, 50),
            "p90": _pct(vals, 90), "p99": _pct(vals, 99), "max": vals[-1]}


def conservation(rows):
    """守恒式校验。缺阶段列的行直接跳过（残差来自缺列而非数据问题，另行以
    「缺失阶段列」标注呈现），避免把部分列数据集误报成 100% 违例。"""
    bad = 0
    checked = 0
    for r in rows:
        if any(r.get(s) is None for s in STAGES):
            continue
        if not r.get("ttft"):
            continue
        checked += 1
        if abs(r["ttft"] - sum(r[s] for s in STAGES)) > max(2.0, 0.02 * r["ttft"]):
            bad += 1
    return bad, checked


def inlen_buckets(rows):
    out = {}
    for lo, hi, label in INLEN_BUCKETS:
        sub = [r for r in rows if lo <= r["input_len"] < hi]
        out[label] = stats_for(sub, "ttft")
    return out


def bucket_share(rows):
    n = len(rows) or 1
    return {label: sum(1 for r in rows if lo <= r["input_len"] < hi) / n
            for lo, hi, label in INLEN_BUCKETS}


# ---------------- 分析 ----------------

def classify(d_ms, d_pct, th_b_ms, th_pct, th_r_ms):
    if d_ms is None:
        return "missing"
    if d_ms <= -th_b_ms and d_pct <= -th_pct:
        return "benefit"
    if d_ms > 0 and (d_ms >= th_r_ms or d_pct >= 2 * th_pct):
        return "regression"
    return "stable"


def build_points(b_rows, a_rows, b_st, a_st, logs_b, logs_a, th):
    """按 Δms 绝对值排序产出收益/回退点，含三级证据链。"""
    points = []
    ttft_d = a_st["ttft"]["mean"] - b_st["ttft"]["mean"]
    for s in STAGES:
        b, a = b_st[s], a_st[s]
        if b["n"] == 0 or a["n"] == 0:
            continue
        d_ms = a["mean"] - b["mean"]
        d_pct = (a["mean"] / b["mean"] - 1.0) * 100.0 if b["mean"] else 0.0
        kind = classify(d_ms, d_pct, th["benefit_ms"], th["benefit_pct"], th["regress_ms"])
        if kind == "stable":
            continue
        contrib = (d_ms / ttft_d * 100.0) if ttft_d else 0.0
        chain = build_chain(s, b_rows, a_rows, logs_b, logs_a)
        points.append({"stage": s, "kind": kind, "d_ms": d_ms, "d_pct": d_pct,
                       "contrib_pct": contrib, "before": b, "after": a, "chain": chain})
    points.sort(key=lambda p: abs(p["d_ms"]), reverse=True)
    return points


def pick_reps(rows, stage):
    """该阶段 best（最小）/ median / worst（最大）三条代表请求。"""
    valid = [r for r in rows if r.get(stage) is not None]
    if not valid:
        return []
    valid.sort(key=lambda r: r[stage])
    return [valid[0], valid[len(valid) // 2], valid[-1]]


def build_chain(stage, b_rows, a_rows, logs_b, logs_a):
    """三级证据链：L1 claim 由调用方拼；这里出 L2 数据行 + L3 日志摘录。"""
    chain = {"l2": [], "l3": []}
    for side, rows, logs in (("before", b_rows, logs_b), ("after", a_rows, logs_a)):
        br = logs["by_req"]["BREAKDOWN"] if logs else {}
        for pos, rep in zip(("best", "median", "worst"), pick_reps(rows, stage)):
            rid = rep["request_id"]
            entry = {"side": side, "kind": "representative", "req": rid, "pos": pos,
                     "stage_value_ms": rep[stage], "lines": []}
            if rid in br:
                fn, ln, text = br[rid]
                entry["lines"].append({"ref": "%s:%d" % (fn, ln), "text": text})
            tag = STAGE_SUPPORT_TAG.get(stage)
            if tag and logs and rid in logs["by_req"].get(tag, {}):
                fn, ln, text = logs["by_req"][tag][rid]
                entry["lines"].append({"ref": "%s:%d" % (fn, ln), "text": text})
            if not entry["lines"]:
                entry["kind"] = "no_log"
            chain["l3"].append(entry)
    return chain


def quality_report(b_rows, a_rows, b_meta, a_meta, min_samples):
    out = []
    for name, rows, meta in (("before", b_rows, b_meta), ("after", a_rows, a_meta)):
        bad, checked = conservation(rows)
        q = {"side": name, "n": len(rows), "csv": meta["path"],
             "missing_stages": meta["missing_stages"],
             "conservation_bad": bad, "conservation_checked": checked,
             "conservation_pct": (100.0 * bad / checked) if checked else 0.0,
             "low_sample": len(rows) < min_samples}
        out.append(q)
    return out


# ---------------- Markdown 渲染 ----------------

def fmt_ms(v, digits=1):
    return ("%%.%df" % digits) % v if v == v else "—"


def render_md(ctx):
    L = []
    b, a = ctx["before"], ctx["after"]
    title = ctx.get("title") or "TP 拆分收益对比报告：%s → %s" % (b["label"], a["label"])
    ps = ctx["points"]
    ttft_b, ttft_a = b["st"]["ttft"], a["st"]["ttft"]
    d_mean = ttft_a["mean"] - ttft_b["mean"]
    d_pct = (ttft_a["mean"] / ttft_b["mean"] - 1.0) * 100.0
    benefits = [p for p in ps if p["kind"] == "benefit"]
    regressions = [p for p in ps if p["kind"] == "regression"]
    q = ctx["quality"]

    L.append("# %s" % title)
    L.append("")
    has_logs = ctx["has_logs"]
    L.append("> 工具：kptools-tpsplit-report v0.1.0 ｜ 生成：%s ｜ 数据来源：TTFT 表格"
             "（kptools-tpsplit-log-analysis 解析）%s"
             % (time.strftime("%Y-%m-%d %H:%M:%S"),
                "＋ 打点原始日志" if has_logs else "（未提供原始日志，证据止步表格层）"))
    L.append("")

    # 1. 结论
    L.append("## 1. 结论")
    L.append("")
    verdict = "净收益" if d_mean < 0 else "净回退"
    L.append("- **总收益：TTFT mean %s→%s ms（%s%%，%s ms）**，P90 %s→%s ms（%s%%），"
             "P99 %s→%s ms（%s%%）。判定：%s。"
             % (fmt_ms(ttft_b["mean"]), fmt_ms(ttft_a["mean"]), fmt_ms(d_pct, 1),
                fmt_ms(d_mean), fmt_ms(ttft_b["p90"]), fmt_ms(ttft_a["p90"]),
                fmt_ms((ttft_a["p90"] / ttft_b["p90"] - 1) * 100 if ttft_b["p90"] else 0),
                fmt_ms(ttft_b["p99"]), fmt_ms(ttft_a["p99"]),
                fmt_ms((ttft_a["p99"] / ttft_b["p99"] - 1) * 100 if ttft_b["p99"] else 0),
                verdict))
    if benefits:
        top = ", ".join("%s（%s ms / %s%%，贡献 %s%%）" % (
            STAGE_CN[p["stage"]], fmt_ms(p["d_ms"]), fmt_ms(p["d_pct"]),
            fmt_ms(abs(p["contrib_pct"]))) for p in benefits[:3])
        L.append("- **主要收益点**（按 |Δms| 排序）：%s。" % top)
    if regressions:
        top = ", ".join("%s（+%s ms / +%s%%）" % (STAGE_CN[p["stage"]], fmt_ms(p["d_ms"]),
                                                  fmt_ms(p["d_pct"])) for p in regressions[:3])
        L.append("- **回退/风险点**：%s。量级与主要收益相比是否可接受，见第 6 节。" % top)
    flags = []
    for side_q in q:
        if side_q["conservation_pct"] > 5:
            flags.append("%s 守恒违例 %.1f%%（>5%%，结论需打折）"
                         % (side_q["side"], side_q["conservation_pct"]))
        if side_q["low_sample"]:
            flags.append("%s 样本量 %d 低于下限 %d（分位数不稳）"
                         % (side_q["side"], side_q["n"], ctx["min_samples"]))
    if ctx["confound"]:
        flags.append("两侧 input_len 分布不一致（L1 距离 %.2f > 0.15），原始均值对比被混杂，"
                     "以第 7 节分层结论为准" % ctx["confound_l1"])
    if not ctx["has_logs"]:
        flags.append("输入未提供原始日志，证据链 L3 缺失（结论只到表格层）")
    L.append("- **数据质量**：%s。" % ("；".join(flags) if flags else "两侧守恒、样本量、"
                                       "input_len 分布均通过检查，无混杂信号"))
    L.append("")

    # 2. 数据与运行态
    L.append("## 2. 数据与运行态")
    L.append("")
    L.append("| 项 | before | after |")
    L.append("| --- | --- | --- |")
    for key, name in (("label", "标签"), ("tp_size", "TP"), ("dp_size", "DP"),
                      ("model", "模型"), ("deploy_note", "部署形态")):
        L.append("| %s | %s | %s |" % (name, b["cfg"].get(key, "—"), a["cfg"].get(key, "—")))
    L.append("| 请求数（表格行） | %d | %d |" % (q[0]["n"], q[1]["n"]))
    L.append("| 守恒违例 | %d/%d（%.1f%%） | %d/%d（%.1f%%） |" % (
        q[0]["conservation_bad"], q[0]["conservation_checked"], q[0]["conservation_pct"],
        q[1]["conservation_bad"], q[1]["conservation_checked"], q[1]["conservation_pct"]))
    miss_b = [x for x in q[0]["missing_stages"] if x]
    miss_a = [x for x in q[1]["missing_stages"] if x]
    if miss_b or miss_a:
        L.append("| 缺失阶段列 | %s | %s |" % ("、".join(miss_b) or "—",
                                             "、".join(miss_a) or "—"))
    L.append("| 原始日志 | %s | %s |" % (
        "、".join(b["logs"]["files"]) if b["logs"] and b["logs"]["files"] else "无",
        "、".join(a["logs"]["files"]) if a["logs"] and a["logs"]["files"] else "无"))
    L.append("")
    L.append("数字口径：表格为**实测**（打点解析），Δ 与贡献度为**计算**值；单位 ms。")
    L.append("")

    # 3. TTFT 总览
    L.append("## 3. TTFT 总览对比")
    L.append("")
    L.append("| 分位数 | before | after | Δms | Δ% |")
    L.append("| --- | --- | --- | --- | --- |")
    for k, name in (("mean", "mean"), ("p50", "P50"), ("p90", "P90"), ("p99", "P99"),
                    ("max", "max")):
        d = ttft_a[k] - ttft_b[k]
        dp = (ttft_a[k] / ttft_b[k] - 1) * 100 if ttft_b[k] else 0
        L.append("| %s | %s | %s | %s | %s |" % (name, fmt_ms(ttft_b[k]), fmt_ms(ttft_a[k]),
                                                  fmt_ms(d), fmt_ms(dp)))
    L.append("")

    # 4. 分阶段拆解
    L.append("## 4. 分阶段拆解对比（按 mean）")
    L.append("")
    L.append("| 阶段 | 分组 | before mean | after mean | Δms | Δ%% | 贡献度* | 判定 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for s in STAGES:
        bs, as_ = ctx["before"]["st"][s], ctx["after"]["st"][s]
        if bs["n"] == 0 or as_["n"] == 0:
            L.append("| %s | %s | — | — | — | — | — | 缺数据 |"
                      % (STAGE_CN[s], STAGE_GROUP[s]))
            continue
        d_ms = as_["mean"] - bs["mean"]
        d_pct = (as_["mean"] / bs["mean"] - 1) * 100 if bs["mean"] else 0
        kind = {"benefit": "收益", "regression": "回退", "stable": "稳定",
                "missing": "缺数据"}[classify(d_ms, d_pct, ctx["th"]["benefit_ms"],
                                              ctx["th"]["benefit_pct"], ctx["th"]["regress_ms"])]
        contrib = d_ms / d_mean * 100 if d_mean else 0
        L.append("| %s（%s） | %s | %s | %s | %s | %s | %s%% | %s |" % (
            STAGE_CN[s], s, STAGE_GROUP[s], fmt_ms(bs["mean"]), fmt_ms(as_["mean"]),
            fmt_ms(d_ms), fmt_ms(d_pct), fmt_ms(contrib), kind))
    L.append("")
    L.append("*贡献度 = 该阶段 Δms ÷ TTFT Δms（改善为负时，负贡献 = 拉低改善；总和 ≈ 100%）。")
    L.append("")

    # 5. 收益点与证据链
    L.append("## 5. 收益点与证据链")
    L.append("")
    if not benefits:
        L.append("未检测到满足阈值（Δms ≤ −%sms 且 Δ%% ≤ −%s%%）的收益点。"
                 % (ctx["th"]["benefit_ms"], ctx["th"]["benefit_pct"]))
    for i, p in enumerate(benefits, 1):
        L.append("### 收益点 %d：%s（`%s`）" % (i, STAGE_CN[p["stage"]], p["stage"]))
        L.append("")
        emit_point(L, p, ctx)

    # 6. 风险与回退点
    L.append("## 6. 风险与回退点")
    L.append("")
    if not regressions:
        L.append("未检测到满足阈值的回退阶段（阈值：Δms ≥ +%sms 或 Δ%% ≥ +%s%%）。"
                 % (ctx["th"]["regress_ms"], 2 * ctx["th"]["benefit_pct"]))
    for i, p in enumerate(regressions, 1):
        L.append("### 回退点 %d：%s（`%s`）" % (i, STAGE_CN[p["stage"]], p["stage"]))
        L.append("")
        emit_point(L, p, ctx)

    # 7. 分输入长度分层
    L.append("## 7. 分输入长度分层对比")
    L.append("")
    if ctx["confound"]:
        L.append("> ⚠️ 两侧 input_len 分布不一致（before 均值 %d vs after 均值 %d token），"
                 "第 3 节的原始均值差有混杂成分；本节按桶分层后的对比才是干净结论。"
                 % (ctx["b_inlen_mean"], ctx["a_inlen_mean"]))
        L.append("")
    L.append("| input_len 桶 | before n | before TTFT mean | after n | after TTFT mean | Δms | Δ% |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    for lo, hi, label in INLEN_BUCKETS:
        bb = ctx["b_buckets"][label]
        ab = ctx["a_buckets"][label]
        if bb["n"] == 0 or ab["n"] == 0:
            L.append("| %s | %d | — | %d | — | — | — |" % (label, bb["n"], ab["n"]))
            continue
        d = ab["mean"] - bb["mean"]
        dp = (ab["mean"] / bb["mean"] - 1) * 100 if bb["mean"] else 0
        L.append("| %s | %d | %s | %d | %s | %s | %s |" % (
            label, bb["n"], fmt_ms(bb["mean"]), ab["n"], fmt_ms(ab["mean"]),
            fmt_ms(d), fmt_ms(dp)))
    L.append("")

    # 8. 假设与未测项
    L.append("## 8. 假设与未测项")
    L.append("")
    L.append("- 本报告只比较 TTFT 及其阶段拆解；**吞吐、显存、长稳（数小时漂移）未在数据内**，"
             "下结论前需另行验证。")
    L.append("- 两次采集的流量形态按 config 声明一致处理；若实际压测脚本/并发参数不同，"
             "排队类阶段（`sched_qwait`、`fe_inputq`）的对比不成立。")
    L.append("- `kv_xfer` 依据打点回填值（pool_worker/kv_transfer 日志），"
             "`defer_finalize` 下 engine 侧 load_kv 恒 0 属已知口径。")
    if not ctx["has_logs"]:
        L.append("- 本次输入缺少原始日志，全部证据止步于表格层（L2），"
                 "未能回溯 BREAKDOWN 原始行（L3）；拿到日志后重跑即可补全证据链。")
    L.append("")
    return "\n".join(L)


def emit_point(L, p, ctx):
    d_ms_str = ("%+.1f" % p["d_ms"]) if p["d_ms"] > 0 else fmt_ms(p["d_ms"])
    L.append("- **L1 Claim**：%s mean %s → %s ms（%s ms，%s%%，占 TTFT 变化的 %s%%）；"
             "P90 %s → %s ms。"
             % (STAGE_CN[p["stage"]], fmt_ms(p["before"]["mean"]), fmt_ms(p["after"]["mean"]),
                d_ms_str, fmt_ms(p["d_pct"]), fmt_ms(abs(p["contrib_pct"])),
                fmt_ms(p["before"]["p90"]), fmt_ms(p["after"]["p90"])))
    L.append("- **L2 支撑数据**（表格实测，mean/P50/P90/P99）：" + "  ".join(
        "%s %s/%s/%s/%s ms" % (side, fmt_ms(p[side]["mean"]), fmt_ms(p[side]["p50"]),
                               fmt_ms(p[side]["p90"]), fmt_ms(p[side]["p99"]))
        for side in ("before", "after")))
    if p["chain"]["l3"] and any(e["lines"] for e in p["chain"]["l3"]):
        L.append("- **L3 日志摘录**（代表请求 best/median/worst，`文件:行号` 可复核）：")
        for e in p["chain"]["l3"]:
            if not e["lines"]:
                continue
            tag = {"best": "最优", "median": "中位", "worst": "最差"}[
                e.get("pos", "median")]
            for line in e["lines"]:
                L.append("  - `%s` %s %s（%s，%s=%s ms）：" % (
                    line["ref"], e["side"], e["req"], tag, p["stage"],
                    fmt_ms(e["stage_value_ms"])))
                L.append("")
                L.append("    ```text")
                L.append("    " + line["text"].replace("|", "\\|"))
                L.append("    ```")
    else:
        L.append("- **L3 日志摘录**：未回溯到原始日志（输入缺 .log 或找不到该请求的 "
                 "BREAKDOWN 行）——本条证据止步于表格层。")
    L.append("")


# ---------------- HTML 渲染 ----------------

def svg_grouped_bars(title, groups, width=880):
    """groups: [(label, v_before, v_after)] → 横向双条（蓝 before / 绿 after），同一比例尺。

    条长按全量程缩放（顶格 620px），值标注在条尾外侧，标签右对齐固定列——三者互不重叠。
    """
    if not groups:
        return ""
    bar_x, bar_max, row_h = 170, 620, 56
    height = 40 + len(groups) * row_h + 14
    vals = [v for _, v1, v2 in groups for v in (v1, v2)]
    vmax = max(abs(v) for v in vals) or 1.0
    # 显式 width/height 属性：老内核浏览器对「只有 viewBox + height:auto」的 SVG 会回落成
    # 默认 150px 高（整张图被压扁），带属性后所有浏览器都能从属性推出宽高比
    parts = ["<svg width='%d' height='%d' viewBox='0 0 %d %d' class='chart' role='img'>"
             % (width, height, width, height)]
    parts.append("<text x='%d' y='22' class='ctitle'>%s</text>" % (bar_x, esc(title)))
    y = 40
    for label, v1, v2 in groups:
        parts.append("<text x='%d' y='%d' class='alab' text-anchor='end'>%s</text>"
                     % (bar_x - 10, y + 32, esc(label)))
        for row_y, v, color, cls in ((y + 8, v1, "#7f9cf5", "b"),
                                    (y + 30, v2, "#48bb78", "a")):
            w = max(2, int(abs(v) / vmax * bar_max))
            parts.append("<rect x='%d' y='%d' width='%d' height='16' rx='2' fill='%s'/>"
                         % (bar_x, row_y, w, color))
            parts.append("<text x='%d' y='%d' class='aval %s'>%s</text>"
                         % (bar_x + w + 6, row_y + 13, cls, fmt_num(v)))
        y += row_h
    parts.append("<text x='%d' y='%d' class='leg' style='fill:#7f9cf5'>■ before</text>"
                 % (bar_x, height - 4))
    parts.append("<text x='%d' y='%d' class='leg' style='fill:#48bb78'>■ after</text>"
                 % (bar_x + 80, height - 4))
    parts.append("</svg>")
    return "".join(parts)


def svg_stage_delta(title, points, width=880):
    """发散横条：改善向左（绿）、回退向右（红），两侧独立比例尺（各侧最大值顶格）。

    长条内嵌白字标值、短条标在条外侧，阶段名右对齐固定列——互不重叠。两侧独立缩放是因为
    改善与回退常差两个数量级，共尺度会把回退压成不可见；图例里标注了各侧顶格值以自证。
    """
    rows = sorted(points, key=lambda p: p["d_ms"])
    if not rows:
        return ""
    label_x, cx = 210, 620
    zone_l, zone_r = 380, 240
    row_h = 30
    height = 40 + len(rows) * row_h + 34
    impr = [p["d_ms"] for p in rows if p["d_ms"] < 0] or [1.0]
    regr = [p["d_ms"] for p in rows if p["d_ms"] > 0] or [1.0]
    s_l = zone_l / max(abs(v) for v in impr)
    s_r = zone_r / max(v for v in regr)
    parts = ["<svg width='%d' height='%d' viewBox='0 0 %d %d' class='chart' role='img'>"
             % (width, height, width, height)]
    parts.append("<text x='%d' y='22' class='ctitle'>%s</text>" % (label_x + 10, esc(title)))
    # 0 轴：Δ=0 分界线（左改善/右回退），用细矩形而非 <line>（个别内核对 line 渲染异常）
    parts.append("<rect x='%d' y='32' width='2' height='%d' fill='#a0aec0'/>"
                 % (cx - 1, height - 64))
    parts.append("<text x='%d' y='28' class='leg' text-anchor='middle'>Δ=0</text>" % cx)
    y = 44
    for p in rows:
        parts.append("<text x='%d' y='%d' class='alab' text-anchor='end'>%s</text>"
                     % (label_x, y + 14, esc(STAGE_CN[p["stage"]])))
        d = p["d_ms"]
        if d < 0:
            w = max(3, int(-d * s_l))
            x = cx - w
            parts.append("<rect x='%d' y='%d' width='%d' height='16' rx='2' fill='#48bb78'/>"
                         % (x, y, w))
            if w >= 74:
                parts.append("<text x='%d' y='%d' class='aval inbar' text-anchor='end'>"
                             "%s ms</text>" % (cx - 6, y + 13, fmt_num(d)))
            else:
                parts.append("<text x='%d' y='%d' class='aval' text-anchor='end'>"
                             "%s ms</text>" % (x - 6, y + 13, fmt_num(d)))
        else:
            w = max(3, int(d * s_r))
            parts.append("<rect x='%d' y='%d' width='%d' height='16' rx='2' fill='#fc8181'/>"
                         % (cx, y, w))
            if w >= 74:
                parts.append("<text x='%d' y='%d' class='aval inbar'>+%s ms</text>"
                             % (cx + 6, y + 13, fmt_num(d)))
            else:
                parts.append("<text x='%d' y='%d' class='aval'>+%s ms</text>"
                             % (cx + w + 6, y + 13, fmt_num(d)))
        y += row_h
    parts.append("<text x='%d' y='%d' class='leg' text-anchor='end'>← 改善"
                 "（最大 −%s ms 顶格，Δ=0 轴向左）</text>"
                 % (cx - 8, height - 12, fmt_num(max(abs(v) for v in impr))))
    parts.append("<text x='%d' y='%d' class='leg'>回退（最大 +%s ms 顶格，"
                 "Δ=0 轴向右）→</text>"
                 % (cx + 8, height - 12, fmt_num(max(regr))))
    parts.append("</svg>")
    return "".join(parts)


def fmt_num(v):
    if v != v:
        return "—"
    return ("%.0f" % v) if abs(v) >= 100 else ("%.1f" % v)


def esc(s):
    return html.escape(str(s), quote=True)


HTML_STYLE = """
body{font-family:'Segoe UI','Microsoft YaHei',sans-serif;margin:0;background:#f7fafc;color:#1a202c}
.wrap{max-width:1000px;margin:0 auto;padding:24px}
h1{font-size:24px} h2{font-size:19px;margin-top:34px;border-bottom:2px solid #e2e8f0;padding-bottom:6px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:10px 0}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px;box-shadow:0 1px 2px rgba(0,0,0,.05)}
.card .k{font-size:12px;color:#718096}.card .v{font-size:18px;font-weight:600;margin-top:4px;white-space:nowrap}
.card .sub{font-size:12px;color:#4a5568;margin-top:3px;white-space:nowrap}
.card .v.good{color:#2f855a}.card .v.bad{color:#c53030}
table{border-collapse:collapse;margin:12px 0;background:#fff;font-size:14.5px;width:100%}
th,td{border:1px solid #e2e8f0;padding:8px 12px;text-align:right}
th:first-child,td:first-child{text-align:left}
th{background:#edf2f7}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600}
.tag.benefit{background:#c6f6d5;color:#22543d}.tag.regression{background:#fed7d7;color:#742a2a}
.tag.stable{background:#e2e8f0;color:#4a5568}
details{background:#fff;border:1px solid #e2e8f0;border-radius:8px;margin:8px 0;padding:8px 14px}
summary{cursor:pointer;font-weight:600}
pre{background:#2d3748;color:#e2e8f0;border-radius:6px;padding:8px 12px;overflow-x:auto;font-size:12px}
.chart{width:100%;height:auto;background:#fff;border:1px solid #e2e8f0;border-radius:8px;margin:10px 0}
.ctitle{font-size:14px;font-weight:600;fill:#2d3748}
.alab{font-size:12px;fill:#4a5568}.aval{font-size:11px;fill:#2d3748}
.aval.b{fill:#5a67d8}.aval.a{fill:#2f855a}
.inbar{fill:#ffffff}
.leg{font-size:11px;fill:#718096}
.warn{background:#fffaf0;border:1px solid #ed8936;border-radius:8px;padding:10px 14px;color:#7b341e}
.note{color:#718096;font-size:12.5px}
"""


def render_html(ctx):
    b, a = ctx["before"], ctx["after"]
    ps = ctx["points"]
    ttft_b, ttft_a = b["st"]["ttft"], a["st"]["ttft"]
    d_mean = ttft_a["mean"] - ttft_b["mean"]
    d_pct = (ttft_a["mean"] / ttft_b["mean"] - 1) * 100
    benefits = [p for p in ps if p["kind"] == "benefit"]
    regressions = [p for p in ps if p["kind"] == "regression"]
    H = []
    title = ctx.get("title") or "TP 拆分收益对比报告：%s → %s" % (b["label"], a["label"])
    H.append("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>")
    H.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    H.append("<title>%s</title>" % esc(title))
    H.append("<style>%s</style></head><body><div class='wrap'>" % HTML_STYLE)
    H.append("<h1>%s</h1>" % esc(title))
    H.append("<p class='note'>kptools-tpsplit-report v0.1.0 ｜ %s ｜ 数据：TTFT 表格%s</p>"
             % (time.strftime("%Y-%m-%d %H:%M:%S"),
                " ＋ 打点原始日志" if ctx["has_logs"] else "（未提供原始日志，证据止步表格层）"))

    H.append("<h2>总览</h2>")

    def card(label, value, sub, cls=""):
        return ("<div class='card'><div class='k'>%s</div><div class='v %s'>%s</div>"
                "<div class='sub'>%s</div></div>" % (label, cls, value, sub))

    H.append("<div class='cards'>")
    H.append(card("TTFT mean",
                  "%s → %s ms" % (fmt_num(ttft_b["mean"]), fmt_num(ttft_a["mean"])),
                  "%s%%（%s ms）" % (fmt_num(d_pct), fmt_num(d_mean)),
                  "good" if d_mean < 0 else "bad"))
    for key in ("p90", "p99"):
        vb, va = ttft_b[key], ttft_a[key]
        H.append(card("TTFT %s" % key.upper(),
                      "%s → %s ms" % (fmt_num(vb), fmt_num(va)),
                      "%s%%（%s ms）" % (fmt_num((va / vb - 1) * 100 if vb else 0),
                                         fmt_num(va - vb)),
                      "good" if va < vb else "bad"))
    cons_max = max(ctx["quality"][0]["conservation_pct"],
                   ctx["quality"][1]["conservation_pct"])
    H.append(card("样本（before / after）",
                  "%d / %d" % (ctx["quality"][0]["n"], ctx["quality"][1]["n"]),
                  "守恒违例 %.1f%%" % cons_max))
    H.append("</div>")
    if not ctx["has_logs"] or ctx["confound"] or ctx["quality"][0]["conservation_pct"] > 5 \
            or ctx["quality"][1]["conservation_pct"] > 5 or ctx["quality"][0]["low_sample"] \
            or ctx["quality"][1]["low_sample"]:
        H.append("<div class='warn'><b>数据质量告警：</b>")
        msgs = []
        if not ctx["has_logs"]:
            msgs.append("未提供原始日志，证据链 L3 缺失（结论只到表格层）")
        for sq in ctx["quality"]:
            if sq["conservation_pct"] > 5:
                msgs.append("%s 守恒违例 %.1f%%" % (sq["side"], sq["conservation_pct"]))
            if sq["low_sample"]:
                msgs.append("%s 样本 %d 低于下限 %d" % (sq["side"], sq["n"], ctx["min_samples"]))
        if ctx["confound"]:
            msgs.append("input_len 分布不一致（L1=%.2f），均值对比有混杂，以分层结论为准"
                       % ctx["confound_l1"])
        H.append("；".join(esc(m) for m in msgs))
        H.append("</div>")

    H.append("<h2>TTFT 分位数对比</h2>")
    H.append(svg_grouped_bars(
        "TTFT 分位数（ms，before 蓝 / after 绿）",
        [("mean", ttft_b["mean"], ttft_a["mean"]), ("P50", ttft_b["p50"], ttft_a["p50"]),
         ("P90", ttft_b["p90"], ttft_a["p90"]), ("P99", ttft_b["p99"], ttft_a["p99"])]))
    H.append("<h2>分阶段 Δ（mean）</h2>")
    H.append(svg_stage_delta("各阶段 Δms（改善向左 / 回退向右）", ps or
                             [{"stage": s, "d_ms": a["st"][s]["mean"] - b["st"][s]["mean"]}
                              for s in STAGES
                              if b["st"][s]["n"] and a["st"][s]["n"]]))

    H.append("<h2>分阶段明细</h2>")
    H.append("<table><tr><th>阶段</th><th>分组</th><th>before</th><th>after</th>"
             "<th>Δms</th><th>Δ%</th><th>判定</th></tr>")
    for s in STAGES:
        bs, as_ = b["st"][s], a["st"][s]
        if bs["n"] == 0 or as_["n"] == 0:
            continue
        d_ms = as_["mean"] - bs["mean"]
        d_pct = (as_["mean"] / bs["mean"] - 1) * 100 if bs["mean"] else 0
        kind = classify(d_ms, d_pct, ctx["th"]["benefit_ms"], ctx["th"]["benefit_pct"],
                        ctx["th"]["regress_ms"])
        tag = {"benefit": "benefit", "regression": "regression", "stable": "stable",
               "missing": "stable"}[kind]
        name = {"benefit": "收益", "regression": "回退", "stable": "稳定", "missing": "缺数据"}[kind]
        H.append("<tr><td>%s<br><small>%s</small></td><td>%s</td><td>%s</td><td>%s</td>"
                 "<td>%s</td><td>%s</td><td><span class='tag %s'>%s</span></td></tr>" % (
                     esc(STAGE_CN[s]), s, STAGE_GROUP[s], fmt_num(bs["mean"]),
                     fmt_num(as_["mean"]), fmt_num(d_ms), fmt_num(d_pct), tag, name))
    H.append("</table>")

    for title, plist in (("收益点与证据链", benefits), ("风险与回退点", regressions)):
        H.append("<h2>%s</h2>" % esc(title))
        if not plist:
            H.append("<p class='note'>未检测到满足阈值的阶段。</p>")
            continue
        for i, p in enumerate(plist, 1):
            H.append("<details open><summary>%d. %s（%s：%s ms / %s%%，贡献 %s%%）</summary>" % (
                i, esc(STAGE_CN[p["stage"]]), p["stage"], fmt_num(p["d_ms"]),
                fmt_num(p["d_pct"]), fmt_num(abs(p["contrib_pct"]))))
            H.append("<table><tr><th>侧</th><th>mean</th><th>P50</th><th>P90</th>"
                     "<th>P99</th></tr>")
            for side in ("before", "after"):
                st = p[side]
                H.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                         % (side, fmt_num(st["mean"]), fmt_num(st["p50"]),
                            fmt_num(st["p90"]), fmt_num(st["p99"])))
            H.append("</table>")
            if any(e["lines"] for e in p["chain"]["l3"]):
                H.append("<p class='note'>日志摘录（best/median/worst 代表请求）：</p>")
                for e in p["chain"]["l3"]:
                    if not e["lines"]:
                        continue
                    for line in e["lines"]:
                        H.append("<p class='note'><code>%s</code> ｜ %s ｜ %s（%s=%s ms）</p>" % (
                            esc(line["ref"]), esc(e["side"]), esc(e["req"]), p["stage"],
                            fmt_num(e["stage_value_ms"])))
                        H.append("<pre>%s</pre>" % esc(line["text"]))
            else:
                H.append("<p class='note'>未回溯到原始日志——证据止步于表格层（L2）。</p>")
            H.append("</details>")

    H.append("<h2>分输入长度分层</h2>")
    H.append(svg_grouped_bars(
        "各 input_len 桶的 TTFT mean（ms）",
        [(label, ctx["b_buckets"][label]["mean"], ctx["a_buckets"][label]["mean"])
         for _, _, label in INLEN_BUCKETS
         if ctx["b_buckets"][label]["n"] and ctx["a_buckets"][label]["n"]]))
    H.append("<table><tr><th>桶</th><th>before n</th><th>before mean</th><th>after n</th>"
             "<th>after mean</th><th>Δms</th></tr>")
    for _, _, label in INLEN_BUCKETS:
        bb, ab = ctx["b_buckets"][label], ctx["a_buckets"][label]
        if not bb["n"] or not ab["n"]:
            H.append("<tr><td>%s</td><td>%d</td><td>—</td><td>%d</td><td>—</td><td>—</td></tr>"
                     % (esc(label), bb["n"], ab["n"]))
            continue
        H.append("<tr><td>%s</td><td>%d</td><td>%s</td><td>%d</td><td>%s</td><td>%s</td></tr>"
                 % (esc(label), bb["n"], fmt_num(bb["mean"]), ab["n"], fmt_num(ab["mean"]),
                    fmt_num(ab["mean"] - bb["mean"])))
    H.append("</table>")

    H.append("<h2>假设与未测项</h2><ul>"
             "<li>只比较 TTFT 及其拆解；吞吐/显存/长稳未在数据内。</li>"
             "<li>两次采集按 config 声明流量一致处理；实际不同则排队类阶段对比不成立。</li>"
             "<li><code>kv_xfer</code> 为打点回填口径（defer_finalize 设计，engine 侧恒 0）。</li></ul>")
    H.append("</div></body></html>")
    return "".join(H)


# ---------------- 主流程 ----------------

def load_side(d, warmup, min_samples):
    cfg = load_config(d)
    rows, meta = load_table(d, warmup)
    logs = scan_logs(d) if any(fn.lower().endswith(".log") for fn in os.listdir(d)) else None
    label = cfg.get("label") or os.path.basename(os.path.abspath(d))
    st = {s: stats_for(rows, s) for s in (["ttft"] + STAGES)}
    return {"dir": d, "cfg": cfg, "label": label, "rows": rows, "meta": meta,
            "logs": logs, "st": st}


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="TP 拆分收益对比分析")
    ap.add_argument("--before", required=True, help="before 数据目录")
    ap.add_argument("--after", required=True, help="after 数据目录")
    ap.add_argument("--out", required=True, help="输出前缀（生成 .md/.json/.html）")
    ap.add_argument("--title", default="")
    ap.add_argument("--benefit-ms", type=float, default=10.0,
                    help="收益点阈值：Δms ≤ -X（default 10）")
    ap.add_argument("--benefit-pct", type=float, default=15.0,
                    help="收益点阈值：Δ%% ≤ -X（default 15）")
    ap.add_argument("--regress-ms", type=float, default=5.0,
                    help="回退点阈值：Δms ≥ +X 或 Δ%% ≥ 2*benefit-pct（default 5）")
    ap.add_argument("--min-samples", type=int, default=100, help="每侧最少样本数")
    ap.add_argument("--warmup-seconds", type=float, default=0.0, help="掐头预热时间")
    args = ap.parse_args()

    for d in (args.before, args.after):
        if not os.path.isdir(d):
            sys.exit("数据目录不存在：%s" % d)
    b = load_side(args.before, args.warmup_seconds, args.min_samples)
    a = load_side(args.after, args.warmup_seconds, args.min_samples)
    if not b["rows"] or not a["rows"]:
        sys.exit("表格为空，无法对比")
    has_logs = all(s["logs"] and s["logs"]["files"] for s in (b, a))

    th = {"benefit_ms": args.benefit_ms, "benefit_pct": args.benefit_pct,
          "regress_ms": args.regress_ms}
    points = build_points(b["rows"], a["rows"], b["st"], a["st"],
                          b["logs"], a["logs"], th)
    quality = quality_report(b["rows"], a["rows"], b["meta"], a["meta"], args.min_samples)
    bs, as_ = bucket_share(b["rows"]), bucket_share(a["rows"])
    l1 = sum(abs(bs[k] - as_[k]) for k in bs)
    confound = l1 > 0.15
    ctx = {
        "before": b, "after": a, "points": points, "quality": quality, "th": th,
        "min_samples": args.min_samples, "confound": confound, "confound_l1": l1,
        "has_logs": has_logs,
        "b_buckets": inlen_buckets(b["rows"]), "a_buckets": inlen_buckets(a["rows"]),
        "b_inlen_mean": (sum(r["input_len"] for r in b["rows"]) / len(b["rows"])),
        "a_inlen_mean": (sum(r["input_len"] for r in a["rows"]) / len(a["rows"])),
        "title": args.title,
    }

    out_dir = os.path.dirname(os.path.abspath(args.out)) or "."
    os.makedirs(out_dir, exist_ok=True)
    md_path = args.out + ".md"
    html_path = args.out + ".html"
    json_path = args.out + ".json"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_md(ctx))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(ctx))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(sanitize_nan({
            "before": {"label": b["label"], "cfg": b["cfg"], "n": len(b["rows"]),
                       "stats": b["st"]},
            "after": {"label": a["label"], "cfg": a["cfg"], "n": len(a["rows"]),
                      "stats": a["st"]},
            "ttft_delta_mean_ms": a["st"]["ttft"]["mean"] - b["st"]["ttft"]["mean"],
            "points": [{k: p[k] for k in ("stage", "kind", "d_ms", "d_pct",
                                          "contrib_pct")} for p in points],
            "quality": quality, "confound_l1": l1, "has_logs": has_logs,
        }), f, ensure_ascii=False, indent=2, default=str)

    # 控制台摘要
    ttft_d = a["st"]["ttft"]["mean"] - b["st"]["ttft"]["mean"]
    log("=" * 62)
    log("对比完成：%s (n=%d) → %s (n=%d)"
        % (b["label"], len(b["rows"]), a["label"], len(a["rows"])))
    log("TTFT mean %s → %s ms (%s%%)" % (fmt_num(b["st"]["ttft"]["mean"]),
                                         fmt_num(a["st"]["ttft"]["mean"]),
                                         fmt_num((a["st"]["ttft"]["mean"] /
                                                  b["st"]["ttft"]["mean"] - 1) * 100)))
    for p in points[:8]:
        log("  [%s] %-14s %+9.1f ms (%+6.1f%%)  贡献 %5.1f%%"
            % (p["kind"][:4], p["stage"], p["d_ms"], p["d_pct"], p["contrib_pct"]))
    log("数据质量：" + ("OK" if has_logs and not ctx["confound"] and
                         all(q["conservation_pct"] <= 5 and not q["low_sample"]
                             for q in quality) else "有告警（见报告第1节）"
                         + ("" if has_logs else "，含缺原始日志")))
    log("输出：%s | %s | %s" % (md_path, html_path, json_path))


if __name__ == "__main__":
    main()
