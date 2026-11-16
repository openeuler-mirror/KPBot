# analyze.py：把 proxy 采集到的数据变成特征表。
#   python analyze.py <采集目录> [<采集目录> ...] --out <输出目录> [--tokenizer <模型目录>]
# 输出（列定义见 README.md「分析输出」）：
#   requests.csv        每个请求一行：身份与时间、请求参数、结构、会话与前缀复用、文本字符构成、vLLM 返回的 token 数与耗时、
#                       （给了 --tokenizer 时）离线渲染与分词结果、预分词统计、词缓存模拟、token 级前缀复用
#   sessions.csv        每个会话一行
#   summary.csv         汇总指标（长表：section, metric, value, unit, note）
#   pretokens_top.csv   （给了 --tokenizer）出现最多的预分词片段
#   pretoken_len.csv    （给了 --tokenizer）预分词片段按字节长度分桶
#   report.html         （除非 --no-html）上述结果的图表版
# 默认对全部全文记录做分词分析；只想先看个大概时用 --max-tok N 限制为最早的 N 条。
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
from array import array
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcap  # noqa: E402

LEN_EDGES = [(1, "1"), (2, "2"), (3, "3"), (4, "4"), (8, "5-8"), (16, "9-16"), (32, "17-32"), (63, "33-63"),
             (127, "64-127"), (255, "128-255"), (10**9, ">=256")]
PT_BUCKETS = [0, 1024, 4096, 8192, 16384, 32768, 65536, 131072, 262144, float("inf")]
GRAIN = 16  # token 级前缀比较的粒度（每 16 个 token 一个链式哈希）


def bucket_label(x: float, edges=PT_BUCKETS) -> str:
    for lo, hi in zip(edges, edges[1:]):
        if lo <= x < hi:
            f = lambda v: f"{int(v) // 1024}k" if v >= 1024 else str(int(v))  # noqa: E731
            return f"{f(lo)}-{f(hi)}" if hi != float("inf") else f">={f(lo)}"
    return "?"


def q(xs: list, p: float):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def parse_time(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return time.mktime(time.strptime(s.replace("T", " "), "%Y-%m-%d %H:%M:%S" if ":" in s else "%Y-%m-%d"))


# ---------------------------------------------------------------- 分词相关（可选）
class TokStats:
    """离线渲染 + 分词 + 预分词 + 词缓存模拟。词缓存按 tokenizers v0.23.2 的 BPE 缓存行为模拟：
    每个线程一个缓存、容量 --cache-cap（默认 10000 个词）、只缓存字节长度 < --cache-maxlen（默认 256）的词、
    写满之后不再插入也不淘汰；ignore_merges=true 的模型里整词就在词表中的直接查表，不经过缓存。"""

    def __init__(self, a: argparse.Namespace):
        self.r = pcap.Renderer(a.tokenizer, a.content_format, json.loads(a.template_kwargs) if a.template_kwargs else None,
                               trust_remote_code=a.trust_remote_code)
        self.tok = self.r.tok
        self.bt = getattr(self.tok, "backend_tokenizer", None)
        self.cap, self.maxlen = a.cache_cap, a.cache_maxlen
        self.warm: set[str] = set()
        self.top: collections.Counter[str] = collections.Counter()
        self.len_hist: collections.Counter[int] = collections.Counter()
        self.len_tok_bytes: collections.Counter[int] = collections.Counter()
        self.warm_series: list[tuple[float, float]] = []
        self.pretok_ok = False
        self.vocab: dict[str, int] = {}
        self.ignore_merges = False
        self.special_ids: set[int] = set(getattr(self.tok, "all_special_ids", []) or [])
        if self.bt is not None and self.bt.pre_tokenizer is not None:
            try:
                model = json.loads(self.bt.to_str()).get("model", {})
                self.ignore_merges = bool(model.get("ignore_merges"))
                self.vocab = self.bt.get_vocab()
                added = [t.content for t in self.bt.get_added_tokens_decoder().values()]
                self.special_ids |= set(self.bt.get_added_tokens_decoder().keys())
                self.added_re = re.compile("|".join(re.escape(x) for x in sorted(added, key=len, reverse=True))) \
                    if added else None
                self.pretok_ok = True
            except Exception as exc:  # 例如 tiktoken 分词器：没有这些接口
                print(f"[analyze] pretoken analysis disabled: {exc!r}", file=sys.stderr)
        self.model_type = type(self.tok).__name__

    def pretokens(self, text: str) -> list[str]:
        out: list[str] = []
        segs = self.added_re.split(text) if self.added_re else [text]
        norm = self.bt.normalizer
        for seg in segs:
            if not seg:
                continue
            if norm is not None:
                seg = norm.normalize_str(seg)
            out += [w for w, _ in self.bt.pre_tokenizer.pre_tokenize_str(seg)]
        return out

    def count(self, text: str) -> dict[str, Any] | None:
        """单条请求内、与其他请求无关的预分词统计（可在子进程里做）。按「不同的片段 × 出现次数」算，结果与逐个片段
        模拟相同：Counter 保持首次出现的顺序，一个片段能否进缓存只取决于它首次出现时缓存是否已满；进了缓存后，
        它在本请求里其余 c-1 次出现都命中。"""
        if not self.pretok_ok:
            return None
        words = self.pretokens(text)
        uniq = collections.Counter(words)
        hist: collections.Counter[int] = collections.Counter()
        via: list[tuple[str, int, bool]] = []  # 不能整词查表、要经过词缓存的片段：(片段, 次数, 长度是否允许缓存)
        direct = hit_c = big = mx = n_cold = 0
        for w, c in uniq.items():
            nb = len(w.encode("utf-8"))
            if nb > mx:
                mx = nb
            if nb >= self.maxlen:
                big += c
            hist[next(i for i, (hi, _l) in enumerate(LEN_EDGES) if nb <= hi)] += c
            if self.ignore_merges and w in self.vocab:
                direct += c
                continue
            via.append((w, c, nb < self.maxlen))
            if nb < self.maxlen and n_cold < self.cap:  # 冷缓存：每条请求从空缓存开始
                n_cold += 1
                hit_c += c - 1
        return {"n": len(words), "uniq": uniq, "hist": hist, "via": via, "direct": direct, "hit_c": hit_c,
                "big": big, "mx": mx}

    def simulate(self, c: dict[str, Any] | None, n_tokens: int, ts: float) -> dict[str, Any]:
        """跨请求的部分：热缓存按到达顺序模拟，全局计数累加。必须在主进程里按到达顺序调用。"""
        f: dict[str, Any] = {}
        if c is None:
            return f
        hit_w = 0
        for w, k, cacheable in c["via"]:
            if w in self.warm:
                hit_w += k
            elif cacheable and len(self.warm) < self.cap:
                self.warm.add(w)
                hit_w += k - 1
        self.top.update(c["uniq"])
        self.len_hist.update(c["hist"])
        if len(self.top) > 3_000_000:  # 防止内存无限增长：只保留出现 2 次以上的
            self.top = collections.Counter({k: v for k, v in self.top.items() if v > 1})
        n, direct, hit_c = c["n"], c["direct"], c["hit_c"]
        via_cache = n - direct
        f["pretokens"] = n
        f["pretokens_unique"] = len(c["uniq"])
        f["pretoken_max_bytes"] = c["mx"]
        f["pretokens_ge256b"] = c["big"]
        f["tokens_per_pretoken"] = round(n_tokens / n, 4) if n else None
        f["vocab_direct_frac"] = round(direct / n, 4) if n else None
        f["wc_hit_cold"] = round(hit_c / via_cache, 4) if via_cache else None
        f["wc_hit_warm"] = round(hit_w / via_cache, 4) if via_cache else None
        f["_wc"] = (via_cache, hit_w, hit_c, direct)
        self.warm_series.append((ts, f["wc_hit_warm"] if f["wc_hit_warm"] is not None else 1.0))
        return f

    def analyze(self, text: str, n_tokens: int, ts: float) -> dict[str, Any]:
        return self.simulate(self.count(text), n_tokens, ts)


def chain_hashes(ids: list[int]) -> array:
    out, h = array("q"), 0
    for i in range(0, len(ids) - len(ids) % GRAIN, GRAIN):
        h = hash((h, tuple(ids[i:i + GRAIN])))
        out.append(h)
    return out


def common_prefix(a, b) -> int:
    """a、b 都是「前缀哈希」序列（第 i 个元素代表前 i+1 段的整体）：相等性单调，二分找第一个不同的位置。"""
    lo, hi = 0, min(len(a), len(b))
    while lo < hi:
        mid = (lo + hi) // 2
        if a[mid] == b[mid]:
            lo = mid + 1
        else:
            hi = mid
    return lo


# ---------------------------------------------------------------- 第一遍：逐条请求
ROLE_KEYS = ("system", "user", "assistant", "tool")


def base_row(r: pcap.Req, nodes: dict) -> dict[str, Any]:
    rec, e = r.rec, r.end or {}
    hd = r.headers()
    params = rec.get("params") or {}
    row: dict[str, Any] = {
        "id": rec["id"], "ts": round(rec["ts"], 3),
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["ts"])) + f".{int(rec['ts'] * 1000) % 1000:03d}",
        "worker": rec.get("worker"), "level": rec.get("lvl"), "gold": int(bool(rec.get("gold"))),
        "endpoint": rec.get("ep"), "model": rec.get("model"), "client": pcap.client_family(hd.get("user-agent")),
        "user_agent": hd.get("user-agent"), "ip": rec.get("ip"), "session": rec.get("sess"),
        "session_src": rec.get("ss"), "parent_session": hd.get("x-parent-session-id"),
        "stream": int(bool(rec.get("stream"))), "downgrade": rec.get("why") or "", "record_error": rec.get("err", ""),
        "followed": int(bool(rec.get("fol"))),
    }
    mt = params.get("max_tokens", params.get("max_completion_tokens"))
    tc = params.get("tool_choice")
    rf = params.get("response_format")
    ctk = params.get("chat_template_kwargs")
    row.update({
        "max_tokens": mt if isinstance(mt, int) else None, "temperature": params.get("temperature"),
        "top_p": params.get("top_p"),
        "tool_choice": tc if isinstance(tc, str) or tc is None else "named",
        "response_format": rf.get("type") if isinstance(rf, dict) else None, "n": params.get("n"),
        "has_stop": None if "stop" in (rec.get("param_meta") or {}) else int(bool(params.get("stop"))),
        "reasoning_effort": params.get("reasoning_effort"),
        "template_kwargs": json.dumps(ctk, ensure_ascii=False, sort_keys=True) if isinstance(ctk, dict) else None,
        "other_params": ";".join(sorted(k for k in params if k not in {
            "model", "stream", "stream_options", "max_tokens", "max_completion_tokens", "temperature", "top_p",
            "tool_choice", "response_format", "n", "stop", "reasoning_effort", "chat_template_kwargs"})),
        "body_bytes": rec.get("bb"),
    })
    # 结构（来自消息链节点，元数据级别也有）
    chain = []
    h = rec.get("ml")
    while h is not None and h in r.seg.nodes:
        n = r.seg.nodes[h]
        chain.append(n)
        h = n.get("p")
    chain.reverse()
    cnt = collections.Counter(n["r"] for n in chain)
    chars = collections.Counter()
    for n in chain:
        chars[n["r"]] += n["c"]
        if n["h"] not in nodes:
            nodes[n["h"]] = [n.get("p"), n["i"], n["c"], None]  # parent, 下标, 字符数, 累计字符数（第二遍算）
    row.update({
        "n_msgs": rec.get("nm"), "n_system": cnt["system"], "n_user": cnt["user"], "n_assistant": cnt["assistant"],
        "n_tool": cnt["tool"], "n_other_role": sum(v for k, v in cnt.items() if k not in ROLE_KEYS),
        "last_role": chain[-1]["r"] if chain else None, "n_tool_calls": sum(n.get("tc", 0) for n in chain),
        "n_media_parts": sum(n.get("md", 0) for n in chain), "media_stripped": (rec.get("ms") or [0])[0],
        "n_tools": rec.get("nt"), "tools_bytes": rec.get("tb"), "tools_hash": rec.get("tools"),
        "system_hash": chain[0]["m"] if chain and chain[0]["r"] in ("system", "developer") else None,
        "chars_total": sum(n["c"] for n in chain) if chain else rec.get("pc"),
        "chars_system": chars["system"], "chars_user": chars["user"], "chars_assistant": chars["assistant"],
        "chars_tool": chars["tool"], "chars_reasoning": sum(n.get("rc", 0) for n in chain),
        "max_msg_chars": max((n["c"] for n in chain), default=None), "last_msg_chars": chain[-1]["c"] if chain else None,
        "prompt_kind": rec.get("pk"),
    })
    row["_chain"] = [n["h"] for n in chain]
    # 响应侧
    pt, ct = e.get("pt"), e.get("ct")
    ot = e.get("ot")
    tpot = None  # 只对流式请求有意义：非流式时 D 的第一块就是整个响应
    if rec.get("stream") and isinstance(ot, int) and ot > 1 and e.get("e2e_ms") is not None \
            and e.get("ttft_ms") is not None:
        tpot = round((e["e2e_ms"] - e["ttft_ms"]) / (ot - 1), 3)
    row.update({
        "status": e.get("st", "no_end_record"), "prompt_tokens": pt, "cached_tokens": ct,
        "cache_hit_ratio": round(ct / pt, 4) if isinstance(pt, int) and pt and isinstance(ct, int) else None,
        "completion_tokens": ot, "finish_reason": e.get("fr"), "prefill_ms": e.get("p_ms"), "ttft_ms": e.get("ttft_ms"),
        "e2e_ms": e.get("e2e_ms"), "tpot_ms": tpot, "recomputed": e.get("rc"), "prefill_node": e.get("pn"),
        "decode_node": e.get("dn"), "out_chars": e.get("oc"), "out_reasoning_chars": e.get("orc"),
        "out_tool_calls": e.get("otc"), "out_cut": int(bool(e.get("o_cut"))) if e else None,
        "gold_n": e.get("ids_n"), "request_id": e.get("rid"),
    })
    return row


def text_of(r: pcap.Req) -> tuple[str, list]:
    if r.rec.get("ep") == "chat":
        msgs = r.messages() or []
        parts = [pcap.message_text(m) for m in msgs]
        tools = r.tools()
        if tools is not None:
            parts.append(json.dumps(tools, ensure_ascii=False))
        return "\n".join(parts), msgs
    p = r.prompt()
    if isinstance(p, str):
        return p, []
    if isinstance(p, list) and p and all(isinstance(x, str) for x in p):
        return "\n".join(p), []
    return "", []


def content_cols(f: dict) -> dict:
    n = f["chars"] or 1
    out = {"text_chars": f["chars"], "text_bytes": f["bytes"], "bytes_per_char": round(f["bytes"] / n, 4),
           "script": pcap.dominant_script(f)}
    for name, _d in pcap.CHAR_CLASSES:
        out["cc_" + name] = round(f["cc_" + name] / n, 5)
    for k in ("lines", "line_max_bytes", "ws_run_max", "ws_runs_ge4", "word_max_bytes", "words_ge64b", "words_ge256b",
              "bytes_in_words_ge256b", "digit_runs", "digit_run_max", "base64_like_runs", "special_like", "code_fences",
              "urls"):
        out[k] = f[k]
    out["json_punct_frac"] = round(f["json_punct"] / (f["bytes"] or 1), 5)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="分析 proxy 采集数据，输出特征 CSV 与报告")
    ap.add_argument("data", nargs="+", help="采集目录（out_dir）或分段文件")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tokenizer", help="模型目录（含 tokenizer.json / chat_template），用于 token 级特征")
    ap.add_argument("--trust-remote-code", action="store_true")
    ap.add_argument("--content-format", default="auto", choices=["auto", "string", "openai"])
    ap.add_argument("--template-kwargs", help='vLLM 启动参数 --default-chat-template-kwargs 的值（JSON）')
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--jobs", type=int, default=1,
                    help="并行进程数；每条请求的渲染与分词放到子进程，结果与单进程相同。线上机器上注意别和 vLLM 抢核")
    ap.add_argument("--max-tok", type=int, default=0,
                    help="默认 0 = 全部。给 N 时只对最早的 N 条全文记录做分词分析（其余请求的 token 列为空），用来先看个大概")
    ap.add_argument("--block", type=int, default=128, help="理想前缀缓存模拟的块大小（token），需为 16 的倍数")
    ap.add_argument("--cache-cap", type=int, default=10000)
    ap.add_argument("--cache-maxlen", type=int, default=256)
    ap.add_argument("--top", type=int, default=2000)
    ap.add_argument("--no-html", action="store_true")
    ap.add_argument("--include-text", action="store_true", help="明确授权后才在报告中输出实际预分词片段")
    a = ap.parse_args()
    if a.block % GRAIN:
        sys.exit(f"--block 必须是 {GRAIN} 的倍数")
    os.makedirs(a.out, exist_ok=True)
    since, until = parse_time(a.since), parse_time(a.until)
    tk = TokStats(a) if a.tokenizer else None

    t_start = time.time()
    rows: list[dict] = []
    nodes: dict[str, list] = {}
    msg_seen: set[str] = set()
    role_comp: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    n_tok = 0
    stats = collections.Counter()
    pool = None
    if a.jobs > 1:
        import multiprocessing  # noqa: PLC0415

        ns = {k: getattr(a, k) for k in ("tokenizer", "content_format", "template_kwargs", "trust_remote_code",
                                         "cache_cap", "cache_maxlen")} if tk is not None else None
        pool = multiprocessing.get_context("spawn").Pool(a.jobs, initializer=_worker_init, initargs=(ns,))
        print(f"[analyze] {a.jobs} worker processes", file=sys.stderr)
    else:
        print("[analyze] single process; add --jobs N to use N cores", file=sys.stderr)
    batch = a.jobs * 4 if pool is not None else 1  # 一批里的请求原文同时在内存里，所以不取太大
    pending: list[tuple[dict, dict]] = []

    def flush() -> None:
        if not pending:
            return
        jobs = [j for _row, j in pending]
        results = pool.map(_work, jobs, chunksize=1) if pool is not None else [_work(j, tk) for j in jobs]
        for (row_, _j), res in zip(pending, results):  # 按到达顺序应用，热缓存模拟的结果与单进程相同
            apply_result(tk, row_, res)
        pending.clear()

    for r in pcap.iter_requests(a.data, progress=True):  # 按到达时刻全局排序（多 worker 的分段交错归并）
        ts = r.rec.get("ts") or 0
        if (since and ts < since) or (until and ts >= until):
            continue
        stats["records"] += 1
        row = base_row(r, nodes)
        if r.text_level and not r.rec.get("err"):
            try:
                text, msgs = text_of(r)
            except pcap.MissingBlob as exc:
                row["record_error"] = f"missing_blob:{exc}"
                stats["missing_blob"] += 1
                rows.append(row)
                continue
            job: dict[str, Any] = {"text": text, "ep": None}
            chain = r.chain() if r.rec.get("ep") == "chat" and r.rec.get("ml") else []
            for n, m in zip(chain, msgs):  # 按角色统计「去重后的」消息正文构成：同一条历史消息只算一次
                if n["m"] in msg_seen:
                    continue
                msg_seen.add(n["m"])
                mf = pcap.text_features(pcap.message_text(m))
                comp = role_comp[n["r"] if n["r"] in ROLE_KEYS else "other"]
                for k, v in mf.items():
                    if k == "chars" or k.startswith("cc_"):
                        comp[k] += v
            tools = r.tools() if r.rec.get("ep") == "chat" else None
            if tools is not None and ("tools", r.rec.get("tools")) not in msg_seen:
                msg_seen.add(("tools", r.rec.get("tools")))
                mf = pcap.text_features(json.dumps(tools, ensure_ascii=False))
                for k, v in mf.items():
                    if k == "chars" or k.startswith("cc_"):
                        role_comp["tools_def"][k] += v
            if tk is not None and (a.max_tok == 0 or n_tok < a.max_tok):
                n_tok += 1
                try:
                    job.update(ep=r.rec.get("ep"), body=r.body(), gold_ids=r.gold_ids(), gold_text=r.gold_text(),
                               pt=row.get("prompt_tokens"))
                except pcap.MissingBlob as exc:
                    row["tok_error"] = f"missing_blob:{exc}"
            pending.append((row, job))
            if len(pending) >= batch:
                flush()
        rows.append(row)
    flush()
    if pool is not None:
        pool.close()
        pool.join()
    n_text = sum(1 for x in rows if x.get("text_chars") is not None)
    if tk is not None and a.max_tok and n_text > n_tok:
        print(f"[analyze] --max-tok {a.max_tok}：{n_text} 条全文请求里只有最早的 {n_tok} 条做了分词分析，"
              f"其余 {n_text - n_tok} 条的 token 列为空。去掉 --max-tok 就是全部", file=sys.stderr)
    rows.sort(key=lambda x: x["ts"])
    t_pass1 = time.time() - t_start
    second_pass(rows, nodes, a.block)
    write_outputs(a, rows, role_comp, tk, stats, t_pass1)
    print(f"[analyze] {len(rows)} requests, {n_tok} tokenized, {time.time() - t_start:.1f}s -> {a.out}",
          file=sys.stderr)
    return 0


def tok_compute(tk: TokStats, job: dict) -> dict[str, Any]:
    """一条请求的离线渲染 + 分词 + 预分词统计。不读写任何跨请求的状态，可以在子进程里做。
    返回 {"row": 写进 requests.csv 的列, "blocks"/"n_ids": 前缀比较用, "count": 给 TokStats.simulate 的输入}。"""
    ep, body, gold_ids, gold_text, pt = job["ep"], job["body"], job["gold_ids"], job["gold_text"], job["pt"]
    row: dict[str, Any] = {}
    out: dict[str, Any] = {"row": row, "blocks": None, "n_ids": None, "count": None, "n_tokens": 0}
    ids = None
    try:
        t0 = time.perf_counter()
        text = tk.r.render(body, ep)
        t1 = time.perf_counter()
        p = body.get("prompt")
        if ep == "completions" and isinstance(p, list) and p and isinstance(p[0], int):
            ids = list(p)
        else:
            ids = tk.r.encode(text, ep, body)
        t2 = time.perf_counter()
        nb = len(text.encode("utf-8", "surrogatepass"))
        row.update({"tok_n": len(ids), "render_bytes": nb, "render_ms": round((t1 - t0) * 1000, 3),
                    "encode_ms": round((t2 - t1) * 1000, 3),
                    "encode_mb_s": round(nb / 2**20 / (t2 - t1), 2) if t2 > t1 and nb else None,
                    "bytes_per_token": round(nb / len(ids), 4) if ids else None,
                    "special_tokens": sum(1 for i in ids if i in tk.special_ids)})
        row["tok_match_vllm"] = int(len(ids) == pt) if isinstance(pt, int) else None
    except Exception as exc:
        row["tok_error"] = f"{type(exc).__name__}: {exc}"[:200]
        text = None
    if gold_ids is not None:
        row["gold_n"] = len(gold_ids)
        if ids is not None:
            row["tok_match_gold"] = int(ids == gold_ids)
            if ids != gold_ids:
                row["tok_first_diff"] = next((i for i, (x, y) in enumerate(zip(ids, gold_ids)) if x != y),
                                             min(len(ids), len(gold_ids)))
    use_ids = gold_ids if gold_ids is not None else ids
    use_text = gold_text if gold_text is not None else text
    row["tok_src"] = "gold" if gold_ids is not None else ("offline" if ids is not None else None)
    if use_ids is not None:
        out["blocks"] = chain_hashes(use_ids)
        out["n_ids"] = len(use_ids)
    if use_text is not None and use_ids is not None:
        out["count"] = tk.count(use_text)
        out["n_tokens"] = len(use_ids)
    return out


# 并行（--jobs N）：每条请求的文本特征、渲染、分词、预分词统计放到子进程；与到达顺序有关的部分（热缓存模拟、
# 全局计数）留在主进程，按原来的顺序应用，所以结果与单进程相同（只有 render_ms / encode_ms 这类耗时列会变）。
_WTK: TokStats | None = None


def _worker_init(ns: dict | None) -> None:
    global _WTK
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")  # 每个子进程只用一个核，总核数由 --jobs 决定
    _WTK = TokStats(argparse.Namespace(**ns)) if ns else None


def _work(job: dict, tk: TokStats | None = None) -> dict[str, Any]:
    res: dict[str, Any] = {"feat": content_cols(pcap.text_features(job["text"])), "tok": None}
    if job.get("ep") is not None:
        res["tok"] = tok_compute(tk or _WTK, job)
    return res


def apply_result(tk: TokStats | None, row: dict, res: dict) -> None:
    row.update(res["feat"])
    t = res["tok"]
    if t is None:
        return
    row.update(t["row"])
    if t["blocks"] is not None:
        row["_blocks"] = t["blocks"]
        row["_n_ids"] = t["n_ids"]
    if t["count"] is not None:
        row.update(tk.simulate(t["count"], t["n_tokens"], row["ts"]))


# ---------------------------------------------------------------- 第二遍：按时间顺序算会话与前缀复用
def second_pass(rows: list[dict], nodes: dict, block: int) -> None:
    def cum(h: str | None) -> int:
        if h is None:
            return 0
        stack = []
        while h is not None and h in nodes and nodes[h][3] is None:
            stack.append(h)
            h = nodes[h][0]
        base = nodes[h][3] if h is not None and h in nodes else 0
        for x in reversed(stack):
            base += nodes[x][2]
            nodes[x][3] = base
        return base

    last_by_session: dict[str, dict] = {}
    thread_prev: dict[tuple, dict] = {}
    turn: collections.Counter[str] = collections.Counter()
    seen_global: set[tuple] = set()
    ideal: set[int] = set()
    step = block // GRAIN
    for row in rows:
        chain = row["_chain"]
        tools = row.get("tools_hash")
        # 全局（跨会话）消息级前缀复用：最长的、之前任何请求出现过的消息前缀（工具定义不同视为不共享）
        k = 0
        lo, hi = 0, len(chain)
        while lo < hi:
            mid = (lo + hi) // 2
            if (tools, chain[mid]) in seen_global:
                lo = mid + 1
            else:
                hi = mid
        k = lo
        for h in chain[k:]:
            seen_global.add((tools, h))
        tot = row["chars_total"] or 0
        pg = cum(chain[k - 1]) if k else 0
        row["prefix_chars_global"] = pg
        row["reuse_ratio_global"] = round(pg / tot, 4) if tot else None
        # 理想前缀缓存（无限容量，块大小 block）：与 vLLM 实际报告的 cached_tokens 对比
        bl = row.get("_blocks")
        if bl is not None:
            n_ids = row["_n_ids"]
            hits = 0
            idx = [j for j in range(step - 1, len(bl), step)]
            for j in idx:
                if bl[j] in ideal:
                    hits += 1
                else:
                    break
            for j in idx:
                ideal.add(bl[j])
            row["ideal_cache_tokens"] = min(hits * block, max(0, (n_ids - 1) // block * block))
        sess = row.get("session")
        if not sess:
            row["kind"] = "no_session"
            continue
        prev_any = last_by_session.get(sess)
        row["turn_idx"] = turn[sess]
        turn[sess] += 1
        row["gap_s"] = round(row["ts"] - prev_any["ts"], 3) if prev_any else None
        last_by_session[sess] = row
        if not chain:  # completions 等没有消息列表的请求：不做消息级比较
            row["kind"] = "no_messages"
            continue
        # 同一会话里按 system 提示分「线程」：起标题、compact 等旁路请求自成一线，不打断主线的前缀比较
        tkey = (sess, row.get("system_hash") or (chain[0] if chain else None), row["endpoint"])
        prev = thread_prev.get(tkey)
        thread_prev[tkey] = row
        if prev is None:
            row["kind"] = "first"
            row["prefix_msgs_prev"] = 0
            row["prefix_chars_prev"] = 0
            row["new_msgs"] = row.get("n_msgs")
            row["new_chars"] = tot
            if row.get("_blocks") is not None:
                row["tok_prefix_prev"] = 0
                row["tok_new"] = row["_n_ids"]
        else:
            pc = prev["_chain"]
            kk = common_prefix(pc, chain) if prev.get("tools_hash") == tools else 0
            same_tools = prev.get("tools_hash") == tools
            if chain == pc and same_tools:
                kind = "repeat"
            elif kk == len(pc) and kk < len(chain):
                kind = "append_" + (row.get("last_role") or "?")
            elif len(chain) < 0.5 * len(pc) and kk <= 2:
                kind = "compact_like"
            else:
                kind = "rewrite"
            row["kind"] = kind
            pcp = cum(chain[kk - 1]) if kk else 0
            row["prefix_msgs_prev"] = kk
            row["prefix_chars_prev"] = pcp
            row["new_msgs"] = len(chain) - kk
            row["new_chars"] = tot - pcp
            row["reuse_ratio_prev"] = round(pcp / tot, 4) if tot else None
            if row.get("_blocks") is not None and prev.get("_blocks") is not None:
                cp = common_prefix(prev["_blocks"], row["_blocks"]) * GRAIN
                row["tok_prefix_prev"] = cp
                row["tok_new"] = row["_n_ids"] - cp
            # 上一个请求没有 token 序列（没抽到全文、超出 --max-tok）时这两列留空，不计入增量分词统计


# ---------------------------------------------------------------- 输出
REQ_COLS = [
    "id", "request_id", "ts", "time", "worker", "level", "gold", "endpoint", "model", "client", "user_agent", "ip", "session",
    "session_src", "parent_session", "stream", "downgrade", "followed", "record_error", "status",
    "max_tokens", "temperature", "top_p", "tool_choice", "response_format", "n", "has_stop", "reasoning_effort",
    "template_kwargs", "other_params",
    "body_bytes", "n_msgs", "n_system", "n_user", "n_assistant", "n_tool", "n_other_role", "last_role",
    "n_tool_calls", "n_media_parts", "media_stripped", "n_tools", "tools_bytes", "tools_hash", "system_hash",
    "chars_total", "chars_system", "chars_user", "chars_assistant", "chars_tool", "chars_reasoning", "max_msg_chars",
    "last_msg_chars", "prompt_kind",
    "turn_idx", "gap_s", "kind", "prefix_msgs_prev", "prefix_chars_prev", "new_msgs", "new_chars", "reuse_ratio_prev",
    "prefix_chars_global", "reuse_ratio_global",
    "text_chars", "text_bytes", "bytes_per_char", "script",
    *["cc_" + n for n, _d in pcap.CHAR_CLASSES],
    "lines", "line_max_bytes", "ws_run_max", "ws_runs_ge4", "word_max_bytes", "words_ge64b", "words_ge256b",
    "bytes_in_words_ge256b", "digit_runs", "digit_run_max", "base64_like_runs", "special_like", "code_fences", "urls",
    "json_punct_frac",
    "prompt_tokens", "cached_tokens", "cache_hit_ratio", "completion_tokens", "finish_reason", "prefill_ms", "ttft_ms",
    "e2e_ms", "tpot_ms", "recomputed", "prefill_node", "decode_node", "out_chars", "out_reasoning_chars",
    "out_tool_calls", "out_cut", "chars_per_token",
    "tok_src", "tok_n", "gold_n", "tok_match_vllm", "tok_match_gold", "tok_first_diff", "tok_error", "render_bytes",
    "render_ms", "encode_ms", "encode_mb_s", "bytes_per_token", "special_tokens", "pretokens", "pretokens_unique",
    "pretoken_max_bytes", "pretokens_ge256b", "tokens_per_pretoken", "vocab_direct_frac", "wc_hit_cold", "wc_hit_warm",
    "tok_prefix_prev", "tok_new", "ideal_cache_tokens",
]


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.6g}" if abs(v) < 1e6 else f"{v:.0f}"
    return v


def write_csv(path: str, cols: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([fmt(r.get(c)) for c in cols])


def dist(xs: list) -> dict:
    xs = [x for x in xs if isinstance(x, (int, float))]
    if not xs:
        return {}
    return {"count": len(xs), "mean": statistics.fmean(xs), "p50": q(xs, 0.5), "p90": q(xs, 0.9), "p95": q(xs, 0.95),
            "p99": q(xs, 0.99), "max": max(xs)}


def write_outputs(a, rows, role_comp, tk, stats, t_pass1) -> None:
    if not a.include_text:
        for row in rows:
            for key in ("template_kwargs", "other_params", "tool_choice", "response_format"):
                row[key] = None
    for r in rows:
        pt = r.get("prompt_tokens")
        if isinstance(pt, int) and pt:
            tc = r.get("text_chars") if r.get("text_chars") is not None else (r.get("chars_total") or 0) + (
                r.get("tools_bytes") or 0)
            r["chars_per_token"] = round(tc / pt, 4) if tc else None
    write_csv(os.path.join(a.out, "requests.csv"), REQ_COLS, rows)
    sessions = build_sessions(rows)
    write_csv(os.path.join(a.out, "sessions.csv"), SESSION_COLS, sessions)
    summ = build_summary(rows, sessions, role_comp, tk, stats, t_pass1)
    with open(os.path.join(a.out, "summary.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["section", "metric", "value", "unit", "note"])
        for s in summ:
            w.writerow([s[0], s[1], fmt(s[2]), s[3] if len(s) > 3 else "", s[4] if len(s) > 4 else ""])
    extra = {}
    if tk is not None and tk.pretok_ok and a.include_text:
        total = sum(tk.top.values()) or 1
        top_rows = []
        dec = tk.bt.decoder
        for rank, (w, c) in enumerate(tk.top.most_common(a.top), 1):
            try:
                text = dec.decode([w]) if dec is not None else w
            except Exception:
                text = w
            nb = len(w.encode("utf-8"))
            top_rows.append({"rank": rank, "pretoken": w, "text": text, "count": c, "share": round(c / total, 6),
                             "bytes": nb, "in_vocab": int(w in tk.vocab), "cacheable": int(nb < tk.maxlen),
                             "n_tokens": len(tk.tok(text, add_special_tokens=False)["input_ids"])})
        write_csv(os.path.join(a.out, "pretokens_top.csv"),
                  ["rank", "pretoken", "text", "count", "share", "bytes", "in_vocab", "cacheable", "n_tokens"], top_rows)
        tot_h = sum(tk.len_hist.values()) or 1
        len_rows = [{"bytes": lbl, "count": tk.len_hist[i], "share": round(tk.len_hist[i] / tot_h, 6)}
                    for i, (_hi, lbl) in enumerate(LEN_EDGES)]
        write_csv(os.path.join(a.out, "pretoken_len.csv"), ["bytes", "count", "share"], len_rows)
        extra = {"top": top_rows[:40], "len": len_rows, "warm_series": tk.warm_series,
                 "example": pretoken_example(tk)}
    print_console(summ)
    if not a.no_html:
        try:
            import report  # noqa: PLC0415

            report.write(os.path.join(a.out, "report.html"), rows, sessions, summ, role_comp, extra, a)
        except ImportError:
            pass


def pretoken_example(tk: TokStats, text: str = 'def main():\n    print("你好，世界")') -> dict | None:
    """报告里解释「预分词片段 / 整词查表 / BPE」用的例子：用本次分析的分词器真实跑出来，不是手写的。"""
    try:
        dec = tk.bt.decoder
        out = []
        for w in tk.pretokens(text):
            shown = dec.decode([w]) if dec is not None else w
            out.append({"text": shown, "bytes": len(w.encode("utf-8")), "in_vocab": w in tk.vocab,
                        "direct": bool(tk.ignore_merges and w in tk.vocab),
                        "n_tokens": len(tk.tok(shown, add_special_tokens=False)["input_ids"])})
        return {"text": text, "pieces": out, "n_tokens": len(tk.tok(text, add_special_tokens=False)["input_ids"])}
    except Exception:  # 例子只用于说明，失败不影响分析
        return None


SESSION_COLS = ["session", "session_src", "client", "model", "parent_session", "n_requests", "n_text", "n_threads",
                "first_time", "last_time", "duration_s", "max_n_msgs", "max_chars_total", "sum_chars_total",
                "sum_new_chars", "reuse_ratio", "n_append", "n_rewrite", "n_compact_like", "n_repeat",
                "sum_prompt_tokens", "sum_cached_tokens", "cache_hit_ratio", "mean_gap_s", "p50_ttft_ms",
                "sum_tok_n", "sum_tok_new", "tok_reuse_ratio"]


def build_sessions(rows: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        if r.get("session"):
            by[r["session"]].append(r)
    out = []
    for s, rs in by.items():
        kinds = collections.Counter(r.get("kind") for r in rs)
        tot = sum(r.get("chars_total") or 0 for r in rs)
        new = sum(r.get("new_chars") or 0 for r in rs)
        pt = sum(r.get("prompt_tokens") or 0 for r in rs)
        ct = sum(r.get("cached_tokens") or 0 for r in rs)
        tn = sum(r.get("_n_ids") or 0 for r in rs if r.get("tok_new") is not None)
        tnew = sum(r.get("tok_new") or 0 for r in rs if r.get("tok_new") is not None)
        gaps = [r["gap_s"] for r in rs if r.get("gap_s") is not None]
        out.append({
            "session": s, "session_src": rs[0].get("session_src"), "client": rs[0].get("client"),
            "model": rs[0].get("model"), "parent_session": rs[0].get("parent_session"), "n_requests": len(rs),
            "n_text": sum(r.get("level") == "text" for r in rs),
            "n_threads": len({(r.get("system_hash"), r.get("endpoint")) for r in rs}),
            "first_time": rs[0]["time"], "last_time": rs[-1]["time"], "duration_s": round(rs[-1]["ts"] - rs[0]["ts"], 3),
            "max_n_msgs": max((r.get("n_msgs") or 0) for r in rs), "max_chars_total": max((r.get("chars_total") or 0) for r in rs),
            "sum_chars_total": tot, "sum_new_chars": new, "reuse_ratio": round(1 - new / tot, 4) if tot else None,
            "n_append": sum(v for k, v in kinds.items() if k and k.startswith("append")),
            "n_rewrite": kinds["rewrite"], "n_compact_like": kinds["compact_like"], "n_repeat": kinds["repeat"],
            "sum_prompt_tokens": pt, "sum_cached_tokens": ct, "cache_hit_ratio": round(ct / pt, 4) if pt else None,
            "mean_gap_s": round(statistics.fmean(gaps), 3) if gaps else None,
            "p50_ttft_ms": q([r.get("ttft_ms") for r in rs], 0.5),
            "sum_tok_n": tn or None, "sum_tok_new": tnew if tn else None,
            "tok_reuse_ratio": round(1 - tnew / tn, 4) if tn else None,
        })
    out.sort(key=lambda x: x["first_time"])
    return out


def build_summary(rows, sessions, role_comp, tk, stats, t_pass1: float) -> list[tuple]:
    S: list[tuple] = []
    add = lambda sec, m, v, unit="", note="": S.append((sec, m, v, unit, note))  # noqa: E731
    n = len(rows)
    text = [r for r in rows if r.get("level") == "text"]
    add("capture", "requests", n, "req", "时间范围内的全部请求")
    add("capture", "text_level", len(text), "req", "含全文的请求（可做文本与分词分析）")
    add("capture", "meta_level", n - len(text), "req", "仅含元数据与消息结构的请求")
    add("capture", "gold", sum(r.get("gold") for r in rows), "req", "proxy 向 P 请求返回 token ids 的请求")
    add("capture", "followed", sum(r.get("followed") for r in rows), "req", "由整会话跟踪（follow_*）采集的请求")
    add("capture", "gold_with_ids", sum(1 for r in rows if r.get("gold_n") is not None), "req",
        "P 返回了 prompt_token_ids 的请求；为 0 表示后端 vLLM 不支持 return_token_ids")
    add("capture", "no_end_record", sum(r["status"] == "no_end_record" for r in rows), "req", "缺少结束记录的请求")
    if rows:
        add("capture", "first_time", rows[0]["time"])
        add("capture", "last_time", rows[-1]["time"])
        span = rows[-1]["ts"] - rows[0]["ts"]
        add("capture", "span_s", round(span, 1), "s")
        per_min = collections.Counter(int(r["ts"] // 60) for r in rows)
        for k, v in dist(list(per_min.values())).items():
            add("traffic", f"requests_per_minute_{k}", round(v, 2), "req/min", "仅统计有请求到达的分钟")
    for col, sec in (("endpoint", "traffic"), ("client", "traffic"), ("model", "traffic"), ("status", "traffic"),
                     ("finish_reason", "traffic"), ("script", "content")):
        c = collections.Counter(r.get(col) for r in rows if r.get(col) is not None)
        tot = sum(c.values()) or 1
        for k, v in c.most_common(30):
            add(sec, f"{col}={k}", v, "req", f"{v / tot:.1%}")
    for col, unit in (("prompt_tokens", "token"), ("chars_total", "char"), ("body_bytes", "byte"), ("n_msgs", "msg"),
                      ("n_tools", "tool"), ("tools_bytes", "byte"), ("completion_tokens", "token"),
                      ("gap_s", "s"), ("new_chars", "char")):
        for k, v in dist([r.get(col) for r in rows]).items():
            add("size", f"{col}_{k}", round(v, 2), unit)
    c = collections.Counter(bucket_label(r["prompt_tokens"]) for r in rows if isinstance(r.get("prompt_tokens"), int))
    tot = sum(c.values()) or 1
    order = {bucket_label(x): i for i, x in enumerate(PT_BUCKETS[:-1])}
    for k in sorted(c, key=lambda x: order.get(x, 99)):
        add("size", f"prompt_tokens_bucket={k}", c[k], "req", f"{c[k] / tot:.1%}")
    tok_sum = sum(r["prompt_tokens"] for r in rows if isinstance(r.get("prompt_tokens"), int))
    for k in sorted(c, key=lambda x: order.get(x, 99)):
        s = sum(r["prompt_tokens"] for r in rows if isinstance(r.get("prompt_tokens"), int)
                and bucket_label(r["prompt_tokens"]) == k)
        add("size", f"prompt_tokens_share_by_bucket={k}", round(s / (tok_sum or 1), 4), "fraction",
            "该区间各请求的 prompt_tokens 之和 / 全部请求的 prompt_tokens 之和")
    # 文本构成（按字符加权）
    for role in ("ALL", *ROLE_KEYS, "tools_def", "other"):
        if role == "ALL":
            comp = collections.Counter()
            for r in text:
                for nme, _d in pcap.CHAR_CLASSES:
                    comp["cc_" + nme] += (r.get("cc_" + nme) or 0) * (r.get("text_chars") or 0)
                comp["chars"] += r.get("text_chars") or 0
            note = "全部含全文的请求；历史消息每重发一次计一次，对应分词器实际处理的文本"
        else:
            comp = role_comp.get(role)
            note = "按角色统计的消息正文；同一条消息只计一次"
            if not comp:
                continue
        tc = comp["chars"] or 1
        for nme, _d in pcap.CHAR_CLASSES:
            add("composition", f"{role}:{nme}", round(comp["cc_" + nme] / tc, 5), "fraction", note)
        add("composition", f"{role}:chars", comp["chars"], "char", note)
    for col in ("words_ge256b", "bytes_in_words_ge256b", "base64_like_runs", "special_like", "ws_runs_ge4",
                "word_max_bytes", "code_fences"):
        vals = [r.get(col) for r in text]
        d = dist(vals)
        if d:
            add("content", f"{col}_p50", round(d["p50"], 2))
            add("content", f"{col}_p99", round(d["p99"], 2))
            add("content", f"{col}_max", d["max"])
            add("content", f"{col}_requests_gt0", sum(1 for v in vals if v), "req")
    tb = sum(r.get("text_bytes") or 0 for r in text)
    add("content", "bytes_in_words_ge256b_share", round(sum(r.get("bytes_in_words_ge256b") or 0 for r in text) / (tb or 1), 5),
        "fraction", "连续无空白片段 ≥256 字节的内容占全文字节数的比例，为文本层面的近似值。词缓存的长度上限按预分词片段在分词器"
                    "内部表示下的字节数判断，实际的超长片段数见 tokenizer 节的 pretokens_ge256b（需 --tokenizer）")
    # 会话
    add("session", "sessions", len(sessions), "session")
    kc = collections.Counter(r.get("kind") for r in rows if r.get("kind") is not None)
    for k, v in kc.most_common():
        add("session", f"kind={k}", v, "req", f"{v / (sum(kc.values()) or 1):.1%}")
    for k, v in dist([s["n_requests"] for s in sessions]).items():
        add("session", f"requests_per_session_{k}", round(v, 2), "req")
    tot_c = sum(r.get("chars_total") or 0 for r in rows if r.get("kind") not in (None, "no_session"))
    pre_c = sum(r.get("prefix_chars_prev") or 0 for r in rows if r.get("kind") not in (None, "no_session"))
    add("session", "reuse_ratio_prev_weighted", round(pre_c / (tot_c or 1), 4), "fraction",
        "与同线程上一请求相同的消息前缀的字符数 / 全部字符数（字符加权）")
    tot_g = sum(r.get("chars_total") or 0 for r in rows)
    add("session", "reuse_ratio_global_weighted",
        round(sum(r.get("prefix_chars_global") or 0 for r in rows) / (tot_g or 1), 4), "fraction",
        "与此前任一请求（工具定义相同）相同的最长消息前缀的字符数 / 全部字符数")
    # vLLM 侧
    pt = sum(r.get("prompt_tokens") or 0 for r in rows)
    ct = sum(r.get("cached_tokens") or 0 for r in rows)
    add("vllm", "prompt_tokens_sum", pt, "token")
    add("vllm", "cached_tokens_sum", ct, "token")
    add("vllm", "cache_hit_ratio_weighted", round(ct / (pt or 1), 4), "fraction", "cached_tokens 之和 / prompt_tokens 之和")
    for col in ("ttft_ms", "prefill_ms", "e2e_ms", "tpot_ms", "chars_per_token"):
        src = [r for r in rows if r.get("stream")] if col in ("ttft_ms", "tpot_ms") else rows
        note = "仅统计流式请求；非流式请求中 D 返回的第一块数据即完整响应" if col in ("ttft_ms", "tpot_ms") else ""
        for k, v in dist([r.get(col) for r in src]).items():
            add("vllm", f"{col}_{k}", round(v, 3), "ms" if col.endswith("_ms") else "", note)
    for k in sorted(c, key=lambda x: order.get(x, 99)):
        xs = [r.get("ttft_ms") for r in rows if r.get("stream") and isinstance(r.get("prompt_tokens"), int)
              and bucket_label(r["prompt_tokens"]) == k]
        d = dist(xs)
        if d:
            add("vllm", f"ttft_ms_p50_by_bucket={k}", round(d["p50"], 2), "ms")
            add("vllm", f"ttft_ms_p95_by_bucket={k}", round(d["p95"], 2), "ms")
    # 分词
    tr = [r for r in rows if r.get("tok_src")]
    if tr:
        add("tokenizer", "tokenized_requests", len(tr), "req")
        m = [r["tok_match_vllm"] for r in tr if r.get("tok_match_vllm") is not None]
        add("tokenizer", "offline_count_match_vllm", round(sum(m) / len(m), 4) if m else None, "fraction",
            "离线分词的 token 数等于 prompt_tokens 的请求占比；小于 1 表示离线预处理与线上不一致")
        g = [r["tok_match_gold"] for r in tr if r.get("tok_match_gold") is not None]
        add("tokenizer", "offline_ids_match_gold", round(sum(g) / len(g), 4) if g else None, "fraction",
            f"离线 token 序列与 golden（vLLM 实际的 token ids）逐 token 相同的请求占比；含 golden 的请求共 {len(g)} 个")
        nb = sum(r.get("render_bytes") or 0 for r in tr)
        nt = sum(r.get("_n_ids") or 0 for r in tr)
        add("tokenizer", "bytes_per_token", round(nb / (nt or 1), 4), "byte/token", "模板后文本的字节数 / token 数")
        by_s = collections.defaultdict(lambda: [0, 0])
        for r in tr:
            if r.get("render_bytes") and r.get("_n_ids"):
                by_s[r.get("script")][0] += r["render_bytes"]
                by_s[r.get("script")][1] += r["_n_ids"]
        for s_, (b_, t_) in sorted(by_s.items(), key=lambda x: -x[1][1]):
            add("tokenizer", f"bytes_per_token_script={s_}", round(b_ / (t_ or 1), 4), "byte/token")
        enc = sum(r.get("encode_ms") or 0 for r in tr) / 1000
        ren = sum(r.get("render_ms") or 0 for r in tr) / 1000
        add("tokenizer", "offline_encode_mb_s", round(nb / 2**20 / enc, 2) if enc else None, "MB/s",
            "本机单线程 HF 分词吞吐；不代表线上机器")
        add("tokenizer", "offline_render_share", round(ren / (ren + enc), 4) if ren + enc else None, "fraction",
            "模板渲染耗时 /（模板渲染耗时 + 分词耗时）")
        wc = [r["_wc"] for r in tr if r.get("_wc")]
        if wc:
            vc, hw, hc, dr = (sum(x[i] for x in wc) for i in range(4))
            add("tokenizer", "pretokens_total", vc + dr, "pretoken")
            add("tokenizer", "vocab_direct_frac", round(dr / ((vc + dr) or 1), 4), "fraction",
                "ignore_merges=true 时经整词查表得到 token 的预分词片段比例")
            add("tokenizer", "word_cache_hit_warm", round(hw / (vc or 1), 4), "fraction",
                f"经过词缓存的片段的命中率；重放条件：单线程、按到达顺序、容量 {tk.cap}、写满后不再插入，缓存在请求之间保留")
            add("tokenizer", "word_cache_hit_cold", round(hc / (vc or 1), 4), "fraction", "经过词缓存的片段的命中率；重放条件：每个请求开始时清空缓存")
            add("tokenizer", "word_cache_filled", len(tk.warm), "word", "重放结束时缓存中的片段数；达到容量后不再增加")
        big = sum(r.get("pretokens_ge256b") or 0 for r in tr)
        add("tokenizer", "pretokens_ge256b", big, "pretoken", "长度 ≥256 字节（按分词器内部表示计）的预分词片段数；不经过词缓存，每次出现均执行 BPE 合并")
        tn = sum(r.get("tok_new") or 0 for r in tr if r.get("tok_new") is not None)
        tt = sum(r.get("_n_ids") or 0 for r in tr if r.get("tok_new") is not None)
        add("tokenizer", "incremental_new_token_share", round(tn / (tt or 1), 4) if tt else None, "fraction",
            "相对同线程上一请求新增的 token 数 / 全部 token 数（比较粒度 16 token）；增量分词可减少的分词量上限为 1 减去该值")
        it = sum(r.get("ideal_cache_tokens") or 0 for r in tr)
        ptt = sum(r.get("prompt_tokens") or 0 for r in tr if r.get("ideal_cache_tokens") is not None)
        ctt = sum(r.get("cached_tokens") or 0 for r in tr if r.get("ideal_cache_tokens") is not None)
        add("tokenizer", "ideal_prefix_cache_hit", round(it / (ptt or 1), 4), "fraction",
            "容量不限、全局共享的前缀缓存的重放命中率；重放仅包含已做离线分词的请求，"
            "时间范围内全部请求均已分词（text_rate=1 且未设 --max-tok）时，该值为命中率上限")
        add("tokenizer", "actual_prefix_cache_hit_same_requests", round(ctt / (ptt or 1), 4), "fraction",
            "同一批请求的实际命中率（cached_tokens 之和 / prompt_tokens 之和）；与上一行之差对应路由分散、缓存容量与淘汰造成的未命中")
    add("meta", "analyze_pass1_s", round(t_pass1, 1), "s", "第一遍处理（读取数据、文本特征、分词）的耗时")
    for k, v in stats.items():
        add("meta", k, v)
    return S


def print_console(summ: list[tuple]) -> None:
    keys = {
        "capture": None, "traffic": ["endpoint=", "client=", "status=", "requests_per_minute_p50"],
        "size": ["prompt_tokens_p50", "prompt_tokens_p90", "prompt_tokens_p99", "prompt_tokens_max",
                 "n_msgs_p50", "n_msgs_max", "prompt_tokens_bucket="],
        "session": None, "vllm": ["cache_hit_ratio_weighted", "ttft_ms_p50", "ttft_ms_p95", "ttft_ms_p50_by_bucket="],
        "tokenizer": None, "content": ["bytes_in_words_ge256b_share", "special_like_requests_gt0"],
        "composition": ["ALL:"],
    }
    cur = None
    for sec, m, v, unit, note in summ:
        want = keys.get(sec, [])
        if sec not in keys or (want is not None and not any(m.startswith(w) for w in want)):
            continue
        if sec != cur:
            print(f"\n== {sec}")
            cur = sec
        print(f"   {m:<48} {fmt(v)!s:>14} {unit:<10} {note}")


if __name__ == "__main__":
    sys.exit(main())
