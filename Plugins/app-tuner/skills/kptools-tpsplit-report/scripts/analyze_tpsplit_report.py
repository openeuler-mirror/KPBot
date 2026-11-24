#!/usr/bin/env python3
"""kptools-tpsplit-report：TP 拆分前后收益对比分析（TTFT 表格 + 打点日志 → 收益报告）。

输入：两套数据目录（before/after），每套含：
  ttft_table.csv   —— kptools-tpsplit-log-analysis 解析出的每请求 TTFT 分阶段表
  *.log            —— 打点原始日志（[TTFT][BREAKDOWN] v3 / [LB_DISPATCH] / [TTFT_KVXFER]
                        / [TTFT_DRAFT] / start_load_kv），可选；[SHM_DEQUEUE_BATCH] 等
                        batch 级行无 req 字段、不进证据链，不读入内存
  config.json      —— 运行态（tp/dp/模型/压测参数），可选；data_source 声明数据来源

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
# 排队类阶段：其耗时随压测负载变化，两侧压测参数不一致时无法归因于 TP 拆分
QUEUE_STAGES = ("sched_qwait", "fe_inputq")
# bench 对比时忽略的键（note 是说明文字，不是压测参数）
BENCH_IGNORE_KEYS = {"note"}
# 贡献度（阶段 Δ ÷ 总 ΔTTFT）的分母保护阈值：|总 ΔTTFT| 低于该值（ms）时记 None。
# 近零对冲下贡献度是无界大数（如 +10ms/-10ms 对冲、总 Δ≈0.01ms → 贡献 62069%），
# 恰为零时又会被误报成 0%——两者都不可信，与 safe_pct 对 Δ% 的保护同级。
CONTRIB_MIN_TTFT_D = 1.0


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


def safe_pct(new, base):
    """相对变化%；基线为 0/NaN 等不可计算时返回 None（缺失），不编造 0.0。"""
    if base is None or base != base or base == 0:
        return None
    val = (new / base - 1.0) * 100.0
    return None if val != val else val


def safe_contrib(d_ms, ttft_d):
    """贡献度；总 ΔTTFT 近零（对冲/无变化）时返回 None，不产出无界大数或假 0%。"""
    if ttft_d is None or ttft_d != ttft_d or abs(ttft_d) < CONTRIB_MIN_TTFT_D:
        return None
    val = d_ms / ttft_d * 100.0
    return None if val != val else val


# ---------------- 数据加载 ----------------

def load_config(d):
    p = os.path.join(d, "config.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f), p
        except Exception:
            return {}, p
    return {}, None


def load_table(d, warmup_s=0.0):
    """读 ttft_table.csv（容忍缺列/多列；缺的阶段按缺失处理；input_len 缺失保留 None）。"""
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
                il = r.get("input_len")
                row = {"request_id": req,
                       "input_len": (int(float(il)) if il not in (None, "") else None),
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
    return rows, {"path": base, "full_path": path, "missing_stages": missing,
                  "warmup_s": warmup_s}


def parse_ts(s):
    """解析 "YYYY-MM-DD HH:MM:SS[.fff]"，保留小数秒。

    strptime 会丢掉 %f、mktime 只给整秒——同一秒内 .100 与 .900 会折叠成同一时刻，
    sub-second 级的 --warmup-seconds 会误剔数据。这里手动分离小数部分再相加。
    """
    s = s.strip().replace(",", ".")
    frac = 0.0
    head, sep, tail = s.rpartition(".")
    if sep and tail.isdigit() and 0 < len(tail) <= 9:
        frac = float("0." + tail)
        s = head
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S")) + frac
    except ValueError:
        return 0.0


LOG_PATTERNS = {
    "BREAKDOWN": re.compile(r"\[TTFT\]\[BREAKDOWN\] v3 .*?\breq=(\S+)"),
    "LB_DISPATCH": re.compile(r"\[LB_DISPATCH\] .*?\breq=(\S+)"),
    "KVXFER": re.compile(r"\[TTFT_KVXFER\]\[put\] .*?\breq=(\S+)"),
    "TTFT_DRAFT": re.compile(r"\[TTFT_DRAFT\] .*?\breq=(\S+)"),
    "LOAD_KV": re.compile(r"start_load_kv .*?\breq=(\S+)"),
}


def scan_logs(d):
    """扫描目录下全部 .log，按 req 建索引（BREAKDOWN 优先；每 req 每类留一条）。

    只索引带 req= 的打点行；[SHM_DEQUEUE_BATCH] 等 batch 级行无 req 字段、进不了
    证据链，不读入内存——GB 级日志下这类最高频的行按行累积常驻是纯浪费。
    """
    idx = {tag: {} for tag in LOG_PATTERNS}
    files = [fn for fn in sorted(os.listdir(d)) if fn.lower().endswith(".log")]
    for fn in files:
        with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                for tag, pat in LOG_PATTERNS.items():
                    m = pat.search(line)
                    if m:
                        idx[tag].setdefault(m.group(1), (fn, lineno, line.rstrip("\n")))
                        break
    return {"files": files, "by_req": idx}


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
        sub = [r for r in rows
               if r["input_len"] is not None and lo <= r["input_len"] < hi]
        out[label] = stats_for(sub, "ttft")
    return out


def bucket_share(rows):
    n = len(rows) or 1
    return {label: sum(1 for r in rows if r["input_len"] is not None
                       and lo <= r["input_len"] < hi) / n
            for lo, hi, label in INLEN_BUCKETS}


# ---------------- 分析 ----------------

def classify(d_ms, d_pct, th_b_ms, th_pct, th_r_ms):
    """d_pct 为 None（基线为 0，相对变化不可计算）时只用绝对阈值判定，不编造 0%。"""
    if d_ms is None:
        return "missing"
    if d_ms <= -th_b_ms and (d_pct is None or d_pct <= -th_pct):
        return "benefit"
    if d_ms > 0 and (d_ms >= th_r_ms or (d_pct is not None and d_pct >= 2 * th_pct)):
        return "regression"
    return "stable"


def compute_kinds(b_st, a_st, th, bench_mismatch):
    """逐阶段判定（含「负载混杂」），收益点列表与 MD/HTML 两张明细表共用同一结果。"""
    kinds = {}
    for s in STAGES:
        bs, as_ = b_st[s], a_st[s]
        if bs["n"] == 0 or as_["n"] == 0:
            kinds[s] = "missing"
            continue
        d_ms = as_["mean"] - bs["mean"]
        d_pct = safe_pct(as_["mean"], bs["mean"])
        kind = classify(d_ms, d_pct, th["benefit_ms"], th["benefit_pct"],
                        th["regress_ms"])
        if bench_mismatch and s in QUEUE_STAGES and kind in ("benefit", "regression"):
            kind = "confounded"
        kinds[s] = kind
    return kinds


def build_points(b_rows, a_rows, b_st, a_st, logs_b, logs_a, th, kinds):
    """按 Δms 绝对值排序产出收益/回退点（判定取 compute_kinds），含三级证据链。"""
    points = []
    ttft_d = a_st["ttft"]["mean"] - b_st["ttft"]["mean"]
    for s in STAGES:
        if kinds[s] not in ("benefit", "regression"):
            continue
        b, a = b_st[s], a_st[s]
        d_ms = a["mean"] - b["mean"]
        d_pct = safe_pct(a["mean"], b["mean"])
        contrib = safe_contrib(d_ms, ttft_d)
        chain = build_chain(s, b_rows, a_rows, logs_b, logs_a)
        points.append({"stage": s, "kind": kinds[s], "d_ms": d_ms, "d_pct": d_pct,
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
             # 0 行可检（全量行缺阶段列或 ttft=0）→ 不可判定，标 None 而非 0% 假通过
             "conservation_pct": (100.0 * bad / checked) if checked else None,
             "low_sample": len(rows) < min_samples}
        out.append(q)
    return out


def find_zero_sides(b_st, a_st):
    """两侧均在场的阶段里，一侧整列恒 0 而另一侧 >0 —— 疑似打点未回填/口径不一致。"""
    out = []
    for s in STAGES:
        bs, as_ = b_st[s], a_st[s]
        if not bs["n"] or not as_["n"]:
            continue
        if bs["mean"] == 0 and as_["mean"] > 0:
            out.append({"stage": s, "zero_side": "before"})
        elif as_["mean"] == 0 and bs["mean"] > 0:
            out.append({"stage": s, "zero_side": "after"})
    return out


def inlen_check(b_rows, a_rows):
    """input_len 有效行统计：任一侧无有效值时分布一致性检查不可判定。"""
    b_valid = [r for r in b_rows if r["input_len"] is not None]
    a_valid = [r for r in a_rows if r["input_len"] is not None]
    out = {"before_valid": len(b_valid), "after_valid": len(a_valid),
           "before_coverage": (len(b_valid) / len(b_rows)) if b_rows else 0.0,
           "after_coverage": (len(a_valid) / len(a_rows)) if a_rows else 0.0}
    if not b_valid or not a_valid:
        out["status"] = "undeterminable"
    elif len(b_valid) < len(b_rows) or len(a_valid) < len(a_rows):
        out["status"] = "partial"
    else:
        out["status"] = "ok"
    return out, b_valid, a_valid


def detect_source(cfg):
    """数据来源：优先 config 显式 data_source，其次 bench.note 线索（synth 合成）。"""
    v = str(cfg.get("data_source", "")).lower()
    if "synth" in v:
        return "synthetic"
    if v in ("measured", "real", "field"):
        return "measured"
    if "synth" in str((cfg.get("bench") or {}).get("note", "")).lower():
        return "synthetic"
    return "measured"


def compare_bench(b_cfg, a_cfg):
    """对比两侧 config 的 bench 压测参数；只声明在一侧或值不同都算不一致。"""
    bb, ab = b_cfg.get("bench"), a_cfg.get("bench")
    if not isinstance(bb, dict) or not isinstance(ab, dict):
        return {"status": "unverified", "mismatch_keys": []}
    keys = (set(bb) | set(ab)) - BENCH_IGNORE_KEYS
    mismatch = sorted(k for k in keys if bb.get(k) != ab.get(k))
    return {"status": "mismatch" if mismatch else "ok", "mismatch_keys": mismatch}


def log_match_count(side):
    """表格请求在 BREAKDOWN 索引里匹配到的数量——日志证据是否真正可用（有文件≠有证据）。"""
    if not side["logs"] or not side["logs"]["files"]:
        return 0
    br = side["logs"]["by_req"].get("BREAKDOWN") or {}
    if not br:
        return 0
    return len({r["request_id"] for r in side["rows"]} & set(br))


def collect_flags(ctx):
    """统一汇总数据质量告警：MD 第 1 节、HTML 告警块、控制台摘要共用。"""
    flags = []
    for sq in ctx["quality"]:
        cpct = sq["conservation_pct"]
        if cpct is None:
            if sq["n"]:
                flags.append("%s 守恒校验不可判定（0 行可检：全量行缺阶段列或 ttft 为 0，"
                             "守恒质量未知）" % sq["side"])
        elif cpct > 5:
            flags.append("%s 守恒违例 %.1f%%（>5%%，结论需打折）" % (sq["side"], cpct))
        if sq["low_sample"]:
            flags.append("%s 样本量 %d 低于下限 %d（分位数不稳）"
                         % (sq["side"], sq["n"], ctx["min_samples"]))
    for z in ctx["zero_sides"]:
        flags.append("`%s` 一侧整列恒 0（%s 侧），疑似打点未回填或两侧口径不一致；"
                     "该阶段 Δ%% 不可计算，判定仅依据绝对阈值"
                     % (z["stage"], z["zero_side"]))
    if ctx["inlen"]["status"] == "undeterminable":
        flags.append("input_len 缺失或全空（before 有效 %d/%d，after 有效 %d/%d），"
                     "长度分布一致性不可判定，分层对比不可用"
                     % (ctx["inlen"]["before_valid"], len(ctx["before"]["rows"]),
                        ctx["inlen"]["after_valid"], len(ctx["after"]["rows"])))
    elif ctx["inlen"]["status"] == "partial":
        flags.append("input_len 部分缺失（before 覆盖 %.0f%%，after 覆盖 %.0f%%），"
                     "分布一致性按有效行计算"
                     % (ctx["inlen"]["before_coverage"] * 100,
                        ctx["inlen"]["after_coverage"] * 100))
    elif ctx["confound"]:
        flags.append("两侧 input_len 分布不一致（L1 距离 %.2f > 0.15），原始均值对比被混杂，"
                     "以第 7 节分层结论为准" % ctx["confound_l1"])
    if not ctx["has_logs"]:
        if ctx["log_unmatched_sides"]:
            flags.append("%s 侧有 .log 文件但没有表格请求匹配到 BREAKDOWN 行"
                         "（日志与表格可能非同源），证据链 L3 缺失（结论只到表格层）"
                         % "、".join(ctx["log_unmatched_sides"]))
        else:
            flags.append("输入未提供原始日志，证据链 L3 缺失（结论只到表格层）")
    if ctx["bench"]["status"] == "mismatch":
        flags.append("两侧压测参数不一致（%s），排队类阶段（sched_qwait、fe_inputq）"
                     "已暂停收益归因（判「负载混杂」）"
                     % "、".join(ctx["bench"]["mismatch_keys"]))
    if ctx["synthetic"]:
        flags.append("本次输入为仿真合成数据（synthetic，非现场实测），"
                     "数字仅供演示与回归，不能当作现场优化结果")
    if ctx["source_mismatch"]:
        flags.append("两侧数据来源不一致（before=%s，after=%s），对比结论不可信"
                     % (ctx["b_source"], ctx["a_source"]))
    return flags


# ---------------- Markdown 渲染 ----------------

KIND_CN = {"benefit": "收益", "regression": "回退", "stable": "稳定",
           "missing": "缺数据", "confounded": "负载混杂"}


def fmt_ms(v, digits=1):
    if v is None or v != v:
        return "—"
    return ("%%.%df" % digits) % v


def fmt_pct_cell(v):
    """表格里的 Δ% 单元格；基线为 0（不可计算）显式标注而不是假 0。"""
    return "—（基线为 0）" if v is None else fmt_ms(v)


def render_md(ctx):
    L = []
    b, a = ctx["before"], ctx["after"]
    title = ctx.get("title") or "TP 拆分收益对比报告：%s → %s" % (b["label"], a["label"])
    ps = ctx["points"]
    ttft_b, ttft_a = b["st"]["ttft"], a["st"]["ttft"]
    d_mean = ttft_a["mean"] - ttft_b["mean"]
    benefits = [p for p in ps if p["kind"] == "benefit"]
    regressions = [p for p in ps if p["kind"] == "regression"]
    q = ctx["quality"]

    def pct_frag(base_v, new_v):
        dp = safe_pct(new_v, base_v)
        return "%s%%" % fmt_ms(dp, 1) if dp is not None else "Δ% 不可算（基线为 0）"

    def contrib_frag(v):
        return ("贡献 %s%%" % fmt_ms(abs(v))) if v is not None \
            else "贡献不可算（总 ΔTTFT≈0）"

    L.append("# %s" % title)
    L.append("")
    has_logs = ctx["has_logs"]
    if ctx["source_mismatch"]:
        src_txt = "数据性质：**不一致**（before=%s，after=%s）" % (
            ctx["b_source"], ctx["a_source"])
    elif ctx["synthetic"]:
        src_txt = "数据性质：**仿真数据**（synthetic，非现场实测）"
    else:
        src_txt = "数据性质：现场实测"
    L.append("> 工具：kptools-tpsplit-report v0.1.0 ｜ 生成：%s ｜ 数据来源：TTFT 表格"
             "（kptools-tpsplit-log-analysis 口径）%s ｜ %s"
             % (time.strftime("%Y-%m-%d %H:%M:%S"),
                "＋ 打点原始日志" if has_logs else "（未匹配到原始日志，证据止步表格层）",
                src_txt))
    L.append("")

    # 1. 结论
    L.append("## 1. 结论")
    L.append("")
    verdict = "净收益" if d_mean < 0 else "净回退"
    L.append("- **总收益：TTFT mean %s→%s ms（%s，%s ms）**，P90 %s→%s ms（%s），"
             "P99 %s→%s ms（%s）。判定：%s。"
             % (fmt_ms(ttft_b["mean"]), fmt_ms(ttft_a["mean"]),
                pct_frag(ttft_b["mean"], ttft_a["mean"]), fmt_ms(d_mean),
                fmt_ms(ttft_b["p90"]), fmt_ms(ttft_a["p90"]),
                pct_frag(ttft_b["p90"], ttft_a["p90"]),
                fmt_ms(ttft_b["p99"]), fmt_ms(ttft_a["p99"]),
                pct_frag(ttft_b["p99"], ttft_a["p99"]),
                verdict))
    if benefits:
        top = ", ".join("%s（%s ms / %s，%s）" % (
            STAGE_CN[p["stage"]], fmt_ms(p["d_ms"]),
            ("%s%%" % fmt_ms(p["d_pct"])) if p["d_pct"] is not None
            else "Δ% 不可算（基线为 0）",
            contrib_frag(p["contrib_pct"])) for p in benefits[:3])
        L.append("- **主要收益点**（按 |Δms| 排序）：%s。" % top)
    if regressions:
        top = ", ".join("%s（+%s ms / %s）" % (
            STAGE_CN[p["stage"]], fmt_ms(p["d_ms"]),
            ("%s%%" % fmt_ms(p["d_pct"])) if p["d_pct"] is not None
            else "Δ% 不可算（基线为 0）") for p in regressions[:3])
        L.append("- **回退/风险点**：%s。量级与主要收益相比是否可接受，见第 6 节。" % top)
    flags = ctx["flags"]
    L.append("- **数据质量**：%s。" % ("；".join(flags) if flags else
                                       "两侧守恒、样本量、input_len 分布、日志证据、"
                                       "压测参数均通过检查，无混杂信号"))
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

    def cons_cell(sq):
        if sq["conservation_checked"] == 0:
            return "不可判定（0 行可检）"
        return "%d/%d（%.1f%%）" % (sq["conservation_bad"], sq["conservation_checked"],
                                   sq["conservation_pct"])

    L.append("| 守恒违例 | %s | %s |" % (cons_cell(q[0]), cons_cell(q[1])))
    miss_b = [x for x in q[0]["missing_stages"] if x]
    miss_a = [x for x in q[1]["missing_stages"] if x]
    if miss_b or miss_a:
        L.append("| 缺失阶段列 | %s | %s |" % ("、".join(miss_b) or "—",
                                              "、".join(miss_a) or "—"))
    L.append("| 原始日志 | %s | %s |" % (
        "、".join(b["logs"]["files"]) if b["logs"] and b["logs"]["files"] else "无",
        "、".join(a["logs"]["files"]) if a["logs"] and a["logs"]["files"] else "无"))
    L.append("| 日志匹配请求 | %d/%d | %d/%d |" % (
        ctx["log_matches"]["before"], len(b["rows"]),
        ctx["log_matches"]["after"], len(a["rows"])))
    L.append("| 数据来源 | %s | %s |" % (ctx["b_source"], ctx["a_source"]))
    L.append("")
    L.append("数字口径：表格为**%s**（打点解析），Δ 与贡献度为**计算**值；单位 ms。"
             % ("仿真数据（synthetic，不能当作现场结果）" if ctx["synthetic"]
                else "现场实测"))
    L.append("")

    # 3. TTFT 总览
    L.append("## 3. TTFT 总览对比")
    L.append("")
    L.append("| 分位数 | before | after | Δms | Δ% |")
    L.append("| --- | --- | --- | --- | --- |")
    for k, name in (("mean", "mean"), ("p50", "P50"), ("p90", "P90"), ("p99", "P99"),
                    ("max", "max")):
        d = ttft_a[k] - ttft_b[k]
        L.append("| %s | %s | %s | %s | %s |" % (name, fmt_ms(ttft_b[k]), fmt_ms(ttft_a[k]),
                                                  fmt_ms(d),
                                                  fmt_pct_cell(safe_pct(ttft_a[k], ttft_b[k]))))
    L.append("")

    # 4. 分阶段拆解
    L.append("## 4. 分阶段拆解对比（按 mean）")
    L.append("")
    L.append("| 阶段 | 分组 | before mean | after mean | Δms | Δ% | 贡献度* | 判定 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for s in STAGES:
        bs, as_ = ctx["before"]["st"][s], ctx["after"]["st"][s]
        if bs["n"] == 0 or as_["n"] == 0:
            L.append("| %s | %s | — | — | — | — | — | 缺数据 |"
                      % (STAGE_CN[s], STAGE_GROUP[s]))
            continue
        d_ms = as_["mean"] - bs["mean"]
        kind = ctx["kinds"][s]
        contrib = safe_contrib(d_ms, d_mean)
        contrib_cell = ("%s%%" % fmt_ms(contrib)) if contrib is not None \
            else "—（总 ΔTTFT≈0）"
        L.append("| %s（%s） | %s | %s | %s | %s | %s | %s | %s |" % (
            STAGE_CN[s], s, STAGE_GROUP[s], fmt_ms(bs["mean"]), fmt_ms(as_["mean"]),
            fmt_ms(d_ms), fmt_pct_cell(safe_pct(as_["mean"], bs["mean"])),
            contrib_cell, KIND_CN[kind]))
    if any(k == "confounded" for k in ctx["kinds"].values()):
        L.append("")
        L.append("> ⚠️ 两侧压测参数不一致（%s），排队类阶段（`sched_qwait`、`fe_inputq`）"
                 "的 Δ 无法区分「TP 拆分」与「负载变化」，已暂停其收益/回退归因"
                 "（判「负载混杂」）。" % "、".join(ctx["bench"]["mismatch_keys"]))
    L.append("")
    L.append("*贡献度 = 该阶段 Δms ÷ TTFT Δms（改善为负时，负贡献 = 拉低改善；总和 ≈ 100%）。"
             "Δ% 为「—（基线为 0）」时表示基线均值为 0、相对变化不可计算，该阶段判定仅依据绝对阈值；"
             "贡献度为「—（总 ΔTTFT≈0）」时表示总改善近零（阶段间对冲），"
             "此时贡献比是无界大数，记不可计算。")
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
    if ctx["inlen"]["status"] == "undeterminable":
        L.append("> ⚠️ input_len 列缺失或全空（before 有效 %d/%d，after 有效 %d/%d），"
                 "长度分布一致性**不可判定**，分层对比不可用——第 3 节的均值差可能含"
                 "长度混杂，下结论前需补齐 input_len 再跑。"
                 % (ctx["inlen"]["before_valid"], len(b["rows"]),
                    ctx["inlen"]["after_valid"], len(a["rows"])))
        L.append("")
    elif ctx["confound"]:
        L.append("> ⚠️ 两侧 input_len 分布不一致（before 均值 %d vs after 均值 %d token），"
                 "第 3 节的原始均值差有混杂成分；本节按桶分层后的对比才是干净结论。"
                 % (ctx["b_inlen_mean"], ctx["a_inlen_mean"]))
        L.append("")
    elif ctx["inlen"]["status"] == "partial":
        L.append("> input_len 部分缺失（before 覆盖 %.0f%%，after 覆盖 %.0f%%），"
                 "本节分层对比基于有效行。"
                 % (ctx["inlen"]["before_coverage"] * 100,
                    ctx["inlen"]["after_coverage"] * 100))
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
        L.append("| %s | %d | %s | %d | %s | %s | %s |" % (
            label, bb["n"], fmt_ms(bb["mean"]), ab["n"], fmt_ms(ab["mean"]),
            fmt_ms(d), fmt_pct_cell(safe_pct(ab["mean"], bb["mean"]))))
    L.append("")

    # 8. 假设与未测项
    L.append("## 8. 假设与未测项")
    L.append("")
    L.append("- 本报告只比较 TTFT 及其阶段拆解；**吞吐、显存、长稳（数小时漂移）未在数据内**，"
             "下结论前需另行验证。")
    if ctx["bench"]["status"] == "unverified":
        L.append("- 两侧 config 未提供 bench 压测参数，流量一致性**未核对**，按声明一致处理；"
                 "若实际压测脚本/并发参数不同，排队类阶段（`sched_qwait`、`fe_inputq`）"
                 "的对比不成立。")
    elif ctx["bench"]["status"] == "mismatch":
        L.append("- 两侧压测参数不一致（%s），排队类阶段（`sched_qwait`、`fe_inputq`）已"
                 "**暂停收益归因**——这部分 Δ 更可能来自负载变化而非 TP 拆分，"
                 "需要同参数重采后再对比。"
                 % "、".join(ctx["bench"]["mismatch_keys"]))
    else:
        L.append("- 两次采集的流量形态按 config 声明一致处理；若实际压测脚本/并发参数不同，"
                 "排队类阶段（`sched_qwait`、`fe_inputq`）的对比不成立。")
    L.append("- `kv_xfer` 依据打点回填值（pool_worker/kv_transfer 日志），"
             "`defer_finalize` 下 engine 侧 load_kv 恒 0 属已知口径；脚本会对"
             "「一侧整列恒 0」的阶段自动告警（疑似未回填/口径不一致）。")
    if ctx["synthetic"]:
        L.append("- 本次输入为**仿真合成数据**（synthetic），收益数字仅用于演示/回归，"
                 "不能当作现场优化结果。")
    if not ctx["has_logs"]:
        L.append("- 本次输入%s，全部证据止步于表格层（L2），"
                 "未能回溯 BREAKDOWN 原始行（L3）；拿到可匹配的日志后重跑即可补全证据链。"
                 % ("缺少原始日志" if not ctx["log_unmatched_sides"]
                    else "%s 侧日志未匹配到表格请求（日志与表格可能非同源）"
                         % "、".join(ctx["log_unmatched_sides"])))
    L.append("")
    return "\n".join(L)


def emit_point(L, p, ctx):
    d_ms_str = ("%+.1f" % p["d_ms"]) if p["d_ms"] > 0 else fmt_ms(p["d_ms"])
    dp_str = ("%s%%" % fmt_ms(p["d_pct"])) if p["d_pct"] is not None \
        else "Δ% 不可算（基线为 0）"
    contrib_str = ("占 TTFT 变化的 %s%%" % fmt_ms(abs(p["contrib_pct"]))) \
        if p["contrib_pct"] is not None else "贡献占比不可算（总 ΔTTFT≈0）"
    L.append("- **L1 Claim**：%s mean %s → %s ms（%s ms，%s，%s）；"
             "P90 %s → %s ms。"
             % (STAGE_CN[p["stage"]], fmt_ms(p["before"]["mean"]), fmt_ms(p["after"]["mean"]),
                d_ms_str, dp_str, contrib_str,
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
    if v is None or v != v:
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
.tag.stable{background:#e2e8f0;color:#4a5568}.tag.confounded{background:#fefcbf;color:#744210}
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
    d_pct = safe_pct(ttft_a["mean"], ttft_b["mean"])
    benefits = [p for p in ps if p["kind"] == "benefit"]
    regressions = [p for p in ps if p["kind"] == "regression"]
    H = []
    title = ctx.get("title") or "TP 拆分收益对比报告：%s → %s" % (b["label"], a["label"])
    if ctx["source_mismatch"]:
        src_note = "数据性质：不一致（before=%s，after=%s）" % (
            ctx["b_source"], ctx["a_source"])
    elif ctx["synthetic"]:
        src_note = "数据性质：<b>仿真数据</b>（synthetic，非现场实测）"
    else:
        src_note = "数据性质：现场实测"
    H.append("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>")
    H.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    H.append("<title>%s</title>" % esc(title))
    H.append("<style>%s</style></head><body><div class='wrap'>" % HTML_STYLE)
    H.append("<h1>%s</h1>" % esc(title))
    H.append("<p class='note'>kptools-tpsplit-report v0.1.0 ｜ %s ｜ 数据：TTFT 表格%s"
             " ｜ %s</p>"
             % (time.strftime("%Y-%m-%d %H:%M:%S"),
                " ＋ 打点原始日志" if ctx["has_logs"] else "（未匹配到原始日志，证据止步表格层）",
                src_note))

    H.append("<h2>总览</h2>")

    def card(label, value, sub, cls=""):
        return ("<div class='card'><div class='k'>%s</div><div class='v %s'>%s</div>"
                "<div class='sub'>%s</div></div>" % (label, cls, value, sub))

    def sub_pct(base_v, new_v, d_ms):
        dp = safe_pct(new_v, base_v)
        head = ("%s%%" % fmt_num(dp)) if dp is not None else "Δ% 不可算（基线为 0）"
        return "%s（%s ms）" % (head, fmt_num(d_ms))

    H.append("<div class='cards'>")
    H.append(card("TTFT mean",
                  "%s → %s ms" % (fmt_num(ttft_b["mean"]), fmt_num(ttft_a["mean"])),
                  sub_pct(ttft_b["mean"], ttft_a["mean"], d_mean),
                  "good" if d_mean < 0 else "bad"))
    for key in ("p90", "p99"):
        vb, va = ttft_b[key], ttft_a[key]
        H.append(card("TTFT %s" % key.upper(),
                      "%s → %s ms" % (fmt_num(vb), fmt_num(va)),
                      sub_pct(vb, va, va - vb),
                      "good" if va < vb else "bad"))
    cps = [sq["conservation_pct"] for sq in ctx["quality"]]
    cons_txt = ("守恒校验不可判定（0 行可检）" if any(c is None for c in cps)
                else "守恒违例 %.1f%%" % max(cps))
    H.append(card("样本（before / after）",
                  "%d / %d" % (ctx["quality"][0]["n"], ctx["quality"][1]["n"]),
                  cons_txt))
    H.append("</div>")
    flags = ctx["flags"]
    if flags:
        H.append("<div class='warn'><b>数据质量告警：</b>%s</div>"
                 % "；".join(esc(m) for m in flags))

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
        kind = ctx["kinds"][s]
        tag = {"benefit": "benefit", "regression": "regression", "stable": "stable",
               "confounded": "confounded", "missing": "stable"}[kind]
        H.append("<tr><td>%s<br><small>%s</small></td><td>%s</td><td>%s</td><td>%s</td>"
                  "<td>%s</td><td>%s</td><td><span class='tag %s'>%s</span></td></tr>" % (
                      esc(STAGE_CN[s]), s, STAGE_GROUP[s], fmt_num(bs["mean"]),
                      fmt_num(as_["mean"]), fmt_num(as_["mean"] - bs["mean"]),
                      fmt_num(safe_pct(as_["mean"], bs["mean"])), tag, KIND_CN[kind]))
    H.append("</table>")
    if any(k == "confounded" for k in ctx["kinds"].values()):
        H.append("<p class='note'>两侧压测参数不一致（%s）：排队类阶段"
                 "（sched_qwait、fe_inputq）已暂停收益归因（判「负载混杂」）。</p>"
                 % esc("、".join(ctx["bench"]["mismatch_keys"])))

    for title, plist in (("收益点与证据链", benefits), ("风险与回退点", regressions)):
        H.append("<h2>%s</h2>" % esc(title))
        if not plist:
            H.append("<p class='note'>未检测到满足阈值的阶段。</p>")
            continue
        for i, p in enumerate(plist, 1):
            dp_h = ("%s%%" % fmt_num(p["d_pct"])) if p["d_pct"] is not None \
                else "Δ% 不可算（基线为 0）"
            contrib_h = ("贡献 %s%%" % fmt_num(abs(p["contrib_pct"]))) \
                if p["contrib_pct"] is not None else "贡献不可算（总 ΔTTFT≈0）"
            H.append("<details open><summary>%d. %s（%s：%s ms / %s，%s）</summary>" % (
                i, esc(STAGE_CN[p["stage"]]), p["stage"], fmt_num(p["d_ms"]),
                dp_h, contrib_h))
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
    if ctx["inlen"]["status"] == "undeterminable":
        H.append("<p class='note'>input_len 列缺失或全空（before 有效 %d/%d，after 有效 "
                  "%d/%d）：长度分布一致性不可判定，分层对比不可用。</p>"
                  % (ctx["inlen"]["before_valid"], len(b["rows"]),
                     ctx["inlen"]["after_valid"], len(a["rows"])))
    elif ctx["confound"]:
        H.append("<div class='warn'>两侧 input_len 分布不一致（L1=%.2f > 0.15），"
                 "原始均值对比有混杂，以本节分层结论为准。</div>" % ctx["confound_l1"])
    elif ctx["inlen"]["status"] == "partial":
        H.append("<p class='note'>input_len 部分缺失（before 覆盖 %.0f%%，after 覆盖 "
                  "%.0f%%），分层对比基于有效行。</p>"
                  % (ctx["inlen"]["before_coverage"] * 100,
                     ctx["inlen"]["after_coverage"] * 100))
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

    if ctx["bench"]["status"] == "unverified":
        bench_li = ("<li>两侧 config 未提供 bench 压测参数，流量一致性未核对，"
                    "按声明一致处理；实际不同则排队类阶段对比不成立。</li>")
    elif ctx["bench"]["status"] == "mismatch":
        bench_li = ("<li>两侧压测参数不一致（%s）：排队类阶段已"
                    "<b>暂停收益归因</b>（判「负载混杂」），需同参数重采后再对比。</li>"
                    % esc("、".join(ctx["bench"]["mismatch_keys"])))
    else:
        bench_li = ("<li>两次采集按 config 声明流量一致处理；"
                    "实际不同则排队类阶段对比不成立。</li>")
    synth_li = ("<li>本次输入为<b>仿真合成数据</b>（synthetic），收益数字仅用于演示/回归，"
                "不能当作现场优化结果。</li>" if ctx["synthetic"] else "")
    H.append("<h2>假设与未测项</h2><ul>"
              "<li>只比较 TTFT 及其拆解；吞吐/显存/长稳未在数据内。</li>"
              "%s"
              "<li><code>kv_xfer</code> 为打点回填口径（defer_finalize 设计，engine 侧恒 0）；"
              "一侧整列恒 0 的阶段会被自动告警（疑似未回填/口径不一致）。</li>%s</ul>"
              % (bench_li, synth_li))
    H.append("</div></body></html>")
    return "".join(H)


# ---------------- 主流程 ----------------

def load_side(d, warmup, min_samples):
    cfg, cfg_path = load_config(d)
    rows, meta = load_table(d, warmup)
    log_files = [fn for fn in sorted(os.listdir(d)) if fn.lower().endswith(".log")]
    logs = scan_logs(d) if log_files else None
    label = cfg.get("label") or os.path.basename(os.path.abspath(d))
    st = {s: stats_for(rows, s) for s in (["ttft"] + STAGES)}
    # 本次实际读取的全部输入文件：供输出路径冲突检测（防 --out 覆盖原件）
    input_files = [meta["full_path"]] + ([cfg_path] if cfg_path else []) \
        + [os.path.join(d, fn) for fn in log_files]
    return {"dir": d, "cfg": cfg, "label": label, "rows": rows, "meta": meta,
            "logs": logs, "st": st, "input_files": input_files}


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

    # ---- 打开任何输出文件前的校验：避免中途异常留下半成品报告/覆盖输入原件 ----
    bm = b["st"]["ttft"]["mean"]
    if bm != bm or bm == 0:
        sys.exit("before 侧 TTFT 均值为 %s（全部请求 ttft_ms=0 或非数值？）："
                 "相对变化不可计算，拒绝生成对比报告——请检查表格数据" % bm)
    out_dir = os.path.dirname(os.path.abspath(args.out)) or "."
    os.makedirs(out_dir, exist_ok=True)
    md_path = args.out + ".md"
    html_path = args.out + ".html"
    json_path = args.out + ".json"

    def norm(p):
        return os.path.normcase(os.path.normpath(os.path.abspath(p)))

    targets = {norm(p) for p in (md_path, html_path, json_path)}
    for inp in b["input_files"] + a["input_files"]:
        if norm(inp) in targets:
            sys.exit("输出路径与输入文件冲突：%s\n--out 的 .md/.html/.json 会覆盖刚读过的"
                     "输入原件，已拒绝执行——请更换 --out 前缀" % inp)

    th = {"benefit_ms": args.benefit_ms, "benefit_pct": args.benefit_pct,
          "regress_ms": args.regress_ms}
    bench = compare_bench(b["cfg"], a["cfg"])
    kinds = compute_kinds(b["st"], a["st"], th, bench["status"] == "mismatch")
    points = build_points(b["rows"], a["rows"], b["st"], a["st"],
                          b["logs"], a["logs"], th, kinds)
    quality = quality_report(b["rows"], a["rows"], b["meta"], a["meta"], args.min_samples)
    zero_sides = find_zero_sides(b["st"], a["st"])
    inlen, b_valid, a_valid = inlen_check(b["rows"], a["rows"])
    if inlen["status"] == "undeterminable":
        confound, l1 = False, None
    else:
        shares_b, shares_a = bucket_share(b_valid), bucket_share(a_valid)
        l1 = sum(abs(shares_b[k] - shares_a[k]) for k in shares_b)
        confound = l1 > 0.15

    def side_has_files(s):
        return bool(s["logs"] and s["logs"]["files"])

    log_matches = {"before": log_match_count(b), "after": log_match_count(a)}
    unmatched_sides = [name for name, s in (("before", b), ("after", a))
                       if side_has_files(s) and log_matches[name] == 0]
    has_logs = (side_has_files(b) and side_has_files(a)
                and log_matches["before"] > 0 and log_matches["after"] > 0)
    b_source, a_source = detect_source(b["cfg"]), detect_source(a["cfg"])
    synthetic = b_source == "synthetic" or a_source == "synthetic"
    source_mismatch = b_source != a_source

    ctx = {
        "before": b, "after": a, "points": points, "quality": quality, "th": th,
        "min_samples": args.min_samples, "confound": confound, "confound_l1": l1,
        "inlen": inlen, "kinds": kinds, "zero_sides": zero_sides, "bench": bench,
        "log_matches": log_matches, "log_unmatched_sides": unmatched_sides,
        "has_logs": has_logs, "b_source": b_source, "a_source": a_source,
        "synthetic": synthetic, "source_mismatch": source_mismatch,
        "b_buckets": inlen_buckets(b["rows"]), "a_buckets": inlen_buckets(a["rows"]),
        "b_inlen_mean": (sum(r["input_len"] for r in b_valid) / len(b_valid)) if b_valid else None,
        "a_inlen_mean": (sum(r["input_len"] for r in a_valid) / len(a_valid)) if a_valid else None,
        "title": args.title,
    }
    ctx["flags"] = collect_flags(ctx)

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
            "stage_kinds": kinds,
            "quality": quality, "confound_l1": l1, "has_logs": has_logs,
            "log_matches": log_matches,
            "data_source": {"before": b_source, "after": a_source},
            "bench": bench, "inlen": inlen, "zero_sides": zero_sides,
            "flags": ctx["flags"],
        }), f, ensure_ascii=False, indent=2, default=str)

    # 控制台摘要
    log("=" * 62)
    log("对比完成：%s (n=%d) → %s (n=%d)"
        % (b["label"], len(b["rows"]), a["label"], len(a["rows"])))
    dp = safe_pct(a["st"]["ttft"]["mean"], b["st"]["ttft"]["mean"])
    log("TTFT mean %s → %s ms (%s)"
        % (fmt_num(b["st"]["ttft"]["mean"]), fmt_num(a["st"]["ttft"]["mean"]),
           ("%s%%" % fmt_num(dp)) if dp is not None else "Δ% 不可算"))
    for p in points[:8]:
        dp_s = ("%+6.1f%%" % p["d_pct"]) if p["d_pct"] is not None else "   —  "
        contrib_s = ("%5.1f%%" % p["contrib_pct"]) if p["contrib_pct"] is not None \
            else "  —  "
        log("  [%s] %-14s %+9.1f ms (%s)  贡献 %s"
            % (p["kind"][:4], p["stage"], p["d_ms"], dp_s, contrib_s))
    if ctx["flags"]:
        log("数据质量：有告警 %d 条（详见报告第 1 节）：%s"
            % (len(ctx["flags"]), "；".join(ctx["flags"])))
    else:
        log("数据质量：OK（守恒、样本量、input_len 分布、日志证据、压测参数均通过）")
    log("输出：%s | %s | %s" % (md_path, html_path, json_path))


if __name__ == "__main__":
    main()
