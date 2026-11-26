#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""extract_metric_prom.py - Prometheus 时间段回采 -> dp_load_metrics CSV（30 数据列 v2 口径，表头与 extract_metric_v2.sh 逐列一致）

用法示例：
  python3 extract_metric_prom.py --prom-url http://prom:9090 --list-jobs --job 'glm53_2_decode_.*' --start now-1h
  python3 extract_metric_prom.py --prom-url http://prom:9090 --job 'glm53_2_.*' \
      --start '2026-11-17 02:00:00' --end '2026-11-17 03:00:00' --step auto -o /tmp/lbprof/out.csv
  python3 extract_metric_prom.py --grafana-url https://grafana:3000 --user admin --datasource-uid <uid> \
      --job 'glm53_2_.*' --start now-1h --step 30s   # 密码经 GRAFANA_PASSWORD 或 --password-file 传入，不进 argv
  python3 extract_metric_prom.py --prom-url http://prom:9090 --pool 'P=glm53_2_prefill_.*' \
      --pool 'D=glm53_2_decode_.*' --start now-1h --end now --step 30s -o /tmp/lbprof/pd/
  python3 extract_metric_prom.py --prom-url http://prom:9090 --model glm53-2 --start now-1h --step 30s

注：--start 仅在 --list-jobs 模式可省略（默认窗口 now-1h ~ now，job 存活判定依赖时间窗）；
    Grafana 密码来源优先级 --password > --password-file > 环境变量 GRAFANA_PASSWORD，推荐后两种
    （--password 会进入 ps 输出 / shell history / agent 执行轨迹）；向 http:// 的 Grafana 发送
    Basic 凭证需显式 --allow-insecure-http。
    CSV 的 timestamp 为 query_range 的查询求值时刻（按 --step 对齐，取该时刻前最近一次抓取的
    样本；步长小于抓取间隔时同一底层样本会复现相邻多行）——不是现场轮询时刻，也不是底层
    抓取事件时间，勿与现场日志精确对齐；语义详见 SKILL.md「采集产物契约」与 csv_format.md。
"""
import argparse
import csv
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_COLS = [
    ("num_requests_running", "vllm:num_requests_running", ""),
    ("num_requests_waiting", "vllm:num_requests_waiting", ""),
    ("num_requests_swapped", "vllm:num_requests_swapped", ""),
    ("request_success_total", "vllm:request_success_total", ""),
    ("request_failure_total", "vllm:request_failure_total", ""),
    ("num_preemptions_total", "vllm:num_preemptions_total", ""),
    ("prompt_tokens_total", "vllm:prompt_tokens_total", ""),
    ("generation_tokens_total", "vllm:generation_tokens_total", ""),
    ("request_validation_time_total", "vllm:request_validation_time_total", ""),
    ("kv_cache_usage_perc", "vllm:gpu_cache_usage_perc", "vllm:kv_cache_usage_perc"),
    ("cpu_cache_usage_perc", "vllm:cpu_cache_usage_perc", ""),
    ("prefix_cache_hit_rate", "vllm:prefix_cache_hit_rate", ""),
    ("pending_prefix_cache_hit_rate", "vllm:pending_prefix_cache_hit_rate", ""),
    ("external_prefix_cache_hit_rate", "vllm:external_prefix_cache_hit_rate", ""),
    ("process_start_time_seconds", "process_start_time_seconds", ""),
]
HISTS = [
    ("ttft", "vllm:time_to_first_token_seconds", ""),
    ("queue", "vllm:request_queue_time_seconds", ""),
    ("prefill", "vllm:prefill_time_seconds", "vllm:request_prefill_time_seconds"),
    ("tpot", "vllm:request_time_per_output_token_seconds", "vllm:time_per_output_token_seconds"),
    ("e2e", "vllm:e2e_request_latency_seconds", "vllm:request_latency_seconds"),
]
AVG_COLS = [h[0] + "_avg" for h in HISTS]
CSV_HEADER = (
    ["timestamp", "ip_port"]
    + [c[0] for c in BASE_COLS]
    + AVG_COLS
    + [h[0] + s for h in HISTS for s in ("_sum", "_count")]
    + ["queue_fixed"]
)
SEGMENT_MAX_POINTS = 11000
AUTO_POINTS = 600
AUTO_MIN_STEP = 5


def parse_tz(tz_str):
    m = re.match(r"^([+-])(\d{2}):?(\d{2})$", tz_str.strip())
    if not m:
        raise SystemExit("--tz 格式非法，期望形如 +08:00")
    sign = 1 if m.group(1) == "+" else -1
    return sign * (int(m.group(2)) * 3600 + int(m.group(3)) * 60)


def parse_time(spec, now, tz_off, is_end=False):
    s = (spec or "").strip()
    if not s:
        raise SystemExit("--start/--end 不能为空")
    if s == "now":
        return now
    m = re.match(r"^(now-)?(-?)(\d+)([smhd])$", s)
    if m:
        if is_end and m.group(1) is None and not s.startswith("-"):
            raise SystemExit("相对时长（如 30m）只适用于 --start；--end 请用 now / now-30m / -2h / 绝对时间")
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(4)]
        return now - int(m.group(3)) * mult
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})$", s)
    if m:
        import calendar
        import datetime as dt
        try:
            t = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4)), int(m.group(5)), int(m.group(6)),
                            tzinfo=dt.timezone(dt.timedelta(seconds=tz_off)))
        except ValueError as e:
            raise SystemExit("时间解析失败: %s" % e)
        return calendar.timegm(t.utctimetuple())
    if re.match(r"^\d+$", s):
        return int(s)
    raise SystemExit("无法解析时间: %r（支持 now / now-30m / -2h / 30m(start) / '2026-11-17 02:00:00' / unix 秒）" % spec)


def parse_step(spec, start, end):
    s = (spec or "auto").strip().lower()
    if s == "auto":
        span = max(end - start, 1)
        return max(AUTO_MIN_STEP, int(round(span / AUTO_POINTS)))
    m = re.match(r"^(\d+)([smhd]?)$", s)
    if not m:
        raise SystemExit("--step 非法: %r（支持 auto / 15s / 1m / 秒数）" % spec)
    mult = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return int(m.group(1)) * mult


def resolve_start(args):
    """--start 缺省处理：--list-jobs 可省略（默认 now-1h，job 存活判定依赖时间窗），其余模式必填。"""
    if args.start is not None:
        return args.start
    if args.list_jobs:
        sys.stderr.write("提示: --list-jobs 未指定 --start，使用默认窗口 now-1h ~ now（按该窗口判定 job 存活）\n")
        return "now-1h"
    raise SystemExit("--start 为必填（now-30m / -2h / 绝对时间 / unix 秒）；仅 --list-jobs 可省略，默认窗口 now-1h ~ now")


def resolve_grafana_password(args):
    """Grafana 密码来源优先级：--password > --password-file > 环境变量 GRAFANA_PASSWORD。

    密码经 argv 传递会暴露在采集机 ps 输出、shell history 与 agent 执行轨迹中，
    推荐后两种入口；无任何来源时返回空串（与旧版行为一致）。"""
    if args.password:
        return args.password
    if args.password_file:
        try:
            with open(args.password_file, encoding="utf-8") as f:
                return f.read().strip()
        except OSError as e:
            raise SystemExit("读取 --password-file 失败: %s" % e)
    return os.environ.get("GRAFANA_PASSWORD", "")


class Client(object):
    def __init__(self, args):
        self.timeout = 60
        self.auth = None
        if args.prom_url and args.grafana_url:
            raise SystemExit("--prom-url 与 --grafana-url 互斥（只填一个，防止凭证据发往另一端点）")
        if args.prom_url:
            self.base = args.prom_url.rstrip("/")
            self.api = self.base + "/api/v1"
            self.err_label = self.api  # 错误信息脱敏端点：不带查询参数
        elif args.grafana_url:
            if not (args.user and args.datasource_uid):
                raise SystemExit("Grafana 代理模式需要 --user 与 --datasource-uid（密码经 GRAFANA_PASSWORD 环境变量或 --password-file 传入）")
            u = urllib.parse.urlsplit(args.grafana_url)
            if u.scheme not in ("http", "https") or not u.netloc:
                raise SystemExit("--grafana-url 非法，期望 http(s)://host[:port]: %r" % args.grafana_url)
            if u.scheme == "http" and not getattr(args, "allow_insecure_http", False):
                raise SystemExit("拒绝向 http:// 的 Grafana 发送 Basic 凭证（Base64 等同明文，可被同网段嗅探）；"
                                 "请改用 https://，或确认网络可信后显式加 --allow-insecure-http")
            password = resolve_grafana_password(args)
            self.base = args.grafana_url.rstrip("/")
            # 错误信息脱敏：只暴露 scheme://host（不带 datasource uid 与查询参数）
            self.err_label = "%s://%s/api/v1" % (u.scheme, u.netloc)
            self.api = "%s/api/datasources/proxy/uid/%s/api/v1" % (self.base, args.datasource_uid)
            import base64
            token = ("%s:%s" % (args.user, password or "")).encode()
            self.auth = "Basic " + base64.b64encode(token).decode()
        else:
            raise SystemExit("需要 --prom-url 或（--grafana-url + --user + --datasource-uid）")

    def _get(self, path, params):
        url = self.api + path + "?" + urllib.parse.urlencode(params)
        last_err = None
        for _ in range(3):
            try:
                req = urllib.request.Request(url)
                if self.auth:
                    req.add_header("Authorization", self.auth)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    import json
                    body = json.loads(resp.read().decode("utf-8"))
                if body.get("status") != "success":
                    raise SystemExit("Prometheus 返回错误: %s" % body.get("error", body))
                return body["data"]
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise SystemExit("认证失败（401）：检查 Grafana 账号密码 / 数据源 UID")
                last_err = e
            except urllib.error.URLError as e:
                last_err = e
            time.sleep(2)
        endpoint = getattr(self, "err_label", None) or self.api
        raise SystemExit("查询失败（重试 3 次）: %s%s -> %s" % (endpoint, path, last_err))

    def query_range(self, promql, start, end, step):
        # 按 --step 求值：每个 t 是查询求值时刻，取该时刻前最近一次抓取的样本；
        # step 小于抓取间隔时，同一底层样本会出现在相邻多个求值点（多行复现）
        npts = int((end - start) // step) + 1
        series = {}
        if npts <= SEGMENT_MAX_POINTS:
            segs = [(start, end)]
        else:
            seg_span = (SEGMENT_MAX_POINTS - 100) * step
            segs = []
            t = start
            while t < end:
                segs.append((t, min(t + seg_span, end)))
                t = t + seg_span + step
        for (s, e) in segs:
            data = self._get("/query_range", {"query": promql, "start": s, "end": e, "step": step})
            if data.get("resultType") != "matrix":
                raise SystemExit("意外的查询结果类型: %s" % data.get("resultType"))
            for r in data.get("result", []):
                key = tuple(sorted(r["metric"].items()))
                bucket = series.setdefault(key, {})
                for t, v in r["values"]:
                    fv = float(v)
                    if fv != fv:  # NaN 视为缺失
                        continue
                    bucket[t] = fv
        return series

    def label_values(self, label):
        data = self._get("/label/%s/values" % label, {})
        return data if isinstance(data, list) else data.get("data", [])

    def query(self, promql, ts=None):
        """即时查询，返回 data dict（resultType=vector 时 result 为列表）"""
        params = {"query": promql}
        if ts is not None:
            params["time"] = ts
        return self._get("/query", params)


def live_jobs_in_window(client, start, end):
    """目标窗口内存活的 job 集合（以 up 序列是否有样本判断），失败返回 None"""
    span = max(end - start, 60)
    try:
        data = client.query('count by (job) (last_over_time(up[%ds]))' % span, end)
    except SystemExit:
        return None
    out = set()
    for r in data.get("result", []):
        try:
            if r.get("value") and float(r["value"][1]) > 0:
                j = r["metric"].get("job", "")
                if j:
                    out.add(j)
        except (ValueError, KeyError, IndexError):
            continue
    return sorted(out)


def suggest_jobs(rx_str, live_jobs, limit=10):
    """按正则里的词元（按 _ 与正则特殊符切分）在存活 job 中做模糊提示。
    例：'glm53_prefill_.*' -> 提示 'glm53_0_prefill_0'（部署序号改名后残留旧名）"""
    toks = [t for t in re.split(r"[|()^\$\[\]\{\}.*+?\\_]+", rx_str) if len(t) >= 3]
    seen = set()
    toks = [t for t in toks if not (t in seen or seen.add(t))]
    if not toks:
        return []
    out = set()
    for j in live_jobs:
        parts = set(j.split("_"))
        if all(any(t in part for part in parts) for t in toks):
            out.add(j)
    return sorted(out)[:limit]


def tag_of(metric, tag_label):
    inst = metric.get(tag_label, "") or ""
    if inst.startswith("http://"):
        inst = inst[7:]
    elif inst.startswith("https://"):
        inst = inst[8:]
    inst = inst.split("/")[0]
    eng = metric.get("engine", "") or ""
    tag = inst if inst else "<unknown>"
    if eng:
        tag = tag + "." + eng
    return tag


def fmt_val(v):
    if v is None:
        return "-1"
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return "%.10g" % v


def collect(client, selector, tag_label, start, end, step, quiet=False):
    sel = selector if selector else ""
    series_by_col = {}

    def fetch(col, name, fallback):
        promql = 'sum by (%s, engine) (%s{%s})' % (tag_label, name, sel)
        got = client.query_range(promql, start, end, step)
        if not got and fallback:
            promql = 'sum by (%s, engine) (%s{%s})' % (tag_label, fallback, sel)
            got = client.query_range(promql, start, end, step)
        mapped = {}
        for key, points in got.items():
            m = dict(key)
            mapped.setdefault(tag_of(m, tag_label), {}).update(points)
        series_by_col[col] = mapped

    def fetch_rate(col, hits_name, queries_name):
        """命中率：优先 hits/queries counter 累计比（queries=0 记 0），counter 不存在时保留 gauge 结果"""
        h = client.query_range('sum by (%s, engine) (%s{%s})' % (tag_label, hits_name, sel), start, end, step)
        q = client.query_range('sum by (%s, engine) (%s{%s})' % (tag_label, queries_name, sel), start, end, step)
        if not h and not q:
            return
        hm, qm = {}, {}
        for key, points in h.items():
            hm.setdefault(tag_of(dict(key), tag_label), {}).update(points)
        for key, points in q.items():
            qm.setdefault(tag_of(dict(key), tag_label), {}).update(points)
        rate = {}
        for tag in set(hm) | set(qm):
            hp, qp = hm.get(tag, {}), qm.get(tag, {})
            rd = {}
            for t in set(hp) | set(qp):
                qv = qp.get(t)
                if qv is None:
                    continue
                rd[t] = (hp.get(t, 0.0) / qv) if qv > 0 else 0.0
            if rd:
                rate[tag] = rd
        if rate:
            series_by_col[col] = rate

    if not quiet:
        sys.stderr.write("回采窗口 [%s, %s] step=%ds，共 %d 条聚合查询（缺失指标自动回退别名）...\n"
                         % (start, end, step, len(BASE_COLS) + 2 * len(HISTS)))
    for col, name, fb in BASE_COLS:
        fetch(col, name, fb)
    for h, name, fb in HISTS:
        fetch(h + "_sum", name + "_sum", (fb + "_sum") if fb else "")
        fetch(h + "_count", name + "_count", (fb + "_count") if fb else "")
    # 命中率：新版 vLLM 只有 hits/queries counter（无 gauge），优先按 counter 累计比落盘
    fetch_rate("prefix_cache_hit_rate", "vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total")
    fetch_rate("external_prefix_cache_hit_rate", "vllm:external_prefix_cache_hits_total", "vllm:external_prefix_cache_queries_total")

    engines = {}
    times = set()
    for col, m in series_by_col.items():
        for tag, pts in m.items():
            engines.setdefault(tag, {})
            engines[tag].setdefault(col, pts)
            times.update(pts.keys())
    merged_ghosts = merge_ghost_engine_tags(engines)
    if merged_ghosts and not quiet:
        sys.stderr.write("已合并 %d 个无 engine 标签的幽灵 tag（仅 process_start_time_seconds，"
                         "如 %s -> %s）\n" % (len(merged_ghosts), merged_ghosts[0][0], merged_ghosts[0][1][0]))
    if not engines:
        jobs = client.label_values("job")
        shown = ", ".join(sorted(jobs)[:20]) + ("...共 %d 个" % len(jobs) if len(jobs) > 20 else "")
        hint = ""
        m = re.search(r'job=~"([^"]+)"', sel or "")
        if m:
            live = live_jobs_in_window(client, start, end)
            if live is not None:
                sug = suggest_jobs(m.group(1), live)
                ghost_hit = sorted(j for j in jobs if j not in live and re.search(m.group(1), j))
                if sug:
                    hint += "\n提示: 正则 %r 未命中窗口内存活序列；你可能想用: %s" % (m.group(1), ", ".join(sug))
                if ghost_hit:
                    suffix = " 等" if len(ghost_hit) > 5 else ""
                    hint += ("\n注意: %s%s 只存在于历史标签取值（改名残留），窗口内无存活序列"
                             % (", ".join(ghost_hit[:5]), suffix))
        raise SystemExit("未取到任何序列（selector=%s）。窗口附近可用的 job（前 20 个）：%s%s\n"
                         "完整 job 列表可用 --list-jobs 查看（会标注窗口内有效/仅历史）"
                         % (sel or "<空>", shown if jobs else "<无>", hint))
    return engines, sorted(times)


def merge_ghost_engine_tags(engines):
    """process_start_time_seconds 等无 engine 标签的序列会产生无 .engine 后缀的幽灵 tag
    （如 xx:9100，只有 1 列数据），真实引擎数据在 xx:9100.0 等带后缀 tag 上。
    把幽灵 tag 的值按实例前缀合并进同实例的带后缀 tag，避免引擎数翻倍。"""
    ghosts = [tag for tag, cols in engines.items()
              if set(cols.keys()) == {"process_start_time_seconds"}]
    merged = []
    for gtag in ghosts:
        targets = sorted(t for t in engines if t != gtag and (t == gtag or t.startswith(gtag + ".")))
        if not targets:
            continue
        ps = engines[gtag]["process_start_time_seconds"]
        for tgt in targets:
            dst = engines[tgt].setdefault("process_start_time_seconds", {})
            for t, v in ps.items():
                if t not in dst:
                    dst[t] = v
        del engines[gtag]
        merged.append((gtag, targets))
    return merged


def fix_negative_hist(engines):
    """已知 vLLM 导出 bug：部分引擎 queue_sum<0 且 prefill_sum 同步虚高（两者相加才真实）。
    修正：queue_sum<0 的点记 queue=0、差值并入 prefill_sum。返回 (fixed 清单, {tag: 修正时间集合})。"""
    fixed = []
    corrected = {}
    for tag, cols in engines.items():
        qs = cols.get("queue_sum", {})
        bad_times = {t for t, v in qs.items() if v < 0}
        if not bad_times:
            continue
        mn = min(qs.values())
        ps_map = cols.get("prefill_sum", {})
        for t in qs:
            if qs[t] < 0:
                orig_q = qs[t]
                qs[t] = 0.0
                if t in ps_map:
                    ps_map[t] = orig_q + ps_map[t]
        corrected[tag] = bad_times
        fixed.append((tag, mn))
    return fixed, corrected


def build_rows(engines, times, tz_off=8 * 3600, corrected=None):
    rows = []
    order = sorted(engines.keys())
    for t in times:
        # 时间戳语义（复核修订）：t 是 query_range 的**查询求值时刻**（按 --step 对齐，
        # 取该时刻前最近一次抓取的样本；步长小于抓取间隔时同一底层样本会复现在相邻多行，
        # 不得视为独立抓取事件、不得与现场日志精确对齐）——与现场轮询脚本取 ROUND_TS
        # （轮询发起时刻）的语义不同。落地为人类可读时间（--tz 口径），格式与现场一致，
        # 报告端同时兼容 epoch 与可读时间。
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t + tz_off))
        for tag in order:
            cols = engines[tag]
            row = [ts_str, tag]
            for col, _, _ in BASE_COLS:
                row.append(fmt_val(cols.get(col, {}).get(t)))
            for h, _, _ in HISTS:
                s = cols.get(h + "_sum", {}).get(t)
                c = cols.get(h + "_count", {}).get(t)
                if s is not None and c is not None and c > 0:
                    row.append(fmt_val(s / c))
                else:
                    row.append("-1")
            for h, _, _ in HISTS:
                row.append(fmt_val(cols.get(h + "_sum", {}).get(t)))
                row.append(fmt_val(cols.get(h + "_count", {}).get(t)))
            row.append("1" if (corrected and t in corrected.get(tag, ())) else "0")
            rows.append(row)
    return order, rows


def fnum(x, nd=2):
    """数值格式化：None -> n/a；近整数去小数；否则保留 nd 位小数"""
    if x is None:
        return "n/a"
    if isinstance(x, int) or (isinstance(x, float) and abs(x - round(x)) < 1e-9):
        return str(int(round(x)))
    return ("%." + str(nd) + "f") % x


def health_check(order, engines, times, fixed, step):
    print("== 产物体检 ==")
    # 该部署未导出的列（全体引擎都无数据）不计入缺失率分母，避免基线误报（与报告端口径一致）
    exp_cols = [c[0] for c in BASE_COLS] + [h[0] + s for h in HISTS for s in ("_sum", "_count")]
    absent = [c for c in exp_cols
              if not any(engines[t].get(c) for t in engines)]
    ncols = len(exp_cols) - len(absent)
    print("引擎数: %d，时间点: %d（step=%ds），数据列: %d" % (len(order), len(times), step, ncols))
    if absent:
        print("列导出说明：以下列该 vLLM 版本未导出（全窗口无数据，属版本能力而非采集丢点），不计入缺失率: %s" % ", ".join(absent))
    absent_set = frozenset(absent)
    warn = []
    for tag in order:
        cols = engines[tag]
        have = sum(len(pts) for col, pts in cols.items() if col not in absent_set)
        expected = ncols * len(times)
        miss_rate = 1.0 - (have / expected) if expected else 1.0
        cover = len(cols.get("request_success_total", {})) / (len(times) or 1)
        if cover < 0.9:
            warn.append(tag)
        print("  %-24s 点数=%-5d 缺失率=%.1f%% 覆盖率=%.1f%%" % (tag, max(len(p) for p in cols.values()) if cols else 0, miss_rate * 100, cover * 100))
    if warn:
        print("覆盖率不足 90%% 的引擎: %s（建议先用 --list-jobs / 放宽 --match 复核）" % ", ".join(warn))
    else:
        print("各引擎覆盖率均 >= 90%")
    # process_start 一致性：跨引擎/跨段累计口径是否可比
    ps_vals = {}
    for tag in order:
        ps = engines[tag].get("process_start_time_seconds", {})
        if ps:
            ps_vals[tag] = max(ps.values())
    if ps_vals:
        uniq = sorted(set(round(v, 1) for v in ps_vals.values()))
        if len(uniq) == 1:
            print("process_start 一致（%s），累计 counter 跨引擎可比" % fnum(uniq[0], 0))
        else:
            print("process_start 不一致（%d 个不同取值，%s ~ %s）：累计 counter 跨引擎/跨段不可比，请以窗口增量口径为准"
                  % (len(uniq), fnum(min(uniq), 0), fnum(max(uniq), 0)))
    else:
        print("未取到 process_start_time_seconds，无法做 uptime 一致性检查")
    if fixed:
        print("queue_sum<0 修正（--negative-hist auto，CSV queue_fixed 列=1 标记被修正点）:")
        for tag, mn in fixed:
            print("  %s: 窗口内最小 queue_sum=%.3f，已修正为 queue=0 且 prefill_sum += queue" % (tag, mn))
    else:
        print("未发现 queue_sum<0 的引擎（或 --negative-hist off 保留原值）")


def write_csv(path, rows):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        w.writerows(rows)
    print("写出: %s（%d 行）" % (path, len(rows)))


def job_prefix(jobs):
    if not jobs:
        return "jobs"
    pref = os.path.commonprefix(sorted(jobs))
    pref = re.sub(r"[_-]*$", "", pref)
    return pref if pref else "jobs"


def safe_fname(s):
    """输出文件名成分消毒：Prometheus job/role 标签值与 --pool 名等会拼进输出
    文件名，job 标签值属远端数据——只保留文件名安全字符（不允许 / 等），
    防止路径穿越写文件（如 job 名含 ../ 时写到 outdir 之外）。"""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s or "")


def model_out_fname(pref, role, ts):
    """--model 模式输出文件名：role（service_role 标签值，如 glm53_2_decode）与
    job 公共前缀 pref 相同或以之为前缀时去重，避免 dp_load_metrics_glm53_2_glm53_2_decode_... 重复。
    ts 为已格式化的时间戳字符串（YYYYmmddHHMMSS）。"""
    pref = safe_fname(pref)
    role = safe_fname(role)
    if role == "all":
        return "dp_load_metrics_%s_%s.csv" % (pref, ts)
    if role == pref or role.startswith(pref + "_"):
        return "dp_load_metrics_%s_%s.csv" % (role, ts)
    return "dp_load_metrics_%s_%s_%s.csv" % (pref, role, ts)


def find_model_jobs(client, model, selector_extra, start, end, step, split_by):
    # 注意：selector_extra 不带前导逗号（由本函数统一加），避免 --model 与 --job/--match
    # 组合时出现 "model_name=...,," 双逗号导致 Prometheus HTTP 400
    sel = 'model_name="%s"%s' % (model, ("," + selector_extra) if selector_extra else "")
    span = max(end - start, 1)
    data = client.query_range('count by (job) (vllm:num_requests_running{%s})' % sel, start, end, span)
    jobs = set()
    for key, pts in data.items():
        if pts:
            jobs.add(dict(key).get("job", ""))
    jobs.discard("")
    if not jobs:
        raise SystemExit("窗口内未发现 model_name=%r 的存活序列；请检查模型名/时间窗（或用 --list-jobs 查看可用 job）" % model)
    groups = {}
    if split_by and split_by != "none":
        data = client.query_range('count by (job, %s) (vllm:num_requests_running{%s})' % (split_by, sel), start, end, span)
        roleless = []
        for key, pts in data.items():
            if not pts:
                continue
            m = dict(key)
            role = m.get(split_by, "")
            if role:
                groups.setdefault(role, set()).add(m.get("job", ""))
            else:
                roleless.append(m.get("job", ""))
        if roleless:
            print("跳过无 %s 标签的序列（多为健康探针等）: %s" % (split_by, ", ".join(sorted(set(roleless)))))
    else:
        groups["all"] = jobs
    if not groups:
        groups["unknown"] = jobs
    keys = {}
    for j in jobs:
        parts = j.split("_")
        keys.setdefault("_".join(parts[:2]) if len(parts) >= 2 else j, set()).add(j)
    if len(keys) > 1:
        print("警告: 窗口内命中多个部署的序列（model_name=%s 可能被多部署共用）:" % model)
        for k, js in sorted(keys.items()):
            print("  %s: %s" % (k, ", ".join(sorted(js))))
        print("如需收窄，请加 --job '<前缀>.*' 后重跑")
    return groups, jobs


def main():
    ap = argparse.ArgumentParser(description="Prometheus 时间段回采 -> dp_load_metrics CSV（v2 30 数据列口径）")
    ap.add_argument("--prom-url")
    ap.add_argument("--grafana-url")
    ap.add_argument("--user")
    ap.add_argument("--password",
                    help="Grafana 密码（不推荐：会进入 argv/ps/history；推荐 GRAFANA_PASSWORD 环境变量或 --password-file）")
    ap.add_argument("--password-file", help="Grafana 密码文件路径（优先级低于 --password）")
    ap.add_argument("--datasource-uid")
    ap.add_argument("--list-jobs", action="store_true")
    ap.add_argument("--job", help="job 名或正则（可与其他条件组合）")
    ap.add_argument("--match", action="append", default=[], help='附加 label 过滤（AND），如 model_name="glm53-2"，可重复')
    ap.add_argument("--model", help="按 model_name 一次选中该部署全部 job")
    ap.add_argument("--split-by", default="service_role",
                    help="--model 模式的拆分标签，默认 service_role；none=合并一份；可换其它标签如 dashboards_tag")
    ap.add_argument("--pool", action="append", default=[], help="NAME=正则，PD 双池多文件输出，可重复")
    ap.add_argument("--start", default=None,
                    help="时间窗起点；--list-jobs 模式可省略（默认 now-1h），其余模式必填")
    ap.add_argument("--end", default="now")
    ap.add_argument("--allow-insecure-http", action="store_true",
                    help="允许向 http:// 的 Grafana 发送 Basic 凭证（默认拒绝）")
    ap.add_argument("--step", default="auto")
    ap.add_argument("--tz", default="+08:00")
    ap.add_argument("--negative-hist", choices=["auto", "off"], default="auto")
    ap.add_argument("--tag-label", default="instance")
    ap.add_argument("-o", "--out")
    ap.add_argument("--out-dir", default=".")
    args = ap.parse_args()

    client = Client(args)

    tz_off = parse_tz(args.tz)
    now = int(time.time())
    args.start = resolve_start(args)
    start = parse_time(args.start, now, tz_off)
    end = parse_time(args.end, now, tz_off, is_end=True)
    if end <= start:
        raise SystemExit("--end 必须晚于 --start")

    if args.list_jobs:
        jobs = client.label_values("job")
        live = live_jobs_in_window(client, start, end)
        if live is None:
            print("（无法查询窗口内存活 job，以下为全时段取值并集，可能包含改名残留）")
            live = []
        valid = sorted(j for j in jobs if j in live)
        ghost = sorted(j for j in jobs if j not in live)
        print("窗口 [%s ~ %s] 内有效 job（%d 个）:" % (start, end, len(valid)))
        for j in valid:
            print("  [有效] %s" % j)
        if ghost:
            shown = ghost[:20]
            print("仅历史标签残留（窗口内无存活序列，%d 个，已折叠；仅列前 20 个）: %s%s"
                  % (len(ghost), ", ".join(shown), " ..." if len(ghost) > 20 else ""))
            try:
                allf = os.path.join(os.path.abspath(args.out_dir), "prom_jobs_all.txt")
                with open(allf, "w", encoding="utf-8") as f:
                    f.write("\n".join(jobs) + "\n")
                print("完整 job 列表已写入: %s" % allf)
            except OSError:
                pass
        if args.job:
            rx = re.compile(args.job)
            hits = [j for j in valid if rx.search(j)]
            print("正则 %r 命中窗口内有效 job %d 个%s" % (args.job, len(hits), "：" + ", ".join(hits) if hits else "（无命中！）"))
            if not hits:
                sug = suggest_jobs(args.job, valid)
                ghost_hit = [j for j in ghost if rx.search(j)]
                if sug:
                    print("提示: 你可能想用: %s" % ", ".join(sug))
                if ghost_hit:
                    print("注意: %s 只存在于历史标签取值（改名残留），窗口内无存活序列" % ", ".join(ghost_hit[:5]))
        return
    step = parse_step(args.step, start, end)

    match_sel = ",".join(args.match)
    job_sel = 'job=~"%s"' % args.job.replace('"', '') if args.job else ""
    parts = [p for p in (job_sel, match_sel) if p]
    base_sel = ",".join(parts)

    pools = []
    if args.pool:
        for p in args.pool:
            if "=" not in p:
                raise SystemExit("--pool 格式: NAME=正则，收到 %r" % p)
            name, rx = p.split("=", 1)
            pools.append((name, 'job=~"%s"' % rx.replace('"', '')))
    if args.model:
        # selector_extra 不带前导逗号（find_model_jobs 内部统一拼接），避免双逗号
        extra = base_sel
        groups, _ = find_model_jobs(client, args.model, extra, start, end, step, args.split_by)
        all_jobs = set()
        for js in groups.values():
            all_jobs |= js
        pref = job_prefix(sorted(all_jobs))
        outdir = args.out if args.out else args.out_dir
        if not os.path.isdir(outdir):
            os.makedirs(outdir)
        for role, jobs in sorted(groups.items()):
            sel = 'model_name="%s",job=~"%s"' % (args.model, "|".join(sorted(jobs)))
            engines, times = collect(client, sel, args.tag_label, start, end, step)
            fixed, corrected = fix_negative_hist(engines) if args.negative_hist == "auto" else ([], {})
            order, rows = build_rows(engines, times, tz_off, corrected)
            health_check(order, engines, times, fixed, step)
            fname = model_out_fname(pref, role, time.strftime("%Y%m%d%H%M%S"))
            write_csv(os.path.join(outdir, fname), rows)
        return
    if pools:
        outdir = args.out if args.out else args.out_dir
        if not os.path.isdir(outdir):
            os.makedirs(outdir)
        for name, sel in pools:
            full_sel = ",".join([p for p in (sel, match_sel) if p])
            engines, times = collect(client, full_sel, args.tag_label, start, end, step)
            if args.negative_hist == "auto":
                fixed, corrected = fix_negative_hist(engines)
            else:
                fixed, corrected = [], {}
            order, rows = build_rows(engines, times, tz_off, corrected)
            health_check(order, engines, times, fixed, step)
            write_csv(os.path.join(outdir, "dp_load_metrics_%s_%s.csv" % (safe_fname(name), time.strftime("%Y%m%d%H%M%S"))), rows)
        return

    if not base_sel:
        raise SystemExit("单池模式需要 --job 或 --match 至少一个条件（或用 --pool / --model）")
    engines, times = collect(client, base_sel, args.tag_label, start, end, step)
    fixed, corrected = fix_negative_hist(engines) if args.negative_hist == "auto" else ([], {})
    order, rows = build_rows(engines, times, tz_off, corrected)
    health_check(order, engines, times, fixed, step)
    out = args.out if args.out else os.path.join(args.out_dir, "dp_load_metrics_prom_%s.csv" % time.strftime("%Y%m%d%H%M%S"))
    write_csv(out, rows)


if __name__ == "__main__":
    main()
