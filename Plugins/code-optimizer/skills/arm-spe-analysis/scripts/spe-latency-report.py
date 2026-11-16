#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# ARM SPE 深度时延分析与报告生成
# arm-spe-analysis skill - scripts/spe-latency-report.py
# 用途：读取标准 CSV（spe-csv.sh 或任意开源 spe-parser 输出），生成
#       - 指令时延分布（直方图 + P50/P90/P99/P999）
#       - 缓存命中层级（data_src 分布 + 各层级平均时延）
#       - 指令追逐（逐 PC / 逐函数：采样数与加权时延双热点）
#       - 高时延指令根因分析（时延 × 数据源 × TLB × 远端 × 分支）
#       - 门禁式指标判定表（SEVERE/MODERATE/MILD/OK）
#       - 可选：与基线 CSV 前后对比
# 用法：
#   python3 spe-latency-report.py samples.csv -o report.md
#   python3 spe-latency-report.py samples.csv --json report.json
#   python3 spe-latency-report.py samples.csv --baseline before.csv -o diff.md
#   python3 spe-latency-report.py samples.csv --min-lat 500 -o tail.md
# =============================================================================

import sys
import os
import csv
import json
import argparse
from collections import OrderedDict

# ---------------------------------------------------------------------------
# 门禁式判定阈值（沿用 spe-hotspot.sh 既有阈值，新增时延/远端/TLB 阈值）
# ---------------------------------------------------------------------------
GRADES = OrderedDict([
    ("l1_miss",        [(30, "SEVERE"), (10, "MODERATE"), (5, "MILD")]),
    ("llc_miss",       [(30, "SEVERE"), (10, "MODERATE"), (5, "MILD")]),
    ("branch_mispred", [(20, "SEVERE"), (5, "MODERATE"), (1, "MILD")]),
    ("tlb_miss",       [(10, "SEVERE"), (5, "MODERATE"), (2, "MILD")]),
    ("remote_ratio",   [(20, "SEVERE"), (10, "MODERATE"), (5, "MILD")]),
    ("p99_latency",    [(1000, "SEVERE"), (300, "MODERATE"), (100, "MILD")]),
    ("tail_ratio",     [(10, "SEVERE"), (5, "MODERATE"), (1, "MILD")]),
])

DATA_SRC_LEVELS = OrderedDict([
    ("L1", "L1 Cache"),
    ("L2", "L2 Cache"),
    ("L3", "L3/LLC Cache"),
    ("PEER", "Peer Cache (同 socket)"),
    ("REMOTE", "Remote Cache/内存 (跨 NUMA)"),
    ("DRAM", "DRAM 内存"),
    ("UNKNOWN", "未知来源"),
])


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    k = int((len(s) - 1) * p / 100.0 + 0.5)
    k = max(0, min(k, len(s) - 1))
    return s[k]


def pct(n, d):
    return (n * 100.0 / d) if d else 0.0


def grade(value, thresholds):
    for thr, name in thresholds:
        if value > thr:
            return name
    return "OK"


def class_data_src(raw):
    """将 data_src 字符串/数值规整为层级。"""
    if not raw:
        return "UNKNOWN"
    s = raw.strip().lower()
    if not s:
        return "UNKNOWN"
    # 数值编码（ARM SPE Data Source packet: 0..7）
    if s.isdigit():
        return {1: "L1", 2: "L2", 3: "L3", 4: "PEER",
                5: "REMOTE", 6: "DRAM", 7: "REMOTE"}.get(int(s), "UNKNOWN")
    if "l1" in s:
        return "L1"
    if "l2" in s:
        return "L2"
    if "l3" in s or "llc" in s:
        return "L3"
    if "peer" in s:
        return "PEER"
    if "remote" in s or "rem_" in s or "rmt" in s:
        return "REMOTE"
    if "ram" in s or "dram" in s or "mem" in s or "drmm" in s:
        return "DRAM"
    return "UNKNOWN"


def class_op(event):
    """按事件类型粗分指令类别（load/store/branch/other）。"""
    e = event.lower()
    if "branch" in e or "mispred" in e:
        return "branch"
    if "store" in e or e.startswith("st"):
        return "store"
    if any(k in e for k in ("ld", "l1d", "l2", "llc", "tlb", "remote",
                            "access", "miss", "load")):
        return "load"
    return "other"


class SpeCSV(object):
    REQUIRED = ("comm", "pid", "cpu", "time", "event", "ip", "sym",
                "dso", "weight", "data_src", "addr")

    def __init__(self, path):
        self.rows = []
        self.total = 0
        with open(path, "r", newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []
            missing = [c for c in self.REQUIRED if c not in header]
            if missing:
                raise ValueError(
                    "CSV 缺少必需列 %s（标准 schema 见 spe-csv.sh --schema）"
                    % ",".join(missing))
            for r in reader:
                self.rows.append(r)
        self.total = len(self.rows)
        # 事件计数
        self.ev = {
            "l1d_miss": 0, "l1d_access": 0, "llc_miss": 0, "llc_access": 0,
            "tlb_access": 0, "tlb_miss": 0, "branch_miss": 0,
            "remote_access": 0,
        }
        for r in self.rows:
            e = (r.get("event") or "").lower()
            if "l1d-miss" in e or "l1_miss" in e:
                self.ev["l1d_miss"] += 1
            # 只统计 access 类事件；l1d-miss 等 miss 事件不视为 access，
            # 否则 l1_hit=(access-miss)/access 恒为 0（miss 率模型下命中率本就不可测）
            if "l1d-access" in e or "l1_access" in e:
                self.ev["l1d_access"] += 1
            if "llc-miss" in e or "llc_miss" in e or "l3-miss" in e:
                self.ev["llc_miss"] += 1
            if "llc-access" in e or "llc_access" in e or "l3-access" in e:
                self.ev["llc_access"] += 1
            if "tlb-access" in e or "tlb_access" in e:
                self.ev["tlb_access"] += 1
            if "tlb-miss" in e or "tlb_miss" in e:
                self.ev["tlb_miss"] += 1
            if "branch-miss" in e or "branch_miss" in e or "mispred" in e:
                self.ev["branch_miss"] += 1
            if "remote-access" in e or "remote_access" in e:
                self.ev["remote_access"] += 1
        self.latencies = [int(r["weight"]) for r in self.rows
                          if (r.get("weight") or "").strip().isdigit()]
        self.have_lat = len(self.latencies) > 0
        self.have_src = any((r.get("data_src") or "").strip()
                            for r in self.rows)
        self.have_addr = any((r.get("addr") or "").strip()
                             for r in self.rows)

    # ---- 基础比率指标 ----
    def rates(self):
        t = self.total
        return {
            "l1_miss": pct(self.ev["l1d_miss"], t),
            "llc_miss": pct(self.ev["llc_miss"], t),
            "branch_mispred": pct(self.ev["branch_miss"], t),
            "tlb_miss": pct(self.ev["tlb_miss"], t),
            "remote_ratio": pct(self.ev["remote_access"], t),
            "l1_hit": (pct(self.ev["l1d_access"] - self.ev["l1d_miss"],
                           self.ev["l1d_access"])
                       if self.ev["l1d_access"] else None),
            "llc_hit": (pct(self.ev["llc_access"] - self.ev["llc_miss"],
                           self.ev["llc_access"])
                        if self.ev["llc_access"] else None),
        }

    # ---- 时延统计 ----
    def latency_stats(self):
        v = self.latencies
        if not v:
            return None
        return {
            "n": len(v), "min": min(v), "max": max(v),
            "mean": sum(v) / len(v),
            "p50": percentile(v, 50), "p90": percentile(v, 90),
            "p99": percentile(v, 99), "p999": percentile(v, 99.9),
        }

    # ---- 直方图 ----
    BUCKETS = [(0, 10), (10, 50), (50, 100), (100, 200),
               (200, 500), (500, 1000), (1000, 10 ** 9)]

    def histogram(self):
        if not self.have_lat:
            return None
        hist = []
        for lo, hi in self.BUCKETS:
            c = sum(1 for v in self.latencies if lo <= v < hi)
            hist.append((lo, hi, c))
        return hist

    # ---- 数据来源层级 ----
    def data_src_dist(self):
        if not self.have_src:
            return None
        dist = OrderedDict((k, {"count": 0, "lat": []})
                           for k in DATA_SRC_LEVELS)
        for r in self.rows:
            lv = class_data_src(r.get("data_src"))
            dist[lv]["count"] += 1
            if (r.get("weight") or "").strip().isdigit():
                dist[lv]["lat"].append(int(r["weight"]))
        for lv, d in dist.items():
            d["pct"] = pct(d["count"], self.total)
            d["mean_lat"] = (sum(d["lat"]) / len(d["lat"])) if d["lat"] else None
        return dist

    # ---- 逐指令/逐函数聚合 ----
    def aggregate(self, key):
        agg = OrderedDict()
        for r in self.rows:
            k = r.get(key) or "unknown"
            a = agg.setdefault(k, {
                "count": 0, "lat": [], "l1_miss": 0, "llc_miss": 0,
                "branch_miss": 0, "event_mix": {},
            })
            a["count"] += 1
            if (r.get("weight") or "").strip().isdigit():
                a["lat"].append(int(r["weight"]))
            e = (r.get("event") or "").lower()
            if "l1d-miss" in e:
                a["l1_miss"] += 1
            if "llc-miss" in e:
                a["llc_miss"] += 1
            if "branch-miss" in e or "mispred" in e:
                a["branch_miss"] += 1
            a["event_mix"][(r.get("event") or "none")] = \
                a["event_mix"].get((r.get("event") or "none"), 0) + 1
        out = []
        for k, a in agg.items():
            lat = a["lat"]
            out.append({
                "key": k, "count": a["count"],
                "sym": k,
                "mean_lat": (sum(lat) / len(lat)) if lat else None,
                "p50": percentile(lat, 50), "p90": percentile(lat, 90),
                "p99": percentile(lat, 99), "max_lat": max(lat) if lat else None,
                "wlat": sum(lat), "nlat": len(lat),
                "l1_miss_rate": pct(a["l1_miss"], a["count"]),
                "llc_miss_rate": pct(a["llc_miss"], a["count"]),
                "br_miss_rate": pct(a["branch_miss"], a["count"]),
                "events": a["event_mix"],
            })
        return out

    # ---- 高时延尾部与根因 ----
    def tail_analysis(self, tail_threshold):
        if not self.have_lat:
            return None
        tail = [r for r in self.rows
                if (r.get("weight") or "").strip().isdigit()
                and int(r["weight"]) >= tail_threshold]
        if not tail:
            return None
        n = len(tail)
        factors = OrderedDict([
            ("DRAM", 0), ("REMOTE", 0), ("PEER", 0), ("L3", 0), ("L2", 0),
            ("L1", 0), ("tlb_miss", 0), ("branch_mispred", 0),
        ])
        for r in tail:
            lv = class_data_src(r.get("data_src"))
            if lv in factors:
                factors[lv] += 1
            e = (r.get("event") or "").lower()
            if "tlb-miss" in e:
                factors["tlb_miss"] += 1
            if "branch-miss" in e or "mispred" in e:
                factors["branch_mispred"] += 1
        # 组合根因：远端 + TLB 等共现
        combos = OrderedDict()
        for r in tail:
            e = (r.get("event") or "").lower()
            lv = class_data_src(r.get("data_src"))
            tags = []
            if lv in ("REMOTE", "DRAM"):
                tags.append("remote_or_dram")
            if "tlb-miss" in e:
                tags.append("tlb_miss")
            if "branch-miss" in e or "mispred" in e:
                tags.append("branch_mispred")
            key = "+".join(tags) if tags else "other"
            combos[key] = combos.get(key, 0) + 1
        return {
            "threshold": tail_threshold, "n": n,
            "ratio": pct(n, len(self.latencies)),
            "factors": {k: {"count": v, "pct": pct(v, n)} for k, v in factors.items()},
            "combos": {k: {"count": v, "pct": pct(v, n)} for k, v in combos.items()},
        }

    # ---- CPU 分布 ----
    def cpu_dist(self):
        dist = OrderedDict()
        for r in self.rows:
            cpu = (r.get("cpu") or "?").strip()
            if cpu.isdigit():
                cpu = str(int(cpu))
            a = dist.setdefault(cpu, {"count": 0, "lat": []})
            a["count"] += 1
            if (r.get("weight") or "").strip().isdigit():
                a["lat"].append(int(r["weight"]))
        out = []
        for cpu, a in dist.items():
            out.append({"cpu": cpu, "count": a["count"],
                        "mean_lat": (sum(a["lat"]) / len(a["lat"]))
                        if a["lat"] else None})
        return out


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------

def fmt_num(x):
    if x is None:
        return "-"
    return "%.1f" % x


def fmt_int(x):
    return "-" if x is None else "%d" % x


def render_metrics(f, c):
    r = c.rates()
    ls = c.latency_stats()
    f.write("## 1. 指标门禁判定\n\n")
    f.write("| 指标 | 值 | 门禁等级 | 判定依据 |\n")
    f.write("|------|----|----------|----------|\n")
    grade_l1 = grade(r["l1_miss"], GRADES["l1_miss"])
    grade_llc = grade(r["llc_miss"], GRADES["llc_miss"])
    grade_br = grade(r["branch_mispred"], GRADES["branch_mispred"])
    grade_tlb = grade(r["tlb_miss"], GRADES["tlb_miss"])
    grade_rmt = grade(r["remote_ratio"], GRADES["remote_ratio"])
    f.write("| l1_miss | %.2f%% | **%s** | L1 未命中率（>30 SEVERE / >10 MODERATE / >5 MILD） |\n"
            % (r["l1_miss"], grade_l1))
    f.write("| llc_miss | %.2f%% | **%s** | LLC 未命中率（>30 SEVERE / >10 MODERATE / >5 MILD） |\n"
            % (r["llc_miss"], grade_llc))
    f.write("| branch_mispred | %.2f%% | **%s** | 分支预测失败率（>20 SEVERE / >5 MODERATE / >1 MILD） |\n"
            % (r["branch_mispred"], grade_br))
    f.write("| tlb_miss | %.2f%% | **%s** | TLB 未命中率（>10 SEVERE / >5 MODERATE / >2 MILD） |\n"
            % (r["tlb_miss"], grade_tlb))
    f.write("| remote_ratio | %.2f%% | **%s** | 远端访问占比（>20 SEVERE / >10 MODERATE / >5 MILD） |\n"
            % (r["remote_ratio"], grade_rmt))
    if r["l1_hit"] is not None:
        f.write("| l1_hit | %.2f%% | - | L1 命中率（有 access 记录时） |\n" % r["l1_hit"])
    if r["llc_hit"] is not None:
        f.write("| llc_hit | %.2f%% | - | LLC 命中率（有 access 记录时） |\n" % r["llc_hit"])
    if ls:
        grade_p99 = grade(ls["p99"], GRADES["p99_latency"])
        f.write("| p99_latency | %d cycles | **%s** | P99 指令时延（>1000 SEVERE / >300 MODERATE / >100 MILD） |\n"
                % (ls["p99"], grade_p99))
        tail_n = sum(1 for v in c.latencies if v >= 1000)
        tail_ratio = pct(tail_n, len(c.latencies))
        grade_tail = grade(tail_ratio, GRADES["tail_ratio"])
        f.write("| tail_ratio(≥1000cyc) | %.2f%% | **%s** | 长时延样本占比（>10 SEVERE / >5 MODERATE / >1 MILD） |\n"
                % (tail_ratio, grade_tail))
    f.write("\n")


def render_latency(f, c):
    ls = c.latency_stats()
    if not ls:
        f.write("## 2. 指令时延分布\n\n> 本数据无 weight（延迟）字段，无法做时延分析。"
                "请使用 `spe-csv.sh` 且确认 perf 支持 `-F weight`（内核 ≥5.10 / arm_spe_pmu 解码）。\n\n")
        return
    f.write("## 2. 指令时延分布\n\n")
    f.write("| 统计量 | 值 |\n|--------|----|\n")
    f.write("| 样本数 | %d |\n" % ls["n"])
    f.write("| 均值 | %.1f cycles |\n" % ls["mean"])
    f.write("| 最小值 | %d |\n" % ls["min"])
    f.write("| P50 | %d |\n" % ls["p50"])
    f.write("| P90 | %d |\n" % ls["p90"])
    f.write("| P99 | %d |\n" % ls["p99"])
    f.write("| P99.9 | %d |\n" % ls["p999"])
    f.write("| 最大值 | %d |\n\n" % ls["max"])

    hist = c.histogram()
    f.write("**直方图**\n\n")
    f.write("| 时延区间 (cycles) | 样本数 | 占比 |\n|-------------------|--------|------|\n")
    for lo, hi, cnt in hist:
        if hi == 10 ** 9:
            rng = "≥%d" % lo
        else:
            rng = "[%d,%d)" % (lo, hi)
        f.write("| %s | %d | %.2f%% |\n" % (rng, cnt, pct(cnt, ls["n"])))
    f.write("\n")


def render_data_src(f, c):
    dist = c.data_src_dist()
    if not dist:
        f.write("## 3. 缓存命中层级\n\n> 本数据无 data_src（数据来源）字段，"
                "无法做命中层级分析。请使用 `spe-csv.sh` 完整字段解析。\n\n")
        return
    f.write("## 3. 缓存命中层级\n\n")
    f.write("| 层级 | 样本数 | 占比 | 平均时延 (cycles) |\n|------|--------|------|------------------|\n")
    for lv, d in dist.items():
        f.write("| %s (%s) | %d | %.2f%% | %s |\n"
                % (lv, DATA_SRC_LEVELS[lv], d["count"], d["pct"],
                   fmt_num(d["mean_lat"])))
    f.write("\n")


def render_instruction_chasing(f, c, top_n, tail_threshold):
    f.write("## 4. 指令追逐（逐指令 / 逐函数双热点）\n\n")
    by_pc = sorted(c.aggregate("ip"), key=lambda x: -x["wlat"] if c.have_lat
                    else -x["count"])
    by_sym = sorted(c.aggregate("sym"), key=lambda x: -x["wlat"] if c.have_lat
                    else -x["count"])
    if not c.have_lat:
        f.write("> 无 weight 字段，以下按采样数排序（频率热点）；"
                "恢复 weight 后按加权时延排序。\n\n")

    def table(f, rows, label):
        f.write("**%s**\n\n" % label)
        f.write("| 指令/函数 | 样本数 | 加权时延 | 均值 | P90 | P99 | 最大 | L1M% | LLCM% | BRM% |\n")
        f.write("|----------|--------|----------|------|-----|-----|------|-------|--------|-------|\n")
        for a in rows[:top_n]:
            f.write("| %s | %d | %s | %s | %s | %s | %s | %.1f | %.1f | %.1f |\n"
                    % (a["key"], a["count"], fmt_num(a["wlat"]),
                       fmt_num(a["mean_lat"]), fmt_int(a["p90"]),
                       fmt_int(a["p99"]), fmt_int(a["max_lat"]),
                       a["l1_miss_rate"], a["llc_miss_rate"],
                       a["br_miss_rate"]))
        f.write("\n")

    table(f, by_pc, "Top %d 指令地址（按加权时延/采样数）" % top_n)
    table(f, by_sym, "Top %d 函数（按加权时延/采样数）" % top_n)

    # 高时延尾部指令
    if c.have_lat:
        tail_ips = sorted(c.aggregate("ip"),
                          key=lambda x: x["p99"] if x["p99"] is not None else 0,
                          reverse=True)
        f.write("**Top %d 高时延尾部指令（按 P99 时延排序，即长时延根因候选）**\n\n"
                % top_n)
        f.write("| 指令 | 样本数 | P50 | P90 | P99 | 最大 | 主要事件 |\n")
        f.write("|------|--------|-----|-----|-----|------|----------|\n")
        for a in tail_ips[:top_n]:
            ev = sorted(a["events"].items(), key=lambda kv: -kv[1])[:2]
            evs = ",".join("%s×%d" % (e, n) for e, n in ev)
            f.write("| %s | %d | %s | %s | %s | %s | %s |\n"
                    % (a["key"], a["count"], fmt_int(a["p50"]),
                       fmt_int(a["p90"]), fmt_int(a["p99"]),
                       fmt_int(a["max_lat"]), evs))
        f.write("\n")


def render_tail_rootcause(f, c, tail_threshold):
    ta = c.tail_analysis(tail_threshold)
    f.write("## 5. 高时延指令根因分析\n\n")
    if ta is None:
        f.write("> 无满足阈值的尾部样本或缺少 weight 字段，跳过根因归因。\n\n")
        return
    f.write("阈值: 时延 ≥ %d cycles，尾部样本 %d 条（占有时延样本 %.2f%%）。\n\n"
            % (ta["threshold"], ta["n"], ta["ratio"]))
    f.write("**单因子归因（尾部样本中含该因子的占比，因子可重叠）**\n\n")
    f.write("| 因子 | 尾部样本数 | 占比 |\n|------|-----------|------|\n")
    for k, v in ta["factors"].items():
        if v["count"]:
            f.write("| %s | %d | %.2f%% |\n" % (k, v["count"], v["pct"]))
    f.write("\n**组合根因（共现标签）**\n\n")
    f.write("| 组合 | 尾部样本数 | 占比 |\n|------|-----------|------|\n")
    for k, v in sorted(ta["combos"].items(), key=lambda kv: -kv[1]["count"]):
        f.write("| %s | %d | %.2f%% |\n" % (k, v["count"], v["pct"]))
    f.write("\n")
    f.write("> 结论口径：单因子归因占比为“尾部样本中包含该因子的比例”（有重叠）；"
            "组合根因按标签共现统计，用于区分单点根因与多因子叠加。\n\n")


def render_aux(f, c):
    f.write("## 6. 补充指标\n\n")
    f.write("### 6.1 事件类型分布\n\n")
    evm = {}
    for r in c.rows:
        e = r.get("event") or "none"
        evm[e] = evm.get(e, 0) + 1
    f.write("| 事件 | 数量 | 占比 |\n|------|------|------|\n")
    for e, n in sorted(evm.items(), key=lambda kv: -kv[1]):
        f.write("| %s | %d | %.2f%% |\n" % (e, n, pct(n, c.total)))
    f.write("\n### 6.2 指令类型（按事件粗分）\n\n")
    opm = {}
    for r in c.rows:
        op = class_op(r.get("event") or "")
        opm.setdefault(op, {"count": 0, "lat": []})
        opm[op]["count"] += 1
        if (r.get("weight") or "").strip().isdigit():
            opm[op]["lat"].append(int(r["weight"]))
    f.write("| 类型 | 样本数 | 占比 | 平均时延 |\n|------|--------|------|----------|\n")
    for op, a in opm.items():
        f.write("| %s | %d | %.2f%% | %s |\n"
                % (op, a["count"], pct(a["count"], c.total),
                   fmt_num((sum(a["lat"]) / len(a["lat"])) if a["lat"] else None)))
    f.write("\n### 6.3 CPU 分布\n\n")
    f.write("| CPU | 样本数 | 平均时延 |\n|-----|--------|----------|\n")
    for d in c.cpu_dist():
        f.write("| %s | %d | %s |\n" % (d["cpu"], d["count"], fmt_num(d["mean_lat"])))
    f.write("\n")


def render_exec_summary(f, c, section_heads):
    r = c.rates()
    ls = c.latency_stats()
    g1 = grade(r["l1_miss"], GRADES["l1_miss"])
    g2 = grade(r["llc_miss"], GRADES["llc_miss"])
    g3 = grade(r["branch_mispred"], GRADES["branch_mispred"])
    g4 = grade(r["tlb_miss"], GRADES["tlb_miss"])
    g5 = grade(r["remote_ratio"], GRADES["remote_ratio"])
    findings = []
    if g1 in ("SEVERE", "MODERATE"):
        findings.append("L1 未命中率 %.2f%%（%s），访存模式或共享冲突可疑" % (r["l1_miss"], g1))
    if g2 in ("SEVERE", "MODERATE"):
        findings.append("LLC 未命中率 %.2f%%（%s），工作集超出 L3 或 NUMA 远端访问" % (r["llc_miss"], g2))
    if g3 in ("SEVERE", "MODERATE"):
        findings.append("分支预测失败率 %.2f%%（%s），分支模式不可预测" % (r["branch_mispred"], g3))
    if g4 in ("SEVERE", "MODERATE"):
        findings.append("TLB 未命中率 %.2f%%（%s），页表遍历开销" % (r["tlb_miss"], g4))
    if g5 in ("SEVERE", "MODERATE"):
        findings.append("远端访问占比 %.2f%%（%s），NUMA 亲和性可疑" % (r["remote_ratio"], g5))
    if ls:
        gp = grade(ls["p99"], GRADES["p99_latency"])
        if gp in ("SEVERE", "MODERATE"):
            findings.append("P99 时延 %d cycles（%s），存在长时延尾部" % (ls["p99"], gp))
    if not findings:
        findings.append("各项指标处于 OK/MILD 区间，无明显微架构瓶颈信号")
    f.write("## 0. 执行摘要\n\n")
    for i, s in enumerate(findings[:5], 1):
        f.write("%d. %s\n" % (i, s))
    f.write("\n")


def render_compare(f, base, cur, metric="l1_miss", top_n=15):
    f.write("## 7. 前后对比（当前 vs 基线）\n\n")
    metr = {
        "l1_miss": ("l1_miss_rate", "L1 未命中率"),
        "llc_miss": ("llc_miss_rate", "LLC 未命中率"),
        "branch_mispred": ("br_miss_rate", "分支预测失败率"),
    }
    key_field, label = metr.get(metric, ("l1_miss_rate", "L1 未命中率"))
    bmap = {}
    for a in base.aggregate("sym"):
        bmap[a["sym"]] = a
    cmap = {}
    for a in cur.aggregate("sym"):
        cmap[a["sym"]] = a
    allk = set(bmap) | set(cmap)
    diffs = []
    for k in allk:
        b = bmap.get(k)
        c = cmap.get(k)
        if not b or not c:
            continue
        diff = (c[key_field] or 0) - (b[key_field] or 0)
        diffs.append((k, b, c, diff))
    diffs.sort(key=lambda x: abs(x[3]), reverse=True)
    f.write("对比指标: **%s**（%s）。\n\n" % (metric, label))
    f.write("| 函数 | 基线样本 | 当前样本 | 基线%s%% | 当前%s%% | Δ(pp) | 判定 |\n"
            % (label, label))
    f.write("|------|----------|----------|----------|----------|-------|------|\n")
    for k, b, c, diff in diffs[:top_n]:
        if abs(diff) < 0.05:
            verdict = "NO CHANGE"
        elif diff < 0:
            verdict = "IMPROVED"
        else:
            verdict = "REGRESSED"
        f.write("| %s | %d | %d | %.1f | %.1f | %+.1f | **%s** |\n"
                % (k, b["count"], c["count"], b[key_field] or 0,
                   c[key_field] or 0, diff, verdict))
    f.write("\n")


def build_json(c, args, tail_threshold):
    r = c.rates()
    ls = c.latency_stats()
    dist = c.data_src_dist()
    ta = c.tail_analysis(tail_threshold)
    data = {
        "meta": {
            "csv": args.csv, "total": c.total,
            "have_latency": c.have_lat, "have_data_src": c.have_src,
            "have_addr": c.have_addr,
        },
        "rates": r,
        "latency": ls,
        "histogram": c.histogram(),
        "data_src": {k: d for k, d in dist.items()} if dist else None,
        "top_pc": sorted(c.aggregate("ip"),
                         key=lambda x: -x["wlat"] if c.have_lat else -x["count"])[:20],
        "top_sym": sorted(c.aggregate("sym"),
                          key=lambda x: -x["wlat"] if c.have_lat else -x["count"])[:20],
        "tail_analysis": ta,
        "cpu": c.cpu_dist(),
        "grades": {k: None for k in GRADES},
    }
    if ls:
        data["grades"]["p99_latency"] = grade(ls["p99"], GRADES["p99_latency"])
        data["grades"]["tail_ratio"] = grade(
            pct(sum(1 for v in c.latencies if v >= 1000), len(c.latencies)),
            GRADES["tail_ratio"])
    data["grades"]["l1_miss"] = grade(r["l1_miss"], GRADES["l1_miss"])
    data["grades"]["llc_miss"] = grade(r["llc_miss"], GRADES["llc_miss"])
    data["grades"]["branch_mispred"] = grade(r["branch_mispred"],
                                             GRADES["branch_mispred"])
    data["grades"]["tlb_miss"] = grade(r["tlb_miss"], GRADES["tlb_miss"])
    data["grades"]["remote_ratio"] = grade(r["remote_ratio"], GRADES["remote_ratio"])
    return data


def main():
    ap = argparse.ArgumentParser(
        description="ARM SPE 深度时延分析与报告生成")
    ap.add_argument("csv", help="SPE 样本 CSV（spe-csv.sh 或开源 spe-parser 输出）")
    ap.add_argument("-o", "--output", help="Markdown 报告输出路径（默认 spe-report-<时间>.md）")
    ap.add_argument("--json", dest="json_out", help="同时输出 JSON 数据文件")
    ap.add_argument("--top", type=int, default=20, help="热点 Top N（默认 20）")
    ap.add_argument("--min-lat", type=int, default=500,
                    help="高时延根因阈值（cycles，默认 500）")
    ap.add_argument("--baseline", help="基线 CSV，输出前后对比段")
    ap.add_argument("--metric", default="l1_miss",
                    choices=["l1_miss", "llc_miss", "branch_mispred"],
                    help="前后对比判定指标（默认 l1_miss）")
    args = ap.parse_args()

    if not os.path.isfile(args.csv):
        print("[error] CSV 不存在: %s" % args.csv, file=sys.stderr)
        return 1

    try:
        c = SpeCSV(args.csv)
    except ValueError as e:
        print("[error] %s" % e, file=sys.stderr)
        return 1
    if c.total == 0:
        print("[error] CSV 无样本记录: %s" % args.csv, file=sys.stderr)
        return 1

    out_path = args.output or ("spe-report-%s.md" % c.total)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# ARM SPE 深度时延分析报告\n\n")
        f.write("- 数据文件: `%s`\n- 样本总数: %d\n"
                % (args.csv, c.total))
        f.write("- 时延字段(weight): %s / 数据源字段(data_src): %s\n\n"
                % ("有" if c.have_lat else "无", "有" if c.have_src else "无"))

        sections = []
        render_exec_summary(f, c, sections)
        render_metrics(f, c)
        render_latency(f, c)
        render_data_src(f, c)
        render_instruction_chasing(f, c, args.top, args.min_lat)
        render_tail_rootcause(f, c, args.min_lat)
        render_aux(f, c)

        if args.baseline:
            if not os.path.isfile(args.baseline):
                print("[error] 基线 CSV 不存在: %s" % args.baseline,
                      file=sys.stderr)
                return 1
            base = SpeCSV(args.baseline)
            render_compare(f, base, c, args.metric, args.top)

        f.write("---\n\n*由 spe-latency-report.py 生成。"
                "时延数值来自 SPE 硬件采样，为统计性样本；"
                "未命中率阈值参考 spe-hotspot.sh 与 spe_latency_analysis.md。*\n")

    print("报告已生成: %s" % out_path)

    if args.json_out:
        data = build_json(c, args, args.min_lat)
        with open(args.json_out, "w", encoding="utf-8") as jf:
            json.dump(data, jf, ensure_ascii=False, indent=2)
        print("JSON 已生成: %s" % args.json_out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
