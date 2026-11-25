#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""generate_report.py - dp_load_metrics CSV -> 负载均衡分析自包含 HTML 报告

用法：
  python generate_report.py --csv a.csv --title "标题" -o report.html
  python generate_report.py --csv a.csv b.csv --label-a proxy_new --label-b coordinator \
      --title "路由策略对比" -o report.html --summary-out summary.json [--extra-analysis extra.md]

口径与指标定义的唯一权威来源：references/report_spec.md。
"""
import argparse
import csv
import html as html_mod
import json
import math
import os
import re
import sys
import time
from datetime import datetime

# ---------- 时间戳解析：epoch 秒 / 可读本地时间 / ISO8601（含时区） ----------
TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S")


def parse_ts(s):
    """宽容解析时间戳：优先 epoch 秒（纯数字），再试可读/ISO 格式。

    返回 epoch 秒（int）或 None（无法解析，调用方累计 skipped 计数）。
    ISO 含时区时按该时区换算，无时区的可读格式按本地时区。
    """
    s = s.strip()
    try:
        return int(float(s))
    except ValueError:
        pass
    for fmt in TS_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is not None:
                # 带时区：换算为 epoch 后转本地时间口径，与无时区格式一致
                return int(dt.timestamp())
            return int(time.mktime(dt.timetuple()))
        except ValueError:
            continue
    return None

BASE_COLS = ["num_requests_running", "num_requests_waiting", "num_requests_swapped",
             "request_success_total", "request_failure_total", "num_preemptions_total",
             "prompt_tokens_total", "generation_tokens_total", "request_validation_time_total",
             "kv_cache_usage_perc", "cpu_cache_usage_perc", "prefix_cache_hit_rate",
             "pending_prefix_cache_hit_rate", "external_prefix_cache_hit_rate",
             "process_start_time_seconds"]
HIST_KEYS = ["ttft", "queue", "prefill", "tpot", "e2e"]
AVG_COLS = [k + "_avg" for k in HIST_KEYS]
V2_DATA_COLS = BASE_COLS + AVG_COLS + [k + s for k in HIST_KEYS for s in ("_sum", "_count")]
LEGACY_DATA_COLS = BASE_COLS + AVG_COLS
COUNTER_COLS = {"request_success_total", "request_failure_total", "num_preemptions_total",
                "prompt_tokens_total", "generation_tokens_total", "request_validation_time_total"} | \
               {k + s for k in HIST_KEYS for s in ("_sum", "_count")}
PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777",
           "#65a30d", "#ea580c", "#4f46e5", "#0d9488", "#a16207"]


def fnum(x, nd=2):
    if x is None:
        return "n/a"
    if isinstance(x, (int,)) or (isinstance(x, float) and abs(x - round(x)) < 1e-9):
        return str(int(round(x)))
    return ("%." + str(nd) + "f") % x


def pct(x, nd=1):
    return "n/a" if x is None else ("%." + str(nd) + "f%%") % (x * 100)


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def pvar(xs):
    if len(xs) < 1:
        return None
    m = mean(xs)
    return sum((x - m) ** 2 for x in xs) / len(xs)


def cv(xs):
    if xs is None or len(xs) < 2:
        return None
    m = mean(xs)
    if m <= 0:
        return None
    return math.sqrt(pvar(xs)) / m


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = mean(xs), mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


class Engine(object):
    def __init__(self, tag):
        self.tag = tag
        # host/port 解析：优先 "host:port[.engine]"，回退 "host_port[.engine]"
        # （下划线写法 + Prometheus 同实例多 engine 的 ".N" 后缀都要归到同一 host）
        m = re.match(r"^(.*?)[_:](\d+)(?:\.(\d+))?$", tag)
        if m:
            self.host, self.port = m.group(1), m.group(2)
            self.engine = m.group(3) or ""
        else:
            self.host, self.port, self.engine = tag, "", ""
        self.series = {}      # col -> [(t, value)]
        self.restarts = 0
        self.process_start = None
        self.delta_cache = {}
        self.wavg_cache = {}

    def add(self, col, t, v):
        self.series.setdefault(col, []).append((t, v))

    def vals(self, col):
        return self.series.get(col, [])

    def valid_points(self, col):
        """有效点（非缺失、非负）按时间稳定排序；同一时间戳保留最后一条
        （与 Prometheus 抓取语义一致）。CSV 行序乱序时由这里保证差分按时间
        正序进行，避免正常增长被误判为重启或同一区间增量被重复计入。"""
        seen = {}
        for t, v in self.series.get(col, []):
            if v is None or v < 0:
                continue
            seen[t] = v
        return sorted(seen.items())

    def delta(self, col):
        """counter 窗口增量，回落切段（疑似重启），段间求和。
        有效点不足 2 个（采集全缺失或仅单点）-> None：视为缺失而非真实
        零流量，不参与 CV/LIF/失衡比等均衡性计算。"""
        if col in self.delta_cache:
            return self.delta_cache[col]
        pts = self.valid_points(col)
        total = None
        if len(pts) >= 2:
            total = 0.0
            seg_start = pts[0][1]
            prev = pts[0][1]
            for t, v in pts[1:]:
                if v < prev:
                    total += prev - seg_start
                    seg_start = v
                prev = v
            total += prev - seg_start
        self.delta_cache[col] = total
        return total

    def window_avg(self, key):
        """v2: 窗口平均时延 = Δsum/Δcount。
        只累计「两端同时有 sum 与 count、且处于同一进程生命周期」的采样区间：
        sum 与 count 各自跳过缺失点会导致区间错位（末端 sum 缺失时 Δsum 取到
        更早的有效点，把 0/Δcount 报成零延迟）。生命周期以两种信号判定，命中
        任一即弃用该区间并从当前点重开段：①任一计数器回落（疑似重启）；
        ②process_start_time_seconds 在区间内发生变化——精确比对、无容差：该列
        是进程启动时刻的常量导出，同进程各采样点恒等，亚秒级差异也是不同进程
        （counter 未回落但进程已重启的场景）。
        无任何有效区间 -> None。"""
        if key in self.wavg_cache:
            return self.wavg_cache[key]
        spts = dict(self.valid_points(key + "_sum"))
        cpts = dict(self.valid_points(key + "_count"))
        ppts = dict(self.valid_points("process_start_time_seconds"))
        ts = sorted(set(spts) & set(cpts))   # 两列同时有效的采样时刻
        dsum = dcnt = 0.0
        prev = None
        for t in ts:
            s, c = spts[t], cpts[t]
            if prev is not None:
                pt, ps_, pc_ = prev
                # process_start 任何变化都视为跨生命周期（精确比对，不带秒级容差）：
                # process_start_time_seconds 是进程启动时刻的常量导出，同进程各采样点
                # 值恒等；哪怕只差亚秒也是不同进程——容差会让「新进程晚启动 0.5 秒、
                # 计数已追上」的跨进程差分被计入
                cross_life = pt in ppts and t in ppts and ppts[t] != ppts[pt]
                if not cross_life and s >= ps_ and c > pc_:
                    dsum += s - ps_
                    dcnt += c - pc_
                # 任一侧 counter 回落、或 process_start 变化（跨生命周期）：
                # 该区间弃用，从当前点重开段
            prev = (t, s, c)
        val = (dsum / dcnt) if dcnt > 0 else None
        self.wavg_cache[key] = val
        return val

    def legacy_avg(self, key):
        vs = [v for _, v in self.valid_points(key + "_avg")]
        return mean(vs) if vs else None

    def gauge_mean(self, col):
        vs = [v for _, v in self.valid_points(col)]
        return mean(vs) if vs else None

    def gauge_max(self, col):
        vs = [v for _, v in self.valid_points(col)]
        return max(vs) if vs else None

    def nonzero_ratio(self, col):
        vs = [v for _, v in self.valid_points(col)]
        if not vs:
            return None
        return sum(1 for v in vs if v > 0) / len(vs)

    def cum(self, col):
        return sum(v for _, v in self.valid_points(col))

    def point_count(self):
        return max((len(v) for v in self.series.values()), default=0)

    def missing_rate(self, data_cols, exclude=frozenset()):
        """缺失率；exclude 为该部署确认未导出（全数据集 100% 缺失）的列，不进分母"""
        cols = [c for c in data_cols if c not in exclude]
        expected = len(cols) * max(self.point_count(), 1)
        miss = 0
        for col in cols:
            miss += sum(1 for _, v in self.series.get(col, []) if v is None)
        return miss / expected if expected else 1.0


class Dataset(object):
    def __init__(self, path, label):
        self.path = path
        self.label = label
        self.engines = {}
        self.fmt = None
        self.data_cols = None
        self.times = []
        self.warnings = []
        self.absent_cols = frozenset()
        self._load()

    def _load(self):
        with open(self.path, newline="", encoding="utf-8-sig") as f:
            rdr = csv.reader(f)
            header = next(rdr)
            if "ttft_sum" in header:
                self.fmt = "v2"
                self.data_cols = V2_DATA_COLS
            elif "ttft_avg" in header:
                self.fmt = "legacy"
                self.data_cols = LEGACY_DATA_COLS
            else:
                raise SystemExit("无法识别 CSV 格式（表头既无 ttft_sum 也无 ttft_avg）: %s" % self.path)
            idx = {c: i for i, c in enumerate(header)}
            tset = set()
            skipped_ts = 0
            qfix_idx = idx.get("queue_fixed")
            for row in rdr:
                if len(row) < 2:
                    continue
                t = parse_ts(row[0])
                if t is None:
                    skipped_ts += 1
                    continue
                tag = row[1].strip()
                eng = self.engines.setdefault(tag, Engine(tag))
                for col in self.data_cols:
                    i = idx.get(col)
                    if i is None or i >= len(row):
                        continue
                    s = row[i].strip()
                    v = None
                    if s not in ("", "-1", "-1.0"):
                        try:
                            v = float(s)
                        except ValueError:
                            v = None
                    eng.add(col, t, v)
                    tset.add(t)
                # queue_fixed 标记列（可选）：1 = 该行 queue/prefill 经过负 queue 修正
                if qfix_idx is not None and qfix_idx < len(row):
                    s = row[qfix_idx].strip()
                    try:
                        eng.add("queue_fixed", t, float(s))
                    except ValueError:
                        eng.add("queue_fixed", t, None)
            self.times = sorted(tset)
            if skipped_ts:
                self.warnings.append("时间戳无法解析 %d 行（支持 epoch 秒 / %s），已跳过" %
                                     (skipped_ts, " / ".join(TS_FORMATS)))
        # host 写法混用告警：同一数据集中 "host:port" 与 "host_port" 并存时分组可能撕裂
        seps = set()
        for eng in self.engines.values():
            if not eng.tag:
                continue
            m = re.match(r"^(.*?)([_:])(\d+)(?:\.\d+)?$", eng.tag)
            if m:
                seps.add(m.group(2))
        if len(seps) > 1:
            self.warnings.append("检测到 ip_port 标识混用冒号与下划线两种写法（%s）——"
                                 "同一主机的引擎可能被拆成不同分组，建议统一写法后重跑" % sorted(seps))
        # 硬失败：解析不到有效采样点，或数据列全空
        if not self.engines or not self.times:
            raise SystemExit(
                "未解析到任何有效采样点：%s\n可能原因：① 列名与 v2/legacy 口径不匹配；"
                "② 时间戳格式不支持（支持 epoch 秒 / %s）；③ 分隔符异常。"
                "可用 --print-schema 输出识别结果排查。" % (self.path, " / ".join(TS_FORMATS)))
        empty_cols = [c for c in self.data_cols
                      if all(v is None for e in self.engines.values() for _, v in e.series.get(c, []))]
        if len(empty_cols) == len(self.data_cols):
            raise SystemExit(
                "所有数据列均无有效值：%s\n可能原因：① 数值列全为 -1/空/非数字；"
                "② 列名与 v2/legacy 口径不匹配；③ 分隔符异常。" % self.path)
        # 全数据集 100% 缺失的列视为「该部署未导出」：不计入缺失率分母，报告脚注标注
        # （vLLM v1 不提供 swapped/request_failure/validation_time/cpu_cache/pending_prefix 等）
        self.absent_cols = frozenset(empty_cols)
        if self.absent_cols:
            self.warnings.append("列导出说明：以下列全窗口无任何数据，通常为该 vLLM 版本不导出（如 vLLM v1 无 swapped/request_failure 等），属版本能力而非采集丢点，不计入缺失率：%s" %
                                 ", ".join(sorted(self.absent_cols)))
        for eng in self.engines.values():
            succ = eng.valid_points("request_success_total")
            restarted = False
            for i in range(1, len(succ)):
                if succ[i][0] != succ[i - 1][0] and succ[i][1] < succ[i - 1][1]:
                    restarted = True
                    break
            ps_pts = eng.valid_points("process_start_time_seconds")  # 按时间序，防乱序行取错末值
            ps_vals = [v for _, v in ps_pts]
            # process_start 任何变化都视为重启（精确比对）：同进程的启动时刻导出恒定，
            # 亚秒级差异也是不同进程，秒级容差会漏掉快速重启
            if len(ps_vals) >= 2 and max(ps_vals) != min(ps_vals):
                restarted = True
            eng.restarts = 1 if restarted else 0
            eng.process_start = ps_pts[-1][1] if ps_pts else None

    # ---------- 派生指标 ----------
    def window(self):
        if not self.times:
            return None, None
        return self.times[0], self.times[-1]

    def step(self):
        if len(self.times) < 2:
            return None
        return int(round((self.times[-1] - self.times[0]) / (len(self.times) - 1)))

    def eng_list(self):
        return sorted(self.engines.values(), key=lambda e: (e.host, e.port, e.tag))

    def engine_metrics(self):
        out = {}
        for e in self.eng_list():
            m = {}
            m["req"] = e.delta("request_success_total")
            m["prompt"] = e.delta("prompt_tokens_total")
            m["gen"] = e.delta("generation_tokens_total")
            m["fail"] = e.delta("request_failure_total")
            m["preempt"] = e.delta("num_preemptions_total")
            for k in HIST_KEYS:
                m[k] = e.window_avg(k) if self.fmt == "v2" else e.legacy_avg(k)
            m["waiting_mean"] = e.gauge_mean("num_requests_waiting")
            m["waiting_peak"] = e.gauge_max("num_requests_waiting")
            m["waiting_nonzero"] = e.nonzero_ratio("num_requests_waiting")
            m["running_mean"] = e.gauge_mean("num_requests_running")
            m["kv_mean"] = e.gauge_mean("kv_cache_usage_perc")
            m["kv_peak"] = e.gauge_max("kv_cache_usage_perc")
            m["hit_local"] = e.gauge_mean("prefix_cache_hit_rate")
            m["hit_external"] = e.gauge_mean("external_prefix_cache_hit_rate")
            m["restarts"] = e.restarts
            m["missing"] = e.missing_rate(self.data_cols, exclude=self.absent_cols)
            m["points"] = e.point_count()
            m["process_start"] = e.process_start
            qf = e.valid_points("queue_fixed")  # 时间序去重，重复时间戳双计会虚增修正比例
            m["queue_fixed"] = (sum(1 for _, v in qf if v >= 1) / len(qf)) if qf else None
            out[e.tag] = m
        return out

    def global_metrics(self):
        em = self.engine_metrics()
        tags = list(em.keys())
        reqs_all = [em[t]["req"] for t in tags]
        reqs = [r for r in reqs_all if r is not None]
        # 有效引擎：request counter 有效点 ≥2、窗口增量可算；其余引擎（采集全
        # 缺失/仅单点）的 req 为 None，标为缺失并从均衡性计算中排除（评审意见：
        # 避免被当作真实零流量拉低均值、抬高 CV/LIF）
        invalid_tags = [t for t in tags if em[t]["req"] is None]
        gens = [em[t]["gen"] for t in tags if em[t]["gen"] is not None]
        prompts = [em[t]["prompt"] for t in tags if em[t]["prompt"] is not None]
        host_of = {t: self.engines[t].host for t in tags}
        between, within, host_loads = self._decompose(reqs_all, tags, host_of)
        total_req = sum(r for r in reqs if r is not None)
        shares = {t: (em[t]["req"] / total_req if total_req > 0 and em[t]["req"] is not None else None) for t in tags}
        ttf = [em[t]["ttft"] for t in tags if em[t]["ttft"] is not None]
        # 队列
        spike = {t for t in tags if em[t]["waiting_nonzero"] is not None and em[t]["waiting_nonzero"] < 0.05}
        qvals = [em[t]["waiting_mean"] for t in tags if t not in spike and em[t]["waiting_mean"] is not None]
        qallzero = bool(qvals) and all(v == 0 for v in qvals) or not qvals
        # waiting 时间累计：集中度 / top1 只统计非毛刺引擎（report_spec §2：非零占比 <5%
        # 的引擎降级为「毛刺」，不参与队列维度计分与集中度）；毛刺引擎的 waiting
        # 占比单独成字段展示，不进集中度分子分母——否则一次瞬时尖峰会被误报为
        # 「排队 100% 集中于该引擎」并在横幅/建议中点名，误导排查方向。
        cums_all = {t: self.engines[t].cum("num_requests_waiting") for t in tags}
        cums = {t: cums_all[t] for t in tags if t not in spike}
        tot_cums = sum(cums.values())
        tot_all = sum(cums_all.values())
        conc = (max(cums.values()) / tot_cums) if tot_cums > 0 else None
        # 相关性
        xs = [shares[t] for t in tags if em[t]["ttft"] is not None and shares[t] is not None]
        ys = [em[t]["ttft"] for t in tags if em[t]["ttft"] is not None and shares[t] is not None]
        # 失衡差（max-min 绝对差值）：工业路由器普遍用"比例阈值 + 绝对差护栏"双条件做均衡决策
        req_spread = (max(reqs) - min(reqs)) if reqs and None not in (max(reqs), min(reqs)) else None
        # 排队 LIF：waiting 均值的 max/mean（毛刺引擎剔除；与 sgl-router 等路由器的排队感知口径同源，
        # 但路由器为瞬时值、此处为窗口均值，仅作对照）
        qm = mean(qvals) if qvals else None
        queue_lif = (max(qvals) / qm) if (qvals and qm is not None and qm > 0) else None
        g = {
            "engines": em,
            "tags": tags,
            "valid_engines": len(reqs),
            "invalid_tags": invalid_tags,
            "total_req": total_req,
            "shares": shares,
            "req_cv": cv(reqs),
            "req_noise": (1.0 / math.sqrt(mean(reqs))) if reqs and mean(reqs) > 0 else None,
            "req_max": max(reqs) if reqs else None,
            "req_min": min(reqs) if reqs else None,
            "req_spread": req_spread,
            "imbalance": (max(reqs) / min(reqs)) if reqs and min(reqs) > 0 else None,
            "lif": (max(reqs) / mean(reqs)) if reqs and mean(reqs) > 0 else None,
            "gen_cv": cv(gens),
            "prompt_cv": cv(prompts),
            "between_var": between,
            "within_var": within,
            "host_loads": host_loads,
            "host_cv": cv(list(host_loads.values())) if len(host_loads) >= 2 else None,
            "host_noise": (1.0 / math.sqrt(mean(list(host_loads.values())))) if len(host_loads) >= 2 and mean(list(host_loads.values())) > 0 else None,
            "host_lif": (max(host_loads.values()) / mean(list(host_loads.values()))) if len(host_loads) >= 2 and mean(list(host_loads.values())) > 0 else None,
            "within_cv": self._within_cv(tags, host_of, em),
            "ttft_cv": cv(ttf),
            "ttft_corr": pearson(xs, ys),
            "queue_cv": None if qallzero else cv(qvals),
            "queue_lif": queue_lif,
            "queue_conc": conc,
            "queue_top": max(cums, key=cums.get) if tot_cums > 0 else None,
            "queue_cums": cums,           # 非毛刺引擎 waiting 时间累计（集中度口径的分母）
            "queue_cums_all": cums_all,   # 全部引擎累计（毛刺引擎占比展示用）
            "queue_spike_waiting_share": (sum(cums_all[t] for t in spike) / tot_all) if tot_all > 0 else None,
            "queue_spikes": sorted(spike),
            "total_prompt": sum(p for p in prompts if p is not None),
            "total_gen": sum(x for x in gens if x is not None),
        }
        return g

    @staticmethod
    def _decompose(vals, tags, host_of):
        byhost = {}
        for t, v in zip(tags, vals):
            byhost.setdefault(host_of[t], []).append(v)
        # None（窗口增量缺失的引擎）不参与主机级负载统计；全部引擎缺失的主机剔除
        host_loads = {h: sum(v for v in vs if v is not None)
                      for h, vs in byhost.items() if any(v is not None for v in vs)}
        allv = [v for v in vals if v is not None]
        if not allv:
            return 0.0, 0.0, host_loads
        N = len(allv)
        mu = mean(allv)
        between = within = 0.0
        for h, vs in byhost.items():
            vs = [v for v in vs if v is not None]
            if not vs:
                continue
            w = len(vs) / N
            between += w * (mean(vs) - mu) ** 2
            within += w * (pvar(vs) or 0.0)
        return between, within, host_loads

    def _within_cv(self, tags, host_of, em):
        per_host = {}
        for t in tags:
            per_host.setdefault(host_of[t], []).append(em[t]["req"])
        cvs = [(h, cv([v for v in vs if v is not None])) for h, vs in per_host.items()
               if len([v for v in vs if v is not None]) >= 2]
        valid = [c for _, c in cvs if c is not None]
        if not valid:
            return None, []
        weights = {h: c for h, c in cvs}
        n = len([v for vs in per_host.values() for v in vs if v is not None])
        wmean = sum(c * len([v for v in per_host[h] if v is not None]) for h, c in weights.items() if c is not None) / \
            sum(len([v for v in per_host[h] if v is not None]) for h, c in weights.items() if c is not None)
        return wmean, sorted([(h, c) for h, c in cvs if c is not None], key=lambda x: -x[1])


def grade(cv_val, th):
    if cv_val is None:
        return "n/a"
    if cv_val <= th[0]:
        return "优"
    if cv_val <= th[1]:
        return "良"
    if cv_val <= th[2]:
        return "中"
    return "差"


GRADE_TH = {
    "req": (0.05, 0.15, 0.30),
    "host": (0.05, 0.15, 0.30),
    "within": (0.05, 0.15, 0.30),
    "queue": (0.3, 0.6, 1.0),
    "ttft": (0.10, 0.20, 0.40),
    "lif": (1.2, 1.5, 2.0),
    "imbalance": (1.3, 1.7, 2.2),
}


def cv_grade(v):
    """CV 工程经验参考分级（无业界统一标准，vLLM 等官方未发布 CV 目标）：
    <0.05 极度均衡 / 0.05~0.15 健康均衡（在线推理生产合格线）/
    0.15~0.30 轻度倾斜（建议队列深度/剩余 KV 感知路由）/ >0.30 严重倾斜
    （抢占/OOM 风险，考虑 Prefix Cache 感知、PD 分离、扩缩容）。
    注意：小样本时随机底噪 1/√N̄ 即可落入轻度倾斜区间（每引擎 44 个请求时
    底噪≈0.15），样本护栏激活时该分级只展示、不参与横幅判定。"""
    if v is None:
        return "n/a"
    if v < 0.05:
        return "极度均衡"
    if v < 0.15:
        return "健康均衡"
    if v <= 0.30:
        return "轻度倾斜"
    return "严重倾斜"

# 小样本护栏阈值：窗口内每引擎平均请求数低于该值时，CV 中随机波动占比不可忽略，
# 计分卡下挂"样本护栏"提示（噪声 1/√N 于此场景为防误判护栏，不参与评级）。
SMALL_SAMPLE_N = 200


# 计分卡行名 -> 指标定义（悬停提示；行名即指标名，定义告诉读者它量的是什么）
SCORECARD_DEFS = {
    "请求分配 CV": "各引擎窗口请求增量（Δ）的变异系数（标准差÷均值）：引擎间请求分到多少的离散度，越小越均匀",
    "尾部失衡 LIF": "最热引擎请求增量 ÷ 全体引擎均值（max/mean）：最热引擎承载了几倍平均流量",
    "失衡比 max/min": "最热引擎 ÷ 最闲引擎的请求增量（max/min）：最热是最闲的几倍",
    "失衡差 max-min（参考）": "最热与最闲引擎的窗口请求增量绝对差（个）：绝对量纲、跨流量强度不可比，与比例阈值互补（防低流量时比例失真）",
    "主机间 CV": "各主机请求增量之和的变异系数：失衡来自主机间调度/权重不均时变大",
    "主机内端口 CV": "同主机内各引擎请求增量的变异系数（按主机数加权）：失衡来自主机内端口分流时变大",
    "Token 负载 CV（参考）": "各引擎输出 token 增量的变异系数：受请求长度构成影响，不单纯反映路由质量，仅参考",
    "等待队列 CV": "各引擎 waiting 队列长度窗口均值的变异系数（毛刺引擎剔除）：排队是否集中在部分引擎",
    "TTFT 一致性 CV": "各引擎窗口平均首字时延（TTFT）的变异系数：不同引擎响应快慢是否一致，分散说明有引擎在变慢",
}


def rowname_html(name):
    tip = SCORECARD_DEFS.get(name, "")
    if tip:
        return "<td title='%s'>%s</td>" % (esc(tip), esc(name))
    return "<td>%s</td>" % esc(name)


def scorecard(g, ds):
    """计分卡：效应量行（LIF、失衡比、队列、TTFT）按固定档评级，与样本量无关；
    请求/主机间/主机内 CV 按工程经验参考区间分级（cv_grade）——小样本时噪声
    底噪会推高 CV，样本护栏激活时 CV 分级只展示、不参与横幅判定（verdict）；
    Token CV 为参考值不评级：token 分布受请求长度构成影响，不单纯反映路由质量。"""
    wcv, wdetail = g["within_cv"] if g["within_cv"] else (None, [])
    rows = [
        ("请求分配 CV", g["req_cv"], cv_grade(g["req_cv"])),
        ("尾部失衡 LIF", g["lif"], grade(g["lif"], GRADE_TH["lif"])),
        ("失衡比 max/min", g["imbalance"], grade(g["imbalance"], GRADE_TH["imbalance"])),
        ("失衡差 max-min（参考）", g["req_spread"], "参考"),
        ("主机间 CV", g["host_cv"], cv_grade(g["host_cv"])),
        ("主机内端口 CV", wcv, cv_grade(wcv)),
        ("Token 负载 CV（参考）", g["gen_cv"], "参考"),
        ("等待队列 CV", g["queue_cv"], grade(g["queue_cv"], GRADE_TH["queue"])),
    ]
    if ds.fmt == "v2":
        rows.append(("TTFT 一致性 CV", g["ttft_cv"], grade(g["ttft_cv"], GRADE_TH["ttft"])))
    else:
        rows.append(("TTFT 一致性 CV", g["ttft_cv"], "n/a（原版口径不计分）"))
    return rows


def verdict(g, sc):
    """横幅判定：效应量行始终参与；CV 分级行在样本护栏激活时（N̄<200，CV 与
    随机噪声不可分）不参与，避免把噪声误判为倾斜。参与项为 0（全部评级缺失，
    如每引擎仅单采样点且队列/时延列全缺）时不默认「基本均衡」，明示证据不足。"""
    small = bool(small_sample_text(g))
    cv_grades = ("极度均衡", "健康均衡", "轻度倾斜", "严重倾斜")
    rank = {"优": 0, "良": 0, "极度均衡": 0, "健康均衡": 0,
            "中": 1, "轻度倾斜": 1, "差": 2, "严重倾斜": 2}
    worst = 0
    joined = 0
    for _name, _v, gv in sc:
        if gv == "参考" or gv.startswith("n/a"):
            continue
        if small and gv in cv_grades:
            continue
        joined += 1
        worst = max(worst, rank.get(gv, 0))
    if joined == 0:
        return "证据不足", "warn"
    if worst >= 2:
        return "显著失衡", "bad"
    if worst == 1:
        return "存在倾斜", "warn"
    return "基本均衡", "good"


def recommendations(g, ds, sc):
    recs = []
    tot = (g["between_var"] or 0) + (g["within_var"] or 0)
    within_share = (g["within_var"] or 0) / tot if tot > 0 else None
    if within_share is not None and within_share > 0.6:
        wcv = g["within_cv"][0] if g["within_cv"] else None
        if wcv is not None and wcv > 0.10:
            recs.append("失衡主要来自主机内端口间（主机内失衡占比 %s）：优先检查负载入口对同主机各端口的分流逻辑（哈希键选取 / 一致性哈希环 / 端口级亲和配置）。"
                        % pct(within_share))
    if within_share is not None and within_share < 0.4 and (g["host_cv"] or 0) > 0.10:
        recs.append("失衡主要来自主机间（主机间 CV %s）：检查各主机节点权重、健康检查剔除逻辑与调度策略。"
                    % fnum(g["host_cv"]))
    if g["queue_conc"] is not None and g["queue_conc"] > 0.5:
        recs.append("排队集中于引擎 %s（集中度 %s）：核查该引擎是否为慢节点或长序列聚集点。"
                    % (g["queue_top"], pct(g["queue_conc"])))
    if g["ttft_corr"] is not None and g["ttft_corr"] > 0.5 and (g["req_cv"] or 0) > 0.10:
        recs.append("TTFT 与负载正相关（r=%s）：高负载引擎时延恶化，属于过载型 TTFT，均衡分流本身即可改善时延。"
                    % fnum(g["ttft_corr"]))
    if g["imbalance"] is not None and g["imbalance"] > 2.0:
        recs.append("引擎间请求失衡比达 %s 倍（max/min），建议优先复核分发侧哈希与权重配置。" % fnum(g["imbalance"]))
    if g.get("lif") is not None and g["lif"] > 1.5:
        recs.append("尾部失衡明显（LIF=%s，最热引擎承载 %.1f 倍均值流量）：窗口增量口径下将最热引擎约 %.0f%% 的流量迁出即可回到均值，优先调低其路由权重或配置溢出阈值。"
                    % (fnum(g["lif"]), g["lif"], (1 - 1 / g["lif"]) * 100))
    restarts = [t for t in g["tags"] if g["engines"][t]["restarts"] > 0]
    if restarts:
        recs.append("窗口内检测到疑似重启引擎：%s，其累计口径增量已按段处理；如需完整累计请扩大采集时长。" % ", ".join(restarts))
    if not recs:
        recs.append("各维度均未见显著失衡信号；若业务仍感知不均，建议扩大采样时长或多时间窗复测。")
    return recs


def esc(s):
    return html_mod.escape(str(s))


def svg_bars(title, items, unit="", width=920, bar_h=18):
    if not items:
        return '<div class="chart-empty">无数据</div>'
    vmax = max(v for _, v in items) or 1.0
    pad_l, pad_r = 150, 90
    label_w = max(pad_l, min(260, max(len(l) for l, _ in items) * 7))
    H = 34 + bar_h * len(items)
    parts = ['<svg viewBox="0 0 %d %d" class="chart" role="img">' % (width, H)]
    parts.append('<text x="%d" y="16" class="chart-title">%s</text>' % (4, esc(title)))
    for i, (label, v) in enumerate(items):
        y = 26 + i * bar_h
        w = (width - label_w - pad_r) * (v / vmax)
        color = PALETTE[i % len(PALETTE)]
        parts.append('<text x="%d" y="%d" class="tick" text-anchor="end">%s</text>' % (label_w - 6, y + 12, esc(label)))
        parts.append('<rect x="%d" y="%d" width="%.1f" height="%d" fill="%s" rx="2"/>' % (label_w, y + 2, w, bar_h - 6, color))
        parts.append('<text x="%.1f" y="%d" class="tick">%s%s</text>' % (label_w + w + 4, y + 12, fnum(v), unit))
    parts.append("</svg>")
    return "".join(parts)


def md_to_html(text):
    """极简 markdown 渲染：按空行分块；块首为 # 标题则输出标题；
    非空行全部为 -/* 列表项则输出 <ul>；否则合并为段落。

    列表分支必须用显式计数 n 做切片且要求 n>0：n==0 时 out[-0:] 等价 out[0:]，
    会把此前累积的全部 HTML 误删并整体包进 <ul>（标题独占一块时曾触发，
    --extra-analysis 的 markdown 又以「## 标题 + 空行 + 正文」为最常见结构）。"""
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if not lines:
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", lines[0])
        if m:
            lvl = len(m.group(1)) + 1
            out.append("<h%d>%s</h%d>" % (lvl, esc(m.group(2)), lvl))
            lines = lines[1:]
        n = len([ln for ln in lines if ln.strip()])
        if n > 0 and all(re.match(r"^\s*[-*]\s+", ln) for ln in lines if ln.strip()):
            for ln in lines:
                if ln.strip():
                    out.append("<li>%s</li>" % inline_md(ln.strip()[2:]))
            body = "".join(out[-n:])
            del out[-n:]
            out.append("<ul>%s</ul>" % body)
        else:
            joined = " ".join(ln.strip() for ln in lines)
            if joined:
                out.append("<p>%s</p>" % inline_md(joined))
    return "".join(out)


def inline_md(s):
    s = esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return s


CSS = """
body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;margin:0;background:#f4f5f7;color:#1c2733;font-size:14px}
.wrap{max-width:1000px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:22px;margin:8px 0 2px} h2{font-size:17px;margin:26px 0 8px;border-left:4px solid #2563eb;padding-left:8px}
.sub{color:#64748b;font-size:12.5px;margin-bottom:14px}
.banner{padding:12px 18px;border-radius:8px;font-size:16px;font-weight:600;margin:10px 0 6px}
.banner.good{background:#e7f6ec;color:#116530}.banner.warn{background:#fdf3e2;color:#8a5300}.banner.bad{background:#fdeaea;color:#991b1b}
.cards{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}
.card{flex:1 1 130px;background:#fff;border:1px solid #e3e7ee;border-radius:8px;padding:10px 12px}
.card .k{color:#64748b;font-size:12px}.card .v{font-size:19px;font-weight:700;margin-top:3px}.card .x{font-size:11px;color:#94a3b8}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px;margin:8px 0}
th,td{border:1px solid #e3e7ee;padding:5px 8px;text-align:right}
th:first-child,td:first-child{text-align:left}
th{background:#f1f5fb;font-weight:600}
tr:nth-child(even) td{background:#fafbfd}
.g-优{color:#116530;font-weight:700}.g-良{color:#2563eb;font-weight:700}.g-中{color:#b45309;font-weight:700}.g-差{color:#b91c1c;font-weight:700}.g-参考{color:#64748b}.g-极度均衡{color:#116530;font-weight:700}.g-健康均衡{color:#2563eb;font-weight:700}.g-轻度倾斜{color:#b45309;font-weight:700}.g-严重倾斜{color:#b91c1c;font-weight:700}
.chart{width:100%;height:auto;background:#fff;border:1px solid #e3e7ee;border-radius:8px}
.chart-title{font-size:12px;fill:#475569}.grid{stroke:#eef1f5}.tick{font-size:10.5px;fill:#64748b}
.legend{display:flex;flex-wrap:wrap;gap:10px;margin:6px 0;font-size:11.5px;color:#475569}
.leg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.note{background:#fff;border:1px solid #e3e7ee;border-left:4px solid #d97706;border-radius:6px;padding:8px 12px;margin:8px 0}
.ok{border-left-color:#16a34a}.bad2{border-left-color:#dc2626}
.chart-empty{color:#94a3b8;padding:20px;text-align:center;background:#fff;border:1px dashed #e3e7ee;border-radius:8px;margin:8px 0}
.small{font-size:12px;color:#64748b}
"""


def card(k, v, x=""):
    return '<div class="card"><div class="k">%s</div><div class="v">%s</div><div class="x">%s</div></div>' % (k, v, x)


def card2(k, va, vb, x=""):
    """A/B 双值总览卡片：A 蓝、B 橙。"""
    return ('<div class="card"><div class="k">%s</div><div class="v" style="font-size:16px">'
            '<span style="color:#2563eb">%s</span><span style="color:#94a3b8"> / </span>'
            '<span style="color:#d97706">%s</span></div><div class="x">%s</div></div>'
            % (k, va, vb, x))


def cum_last(ds):
    """各引擎 request_success_total 末个有效值（按时间序，防乱序行取错）求和（累计口径）。"""
    tot = 0.0
    for e in ds.eng_list():
        pts = e.valid_points("request_success_total")
        if pts:
            tot += pts[-1][1]
    return tot


def fmt_ts(ts):
    """数据口径时间窗口：与采集侧可读时间同格式（YYYY-MM-DD HH:MM:SS，本地时区）"""
    if ts is None:
        return "n/a"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return str(ts)


def gcell(gv):
    return '<td class="g-%s">%s</td>' % (gv, gv)


def section_zero(ds, g):
    em = g["engines"]
    t0, t1 = ds.window()
    dur = (t1 - t0) if t0 is not None and t1 is not None else 0
    intensity = (g["total_req"] / dur) if dur > 0 else None
    valid_n = g.get("valid_engines")
    eng_txt = str(len(ds.engines)) if (valid_n is None or valid_n == len(ds.engines)) \
        else "%d（有效 %d）" % (len(ds.engines), valid_n)
    rows = [
        ("数据文件", esc(os.path.basename(ds.path))),
        ("采集时间窗口", "%s ~ %s" % (fmt_ts(t0), fmt_ts(t1))),
        ("窗口时长", ("%ss" % fnum(dur, 0)) if dur else "n/a"),
        ("指标列数（口径）", "30 列（v2）" if ds.fmt == "v2" else "20 列（原版）"),
        ("引擎数", eng_txt),
        ("累计请求数（从进程启动开始计数）", fnum(cum_last(ds), 0)),
        ("窗口内新增请求数", fnum(g["total_req"], 0)),
        ("窗口流量强度（req/s）", fnum(intensity, 2) if intensity is not None else "n/a"),
    ]
    miss = sorted(((t, em[t]["missing"]) for t in g["tags"]), key=lambda x: -x[1])
    high_miss = [t for t, m in miss if m > 0.3]
    restarts = [t for t in g["tags"] if em[t]["restarts"] > 0]
    ps = {t: em[t]["process_start"] for t in g["tags"] if em[t]["process_start"] is not None}
    qfixed = [t for t in g["tags"] if (em[t].get("queue_fixed") or 0) > 0]
    quality = ["采样 %d 点 × %ss" % (len(ds.times), ds.step() or 0)]
    if g.get("invalid_tags"):
        quality.append("有效引擎 %s/%d（%s 的请求累计值有效点不足 2 个，窗口增量缺失，已排除出均衡性计算）"
                       % (fnum(g.get("valid_engines"), 0), len(g["tags"]), esc(", ".join(g["invalid_tags"]))))
    if miss:
        quality.append("缺失率最高 %s（%s）" % (pct(miss[0][1]), esc(miss[0][0])))
    quality.append("疑似重启：" + (esc(", ".join(restarts)) if restarts else "无"))
    if ps:
        same = len(set(round(v, 1) for v in ps.values())) == 1
        quality.append("uptime " + ("一致（累计口径可比）" if same else
                                    "不一致（累计口径不可比，%s 个引擎启动时间不同）" % len(set(round(v, 1) for v in ps.values()))))
    html_rows = "".join("<tr><td>%s</td><td>%s</td></tr>" % (k, v) for k, v in rows)
    tail = "<p class='small'>质量检查：%s。统计单位「引擎」= 采集分组（ip_port 或 ip_port.engine），现场采集下 1 个端点 = 1 个 vLLM 服务实例。</p>" % ("；".join(quality))
    if ds.fmt == "legacy":
        tail += "<p class='small'>TTFT 口径：原版累计平均，第五节 TTFT 数值仅供参考，不参与计分。</p>"
    if ds.absent_cols:
        tail += ("<p class='small'>列导出说明：以下列全窗口无任何数据——通常为该部署的 vLLM 版本不导出这些指标"
                 "（如 vLLM v1 无 swapped/request_failure 计数等），属版本能力而非采集丢点；"
                 "已从缺失率分母剔除，其余统计不受影响：%s。</p>" % esc(", ".join(sorted(ds.absent_cols))))
    if qfixed:
        tail += ("<p class='small'>负 queue 修正：%d/%d 个引擎的 queue/prefill 经过已知 vLLM 导出 bug 修正"
                 "（queue_sum&lt;0 时记 queue=0、差值并入 prefill），queue=0 的引擎可能是被修正而非真无排队。</p>"
                 % (len(qfixed), len(g["tags"])))
    notes = ""
    if high_miss:
        notes += '<div class="note bad2">存在缺失率 &gt;30%% 的引擎（%s），相关维度计分已受影响，建议复核采集质量。</div>' % esc(", ".join(high_miss))
    if any(em[t]["restarts"] > 0 for t in g["tags"]):
        notes += '<div class="note">窗口内存在疑似重启，重启跨段的 counter 增量已弃用（按段求和）。</div>'
    return "<table>%s</table>%s%s" % (html_rows, tail, notes)


def small_sample_text(g):
    """小样本护栏文本（无则返回 ""）：窗口内每引擎平均请求 < SMALL_SAMPLE_N 时，
    提示 CV 中的随机成分。

    噪声底噪 = 1/√N̄（泊松计数的随机波动）。它只回答"CV 能否与随机波动区分开"，
    不回答"差异是否重要"——大样本下任何微小偏差都"显著"，小样本下则容易把噪声
    当故障。因此噪声只作护栏提示，不参与评级；是否动手以 LIF/失衡比与业务影响为准。
    """
    # N̄ 按参与均衡计算的有效引擎数计：被剔除的失效引擎没有请求样本，
    # 留在分母里会把 N̄ 拉低、让护栏在样本充足时也误触发
    n_bar = g["total_req"] / max(g.get("valid_engines") or len(g["tags"]), 1)
    noise = g.get("req_noise")
    cvv = g.get("req_cv")
    if noise is None or not noise or n_bar >= SMALL_SAMPLE_N:
        return ""
    var_share = min((noise / cvv) ** 2, 1.0) if (cvv is not None and cvv > 0) else None
    txt = ("样本护栏：每引擎平均仅 %s 个请求（< %s），纯随机分发的 CV 底噪 ≈ 1/√N̄ = %s"
           % (fnum(n_bar, 0), SMALL_SAMPLE_N, fnum(noise, 3)))
    if cvv is not None:
        txt += "；当前请求 CV = %s" % fnum(cvv, 3)
        if var_share is not None and var_share >= 0.05:
            txt += "，其中约 %s 的方差可能来自随机波动" % pct(var_share, 0)
    txt += ("。CV 参考行在此样本量下判读需谨慎，请以 LIF/失衡比等效应量与业务影响为准，"
            "必要时扩大采样时长复测。")
    return txt


def small_sample_note(g):
    txt = small_sample_text(g)
    return "<p class='small'>%s</p>" % txt if txt else ""


def threshold_note(g=None, detailed=True):
    """计分卡评级依据说明（单池与对比版共用）。"""
    note = ('<div class="note"><b>评级说明</b>：等级档位<b>没有业界统一标准</b>（vLLM 等官方未发布 CV 目标），'
            '按工程经验设定，可在脚本中调整。<br>'
            '<b>效应量固定档</b>（与样本量无关）：LIF ≤1.2 优 · ≤1.5 良 · ≤2.0 中 · &gt;2.0 差；'
            '失衡比 max/min ≤1.3 优 · ≤1.7 良 · ≤2.2 中 · &gt;2.2 差；等待队列分散与 TTFT 一致性为固定档。<br>'
            '<b>CV 工程经验参考区间</b>：&lt;0.05 极度均衡（理想，多见于离线批/请求长度统一场景）；'
            '0.05~0.15 健康均衡（在线推理生产合格线，一般不用干预）；0.15~0.30 轻度倾斜（部分引擎短时排队、'
            'KV 偏高，建议队列深度/剩余 KV 感知路由）；&gt;0.30 严重倾斜（热点+饥饿、抢占/OOM 风险，'
            '考虑 Prefix Cache 感知、PD 分离、扩缩容）。<br>'
            '注意：小样本时随机底噪 1/√N̄ 即可落入轻度倾斜区间（每引擎 44 个请求时底噪≈0.15）——'
            '样本护栏激活时 CV 分级<b>只展示、不参与横幅判定</b>。Token 负载 CV 为参考值不评级'
            '（token 分布受请求长度构成影响，不单纯反映路由质量）。<br>'
            '判定优先级：业务影响（TTFT 恶化、排队集中、KV 饱和、抢占）高于档位本身。</div>')
    if not detailed:
        note = ('<div class="note"><b>评级说明</b>：效应量按固定档（LIF/失衡比/队列/TTFT）；'
                'CV 按工程经验参考区间分级（&lt;0.05 极度均衡 / 0.05~0.15 健康均衡 / 0.15~0.30 轻度倾斜 / '
                '&gt;0.30 严重倾斜），小样本时另有样本护栏、CV 不参与横幅判定。'
                '判定优先级：业务影响高于档位本身。</div>')
    if g is not None:
        note += small_sample_note(g)
    return note


def section_one(sc, g):
    rows = ""
    for name, cvv, gv in sc:
        rows += "<tr>%s<td>%s</td>%s</tr>" % (rowname_html(name), fnum(cvv), gcell(gv))
    guide = ("<p class='small'>说明：<b>行名即指标名</b>（悬停可见定义）。"
             "<b>请求分配 CV</b> = 各引擎窗口请求增量的变异系数（引擎间请求分到多少的离散度，越小越均匀）；"
             "主机间/主机内 CV 是同一分布按主机/端口分组的切片，用于定位失衡来源。"
             "效应量行（LIF、失衡比、队列、TTFT）按固定档评级；"
             "请求/主机间/主机内 CV 按工程经验参考区间分级，Token CV 为参考值"
             "（受请求长度构成影响）。小样本时 CV 分级判读以样本护栏提示为准。判定依据见下方评级说明。</p>")
    return ("<table><tr><th>维度</th><th>数值</th><th>评级</th></tr>%s</table>"
            "%s%s" % (rows, guide, threshold_note(g)))


def bar_cell(text, frac, color="#2563eb", td_width="40%"):
    """长条形图列：浅灰轨道占满单元格宽度，彩色长条按比例绘制，数字标注在条内（白）或条外（灰）。

    frac 为 0~1 的相对比例（相对本表最大值）；轨道铺满整格，因此不存在条形图列的空白浪费。
    """
    if frac is None or frac <= 0 or not text:
        return "<td>%s</td>" % (text or "n/a")
    f = max(1.5, min(frac, 1.0) * 100)
    if f >= 16:
        label = ('<span style="position:absolute;left:6px;top:0;font-size:10px;'
                 'line-height:14px;color:#fff">%s</span>' % text)
    else:
        label = ('<span style="position:absolute;left:calc(%.2f%% + 5px);top:0;font-size:10px;'
                 'line-height:14px;color:#475569">%s</span>' % (f, text))
    return ('<td style="width:%s"><div style="position:relative;width:100%%;height:14px;'
            'background:#eef1f6;border-radius:2px">'
            '<div style="position:absolute;left:0;top:0;height:14px;width:%.2f%%;'
            'background:%s;border-radius:2px"></div>%s</div></td>' % (td_width, f, color, label))


def section_two(g, ds=None):
    em = g["engines"]
    items = sorted(((t, em[t]["req"]) for t in g["tags"]),
                   key=lambda x: (x[1] is None, -(x[1] or 0)))
    smax = max((g["shares"][t] for t in g["tags"] if g["shares"].get(t)), default=0)
    # 累计请求数（counter 末值=进程启动以来的累计量）：列存在且有有效数据时才显示
    cum_last = {}
    for t in g["tags"]:
        e = ds.engines.get(t) if ds else None
        if e:
            pts = e.valid_points("request_success_total")  # 按时间序取末值，防行序乱序取错
            if pts:
                cum_last[t] = pts[-1][1]
    has_cum = len(cum_last) > 0
    cum_max = max(cum_last.values()) if cum_last else 0
    bar_w = "16%" if has_cum else "40%"
    rows = ""
    for t, _ in items:
        row = "<tr><td>%s</td><td>%s</td>%s" % (
            esc(t), fnum(em[t]["req"], 0),
            bar_cell(pct(g["shares"][t]),
                     (g["shares"][t] / smax) if (smax and g["shares"][t] is not None) else None,
                     td_width=bar_w))
        if has_cum:
            row += bar_cell(fnum(cum_last[t], 0) if t in cum_last else None,
                            (cum_last[t] / cum_max) if (t in cum_last and cum_max) else None,
                            "#64748b", td_width="16%")
        rows += row + "</tr>"
    head = "<table><tr><th>引擎</th><th>窗口请求增量</th><th>请求占比</th>"
    if has_cum:
        head += "<th>累计请求数</th>"
    tbl = head + "%s</table>" % rows
    guide = ("<p class='small'>说明：请求占比 = 该引擎窗口内新增请求数 ÷ 全部引擎增量之和，条形长度为各引擎请求占比的直观对比——"
             "所有引擎应接近平均值 1/N（本例 N=%d，均值 %s），偏离越多分配越偏斜。"
             "累计请求数从进程启动开始计数（取窗口最后一个采样点的读数），灰色条形为累计占比的直观对比——"
             "与请求占比（蓝色）对照可看出本窗口的失衡是新增现象还是长期分流倾向。"
             "累计口径受各引擎启动时间/重启影响，仅 uptime 一致时可横向比较。增量 n/a 表示该引擎"
             "请求累计值有效点不足 2 个（窗口增量缺失），不参与均衡计算。</p>"
             % (g.get("valid_engines") or len(g["tags"]), pct(1.0 / max(g.get("valid_engines") or len(g["tags"]), 1))))
    return tbl + guide


def section_three(g):
    tot = (g["between_var"] or 0) + (g["within_var"] or 0)
    bsh = (g["between_var"] or 0) / tot if tot > 0 else None
    wsh = (g["within_var"] or 0) / tot if tot > 0 else None
    wcv, wdetail = g["within_cv"] if g["within_cv"] else (None, [])
    hl = g["host_loads"] or {}
    htot = sum(hl.values()) if hl else 0
    lead = ""
    if bsh is not None and htot > 0:
        top_h, top_v = max(hl.items(), key=lambda x: x[1])
        low_h, low_v = min(hl.items(), key=lambda x: x[1])
        if bsh >= wsh:
            lead = ("结论：失衡主要来自<b>主机间</b>（%s）——最重主机 %s 占 %s，最轻主机 %s 仅 %s%s。"
                    % (pct(bsh), esc(top_h), pct(top_v / htot), esc(low_h), pct(low_v / htot),
                       "，同主机内两端口基本均分（主机内 CV %s）" % fnum(wcv, 2) if (wcv is not None and wcv <= 0.10) else ""))
        else:
            lead = ("结论：失衡主要来自<b>主机内端口级分流</b>（%s）——主机之间负载接近，同一主机内部端口份额差异明显（主机内 CV %s）。"
                    % (pct(wsh), fnum(wcv, 2)))
    head = '<div class="cards">%s%s</div>' % (
        card("主机间失衡占比", pct(bsh) if bsh is not None else "n/a"),
        card("主机内失衡占比", pct(wsh) if wsh is not None else "n/a"))
    host_items = sorted(hl.items(), key=lambda x: -x[1])
    hvmax = max((v for _, v in host_items), default=0)
    hrows = "".join("<tr><td>%s</td><td>%s</td>%s</tr>" % (
        esc(h), fnum(v, 0),
        bar_cell(pct(v / htot) if htot else "n/a", (v / hvmax) if hvmax else None)) for h, v in host_items)
    host_tbl = "<table><tr><th>主机</th><th>请求增量</th><th>主机占比</th></tr>%s</table>" % hrows
    detail = ""
    if wdetail:
        detail = "<p class='small'>主机内端口级 CV：%s</p>" % esc(", ".join("%s=%s" % (h, fnum(c, 2)) for h, c in wdetail[:12]))
    howto = ("<p class='small'>说明：把总失衡拆成「主机之间」与「同一主机内部端口之间」两部分——"
             "前者对应调负载入口的主机权重/健康检查，后者对应改哈希键/端口分流逻辑。</p>")
    return ("<p>%s</p>%s%s%s%s" % (lead, head, host_tbl, detail, howto)) if lead else "".join([head, host_tbl, detail, howto])


def section_four(g):
    em = g["engines"]
    items_gen = sorted(((t, em[t]["gen"]) for t in g["tags"]),
                       key=lambda x: (x[1] is None, -(x[1] or 0)))
    gmax = max((v for _, v in items_gen if v), default=0)
    rows = ""
    for t, v in items_gen:
        kvp = em[t].get("kv_peak")
        rows += "<tr><td>%s</td><td>%s</td>%s<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(t), fnum(em[t]["prompt"], 0),
            bar_cell(fnum(v, 0), (v / gmax) if (gmax and v is not None) else None, td_width="18%"),
            pct(em[t]["hit_local"]), pct(em[t]["hit_external"]), pct(em[t]["kv_mean"]),
            pct(kvp) if kvp is not None else "n/a")
    tbl = ("<table><tr><th>引擎</th><th>输入 token 增量</th><th>输出 token 增量</th>"
           "<th>本地前缀命中率</th><th>外部前缀命中率</th><th>KV 缓存占用(均值)</th><th>KV 峰值</th></tr>%s</table>" % rows)
    guide = ("<p class='small'>说明：Token 增量反映各引擎承担的计算量（比请求数更贴近真实负载，长序列请求权重更大）；"
             "条形为输出 token 的直观对比。KV 均值看常态水位，KV 峰值看瞬时最坏情况——"
             "峰值接近 100% 会触发抢占/换出，比均值更能预警容量风险。命中率越高说明缓存复用越好。</p>")
    return tbl + guide


def section_five(g, ds):
    em = g["engines"]
    if ds.fmt == "legacy":
        note = '<div class="note">本数据为原版口径：TTFT 列为累计平均（非窗口增量平均），数值反映自进程启动以来的整体水平，解读当前窗口时延时请谨慎。</div>'
    else:
        note = ""
    corr = g["ttft_corr"]
    corr_txt = "n/a" if corr is None else "%s（%s相关）" % (fnum(corr), "强" if abs(corr) > 0.7 else ("中等" if abs(corr) > 0.3 else "弱"))
    head = '<div class="cards">%s%s%s%s</div>' % (
        card("TTFT CV", fnum(g["ttft_cv"]), "窗口平均口径" if ds.fmt == "v2" else "累计平均口径"),
        card("TTFT-负载相关 r", corr_txt),
        card("Queue 窗口均值", fnum(mean([em[t]["queue"] for t in g["tags"] if em[t]["queue"] is not None]), 3), "秒"),
        card("Prefill 窗口均值", fnum(mean([em[t]["prefill"] for t in g["tags"] if em[t]["prefill"] is not None]), 3), "秒"))
    rows = ""
    for t in g["tags"]:
        rows += ("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(t), fnum(em[t]["req"], 0), pct(g["shares"][t]),
            fnum(em[t]["ttft"], 3), fnum(em[t]["queue"], 3), fnum(em[t]["prefill"], 3), fnum(em[t]["e2e"], 3)))
    tbl = ("<table><tr><th>引擎</th><th>请求增量</th><th>请求占比</th><th>TTFT</th><th>Queue</th><th>Prefill</th><th>E2E</th></tr>%s</table>" % rows)
    guide = ("<p class='small'>说明：按 vLLM 官方口径，请求时延由事件链 arrival→QUEUED→SCHEDULED→首个 NEW_TOKENS 组成："
             "TTFT ≈ 输入处理 + Queue（排队等调度）+ Prefill（预填充计算），首 token 在预填充末产生。"
             "对比各引擎三列可定位差异来源——Queue 差异大 = 过载型（分流不均），Prefill 差异大 = 序列长度或算力差异，"
             "TTFT 明显大于 Queue+Prefill 时说明还有其他开销。上方 r 为请求占比与 TTFT 的相关性：越接近 1 说明负载越重的引擎时延越差。</p>")
    return note + head + tbl + guide


def section_six(ds, g):
    """窗口内分流概览表：引擎 / 增量 / 窗口速率 / running 均值。"""
    em = g["engines"]
    w0, w1 = ds.window()
    dur = (w1 - w0) if (w0 is not None and w1 is not None and w1 > w0) else 0
    rows = []
    for t in g["tags"]:
        req = em[t]["req"]
        rate = (req / dur) if (req is not None and dur) else None
        running = em[t].get("running_mean")
        rows.append((t, req, rate, running))
    rows.sort(key=lambda x: -(x[1] or 0))
    rmax = max((r for _t, r, _ra, _ru in rows if r), default=0)
    tbl_rows = "".join(
        "<tr><td>%s</td>%s<td>%s</td><td>%s</td></tr>" % (
            esc(t),
            bar_cell(fnum(req, 0), (req / rmax) if (req and rmax) else None),
            ("%s req/s" % fnum(rate, 2)) if rate is not None else "n/a",
            fnum(running, 1))
        for t, req, rate, running in rows)
    tbl = ("<table><tr><th>引擎</th><th>增量</th><th>窗口速率</th><th>running 均值</th></tr>%s</table>"
           % tbl_rows)
    guide = ("<p class='small'>说明：增量为窗口内该引擎处理的新增请求数（条形按各引擎增量比例绘制）；"
             "窗口速率 = 增量 ÷ 窗口时长；running 均值为运行中请求数的窗口平均——"
             "增量/速率差异即分流倾斜，running 均值差异反映各引擎并发占用是否同步失衡。</p>")
    return tbl + guide


def section_seven(g, ds):
    em = g["engines"]
    # 集中度口径：非毛刺引擎之间归一（与「排队集中度」卡片一致）；
    # 毛刺引擎只展示其占全窗口 waiting 的比例，不参与集中度判定与 top1 标注
    cums = g["queue_cums"]
    cums_all = g["queue_cums_all"]
    tot = sum(cums.values())
    tot_all = sum(cums_all.values())
    cmax = max(cums.values()) if cums else 0
    rows = ""
    for t in g["tags"]:
        spike = t in g["queue_spikes"]
        if spike:
            label = pct(cums_all[t] / tot_all) if tot_all > 0 else "0.0%"
        else:
            label = pct(cums[t] / tot) if tot > 0 else "0.0%"
            if g["queue_top"] == t:
                label += "（集中）"
        rows += ("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>%s</tr>" % (
            esc(t) + ("（毛刺，不参与计分）" if spike else ""),
            fnum(em[t]["waiting_mean"], 3), fnum(em[t]["waiting_peak"], 0),
            pct(em[t]["waiting_nonzero"]), pct(g["shares"][t]),
            bar_cell(label, None if spike else ((cums[t] / cmax) if cmax else None), "#dc2626", td_width="18%")))
    tbl = ("<table><tr><th>引擎</th><th>waiting 均值</th><th>峰值</th><th>非零占比</th>"
           "<th>请求占比</th><th>waiting 累计占比</th></tr>%s</table>" % rows)
    head = ('<div class="cards">%s%s%s</div>' % (
        card("排队集中度", pct(g["queue_conc"]), "最大非毛刺引擎 waiting 累计占比"),
        card("waiting CV", fnum(g["queue_cv"]), "毛刺引擎已剔除"),
        card("排队 LIF", fnum(g["queue_lif"]), "waiting 均值 max/mean（窗口口径）")))
    spike_note = ""
    if g["queue_spikes"] and (g.get("queue_spike_waiting_share") or 0) > 0:
        spike_note = ("<p class='small'>毛刺说明：%s 的 waiting 非零占比 &lt;5%%，已降级为「毛刺」："
                      "其 waiting 累计占全窗口的 %s，不参与集中度 / waiting CV / 排队 LIF 口径"
                      "（表中占比列按全窗口总量归一展示，其余引擎按非毛刺总量归一，与集中度卡片同口径）。</p>"
                      % (esc(", ".join(g["queue_spikes"])), pct(g["queue_spike_waiting_share"])))
    guide = ("<p class='small'>说明：waiting 为各引擎等待队列长度——单引擎持续有等待说明该引擎过载；"
             "所有引擎都在排队说明整体供不应求（不是分流问题）。红色条形为各引擎 waiting 时间累计占比"
             "（标注「集中」的为排队 top1，即排队集中度卡片对应的引擎，毛刺引擎不参与该判定）。"
             "排队 LIF（waiting 均值的 max/mean）衡量排队向尾部热点引擎的集中程度：与 waiting CV 互补——"
             "CV 看各引擎排队水平的整体离散度，LIF 直指最热队列是平均的几倍。</p>")
    return head + tbl + spike_note + guide


# 短窗口阈值：采样窗口短于该时长且判定非均衡时，报告挂"窗口范围说明"——
# 窗口内失衡 ≠ 长期系统性偏置（突发/阶段性流量都会推高短窗 CV）。
SHORT_WINDOW_SEC = 1800


def fmt_dur(sec):
    """时长自适应格式化：<1 分钟按秒显示（避免四舍五入成"0 分钟"），<1 小时按分钟，其余按小时"""
    if sec is None or sec <= 0:
        return "n/a"
    if sec < 60:
        return "%s 秒" % fnum(sec, 0)
    if sec < 3600:
        return "%s 分钟" % fnum(sec / 60, 0)
    return "%s 小时" % fnum(sec / 3600, 1)


def window_scope_note(ds, verdict_txt):
    """短窗口判定非均衡时提示"窗口内失衡不等于长期系统性偏置"。

    两件事的区分：窗口判定回答"采样时段内是否失衡"（对窗口内用户体验真实）；
    长窗口/累计均衡只说明"路由无长期系统性偏置"。短窗失衡 + 长窗均衡 = 时段性
    现象，处置方向是突发应对（队列感知路由、突发吸收），不是调长期权重。"""
    t0, t1 = ds.window()
    dur = (t1 - t0) if t0 is not None and t1 is not None else 0
    if not dur or dur >= SHORT_WINDOW_SEC or verdict_txt in ("基本均衡", "证据不足"):
        return ""
    return ('<div class="note">窗口范围说明：本判定仅基于 %s 的采样窗口。窗口内失衡不代表长期'
            '系统性偏置——突发/阶段性流量与小样本随机波动都会推高短窗口 CV；若更长时间窗'
            '（或累计口径）显示均衡，说明路由无长期偏置，本窗口的失衡属于时段性现象。'
            '但它对窗口内用户体验是真实的（时延与排队按即时发生计）：请结合第七节窗口内走势'
            '判断失衡是否集中于某一时段，并以业务影响（TTFT 相关性、排队、KV）决定是否需要'
            '突发应对（如队列感知路由），而不是调整长期权重。</div>' % fmt_dur(dur))


def section_eight(g, ds, sc, recs, extra_html=""):
    v, cls = verdict(g, sc)
    items = []
    if g["req_cv"] is not None:
        items.append("请求 CV %s（参考）" % fnum(g["req_cv"], 2))
    if g["lif"] is not None:
        items.append("尾部失衡 LIF %s（最热引擎承载 %.1f 倍均值流量）" % (fnum(g["lif"], 2), g["lif"]))
    if g["imbalance"] is not None:
        items.append("失衡比 max/min %s" % fnum(g["imbalance"], 2))
    if g["req_spread"] is not None:
        items.append("失衡差 max-min %s 个请求（最热与最闲引擎的窗口增量绝对差）" % fnum(g["req_spread"], 0))
    n_bar = g["total_req"] / max(g.get("valid_engines") or len(g["tags"]), 1)  # 与样本护栏同口径：按有效引擎数
    if n_bar < SMALL_SAMPLE_N and g.get("req_noise"):
        items.append("样本护栏：每引擎平均仅 %s 个请求，随机底噪 ≈ 1/√N̄ = %s，CV 参考值判读需谨慎"
                     % (fnum(n_bar, 0), fnum(g["req_noise"], 3)))
    tot = (g["between_var"] or 0) + (g["within_var"] or 0)
    if tot > 0:
        items.append("失衡结构：主机间方差 %s / 主机内 %s" % (
            pct((g["between_var"] or 0) / tot), pct((g["within_var"] or 0) / tot)))
    if g["ttft_cv"] is not None and ds.fmt == "v2":
        items.append("TTFT 一致性 CV %s" % fnum(g["ttft_cv"], 2))
    if g["ttft_corr"] is not None:
        items.append("TTFT-负载相关 r=%s" % fnum(g["ttft_corr"], 2))
    if g["queue_conc"] is not None:
        items.append("排队集中度 %s%s" % (pct(g["queue_conc"]),
                                    ("（top1：%s）" % g["queue_top"]) if g["queue_top"] and g["queue_conc"] > 0.5 else ""))
    shares = {t: v for t, v in g["shares"].items() if v is not None}
    if shares:
        top_t = max(shares, key=shares.get)
        kvp = g["engines"][top_t].get("kv_peak") if top_t in g["engines"] else None
        if kvp is not None:
            # top_t 不在此处预转义：items 统一经 inline_md() 转义，预转义会导致二次转义
            items.append("最热引擎 %s：请求占比 %s、KV 峰值 %s" % (top_t, pct(shares[top_t]), pct(kvp)))
    items.extend(recs)
    if v == "证据不足":
        items.append("参与判定的评级项为 0（全部缺失）：无有效判据，建议延长采样时长、确认指标导出后补采重跑")
    ul = "<ul>%s</ul>" % "".join("<li>%s</li>" % inline_md(x) for x in items)
    return window_scope_note(ds, v) + ul + extra_html


def banner_detail(g):
    """单池报告横幅：判定后的简要依据（CV/LIF、失衡来源、热点、时延/排队）。"""
    parts = []
    if g["req_cv"] is not None:
        parts.append("请求 CV %s、尾部 LIF %s" % (fnum(g["req_cv"], 2), fnum(g["lif"], 2)))
    tot = (g["between_var"] or 0) + (g["within_var"] or 0)
    if tot > 0:
        bsh = (g["between_var"] or 0) / tot
        wsh = (g["within_var"] or 0) / tot
        if bsh >= wsh:
            hl = g["host_loads"] or {}
            top = max(hl.items(), key=lambda x: x[1]) if hl else None
            parts.append("失衡 %s 来自主机间%s" % (
                pct(bsh), ("（主机 %s 负载最高，%s）" % (esc(top[0]), pct(top[1] / sum(hl.values())))) if top else ""))
        else:
            parts.append("失衡 %s 来自主机内端口级分流" % pct(wsh))
    shares = {t: v for t, v in g["shares"].items() if v is not None}
    if shares and g["lif"]:
        top_t, top_s = max(shares.items(), key=lambda x: x[1])
        parts.append("最热引擎 %s 请求占比 %s（%s 倍均值）" % (esc(top_t), pct(top_s), fnum(g["lif"], 1)))
    if g["ttft_corr"] is not None and g["ttft_corr"] >= 0.7:
        parts.append("TTFT 与负载强相关（r=%s），属过载型时延" % fnum(g["ttft_corr"], 2))
    if g["queue_conc"] is not None and g["queue_conc"] > 0.5 and g["queue_top"]:
        parts.append("排队集中于 %s（%s）" % (esc(g["queue_top"]), pct(g["queue_conc"])))
    return "；".join(parts)


def render_single(ds, g, title, extra_html=""):
    sc = scorecard(g, ds)
    recs = recommendations(g, ds, sc)
    v, cls = verdict(g, sc)
    detail = banner_detail(g)
    if v == "证据不足":
        detail = "参与判定的评级项为 0（采样点/有效引擎/指标列不足），无有效判据——建议延长采样时长、确认指标导出后补采重跑"
    banner = ('<div class="banner %s">综合结论：%s%s</div>'
              % (cls, v, (' <span class="small">—— %s</span>' % detail) if detail else ""))
    cards = ('<div class="cards">%s%s%s%s%s%s</div>' % (
        card("引擎数", str(len(ds.engines)), "每端点=1 vLLM 实例"),
        card("窗口请求总量", fnum(g["total_req"], 0)),
        card("请求 CV", fnum(g["req_cv"]), "越小越均衡"),
        card("尾部失衡 LIF", fnum(g["lif"]), "max/均值，越小越均衡"),
        card("失衡比 max/min", fnum(g["imbalance"])),
        card("主机内方差占比", pct((g["within_var"] or 0) / ((g["between_var"] or 0) + (g["within_var"] or 0)) if (g["between_var"] or 0) + (g["within_var"] or 0) > 0 else None))))
    secs = [
        ("一、数据口径", section_zero(ds, g)),
        ("二、均衡性计分卡", section_one(sc, g)),
        ("三、请求分配（窗口增量口径）", section_two(g, ds)),
        ("四、失衡来源", section_three(g)),
        ("五、Token 负载与缓存命中", section_four(g)),
        ("六、TTFT 与时延分解", section_five(g, ds)),
        ("七、窗口内均衡走势与引擎分流", section_six(ds, g)),
        ("八、等待队列分布", section_seven(g, ds)),
        ("九、综合结论与建议", section_eight(g, ds, sc, recs, extra_html)),
    ]
    body = banner + cards + "".join("<h2>%s</h2>%s" % (t, c) for t, c in secs)
    return page(title, ds.label, body)


def section_three_compare(dsa, ga, dsb, gb):
    """对比版：A/B 失衡结构对照（主机间 vs 主机内端口级）。"""
    def split(g):
        tot = (g["between_var"] or 0) + (g["within_var"] or 0)
        if tot <= 0:
            return None, None
        return (g["between_var"] or 0) / tot, (g["within_var"] or 0) / tot

    ba, wa_ = split(ga)
    bb, wb_ = split(gb)

    def src(bsh, wsh):
        if bsh is None:
            return "n/a"
        return "主机间（%s）" % pct(bsh) if bsh >= wsh else "主机内端口级（%s）" % pct(wsh)

    cards = '<div class="cards">%s%s%s</div>' % (
        card2("主要失衡来源", src(ba, wa_), src(bb, wb_)),
        card2("主机间失衡占比", pct(ba), pct(bb)),
        card2("主机内失衡占比", pct(wa_), pct(wb_)))
    hla = ga["host_loads"] or {}
    hlb = gb["host_loads"] or {}
    hosts = sorted(set(hla) | set(hlb))
    tota = sum(hla.values()) or 1.0
    totb = sum(hlb.values()) or 1.0
    hamax = max((v for v in hla.values() if v), default=0)
    hbmax = max((v for v in hlb.values() if v), default=0)
    rows = ""
    for h in hosts:
        va = hla.get(h)
        vb = hlb.get(h)
        rows += "<tr><td>%s</td><td>%s</td><td>%s</td>%s%s</tr>" % (
            esc(h), fnum(va, 0), fnum(vb, 0),
            bar_cell(pct(va / tota) if va is not None else "n/a", (va / hamax) if (va and hamax) else None, "#2563eb", td_width="16%"),
            bar_cell(pct(vb / totb) if vb is not None else "n/a", (vb / hbmax) if (vb and hbmax) else None, "#d97706", td_width="16%"))
    tbl = ("<table><tr><th>主机</th><th>A 请求增量</th><th>B 请求增量</th>"
           "<th>A 主机占比</th><th>B 主机占比</th></tr>%s</table>" % rows)
    wca, wda = ga["within_cv"] if ga["within_cv"] else (None, [])
    wcb, wdb = gb["within_cv"] if gb["within_cv"] else (None, [])
    det = "<p class='small'>主机内端口级 CV（加权）：A = %s，B = %s" % (fnum(wca, 2), fnum(wcb, 2))
    if wda:
        # 主机名来自 CSV 的 ip_port（远端可控），必须逐个 esc()——与单池版 section_three
        # 同口径，未转义会把 <img>/<script> 等标记注入自包含报告（HTML 注入）
        det += "；A 明细：" + ", ".join("%s=%s" % (esc(h), fnum(c, 2)) for h, c in wda[:8])
    if wdb:
        det += "；B 明细：" + ", ".join("%s=%s" % (esc(h), fnum(c, 2)) for h, c in wdb[:8])
    det += "。</p>"
    howto = ("<p class='small'>说明：把总失衡拆成「主机之间」与「同一主机内部端口之间」两部分——"
             "前者对应调负载入口的主机权重/健康检查，后者对应改哈希键/端口分流逻辑。</p>")
    return cards + tbl + det + howto


def section_compare_flow(dsa, ga, dsb, gb, inter):
    """对比版六：窗口内分流概览对照表（引擎 / A、B 增量（条形）/ 速率 / running 均值）。"""
    da = (dsa.window()[1] - dsa.window()[0]) if dsa.times else 0
    db = (dsb.window()[1] - dsb.window()[0]) if dsb.times else 0
    amax = max((ga["engines"][t]["req"] for t in inter
                if ga["engines"].get(t) and ga["engines"][t]["req"] is not None), default=0)
    bmax = max((gb["engines"][t]["req"] for t in inter
                if gb["engines"].get(t) and gb["engines"][t]["req"] is not None), default=0)
    rows = ""
    for t in inter:
        ea = ga["engines"].get(t, {})
        eb = gb["engines"].get(t, {})
        ra = ea.get("req")
        rb = eb.get("req")
        rate_txt = "%s / %s" % (fnum(ra / da, 2) if (ra and da) else "n/a",
                                 fnum(rb / db, 2) if (rb and db) else "n/a")
        run_txt = "%s / %s" % (fnum(ea.get("running_mean"), 1), fnum(eb.get("running_mean"), 1))
        rows += ("<tr><td>%s</td>%s%s<td>%s</td><td>%s</td></tr>" % (
            esc(t),
            bar_cell(fnum(ra, 0), (ra / amax) if (ra and amax) else None, "#2563eb", td_width="16%"),
            bar_cell(fnum(rb, 0), (rb / bmax) if (rb and bmax) else None, "#d97706", td_width="16%"),
            rate_txt, run_txt))
    tbl = ("<table><tr><th>引擎</th><th>A 增量</th><th>B 增量</th>"
           "<th>窗口速率 A/B</th><th>running 均值 A/B</th></tr>%s</table>" % rows)
    guide = ("<p class='small'>说明：增量为各引擎窗口内新增请求数（A 蓝 / B 橙条形按各自最大值绘制）；"
             "窗口速率 = 增量 ÷ 窗口时长；running 均值为运行中请求数的窗口平均。"
             "对比两侧条形与数值即可看出两种策略的分流差异。</p>")
    return tbl + guide


def section_compare_queue(ga, gb, inter):
    """对比版：等待队列对照（集中度卡片 + 每引擎 waiting 均值/峰值/非零占比）。"""
    rows = ""
    for t in inter:
        ea, eb = ga["engines"].get(t, {}), gb["engines"].get(t, {})
        rows += "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(t), fnum(ea.get("waiting_mean"), 3), fnum(eb.get("waiting_mean"), 3),
            fnum(ea.get("waiting_peak"), 0), fnum(eb.get("waiting_peak"), 0),
            pct(ea.get("waiting_nonzero")) + " / " + pct(eb.get("waiting_nonzero")))
    tbl = ("<table><tr><th>引擎</th><th>A waiting 均值</th><th>B waiting 均值</th>"
           "<th>A 峰值</th><th>B 峰值</th><th>非零占比 A/B</th></tr>%s</table>" % rows)
    cards = '<div class="cards">%s%s</div>' % (
        card2("排队集中度", pct(ga["queue_conc"]), pct(gb["queue_conc"]), "最大引擎 waiting 累计占比"),
        card2("waiting CV", fnum(ga["queue_cv"]), fnum(gb["queue_cv"]), "毛刺引擎已剔除"))
    return cards + tbl


def section_compare_summary(dsa, ga, dsb, gb, ratio=None):
    """对比报告底部：综合总结 + 指标佐证清单。"""
    def wshare(g):
        vs = [v for v in g["shares"].values() if v is not None]
        return max(vs) if vs else None

    def within_cv(g):
        return g["within_cv"][0] if g["within_cv"] else None

    def within_share(g):
        tot = (g["between_var"] or 0) + (g["within_var"] or 0)
        return (g["within_var"] or 0) / tot if tot > 0 else None

    rows = [
        ("引擎请求 CV", ga["req_cv"], gb["req_cv"], "num"),
        ("尾部失衡 LIF", ga["lif"], gb["lif"], "num"),
        ("失衡比 max/min", ga["imbalance"], gb["imbalance"], "num"),
        ("最热引擎请求占比", wshare(ga), wshare(gb), "pct"),
        ("主机间 CV", ga["host_cv"], gb["host_cv"], "num"),
        ("主机内端口 CV", within_cv(ga), within_cv(gb), "num"),
        ("TTFT 一致性 CV", ga["ttft_cv"], gb["ttft_cv"], "num"),
        ("排队集中度", ga["queue_conc"], gb["queue_conc"], "pct"),
    ]
    items, wins_a, wins_b, total = [], 0, 0, 0
    for name, va, vb, kind in rows:
        if va is None or vb is None:
            continue
        total += 1
        fa = pct(va, 2) if kind == "pct" else fnum(va, 3)
        fb = pct(vb, 2) if kind == "pct" else fnum(vb, 3)
        if abs(va - vb) < 1e-9:
            items.append("<li>%s：A %s = B %s（持平）</li>" % (name, fa, fb))
            continue
        better_a = va < vb
        if better_a:
            wins_a += 1
        else:
            wins_b += 1
        delta = ""
        if va > 0:
            delta = "（%s%.0f%%）" % ("↓" if vb < va else "↑", abs(vb - va) / va * 100)
        items.append("<li>%s：A %s → B %s%s<b>（%s 更优）</b></li>"
                     % (name, fa, fb, delta, esc(dsa.label if better_a else dsb.label)))

    cva, cvb = ga["req_cv"], gb["req_cv"]
    guarded = bool(small_sample_text(ga)) or bool(small_sample_text(gb))
    # 评审修复：① 一侧零流量时 ratio 为 0，1/ratio 会除零且对比本身不可比；
    # ② 引擎集合不一致时胜负判定基于各自全池 CV，独有引擎可翻转结论 → 暂停判定；
    #    平局（CV 差 <0.02 的「基本相当」）同样基于不同引擎全集，一并暂停——
    #    集合差异先于平局/胜负分支处理，不带 CV 差值门槛；
    # ③ 胜方按请求 CV 选出，占优项数必须取该方自身的计数（max(wins_a, wins_b)
    #    可能把另一方的胜场写给胜方）
    only_a = sorted(set(ga["tags"]) - set(gb["tags"]))
    only_b = sorted(set(gb["tags"]) - set(ga["tags"]))
    zero_side = (ga["total_req"] == 0 or gb["total_req"] == 0)
    if zero_side:
        lead = ("对比不可比：一侧窗口零流量（A 总请求 %s，B 总请求 %s）——无新增请求的一侧不存在"
                "请求分布形态，本对比无意义；请核对窗口内是否确无流量或采集异常，必要时重采。"
                % (fnum(ga["total_req"], 0), fnum(gb["total_req"], 0)))
    elif (only_a or only_b) and cva is not None and cvb is not None:
        lead = ("引擎集合不一致（仅 A 有 %s；仅 B 有 %s）：请求 CV %s vs %s 基于各自全池引擎，"
                "独有引擎即可翻转结论——胜负与平局判定一并不作，请对齐两份采集的引擎集合后重比，"
                "或参考「三、请求分配对比（相同引擎）」的逐引擎明细。"
                % (esc(", ".join(only_a)) or "无", esc(", ".join(only_b)) or "无",
                   fnum(cva, 3), fnum(cvb, 3)))
    elif cva is not None and cvb is not None and abs(cva - cvb) >= 0.02 and guarded:
        # 与横幅同口径：护栏激活时不作胜负判定，避免「顶部宣布胜负、计分卡自曝底噪」自相矛盾
        lead = ("样本护栏激活（A/B 任一池每引擎平均请求 &lt; %s）：请求 CV 差异 %s vs %s 可能来自随机波动，"
                "本报告不作胜负判定，请以 LIF/失衡比等效应量与业务影响为准，必要时扩大采样时长复测。"
                % (SMALL_SAMPLE_N, fnum(cva, 3), fnum(cvb, 3)))
    elif cva is None or cvb is None or total == 0:
        lead = ("对比证据不足：请求分布指标缺失或可比指标为 0 项（有效引擎不足/采样点过少），"
                "无可支撑对比的判据，请检查两份数据的采集质量后补采重试。")
    elif abs(cva - cvb) < 0.02:
        lead = "综合各项指标，两者的均衡效果基本相当（请求 CV 差异 &lt; 0.02），没有明显的均衡性优劣。"
    else:
        if cva < cvb:
            wl, wn, ln = dsa.label, wins_a, wins_b
        else:
            wl, wn, ln = dsb.label, wins_b, wins_a
        lead = ("综合各项指标，<b>%s</b> 的负载均衡效果更好：在 %d 项可比指标中占优 %d 项"
                "（请求 CV %s vs %s，尾部失衡 LIF %s vs %s）" % (
                    esc(wl), total, wn,
                    fnum(cva, 3), fnum(cvb, 3), fnum(ga["lif"], 2), fnum(gb["lif"], 2)))
        wsa, wsb = within_share(ga), within_share(gb)
        if wsa is not None and wsb is not None and abs(wsa - wsb) >= 0.2:
            # 按两侧 within 占比的实际大小关系措辞（不套固定方向模板）：占比较高一侧
            # 的失衡更多来自主机内端口级分流，较低一侧更多来自主机间调度；仅当较低
            # 一侧确实 <0.2 时才用「微弱残余」定性，否则只陈述数值（评审意见：
            # 固定模板会在占优方 within 占比更高时输出与事实相反的结构结论）
            if wsa > wsb:
                hi_l, lo_l, hi_v, lo_v = dsa.label, dsb.label, wsa, wsb
            else:
                hi_l, lo_l, hi_v, lo_v = dsb.label, dsa.label, wsb, wsa
            lead += ("；失衡结构上，主机内方差占比 %s 为 %s、%s 为 %s：%s 的失衡更多来自"
                     "主机内端口级分流，%s 更多来自主机间调度"
                     % (esc(hi_l), pct(hi_v), esc(lo_l), pct(lo_v), esc(hi_l), esc(lo_l)))
            if lo_v < 0.2:
                lead += ("（%s 侧主机内仅 %s、属微弱残余，排查 %s 侧优先看主机间权重/健康检查）"
                         % (esc(lo_l), pct(lo_v), esc(lo_l)))
        if ln:
            lead += "；另有 %d 项指标另一方略好，见下方逐项对照" % ln
        lead += "。"
    if not zero_side and ratio is not None and (ratio > 1.5 or ratio < 1 / 1.5):
        lead += ('<br><span class="small">注：两窗口流量强度差 %.2fx，结论基于分布形态与窗口请求占比，'
                 '不比较累计绝对量。</span>' % max(ratio, 1 / ratio))
    wa_ = dsa.window()
    wb_ = dsb.window()
    da_ = (wa_[1] - wa_[0]) if dsa.times else 0
    db_ = (wb_[1] - wb_[0]) if dsb.times else 0
    scope = ""
    if da_ and db_ and da_ < SHORT_WINDOW_SEC and db_ < SHORT_WINDOW_SEC:
        scope = ('<div class="note">窗口范围说明：两个采样窗口均较短（&lt; %s 分钟）。短窗口的 A/B 对比'
                 '容易被突发/阶段性流量与小样本随机波动主导——某一边窗口内恰逢突发就会"赢得"或"输掉"对比。'
                 '建议用更长时间窗（Prometheus 回采，相同 step 与时长）复核本结论后再做策略决策。</div>'
                 % fnum(SHORT_WINDOW_SEC / 60, 0))
    return ("<h2>九、综合总结</h2><p>%s</p><ul>%s</ul>%s" % (lead, "".join(items), scope))


def render_compare(dsa, ga, dsb, gb, title, extra_html=""):
    sca = scorecard(ga, dsa)
    scb = scorecard(gb, dsb)
    tags_a = set(ga["tags"])
    tags_b = set(gb["tags"])
    only_a = sorted(tags_a - tags_b)
    only_b = sorted(tags_b - tags_a)
    inter = sorted(tags_a & tags_b)
    ratio = (ga["total_req"] / gb["total_req"]) if gb["total_req"] else None
    # 评审修复：一侧零流量时 ratio 为 0，max(ratio, 1/ratio) 会除零；且零流量一侧
    # 不存在分布形态，对比不可比——单独标记并在横幅与总结中明示
    zero_side = bool(ga["total_req"] == 0 or gb["total_req"] == 0)
    intensity_warn = (not zero_side) and ratio is not None and (ratio > 1.5 or ratio < 1 / 1.5)
    d0 = (dsa.window()[1] - dsa.window()[0]) if dsa.times else 0
    d1 = (dsb.window()[1] - dsb.window()[0]) if dsb.times else 0
    step_ok = dsa.step() == dsb.step() and d0 and d1 and abs(d0 - d1) <= max(dsa.step() or 0, 1)
    notes = ""
    if only_a or only_b:
        notes += '<div class="note">引擎集合不一致：仅 A 有 %s；仅 B 有 %s。对比仅基于相同引擎。</div>' % (esc(", ".join(only_a)) or "无", esc(", ".join(only_b)) or "无")
    if zero_side:
        notes += ('<div class="note bad2">一侧窗口零流量（A 总请求 %s，B 总请求 %s）：无新增请求的一侧'
                  '不存在请求分布形态，本对比不可比——请核对窗口内是否确无流量或采集异常，必要时重采。</div>'
                  % (fnum(ga["total_req"], 0), fnum(gb["total_req"], 0)))
    if not step_ok:
        notes += '<div class="note">两份数据时长/步长不一致（A: %ss/%s，B: %ss/%s），窗口增量对比存在口径偏差，建议同时长同步长采集。</div>' % (
            fnum(d0, 0), dsa.step(), fnum(d1, 0), dsb.step())
    if intensity_warn:
        notes += '<div class="note bad2">流量强度差 %.2fx（A 总请求 %s vs B %s）：对比结论只看分布形态与窗口增量请求占比，累计绝对量不可比。</div>' % (
            max(ratio, 1 / ratio), fnum(ga["total_req"], 0), fnum(gb["total_req"], 0))
    # A/B uptime（process_start）一致性：跨段重启会导致累计 counter 不可比
    ps_a = {t: ga["engines"][t]["process_start"] for t in ga["tags"] if ga["engines"][t]["process_start"] is not None}
    ps_b = {t: gb["engines"][t]["process_start"] for t in gb["tags"] if gb["engines"][t]["process_start"] is not None}
    if ps_a and ps_b:
        ma = round(max(ps_a.values()), 1)
        mb = round(max(ps_b.values()), 1)
        if abs(ma - mb) > 1.0:
            notes += ('<div class="note">两段 uptime 不一致（A 最近 process_start≈%s，B≈%s，说明两段之间发生过重启），'
                      '累计 counter 绝对量跨段不可比；本报告结论基于窗口增量口径，不受影响。</div>' % (fnum(ma, 0), fnum(mb, 0)))
    # 负 queue 修正比例差异：修正比例悬殊时 queue/prefill 两列跨段口径不一致，读者易误判
    qfa = sum(1 for t in ga["tags"] if (ga["engines"][t].get("queue_fixed") or 0) > 0)
    qfb = sum(1 for t in gb["tags"] if (gb["engines"][t].get("queue_fixed") or 0) > 0)
    if (qfa or qfb) and abs(qfa - qfb) / max(len(ga["tags"]), len(gb["tags"]), 1) > 0.2:
        notes += ('<div class="note bad2">两段被负 queue 修正的引擎比例差异较大（A %d/%d，B %d/%d）：'
                  '被修正引擎 queue=0、差值并入 prefill，未修正引擎保留原始 queue/prefill——'
                  '跨段逐引擎比较 queue_avg/prefill_avg 时请以 queue+prefill 合计为准，避免把修正差异误读为排队差异。</div>'
                  % (qfa, len(ga["tags"]), qfb, len(gb["tags"])))
    cva, cvb = ga["req_cv"], gb["req_cv"]

    def _pair(name, va, vb, nd=3):
        if va is None or vb is None:
            return None
        return "%s %s vs %s" % (name, fnum(va, nd), fnum(vb, nd))

    det = [x for x in (
        _pair("请求 CV", cva, cvb, 2),
        _pair("尾部 LIF", ga["lif"], gb["lif"], 2),
        _pair("主机间 CV", ga["host_cv"], gb["host_cv"], 2),
        _pair("TTFT CV", ga["ttft_cv"], gb["ttft_cv"], 2),
    ) if x]
    det_txt = ("（%s）" % "；".join(det)) if det else ""
    # 小样本护栏（与单池 verdict 同口径）：A/B 任一池 N̄<200 时，CV 差值可能是
    # 随机波动，横幅不作胜负判定，改由 LIF/失衡比等效应量与业务影响决断
    guarded = bool(small_sample_text(ga)) or bool(small_sample_text(gb))
    if zero_side:
        banner = ('<div class="banner warn">对比结论：不可比——一侧窗口零流量（A 总请求 %s，B 总请求 %s），'
                  '不存在请求分布形态可比性%s</div>'
                  % (fnum(ga["total_req"], 0), fnum(gb["total_req"], 0), det_txt))
    elif (only_a or only_b) and cva is not None and cvb is not None:
        # 评审修复：引擎集合不一致时胜负判定基于各自全池 CV，B 独有的空闲引擎即可让
        # A 胜出——暂停判定，指向基于相同引擎的明细；平局（「均衡程度相当」）同样
        # 基于不同引擎全集，一并不作——集合差异先于平局/胜负处理，无 CV 差值门槛
        banner = ('<div class="banner warn">对比结论：不作胜负与平局判定——引擎集合不一致（仅 A 有 %s；仅 B 有 %s），'
                  '请求 CV %s vs %s 基于各自全池引擎，独有引擎可翻转结论；请对齐采集的引擎集合后重比，'
                  '明细见「三、请求分配对比（相同引擎）」%s</div>'
                  % (esc(", ".join(only_a)) or "无", esc(", ".join(only_b)) or "无",
                     fnum(cva, 3), fnum(cvb, 3), det_txt))
    elif cva is not None and cvb is not None and abs(cva - cvb) >= 0.02 and guarded:
        banner = ('<div class="banner warn">对比结论：不作胜负判定——请求 CV %s vs %s，'
                  '但样本护栏激活（A/B 任一池每引擎平均请求 &lt; %s，随机底噪即可解释该差异），'
                  '请以 LIF/失衡比等效应量与业务影响为准，必要时扩大采样时长复测%s</div>'
                  % (fnum(cva, 3), fnum(cvb, 3), SMALL_SAMPLE_N, det_txt))
    elif cva is not None and cvb is not None and abs(cva - cvb) >= 0.02:
        winner, wcls = (dsa.label, "good") if cva < cvb else (dsb.label, "good")
        banner = '<div class="banner %s">对比结论：%s 请求分布更均衡%s</div>' % (wcls, esc(winner), det_txt)
    elif cva is None or cvb is None:
        banner = ('<div class="banner warn">对比结论：证据不足——至少一池的请求分布指标缺失'
                  '（有效引擎不足/采样点过少），请检查采集质量后重试%s</div>' % det_txt)
    else:
        banner = '<div class="banner warn">对比结论：两者均衡程度相当（请求 CV 差异 &lt; 0.02）%s</div>' % det_txt

    sc_tbl = ("<table><tr><th>维度</th><th>A 数值</th><th>A 评级</th>"
              "<th>B 数值</th><th>B 评级</th></tr>")
    for (na, ca_, ga_), (nb, cb_, gb_) in zip(sca, scb):
        sc_tbl += "<tr>%s<td>%s</td>%s<td>%s</td>%s</tr>" % (
            rowname_html(na), fnum(ca_), gcell(ga_), fnum(cb_), gcell(gb_))
    sc_tbl += "</table>"
    moves = []
    for t in inter:
        sa = ga["shares"].get(t)
        sb = gb["shares"].get(t)
        # 任一侧增量缺失（有效点不足 2 个）时占比为 None：保留行展示 n/a
        # 而不是整行丢弃——否则该引擎从对比明细中无声消失
        moves.append((t, sa, sb, (sb - sa) if (sa is not None and sb is not None) else None))
    moves.sort(key=lambda x: (x[3] is None, -abs(x[3] or 0)))
    amax = max((a for _t, a, _b, _d in moves if a), default=0)
    bmax = max((b for _t, _a, b, _d in moves if b), default=0)
    req_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td>%s%s</tr>" % (
            esc(t), fnum(ga["engines"][t]["req"], 0), fnum(gb["engines"][t]["req"], 0),
            bar_cell(pct(a), (a / amax) if (a and amax) else None, "#2563eb", td_width="16%"),
            bar_cell(pct(b), (b / bmax) if (b and bmax) else None, "#d97706", td_width="16%"))
        for t, a, b, d in moves)
    req_tbl = ("<h2>三、请求分配对比（相同引擎）</h2>"
               "<p class='small'>按请求增量占比的变化幅度降序排列（变化最大的引擎排最前）；"
               "请求增量为窗口内新增请求数，请求增量占比 = 该引擎增量 ÷ 全部引擎增量之和；"
               "增量 n/a 的引擎（请求累计值有效点不足 2 个）列在表尾，不参与变化排序与对比结论。</p>"
               "<table><tr><th>引擎</th><th>A 请求增量</th><th>B 请求增量</th>"
               "<th>A 请求增量占比</th><th>B 请求增量占比</th></tr>%s</table>" % req_rows)
    wa = dsa.window()
    wb = dsb.window()
    dur_a = (wa[1] - wa[0]) if dsa.times else 0
    dur_b = (wb[1] - wb[0]) if dsb.times else 0

    def _within(g):
        tot_v = (g["between_var"] or 0) + (g["within_var"] or 0)
        return (g["within_var"] or 0) / tot_v if tot_v > 0 else None

    overview = ('<div class="cards">%s%s%s%s%s%s</div>'
                '<p class="small">卡片格式：<span style="color:#2563eb">A</span> / '
                '<span style="color:#d97706">B</span>，A = %s，B = %s。</p>' % (
                    card2("引擎数", str(len(dsa.engines)), str(len(dsb.engines)), "每端点=1 vLLM 实例"),
                    card2("窗口请求总量", fnum(ga["total_req"], 0), fnum(gb["total_req"], 0)),
                    card2("请求 CV", fnum(ga["req_cv"]), fnum(gb["req_cv"]), "越小越均衡"),
                    card2("尾部失衡 LIF", fnum(ga["lif"]), fnum(gb["lif"]), "max/均值，越小越均衡"),
                    card2("失衡比 max/min", fnum(ga["imbalance"]), fnum(gb["imbalance"])),
                    card2("主机内方差占比", pct(_within(ga)), pct(_within(gb))),
                    esc(dsa.label), esc(dsb.label)))

    def _row(name, va, vb):
        return "<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (name, va, vb)

    inta = (ga["total_req"] / dur_a) if dur_a > 0 else None
    intb = (gb["total_req"] / dur_b) if dur_b > 0 else None
    calib = ("<h2>一、数据口径对比</h2>"
             "<table><tr><th>项目</th><th>A：%s</th><th>B：%s</th></tr>%s</table>"
             "<p class='small'>口径说明：均衡对比以窗口增量与分布形态为准；累计请求数为 counter 末值，"
             "只反映实例历史量，不参与均衡结论。采样：A %d 点 × %ss，B %d 点 × %ss。"
             "统计单位「引擎」= 一个采集分组（ip_port 或 ip_port.engine），"
             "现场采集下 1 个 ip:port 端点 = 1 个 vLLM 服务实例；Prometheus 多引擎实例会按 engine 细分。</p>" % (
                 esc(dsa.label), esc(dsb.label),
                 _row("数据文件", esc(os.path.basename(dsa.path)), esc(os.path.basename(dsb.path)))
                 + _row("采集时间窗口", "%s ~ %s" % (fmt_ts(wa[0]), fmt_ts(wa[1])),
                        "%s ~ %s" % (fmt_ts(wb[0]), fmt_ts(wb[1])))
                 + _row("窗口时长", ("%ss" % fnum(dur_a, 0)) if dur_a else "n/a",
                        ("%ss" % fnum(dur_b, 0)) if dur_b else "n/a")
                 + _row("指标列数（口径）", "30 列（v2）" if dsa.fmt == "v2" else "20 列（原版）",
                        "30 列（v2）" if dsb.fmt == "v2" else "20 列（原版）")
                 + _row("引擎数（共同 %d 个）" % len(inter), str(len(dsa.engines)), str(len(dsb.engines)))
                 + _row("累计请求数（从进程启动开始计数）", fnum(cum_last(dsa), 0), fnum(cum_last(dsb), 0))
                 + _row("窗口内新增请求数", fnum(ga["total_req"], 0), fnum(gb["total_req"], 0))
                 + _row("窗口流量强度（req/s）",
                        fnum(inta, 2) if inta is not None else "n/a",
                        fnum(intb, 2) if intb is not None else "n/a"),
                  len(dsa.times), dsa.step() or 0, len(dsb.times), dsb.step() or 0))
    absent_note = ""
    if dsa.absent_cols or dsb.absent_cols:
        absent_note = ("<p class='small'>列导出说明：全窗口无任何数据的列——通常为该部署的 vLLM 版本"
                       "不导出这些指标（如 vLLM v1 无 swapped/request_failure 计数等），"
                       "属版本能力而非采集丢点；已从缺失率分母剔除，其余统计不受影响——A：%s；B：%s。</p>"
                       % (esc(", ".join(sorted(dsa.absent_cols))) or "无",
                          esc(", ".join(sorted(dsb.absent_cols))) or "无"))
    calib += absent_note

    # 小样本护栏：仅当某池每引擎平均请求 < SMALL_SAMPLE_N 时提示（噪声不参与评级）
    guard_parts = []
    for lbl, gg in ((dsa.label, ga), (dsb.label, gb)):
        guard_txt = small_sample_text(gg)
        if guard_txt:
            guard_parts.append("<p class='small'><b>%s</b>——%s</p>" % (esc(lbl), guard_txt))
    noise_line = "".join(guard_parts)

    rows_tok = ""
    rows_lat = ""
    for t in inter:
        ea = ga["engines"][t]
        eb = gb["engines"][t]
        ha, hb = ea.get("hit_external"), eb.get("hit_external")
        kva, kvb = ea.get("kv_mean"), eb.get("kv_mean")
        kpa, kpb = ea.get("kv_peak"), eb.get("kv_peak")
        rows_tok += ("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                     "<td>%s / %s</td><td>%s / %s</td></tr>" % (
                     esc(t), fnum(ea["gen"], 0), fnum(eb["gen"], 0), pct(ha), pct(hb),
                     pct(kva) if kva is not None else "n/a", pct(kvb) if kvb is not None else "n/a",
                     pct(kpa) if kpa is not None else "n/a", pct(kpb) if kpb is not None else "n/a"))
        rows_lat += ("<tr><td>%s</td><td>%s / %s</td><td>%s / %s</td><td>%s / %s</td>"
                     "<td>%s / %s</td><td>%s / %s</td></tr>" % (
                         esc(t), fnum(ea["ttft"], 3), fnum(eb["ttft"], 3),
                         fnum(ea["queue"], 3), fnum(eb["queue"], 3),
                         fnum(ea["prefill"], 3), fnum(eb["prefill"], 3),
                         fnum(ea["tpot"], 3), fnum(eb["tpot"], 3),
                         fnum(ea["e2e"], 3), fnum(eb["e2e"], 3)))
    tok_tbl = ("<h2>五、Token 与缓存命中对比（相同引擎）</h2>"
               "<p class='small'>输出 token 增量为窗口内新增生成 token；缓存命中为外部前缀缓存命中率均值。"
               "命中率差异可解释『用均衡换缓存收益』的策略权衡：亲和路由往往均衡更差、命中更高。"
               "KV 均值看常态水位，KV 峰值看瞬时最坏情况——峰值接近 100%% 会触发抢占/换出。</p>"
               "<table><tr><th>引擎</th><th>A 输出 token 增量</th><th>B 输出 token 增量</th>"
               "<th>A 外部缓存命中</th><th>B 外部缓存命中</th>"
               "<th>KV 均值 A/B</th><th>KV 峰值 A/B</th></tr>%s</table>" % rows_tok)
    lat_tbl = ("<h2>六、TTFT 与时延对比（相同引擎）</h2>"
               "<p class='small'>时延为窗口平均口径（秒）。按 vLLM 官方口径，TTFT ≈ 输入处理 + Queue（排队等调度）"
               "+ Prefill（预填充计算），首 token 在预填充末产生；Queue 差异大 = 过载型（分流不均），"
               "Prefill 差异大 = 序列长度或算力差异。E2E 受生成长度影响，跨池对比以 TTFT / Queue / TPOT 为主。</p>"
               "<table><tr><th>引擎</th><th>TTFT A/B</th><th>Queue A/B</th><th>Prefill A/B</th>"
               "<th>TPOT A/B</th><th>E2E A/B</th></tr>%s</table>" % rows_lat)

    # 各板块指标说明（静态）
    n1 = ("<p class='small'>说明：对比 A、B 各维度的 CV 与评级即可判断哪个分流更均匀——"
          "「优」多的更均衡，「差」多的需要干预。</p>")
    n2 = ("<p class='small'>说明：按请求占比变化幅度降序排列，排在最前的引擎在两次采集间分到的流量变化最大。</p>")
    n3 = ("<p class='small'>说明：A/B 各自把总失衡拆成主机间与主机内两部分，占比高的一侧就是要调的方向。</p>")
    n4 = ("<p class='small'>说明：Token 增量对比计算量差异；命中率高低可解释『用均衡换缓存收益』的策略权衡。</p>")
    n5 = ("<p class='small'>说明：TTFT/Queue/Prefill/TPOT 每格为 A / B 对照；"
          "按 vLLM 官方口径 TTFT ≈ 输入处理 + Queue + Prefill，Queue 差异大 = 过载型，Prefill 差异大 = 序列/算力差异；"
          "E2E 受生成长度影响仅作参考。</p>")
    n7 = ("<p class='small'>说明：对比排队集中度与各引擎 waiting——集中度高的一侧更可能有单引擎过载瓶颈。</p>")

    body = (banner + notes + overview + calib
            + "<h2>二、计分卡对比</h2>" + sc_tbl + noise_line + threshold_note() + n1
            + req_tbl + n2
            + "<h2>四、失衡结构对比</h2>" + section_three_compare(dsa, ga, dsb, gb)
            + tok_tbl + n4
            + lat_tbl + n5
            + "<h2>七、窗口内分流对比</h2>" + section_compare_flow(dsa, ga, dsb, gb, inter)
            + "<h2>八、等待队列对比</h2>" + section_compare_queue(ga, gb, inter) + n7
            + section_compare_summary(dsa, ga, dsb, gb, ratio)
            + extra_html)
    return page(title, "%s vs %s | A=%s | B=%s" % (dsa.label, dsb.label, os.path.basename(dsa.path), os.path.basename(dsb.path)), body)


def page(title, subtitle, body):
    return ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>%s</title><style>%s</style></head><body><div class='wrap'>"
            "<h1>%s</h1><div class='sub'>%s · 生成于 %s · kptools-dplb-report</div>%s"
            "</div></body></html>") % (esc(title), CSS, esc(title), esc(subtitle),
                                       datetime.now().strftime("%Y-%m-%d %H:%M:%S"), body)


def summary_json(datasets, globals_, args):
    out = {
        "generated_at": datetime.now().isoformat(),
        "files": [{"path": d.path, "label": d.label, "fmt": d.fmt,
                   "engines": len(d.engines), "points": len(d.times)} for d in datasets],
    }
    for d, g in zip(datasets, globals_):
        em = {t: {k: (round(v, 6) if isinstance(v, float) else v) for k, v in m.items()} for t, m in g["engines"].items()}
        sec = {
            "total_req": g["total_req"],
            "valid_engines": g.get("valid_engines"),
            "invalid_tags": g.get("invalid_tags"),
            "req_cv": g["req_cv"],
            "imbalance": g["imbalance"],
            "lif": g["lif"],
            "host_lif": g.get("host_lif"),
            "gen_cv": g["gen_cv"],
            "between_var": g["between_var"],
            "within_var": g["within_var"],
            "host_cv": g["host_cv"],
            "within_cv": g["within_cv"][0] if g["within_cv"] else None,
            "ttft_cv": g["ttft_cv"],
            "ttft_corr": g["ttft_corr"],
            "queue_cv": g["queue_cv"],
            "queue_conc": g["queue_conc"],
            "queue_top": g["queue_top"],
            "queue_spike_waiting_share": g["queue_spike_waiting_share"],
            "queue_spikes": g["queue_spikes"],
            "host_loads": g["host_loads"],
            "shares": g["shares"],
            "engines": em,
            "scorecard": scorecard(g, d),
        }
        out.setdefault("datasets", []).append({"label": d.label, **sec})
    return out


def main():
    ap = argparse.ArgumentParser(description="dp_load_metrics CSV -> 负载均衡 HTML 报告")
    ap.add_argument("--csv", nargs="+", required=True, help="一份（单池分析）或两份（A vs B 对比）CSV")
    ap.add_argument("--label-a", default=None)
    ap.add_argument("--label-b", default=None)
    ap.add_argument("--title", default="DP 负载均衡分析报告")
    ap.add_argument("-o", "--out", default=None, help="输出 HTML 路径（--print-schema 模式可省略）")
    ap.add_argument("--summary-out", default=None, help="指标摘要 JSON 输出路径")
    ap.add_argument("--extra-analysis", default=None, help="附加深度分析 markdown 文件，渲染在综合结论后")
    ap.add_argument("--print-schema", action="store_true", help="仅输出 CSV 识别结果（格式/列/引擎/时间范围）用于排查，不生成报告")
    args = ap.parse_args()
    if len(args.csv) > 2:
        raise SystemExit("--csv 至多两份（PD 双池请各自生成单池报告，不要做成 A vs B 对比）")
    if not args.out and not args.print_schema:
        raise SystemExit("缺少 -o/--out（输出 HTML 路径）；若只想排查 CSV 格式，请加 --print-schema（无需 -o）")

    # 输出路径冲突防护：任一输出与输入 CSV / 附加分析文件指向同一文件时直接拒绝，
    # 避免 HTML/JSON 打开清空原始采样、或 --summary-out 随后覆盖刚写出的 HTML。
    # 判定 = 归一化字符串相等 或 samefile：realpath 只解析符号链接，识别不了同
    # inode 的硬链接——已存在的输出与输入互为硬链接时字符串比对会漏过，写输出
    # 仍会改写原始采样；samefile 按 st_dev/st_ino 比对，任一路径不存在（或不可
    # stat，如跨卷/权限）时返回 False——不存在的路径不可能构成已存在的硬链接
    def _canon(p):
        return os.path.normcase(os.path.realpath(os.path.abspath(p)))

    def _same_file(p1, p2):
        try:
            return os.path.samefile(p1, p2)
        except OSError:
            return False

    def _clash(p1, p2):
        return p1 is not None and p2 is not None and \
            (_canon(p1) == _canon(p2) or _same_file(p1, p2))

    clashes = []
    if any(_clash(args.out, c) for c in args.csv):
        clashes.append("-o/--out 与输入 CSV 相同：%s" % args.out)
    if args.summary_out and any(_clash(args.summary_out, c) for c in args.csv):
        clashes.append("--summary-out 与输入 CSV 相同：%s" % args.summary_out)
    if _clash(args.out, args.extra_analysis):
        clashes.append("-o/--out 与 --extra-analysis 相同：%s" % args.out)
    if _clash(args.summary_out, args.extra_analysis):
        clashes.append("--summary-out 与 --extra-analysis 相同：%s" % args.summary_out)
    if _clash(args.out, args.summary_out):
        clashes.append("-o/--out 与 --summary-out 相同（JSON 会覆盖 HTML）：%s" % args.out)
    if clashes:
        raise SystemExit("输出路径与输入文件冲突，拒绝执行以保留原文件：\n  " + "\n  ".join(clashes))

    def guess(path, fallback):
        b = os.path.splitext(os.path.basename(path))[0]
        m = re.search(r"_(proxy\w*|coordinator|v2|prom|P|D)$", b)
        if m:
            return m.group(1)
        return fallback

    dsa = Dataset(args.csv[0], args.label_a or guess(args.csv[0], "A"))
    if args.print_schema:
        w0, w1 = dsa.window()
        print("file: %s" % dsa.path)
        print("format: %s (%d data cols)" % (dsa.fmt, len(dsa.data_cols)))
        print("engines: %d -> %s" % (len(dsa.engines), ", ".join(sorted(dsa.engines))))
        print("time range: %s ~ %s (%d points, step=%ss)" % (
            fmt_ts(w0) if w0 else "n/a",
            fmt_ts(w1) if w1 else "n/a",
            len(dsa.times), dsa.step() or 0))
        for w in dsa.warnings:
            print("warning: %s" % w)
        if len(args.csv) == 2:
            dsb = Dataset(args.csv[1], args.label_b or guess(args.csv[1], "B"))
            v0, v1 = dsb.window()
            print("file: %s" % dsb.path)
            print("format: %s (%d data cols)" % (dsb.fmt, len(dsb.data_cols)))
            print("engines: %d -> %s" % (len(dsb.engines), ", ".join(sorted(dsb.engines))))
            print("time range: %s ~ %s (%d points, step=%ss)" % (
                fmt_ts(v0) if v0 else "n/a",
                fmt_ts(v1) if v1 else "n/a",
                len(dsb.times), dsb.step() or 0))
            for w in dsb.warnings:
                print("warning: %s" % w)
        return
    ga = dsa.global_metrics()
    extra_html = ""
    if args.extra_analysis and os.path.isfile(args.extra_analysis):
        with open(args.extra_analysis, encoding="utf-8") as f:
            extra_html = "<h2>附：深度分析</h2><div class='note'>" + md_to_html(f.read()) + "</div>"
    if len(args.csv) == 2:
        dsb = Dataset(args.csv[1], args.label_b or guess(args.csv[1], "B"))
        gb = dsb.global_metrics()
        html = render_compare(dsa, ga, dsb, gb, args.title, extra_html)
        datasets, globals_ = [dsa, dsb], [ga, gb]
    else:
        html = render_single(dsa, ga, args.title, extra_html)
        datasets, globals_ = [dsa], [ga]
    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    if args.summary_out:
        sj = summary_json(datasets, globals_, args)
        d = os.path.dirname(os.path.abspath(args.summary_out))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(args.summary_out, "w", encoding="utf-8") as f:
            json.dump(sj, f, ensure_ascii=False, indent=2)
    print("== 关键指标摘要 ==")
    for d, g in zip(datasets, globals_):
        tot = (g["between_var"] or 0) + (g["within_var"] or 0)
        wcv = g["within_cv"][0] if g["within_cv"] else None
        print("[%s] 引擎=%d（有效 %s）窗口请求=%s 请求CV=%s LIF=%s 失衡比=%s 主机间CV=%s 主机间LIF=%s 主机内CV=%s "
              "方差分解(主机间/主机内)=%s/%s TTFT_CV=%s TTFT-负载r=%s 排队集中度=%s" % (
                  d.label, len(d.engines), fnum(g.get("valid_engines"), 0), fnum(g["total_req"], 0), fnum(g["req_cv"]), fnum(g["lif"]), fnum(g["imbalance"]),
                  fnum(g["host_cv"]), fnum(g.get("host_lif")), fnum(wcv),
                  pct((g["between_var"] or 0) / tot if tot > 0 else None),
                  pct((g["within_var"] or 0) / tot if tot > 0 else None),
                  fnum(g["ttft_cv"]), fnum(g["ttft_corr"]), pct(g["queue_conc"])))
        if d.fmt == "legacy":
            print("    口径提示: 原版 CSV，TTFT 为累计平均，报告已挂口径警示（TTFT 维度不计分）")
    print("报告已生成: %s" % os.path.abspath(args.out))
    if args.summary_out:
        print("指标摘要: %s" % os.path.abspath(args.summary_out))


if __name__ == "__main__":
    main()
