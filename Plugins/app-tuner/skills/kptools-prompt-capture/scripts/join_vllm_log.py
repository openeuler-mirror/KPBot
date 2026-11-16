# join_vllm_log.py：把 vLLM（rosinL/vllm 5d1d24cd 的 [TTFT_DEBUG] 日志）里每个请求的渲染/分词/引擎耗时，
# 按请求编号接到 analyze.py 的 requests.csv 上，回答「分词占用户 TTFT 多少」「分词时间里多少是排队」。
#   python join_vllm_log.py --requests <analysis>/requests.csv --p-log P0.log [P1.log ...] --d-log D0.log [...] --out <目录>
#          [--data <采集目录> --tokenizer <模型目录>]   # 给了就在本机重分一遍，算纯计算时间与排队时间
# 对接依据：proxy 给每个请求生成的 X-Request-Id（采集 e 行 rid / requests.csv 的 request_id）同时发给 P 与 D；
#   vLLM 把它变成 chatcmpl-<rid> / cmpl-<rid>，出现在 [TTFT_DEBUG][Prometheus] req= 行；
#   [TTFT_DEBUG][Tokenizer] 行原版没有编号（vllm_patch/ 里的补丁给它加上 req= tokens= chars=）。
# 输出：
#   requests_joined.csv   requests.csv 全部列 + p_render_ms p_tokenize_ms p_tok_n p_chars p_hash_ms … p_forward_ms（D 同名 d_*）
#                         + tok_ms（P、D 的渲染+分词合计）tok_share_ttft（÷ ttft_ms）d_dup_ms d_dup_share（D 那次重复分词）
#                         + pure_ms（本机重分的纯计算）p_wait_ms d_wait_ms（线上 tokenize − 纯计算）
#   ttft_breakdown.csv    想法 1/3/4：按 prompt 长度分桶、按前缀缓存命中率分组、按请求类型分组的 TTFT 与分词占比（p50/p90）
#   tokenize_wait.csv     想法 2：按长度分桶的线上 tokenize、纯计算、排队时间（需要 --data --tokenizer）
#   tokenizer_lines.csv   日志里每一行 Tokenizer 记录（不带编号的旧格式也在这里，只能看分布）
#   join_report.html      上述内容的报告：对接情况、分词占 TTFT（按长度/命中率/类型）、计算 vs 排队、引擎各阶段耗时
# 只依赖标准库；--tokenizer 需要 transformers。
from __future__ import annotations

import argparse
import collections
import csv
import json
import math
import os
import re
import statistics
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcap  # noqa: E402
from private_io import prepare_output_dir, private_open
import report  # noqa: E402

RE_LINE = re.compile(r"\[TTFT_DEBUG\]\[(?P<kind>\w+)\]\s*(?P<body>.*)$")
RE_KV = re.compile(r"(\w+)=(\S+)")
RE_TS = re.compile(r"(?:INFO|WARNING|DEBUG|ERROR)\s+(\d\d-\d\d \d\d:\d\d:\d\d)")
PT_BUCKETS = [0, 1024, 4096, 8192, 16384, 32768, 65536, 131072, 262144, float("inf")]
ENGINE_KEYS = ("hash", "ext", "alloc", "overhead", "load", "forward", "save", "update")


def bucket_label(x: float) -> str:
    for lo, hi in zip(PT_BUCKETS, PT_BUCKETS[1:]):
        if lo <= x < hi:
            f = lambda v: f"{int(v) // 1024}k" if v >= 1024 else str(int(v))  # noqa: E731
            return f"{f(lo)}-{f(hi)}" if hi != float("inf") else f">={f(lo)}"
    return "?"


def q(xs: list, p: float):
    xs = sorted(x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x)))
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def fnum(v: str) -> float | None:
    try:
        return float(v)
    except ValueError:
        return None


# ---------------------------------------------------------------- 读日志
def parse_logs(paths: list[str], side: str) -> tuple[dict[str, dict], list[dict], dict[str, int]]:
    """返回 (按 rid 的记录, 全部 Tokenizer 行, 计数)。同一 rid 出现多次（重算）时取第一次，之后计 dup。"""
    by_rid: dict[str, dict] = {}
    tok_lines: list[dict] = []
    n = collections.Counter()
    for path in paths:
        pg = pcap.Progress(f"join {side} {os.path.basename(path)}", os.path.getsize(path) >> 20, unit="MB")
        with open(path, encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                if not lineno & 0x3FFF:  # 每 16384 行看一次进度（按已读字节算）
                    pg.done = f.buffer.tell() >> 20
                    pg.step(0)
                m = RE_LINE.search(line)
                if not m:
                    continue
                kind, body = m.group("kind"), m.group("body")
                kv = dict(RE_KV.findall(body))
                ts = RE_TS.search(line)
                n[kind] += 1
                if kind == "Tokenizer":
                    rec = {"side": side, "file": os.path.basename(path), "line": lineno, "ts": ts.group(1) if ts else "",
                           "rid": kv.get("req") if kv.get("req") not in (None, "-") else None,
                           "render_ms": (fnum(kv.get("render", "")) or 0) * 1000,
                           "tokenize_ms": (fnum(kv.get("tokenize", "")) or 0) * 1000,
                           "n": int(kv.get("n") or 1), "tokens": int(kv["tokens"]) if kv.get("tokens") else None,
                           "chars": int(kv["chars"]) if kv.get("chars") else None}
                    tok_lines.append(rec)
                    if rec["rid"]:
                        d = by_rid.setdefault(rec["rid"], {})
                        if "render_ms" in d:
                            n["Tokenizer_dup"] += 1
                            continue
                        d.update({"render_ms": rec["render_ms"], "tokenize_ms": rec["tokenize_ms"],
                                  "tok_n": rec["tokens"], "chars": rec["chars"]})
                elif kind == "Prometheus":
                    req = kv.get("req", "")
                    rid = re.sub(r"^(chatcmpl|cmpl)-", "", req)
                    d = by_rid.setdefault(rid, {})
                    if "forward_ms" in d:
                        n["Prometheus_dup"] += 1
                        continue
                    for k in ENGINE_KEYS:
                        v = fnum(kv.get(k, ""))
                        d[k + "_ms"] = v * 1000 if v is not None else None
                    d["prom_req"] = req
    return by_rid, tok_lines, dict(n)


# ---------------------------------------------------------------- 纯计算时间（本机重分）
_R: "pcap.Renderer | None" = None


def _pure_init(tokenizer: str, content_format: str, template_kwargs: str | None, trust_remote_code: bool) -> None:
    global _R
    _R = pcap.Renderer(tokenizer, content_format, json.loads(template_kwargs) if template_kwargs else None,
                       trust_remote_code=trust_remote_code)


def _pure_one(job: tuple) -> tuple[str, dict]:
    rid, req, repeat = job
    r = _R
    try:
        body = req.body()
        ep = req.rec.get("ep")
        best_r = best_t = None
        for _ in range(repeat):
            t0 = time.perf_counter()
            text = req.gold_text() or r.render(body, ep)
            t1 = time.perf_counter()
            ids = r.encode(text, ep, body)
            t2 = time.perf_counter()
            best_r = min(best_r, t1 - t0) if best_r is not None else t1 - t0
            best_t = min(best_t, t2 - t1) if best_t is not None else t2 - t1
        return rid, {"pure_render_ms": round(best_r * 1000, 3), "pure_ms": round(best_t * 1000, 3),
                     "pure_tok_n": len(ids), "pure_bytes": len(text.encode("utf-8", "surrogatepass"))}
    except Exception as exc:
        return rid, {"pure_err": f"{type(exc).__name__}: {exc}"[:120]}


def pure_compute(data: list[str], tokenizer: str, rids: set[str], a) -> dict[str, dict]:
    """对采集里能重建的请求，在本机渲染 + 分词一遍，得到纯计算时间（不含排队）。
    --jobs N：N 个进程各占一个核同时测。每条仍是单线程分词，但进程之间会争内存带宽与缓存，测得的时间比
    单进程略长；要拿来和线上比的数字用 --jobs 1 测，或者用 --jobs 先看大概。"""
    out: dict[str, dict] = {}

    def jobs():
        for req in pcap.iter_requests(data, progress="join"):
            rid = (req.end or {}).get("rid")
            if not rid or rid not in rids or not req.text_level:
                continue
            yield rid, (pcap.detach(req) if a.jobs > 1 else req), a.repeat

    if a.jobs > 1:
        print(f"[join] 纯计算时间由 {a.jobs} 个进程同时测：进程之间互相挤占，测得的时间偏长（本机 M2 Pro 实测 4 进程 "
              f"+4%、8 进程 +24%），由它算出的排队时间相应偏短。要精确数字用 --jobs 1", file=sys.stderr)
    with pcap.Workers(a.jobs, _pure_init, (tokenizer, a.content_format, a.template_kwargs, a.trust_remote_code),
                      tag="join") as w:
        for rid, res in w.imap(_pure_one, jobs()):
            out[rid] = res
    return out


# ---------------------------------------------------------------- 汇总表
def dist_row(label: str, group: str, rows_all: list[dict]) -> dict:
    """一组请求的汇总。除 n 以外的所有统计只在接上了渲染/分词耗时的请求上算：
    否则 TTFT 分位数与加权占比的分母会混入没有分子的请求。"""
    rows = [x for x in rows_all if x.get("tok_ms") is not None]

    def col(k):
        return [x[k] for x in rows if x.get(k) is not None]

    return {
        "group": group, "label": label, "n": len(rows_all),
        "n_joined": len(rows),
        "ttft_p50": q(col("ttft_ms"), .5), "ttft_p90": q(col("ttft_ms"), .9),
        "tok_ms_p50": q(col("tok_ms"), .5), "tok_ms_p90": q(col("tok_ms"), .9),
        "tok_share_p50": q(col("tok_share_ttft"), .5), "tok_share_p90": q(col("tok_share_ttft"), .9),
        "p_tokenize_p50": q(col("p_tokenize_ms"), .5), "p_render_p50": q(col("p_render_ms"), .5),
        "d_tokenize_p50": q(col("d_tokenize_ms"), .5), "d_render_p50": q(col("d_render_ms"), .5),
        "d_dup_share_p50": q(col("d_dup_share"), .5),
        "p_forward_p50": q(col("p_forward_ms"), .5), "p_load_p50": q(col("p_load_ms"), .5),
        "d_load_p50": q(col("d_load_ms"), .5),
        "prefill_ms_p50": q(col("prefill_ms"), .5),
        "tok_ms_sum": sum(col("tok_ms")), "ttft_sum": sum(col("ttft_ms")),
        "tok_share_weighted": round(sum(col("tok_ms")) / sum(col("ttft_ms")), 4) if col("ttft_ms") and sum(col("ttft_ms")) else None,
    }


def cache_group(r: dict) -> str | None:
    pt, ct = r.get("prompt_tokens"), r.get("cached_tokens")
    if pt is None or ct is None or pt <= 0:
        return None
    h = ct / pt
    return "hit<50%" if h < 0.5 else ("hit50-90%" if h < 0.9 else "hit>=90%")


def main() -> int:
    ap = argparse.ArgumentParser(description="把 vLLM [TTFT_DEBUG] 日志按请求接到采集分析结果上")
    ap.add_argument("--requests", required=True, help="analyze.py 输出的 requests.csv")
    ap.add_argument("--p-log", nargs="+", default=[], help="P 实例的 vLLM 日志（每个实例一个文件）")
    ap.add_argument("--d-log", nargs="+", default=[], help="D 实例的 vLLM 日志")
    ap.add_argument("--out", required=True)
    ap.add_argument("--data", nargs="*", help="采集目录：与 --tokenizer 一起给时本机重分一遍算纯计算时间")
    ap.add_argument("--tokenizer")
    ap.add_argument("--trust-remote-code", action="store_true")
    ap.add_argument("--content-format", default="auto", choices=["auto", "string", "openai"])
    ap.add_argument("--template-kwargs")
    ap.add_argument("--repeat", type=int, default=2, help="纯计算时间每条测几次取最小")
    ap.add_argument("--jobs", type=int, default=1,
                    help="纯计算时间用几个进程同时测。并行时进程之间互相挤占，测得的时间略长；要精确数字用 1")
    ap.add_argument("--no-html", action="store_true", help="不生成 join_report.html")
    a = ap.parse_args()
    prepare_output_dir(a.out)
    if not a.p_log and not a.d_log:
        sys.exit("至少给一个 --p-log 或 --d-log")

    with open(a.requests, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    num_cols = ("ttft_ms", "prefill_ms", "e2e_ms", "prompt_tokens", "cached_tokens", "chars_total", "text_bytes",
                "new_chars", "reuse_ratio_prev")
    for r in rows:
        for k in num_cols:
            r[k] = float(r[k]) if r.get(k) not in (None, "") else None
        r["stream"] = r.get("stream") in ("1", "True", "true")
    p_by, p_lines, p_n = parse_logs(a.p_log, "P") if a.p_log else ({}, [], {})
    d_by, d_lines, d_n = parse_logs(a.d_log, "D") if a.d_log else ({}, [], {})
    tokenizer_has_rid = any(x["rid"] for x in p_lines + d_lines)

    rids = {r["request_id"] for r in rows if r.get("request_id")}
    pure: dict[str, dict] = {}
    if a.data and a.tokenizer:
        pure = pure_compute(a.data, a.tokenizer, rids, a)

    joined_p = joined_d = joined_tok = 0
    for r in rows:
        rid = r.get("request_id")
        for side, by in (("p", p_by), ("d", d_by)):
            d = by.get(rid) if rid else None
            if not d:
                continue
            if r.get("recomputed") not in (None, "", "0"):
                r["join_note"] = "recomputed: 日志里同一编号出现多次，取第一次"
            if side == "p":
                joined_p += 1
            else:
                joined_d += 1
            for k in ("render_ms", "tokenize_ms", "tok_n", "chars"):
                if k in d:
                    r[f"{side}_{k}"] = d[k]
            for k in ENGINE_KEYS:
                if d.get(k + "_ms") is not None:
                    r[f"{side}_{k}_ms"] = d[k + "_ms"]
        parts = [r.get(k) for k in ("p_render_ms", "p_tokenize_ms", "d_render_ms", "d_tokenize_ms")]
        if any(v is not None for v in parts):
            joined_tok += 1
            r["tok_ms"] = round(sum(v for v in parts if v is not None), 3)
            if r.get("ttft_ms"):
                r["tok_share_ttft"] = round(r["tok_ms"] / r["ttft_ms"], 4)
            dd = [r.get("d_render_ms"), r.get("d_tokenize_ms")]
            if any(v is not None for v in dd):
                r["d_dup_ms"] = round(sum(v for v in dd if v is not None), 3)
                if r.get("ttft_ms"):
                    r["d_dup_share"] = round(r["d_dup_ms"] / r["ttft_ms"], 4)
        if rid in pure:
            r.update(pure[rid])
            if r.get("pure_ms") is not None:
                for side in ("p", "d"):
                    if r.get(f"{side}_tokenize_ms") is not None:
                        r[f"{side}_wait_ms"] = round(r[f"{side}_tokenize_ms"] - r["pure_ms"], 3)
    # 写 joined
    extra_cols = ["p_render_ms", "p_tokenize_ms", "p_tok_n", "p_chars", *[f"p_{k}_ms" for k in ENGINE_KEYS],
                  "d_render_ms", "d_tokenize_ms", "d_tok_n", "d_chars", *[f"d_{k}_ms" for k in ENGINE_KEYS],
                  "tok_ms", "tok_share_ttft", "d_dup_ms", "d_dup_share", "pure_render_ms", "pure_ms", "pure_tok_n",
                  "pure_bytes", "p_wait_ms", "d_wait_ms", "pure_err", "join_note"]
    base_cols = list(rows[0].keys()) if rows else []
    cols = [c for c in base_cols if c not in extra_cols] + extra_cols
    with private_open(os.path.join(a.out, "requests_joined.csv"), newline="") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else (int(v) if isinstance(v, bool) or (isinstance(v, float) and v.is_integer())
                                                   else v)) for k, v in r.items()})
    # 想法 1/3/4：分组表
    ok = [r for r in rows if r.get("ttft_ms") and r.get("status") == "ok" and r["stream"]]
    groups: list[dict] = [dist_row("ALL", "all", ok)]
    by_b: dict[str, list] = collections.defaultdict(list)
    for r in ok:
        if r.get("prompt_tokens") is not None:
            by_b[bucket_label(r["prompt_tokens"])].append(r)
    order = {bucket_label(x): i for i, x in enumerate(PT_BUCKETS[:-1])}
    for k in sorted(by_b, key=lambda x: order.get(x, 99)):
        groups.append(dist_row(k, "prompt_tokens", by_b[k]))
    by_c: dict[str, list] = collections.defaultdict(list)
    for r in ok:
        g = cache_group(r)
        if g:
            by_c[g].append(r)
    for k in ("hit<50%", "hit50-90%", "hit>=90%"):
        if by_c.get(k):
            groups.append(dist_row(k, "cache_hit", by_c[k]))
    by_k: dict[str, list] = collections.defaultdict(list)
    for r in ok:
        by_k[r.get("kind") or "?"].append(r)
    for k, rs in sorted(by_k.items(), key=lambda kv: -len(kv[1])):
        groups.append(dist_row(k, "kind", rs))
    gcols = list(groups[0].keys())
    with private_open(os.path.join(a.out, "ttft_breakdown.csv"), newline="") as f:
        w = csv.DictWriter(f, gcols)
        w.writeheader()
        for g in groups:
            w.writerow({k: (round(v, 3) if isinstance(v, float) else v) for k, v in g.items()})
    # 想法 2：排队
    wait_rows = []
    if pure:
        for k in sorted(by_b, key=lambda x: order.get(x, 99)):
            rs = [r for r in by_b[k] if r.get("pure_ms") is not None]
            if not rs:
                continue
            for side in ("p", "d"):
                tk = [r[f"{side}_tokenize_ms"] for r in rs if r.get(f"{side}_tokenize_ms") is not None]
                wt = [r[f"{side}_wait_ms"] for r in rs if r.get(f"{side}_wait_ms") is not None]
                if not tk:
                    continue
                wait_rows.append({"bucket": k, "side": side.upper(), "n": len(tk),
                                  "online_tokenize_p50": q(tk, .5), "online_tokenize_p90": q(tk, .9),
                                  "pure_p50": q([r["pure_ms"] for r in rs], .5),
                                  "wait_p50": q(wt, .5), "wait_p90": q(wt, .9),
                                  "wait_share_p50": q([r[f"{side}_wait_ms"] / r[f"{side}_tokenize_ms"] for r in rs
                                                       if r.get(f"{side}_wait_ms") is not None and r[f"{side}_tokenize_ms"]], .5),
                                  "pure_mb_s": round(sum(r["pure_bytes"] for r in rs) / 2**20 /
                                                     (sum(r["pure_ms"] for r in rs) / 1000 or 1e-9), 2)})
        with private_open(os.path.join(a.out, "tokenize_wait.csv"), newline="") as f:
            w = csv.DictWriter(f, ["bucket", "side", "n", "online_tokenize_p50", "online_tokenize_p90", "pure_p50",
                                   "wait_p50", "wait_p90", "wait_share_p50", "pure_mb_s"])
            w.writeheader()
            for x in wait_rows:
                w.writerow({k: (round(v, 3) if isinstance(v, float) else v) for k, v in x.items()})
    with private_open(os.path.join(a.out, "tokenizer_lines.csv"), newline="") as f:
        w = csv.DictWriter(f, ["side", "file", "line", "ts", "rid", "render_ms", "tokenize_ms", "n", "tokens", "chars"])
        w.writeheader()
        for x in p_lines + d_lines:
            w.writerow(x)
    summary = {"requests": len(rows), "with_request_id": len(rids), "p_log_lines": p_n, "d_log_lines": d_n,
               "joined_p": joined_p, "joined_d": joined_d, "joined_tokenizer": joined_tok,
               "tokenizer_lines_have_rid": tokenizer_has_rid, "pure_computed": len(pure),
               "pure_jobs": a.jobs if pure else None}
    with private_open(os.path.join(a.out, "join_summary.json")) as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    # 控制台
    print(f"[join] requests.csv {len(rows)} 行，其中 {len(rids)} 行有 request_id；P 日志 {p_n}，D 日志 {d_n}")
    print(f"[join] 接上 P {joined_p} 条、D {joined_d} 条；有渲染/分词耗时的 {joined_tok} 条"
          + ("" if tokenizer_has_rid else "（Tokenizer 行没有请求编号：只能看 tokenizer_lines.csv 的分布，需要打 vllm_patch）"))
    if not tokenizer_has_rid and (p_lines or d_lines):
        for side, ls in (("P", p_lines), ("D", d_lines)):
            if ls:
                print(f"       {side} Tokenizer 行 {len(ls)} 条：tokenize p50 {q([x['tokenize_ms'] for x in ls], .5):.2f} ms "
                      f"p90 {q([x['tokenize_ms'] for x in ls], .9):.2f} ms，render p50 {q([x['render_ms'] for x in ls], .5):.2f} ms")
    print("\n分词（P 渲染+分词 + D 渲染+分词）占用户 TTFT 的比例（流式、状态 ok）：")
    print(f"   {'分组':<14}{'n':>5}{'接上':>5}{'TTFT p50':>10}{'分词 p50':>10}{'占比 p50':>9}{'占比 p90':>9}{'D 重复 p50':>11}{'加权占比':>9}")
    for g in groups:
        f_ = lambda v, d=1: "-" if v is None else (f"{v:.{d}f}" if isinstance(v, float) else str(v))  # noqa: E731
        pct = lambda v: "-" if v is None else f"{100 * v:.1f}%"  # noqa: E731
        print(f"   {g['group'] + ':' + g['label']:<14}{g['n']:>5}{g['n_joined']:>5}{f_(g['ttft_p50']):>10}{f_(g['tok_ms_p50'], 2):>10}"
              f"{pct(g['tok_share_p50']):>9}{pct(g['tok_share_p90']):>9}{pct(g['d_dup_share_p50']):>11}{pct(g['tok_share_weighted']):>9}")
    if wait_rows:
        print("\n线上 tokenize 与本机纯计算的差（= 排队/凑批等待；本机与线上机器不同时只看相对值）：")
        for x in wait_rows:
            print(f"   {x['bucket']:<10}{x['side']} n={x['n']:<4} 线上 p50 {x['online_tokenize_p50']:.2f} ms  纯计算 p50 {x['pure_p50']:.2f} ms  "
                  f"等待 p50 {x['wait_p50']:.2f} ms（占 {100 * (x['wait_share_p50'] or 0):.0f}%）  本机 {x['pure_mb_s']} MB/s")
    if not a.no_html:
        write_html(os.path.join(a.out, "join_report.html"), rows, ok, groups, wait_rows, summary, a)
    print(f"[join] -> {a.out}")
    return 0


# ---------------------------------------------------------------- HTML 报告
STAGES = [("render_ms", "模板渲染"), ("tokenize_ms", "分词"), ("hash_ms", "块哈希与本地缓存查找"), ("ext_ms", "外部 KV 查找"),
          ("alloc_ms", "KV 块分配"), ("overhead_ms", "调度开销"), ("load_ms", "加载 KV"), ("forward_ms", "前向计算"),
          ("save_ms", "保存 KV"), ("update_ms", "输出更新")]


def write_html(path: str, rows: list[dict], ok: list[dict], groups: list[dict], wait_rows: list[dict], summary: dict, a) -> None:
    """版式与措辞约定同 report.py：图上方为图注（横轴、纵轴、标注），下方为由数据算出的结论；关键数字加粗。"""
    E, B, num, pct, fig, defs, table, kpi, h2 = (report.E, report.B, report.num, report.pct, report.fig, report.defs,
                                                   report.table, report.kpi, report.h2)
    n_ok = len(ok)
    all_g = groups[0] if groups else {}
    P: list[str] = []
    P.append("<header class='top'><p class='eyebrow'>PROMPT CAPTURE · 与 vLLM 日志对接</p><h1>分词耗时与 TTFT 对接报告</h1><dl class='meta'>"
             f"<dt>请求表</dt><dd>{E(os.path.abspath(a.requests))}</dd>"
             f"<dt>P 日志</dt><dd>{E(', '.join(os.path.abspath(x) for x in a.p_log) or '–')}</dd>"
             f"<dt>D 日志</dt><dd>{E(', '.join(os.path.abspath(x) for x in a.d_log) or '–')}</dd>"
             f"<dt>纯计算耗时</dt><dd>{E(a.tokenizer + '（本机重新分词，每个请求取 ' + str(a.repeat) + ' 次中的最小值' + ('；' + str(a.jobs) + ' 个进程并行测量' if a.jobs > 1 else '') + '）') if a.tokenizer and a.data else '未计算（需提供 --data 与 --tokenizer）'}</dd>"
             f"<dt>生成时间</dt><dd>{time.strftime('%Y-%m-%d %H:%M:%S')}</dd></dl></header>")
    P.append("<nav class='toc'><div class='row sec'><a href='#summary'>摘要</a><a href='#join'>1 对接情况</a><a href='#share'>2 分词占 TTFT</a>"
             "<a href='#wait'>3 计算与等待</a><a href='#stages'>4 引擎各阶段</a></div></nav>")
    # 摘要
    b = []
    b.append(f"requests.csv 含 {B(num(summary['requests']))} 个请求，其中 {B(num(summary['with_request_id']))} 个含请求编号；"
             f"与 P 侧日志对接成功 {B(num(summary['joined_p']))} 个，与 D 侧日志对接成功 {B(num(summary['joined_d']))} 个。")
    if not summary["tokenizer_lines_have_rid"] and (summary["p_log_lines"] or summary["d_log_lines"]):
        b.append("<mark>vLLM 的 [Tokenizer] 日志不含请求编号（未应用 vllm_patch）</mark>，渲染与分词耗时无法按请求对接；其分布见 tokenizer_lines.csv。")
    if all_g.get("tok_share_p50") is not None:
        b.append(f"在流式且状态为 ok 的 {B(num(n_ok))} 个请求上，P、D 两侧渲染与分词耗时合计占 TTFT 的比例：中位数 {B(pct(all_g['tok_share_p50']))}，"
                 f"P90 {B(pct(all_g['tok_share_p90']))}，token 加权 {B(pct(all_g['tok_share_weighted']))}。该比例为分词优化可缩短 TTFT 的上限。")
    if all_g.get("d_dup_share_p50") is not None:
        b.append(f"D 侧重复渲染与分词的耗时占 TTFT 的中位数为 {B(pct(all_g['d_dup_share_p50']))}；该比例为 D 复用 P 的 token ids 可缩短 TTFT 的上限。")
    if wait_rows:
        ws = [w for w in wait_rows if w.get("wait_share_p50") is not None]
        if ws:
            hi = max(ws, key=lambda w: w["wait_share_p50"])
            b.append(f"线上分词耗时中等待（凑批排队）所占比例的中位数，各区间中最高为 {B(pct(hi['wait_share_p50']))}（{E(hi['side'])} 侧 {E(hi['bucket'])} 区间）。"
                     + ("该区间的等待超过线上分词耗时的一半，耗时主要来自分词线程的串行凑批。" if hi["wait_share_p50"] > .5 else "各区间的等待均未超过线上分词耗时的一半。"))
    P.append("<h2 id='summary'><span>摘要</span></h2><div class='summary'><ul>" + "".join(f"<li>{x}</li>" for x in b) + "</ul></div>")
    kp = [kpi(num(n_ok), "个", "流式且状态为 ok 的请求", "占比统计的范围", "proxy"),
          kpi(num(all_g.get("n_joined")), "个", "含渲染与分词耗时的请求", "在 vLLM 日志中匹配到相同请求编号", "log"),
          kpi(pct(all_g.get("tok_share_p50")), "", "分词占 TTFT 的中位数", "P、D 两侧渲染与分词耗时之和 / TTFT", "log", "timer"),
          kpi(pct(all_g.get("tok_share_p90")), "", "分词占 TTFT 的 P90", "按升序排列后位于 90% 位置的值", "log", "timer"),
          kpi(pct(all_g.get("d_dup_share_p50")), "", "D 侧重复分词占 TTFT 的中位数", "D 侧渲染与分词耗时 / TTFT", "log", "timer"),
          kpi(num(all_g.get("ttft_p50"), 1), "ms", "TTFT 首块近似中位数", "请求到达 proxy 至 D 首块数据", "timer"),
          kpi(num(all_g.get("tok_ms_p50"), 2), "ms", "渲染与分词耗时的中位数", "P、D 两侧之和", "log")]
    P.append("<div class='kpis k4'>" + "".join(kp) + "</div>")
    # 1 对接
    P.append(h2("join", "1", "对接情况", "proxy", "log"))
    P.append(defs([("请求编号", "proxy 生成的 X-Request-Id，记录于采集数据 e 行的 rid 字段；在 vLLM 日志中的形式为 chatcmpl-<rid> 或 cmpl-<rid>"),
                   ("Tokenizer 行", "vLLM 日志 [TTFT_DEBUG][Tokenizer]：模板渲染与分词各自的耗时；应用 vllm_patch 后含请求编号"),
                   ("Prometheus 行", "vLLM 日志 [TTFT_DEBUG][Prometheus]：请求结束时引擎各阶段的累计耗时。数值为整步耗时，同一步内的各请求各记一份"),
                   ("重算", "proxy 对同一请求以相同编号重新发送（recompute）时，该编号在日志中出现两次，取第一次；此类请求在 requests_joined.csv 的 join_note 列标出")]))
    nreq = summary["requests"] or 1
    jt = [["requests.csv 中的请求", num(summary["requests"]), "100.0%"], ["含请求编号的请求", num(summary["with_request_id"]), pct(summary["with_request_id"] / nreq)],
          ["P 日志：Tokenizer / Prometheus 行", f"{num(summary['p_log_lines'].get('Tokenizer', 0))} / {num(summary['p_log_lines'].get('Prometheus', 0))}", ""],
          ["D 日志：Tokenizer / Prometheus 行", f"{num(summary['d_log_lines'].get('Tokenizer', 0))} / {num(summary['d_log_lines'].get('Prometheus', 0))}", ""],
          ["与 P 侧 / D 侧日志对接成功的请求", f"{num(summary['joined_p'])} / {num(summary['joined_d'])}", f"{pct(summary['joined_p'] / nreq)} / {pct(summary['joined_d'] / nreq)}"],
          ["含渲染与分词耗时的请求", num(summary["joined_tokenizer"]), pct(summary["joined_tokenizer"] / nreq)],
          ["Tokenizer 行是否含请求编号", "是" if summary["tokenizer_lines_have_rid"] else "否", ""],
          ["已测量纯计算耗时的请求", num(summary["pure_computed"]), pct(summary["pure_computed"] / nreq)]]
    P.append(table(["项目", "数量", ("占比", "占 requests.csv 中的请求")], jt, {1, 2}, {1}))
    # 2 分词占 TTFT
    P.append(h2("share", "2", "分词耗时占 TTFT 的比例", "log", "timer"))
    P.append(defs([("tok_ms", "P 侧与 D 侧的模板渲染耗时、分词耗时之和，单位毫秒；日志中的分词耗时含凑批等待"),
                   ("ttft_ms", "请求到达 proxy 至 D 第一块响应数据到达的时间；该块不一定包含首 token。仅统计流式且状态为 ok 的请求"),
                   ("分词占 TTFT", "tok_ms / ttft_ms。token 加权值为全部请求的 tok_ms 之和 / ttft_ms 之和"),
                   ("P50 / P90", "各请求的该比例按升序排列后位于 50% / 90% 位置的值；P50 即中位数"),
                   ("分组", "按 prompt_tokens 区间（1k = 1024）、前缀缓存命中率（cached_tokens / prompt_tokens）、请求类型（本请求的 messages 相对同线程上一请求的变化）分别分组")]))
    kind_cn = {**report.KIND_CN, "hit<50%": "命中率 <50%", "hit50-90%": "命中率 50–90%", "hit>=90%": "命中率 ≥90%"}
    for grp, title, sec_id in (("prompt_tokens", "按 prompt 长度", "share_len"), ("cache_hit", "按前缀缓存命中率", "share_hit"), ("kind", "按请求类型", "share_kind")):
        gs = [g for g in groups if g["group"] == grp]
        if not gs:
            continue
        labs = [kind_cn.get(g["label"], g["label"]) for g in gs]
        p50 = [None if g["tok_share_p50"] is None else g["tok_share_p50"] * 100 for g in gs]
        p90 = [None if g["tok_share_p90"] is None else g["tok_share_p90"] * 100 for g in gs]
        dd = [None if g["d_dup_share_p50"] is None else g["d_dup_share_p50"] * 100 for g in gs]
        ntot = sum(g["n"] for g in gs) or 1
        tbl = table([title.replace("按", ""), "请求数", "占请求数", "对接成功", ("TTFT P50", "ms"), ("tok_ms P50", "ms"), ("分词占 TTFT", "P50"), ("分词占 TTFT", "P90"), ("D 侧重复占 TTFT", "P50"), ("分词占 TTFT", "token 加权")],
                    [[l, g["n"], pct(g["n"] / ntot), g["n_joined"], num(g["ttft_p50"], 1), num(g["tok_ms_p50"], 2), pct(g["tok_share_p50"]), pct(g["tok_share_p90"]),
                      pct(g["d_dup_share_p50"]), pct(g["tok_share_weighted"])] for l, g in zip(labs, gs)], set(range(1, 10)), {6})
        have = [(l, g) for l, g in zip(labs, gs) if g["tok_share_p50"] is not None]
        fnd = ""
        if have:
            hi, lo = max(have, key=lambda x: x[1]["tok_share_p50"]), min(have, key=lambda x: x[1]["tok_share_p50"])
            fnd = (f"分词占 TTFT 的中位数，各组中最高为「{E(hi[0])}」的 {B(pct(hi[1]['tok_share_p50']))}（{B(num(hi[1]['n']))} 个请求，占 {B(pct(hi[1]['n'] / ntot))}）"
                   + (f"，最低为「{E(lo[0])}」的 {B(pct(lo[1]['tok_share_p50']))}。" if lo is not hi else "。"))
        P.append(fig(f"分词占 TTFT 的比例，{title}分组", report.grouped(labs, [("分词占 TTFT，P50", p50), ("分词占 TTFT，P90", p90), ("D 侧重复分词占 TTFT，P50", dd)],
                                                         unit="%", width=1080, sub=[f"n={num(g['n'])} · {pct(g['n'] / ntot)}" for g in gs]),
                     read="横轴：分组，其下为该组的请求数及其占全部请求的比例。纵轴：占 TTFT 的比例，单位 %。"
                          "统计范围为含渲染与分词耗时、流式且状态为 ok 的请求。",
                     finding=fnd, data_table=tbl))
    # 3 排队
    P.append(h2("wait", "3", "分词耗时中的计算与等待", "log", "off"))
    if wait_rows:
        P.append(defs([("线上分词耗时", "vLLM 日志中该请求的 tokenize 耗时，含 AsyncMicrobatchTokenizer 的凑批等待"),
                       ("纯计算耗时", "本机对同一段模板后文本重新分词的耗时；含 golden 的请求使用 golden 文本。本机与线上机器不同时，该值仅用于各区间之间的相对比较"),
                       ("等待", "线上分词耗时 − 纯计算耗时；等待占比 = 等待 / 线上分词耗时")]))
        for side in ("P", "D"):
            ws = [w for w in wait_rows if w["side"] == side]
            if not ws:
                continue
            labs = [w["bucket"] for w in ws]
            hv = [w for w in ws if w.get("wait_share_p50") is not None]
            fnd = ""
            if hv:
                hi = max(hv, key=lambda w: w["wait_share_p50"])
                fnd = (f"{side} 侧等待占比的中位数，各区间中最高为 {B(hi['bucket'])} 区间的 {B(pct(hi['wait_share_p50']))}；"
                       f"该区间线上分词耗时的中位数为 {B(num(hi['online_tokenize_p50'], 2))} ms，纯计算耗时的中位数为 {B(num(hi['pure_p50'], 2))} ms。"
                       + ("<mark>该区间的等待超过线上分词耗时的一半，耗时主要来自凑批排队</mark>。" if hi["wait_share_p50"] > .5 else ""))
            P.append(fig(f"{side} 侧的线上分词耗时、纯计算耗时与等待，按 prompt 长度分组",
                         report.grouped(labs, [("线上分词耗时，P50", [w["online_tokenize_p50"] for w in ws]), ("纯计算耗时，P50", [w["pure_p50"] for w in ws]),
                                               ("等待，P50", [w["wait_p50"] for w in ws])], unit=" ms", width=1080, sub=[f"n={num(w['n'])}" for w in ws]),
                         read="横轴：prompt_tokens 区间，其下 n 为该区间的请求数。纵轴：耗时的中位数，单位毫秒。",
                         finding=fnd,
                         warn=(f"纯计算耗时以 {a.jobs} 个进程并行测得，受进程间争用影响，数值高于单进程实测值；需引用的数值应以 --jobs 1 重新测量。" if getattr(a, "jobs", 1) > 1 else ""),
                         data_table=table(["区间", "请求数", ("线上分词 P50", "ms"), ("线上分词 P90", "ms"), ("纯计算 P50", "ms"), ("等待 P50", "ms"), ("等待 P90", "ms"), ("等待占比", "P50"), ("本机吞吐", "MB/s")],
                                          [[w["bucket"], w["n"], num(w["online_tokenize_p50"], 2), num(w["online_tokenize_p90"], 2), num(w["pure_p50"], 2),
                                            num(w["wait_p50"], 2), num(w["wait_p90"], 2), pct(w["wait_share_p50"]), w["pure_mb_s"]] for w in ws], set(range(1, 9)), {7})))
    else:
        P.append("<p class='def'>未计算。纯计算耗时的测量需提供 --data（采集目录）与 --tokenizer（模型目录）；在与线上相同的机器上测量时，其数值可与线上分词耗时直接比较。</p>")
    # 4 引擎阶段
    P.append(h2("stages", "4", "P、D 两侧各阶段耗时", "log"))
    P.append(defs([("口径", "流式且状态为 ok 的请求上各阶段耗时的中位数，单位毫秒。模板渲染与分词取自 [Tokenizer] 行；其余阶段取自 [Prometheus] 行，"
                          "为该请求所经历各步的整步耗时之和，同一步内的各请求各记一份。<b>各阶段的数值不可相加</b>，因此不给出占比")], raw=True))
    for side in ("p", "d"):
        labs, vals = [], []
        for k, cn in STAGES:
            xs = [r.get(f"{side}_{k}") for r in ok if r.get(f"{side}_{k}") is not None]
            if xs:
                labs.append(cn)
                vals.append(q(xs, .5))
        if labs:
            top = max(zip(labs, vals), key=lambda x: x[1])
            tk = dict(zip(labs, vals))
            P.append(fig(f"{side.upper()} 侧各阶段耗时中位数", report.hbars(labs, vals, unit=" ms", fmt=lambda v: num(v, 2), lab_w=170, width=1080),
                         read="柱长：该阶段耗时的中位数，单位毫秒。",
                         finding=f"{side.upper()} 侧耗时中位数最大的阶段为 {B(top[0])}，{B(num(top[1], 2))} ms"
                                 + "".join(f"；{nm}为 {B(num(tk[nm], 2))} ms" for nm in ("模板渲染", "分词") if nm in tk and nm != top[0]) + "。"))
    P.append("<p class='foot'>由 prompt_capture/tools/join_vllm_log.py 生成。字段定义见 docs/REFERENCE.md §7.9。</p>")
    doc = (f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>分词耗时与 TTFT 对接报告</title><style>{report.CSS}</style></head><body><main>{''.join(P)}</main><script>{report.JS}</script></body></html>")
    with private_open(path) as f:
        f.write(doc)


if __name__ == "__main__":
    sys.exit(main())
