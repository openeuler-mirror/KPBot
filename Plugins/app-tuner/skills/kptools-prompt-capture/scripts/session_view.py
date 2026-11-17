# session_view.py：把采集数据里的会话逐个「摊开」——每次请求由哪些消息/工具调用/工具结果/模板标记组成，各占多少 token，
# 相邻请求之间哪些变了、为什么变（追加、compact、模板改写历史），以及 vLLM 实际的 prompt_tokens / cached_tokens / 耗时。
#   python session_view.py <采集目录> --out <输出目录> --tokenizer <模型目录> [--session ID ...] [--top 10] [--md new|full|none]
# 输出：
#   sessions.html            交互式：会话列表 → 单会话（生长曲线、消息梯子图、逐请求表、逐消息表、模板后原文分片着色）；
#                            另有「全部会话」总览：token 构成、每步增量、工具结果大小、模板改写损失、可省量估算
#   session_<id>.md          单会话的原始数据文本版：每次请求的参数、消息表、与上一请求的差异、模板后文本（默认只印新增片段）
#   session_<id>.json        单会话的结构化解剖结果（片段区间、token 数、diff）
#   session_requests.csv     每个请求一行的解剖数字；session_messages.csv 每条消息一行
# 没有 --tokenizer 时只有字符级数字（token 列为空）；有 golden 且离线分词与之逐个一致时，token 数即 vLLM 实际的数。
from __future__ import annotations

import argparse
import collections
import csv
import html
import hashlib
import json
import os
import statistics
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import anatomy  # noqa: E402
import pcap  # noqa: E402
from private_io import prepare_output_dir, private_open

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")


def q(xs: list, p: float):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def tfmt(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) + f".{int(ts * 1000) % 1000:03d}"


# ---------------------------------------------------------------- 选会话
def collect_sessions(paths: list[str], a: argparse.Namespace) -> dict[str, list[pcap.Req]]:
    by: dict[str, list[pcap.Req]] = collections.defaultdict(list)
    for r in pcap.iter_requests(paths, progress="session_view"):
        s = r.rec.get("sess")
        if not s:
            continue
        ua = (r.headers() or {}).get("user-agent")
        if a.client and pcap.client_family(ua) != a.client:
            continue
        by[s].append(r)
    return by


def pick(by: dict[str, list[pcap.Req]], a: argparse.Namespace) -> list[str]:
    if a.session:
        out = []
        for want in a.session:
            hits = [s for s in by if s == want or s.startswith(want)]
            if not hits:
                print(f"[session_view] 找不到会话 {want!r}", file=sys.stderr)
            out += hits
        return out
    cands = [(s, rs) for s, rs in by.items() if len(rs) >= a.min_requests]
    if a.text_only:
        cands = [(s, rs) for s, rs in cands if all(r.text_level for r in rs)]
    cands.sort(key=lambda kv: (-sum(1 for r in kv[1] if r.text_level), -len(kv[1])))
    if a.top and len(cands) > a.top:
        print(f"[session_view] 符合条件的会话共 {len(cands)} 个，仅分析含全文请求数最多的 {a.top} 个；"
              f"--top 0 是全部，--session ID 指定某几个", file=sys.stderr)
    return [s for s, _ in (cands[:a.top] if a.top else cands)]


# ---------------------------------------------------------------- 序列化
def req_dict(x: anatomy.ReqAnatomy) -> dict:
    d = x.diff
    return {
        "i": x.turn, "id": x.id, "ts": x.ts, "time": tfmt(x.ts), "gap": x.gap_s, "thread": x.thread,
        "tt": x.turn_in_thread, "ep": x.ep, "level": x.level, "stream": x.stream, "kind": d.get("kind"),
        "cause": d.get("cause"), "n_msgs": x.n_msgs, "n_tools": x.n_tools, "tools_bytes": x.tools_bytes,
        "tok": x.n_tokens, "pt": x.pt, "ct": x.ct, "prefill_ms": x.prefill_ms, "ttft_ms": x.ttft_ms,
        "e2e_ms": x.e2e_ms, "status": x.status, "finish": x.finish, "out_tok": x.out_tokens, "out_chars": x.out_chars,
        "out_reasoning": x.out_reasoning, "out_calls": x.out_tool_calls, "body_bytes": x.body_bytes,
        "by_kind": x.by_kind_tok, "by_kind_c": x.by_kind_chars,
        "prefix_msgs": d.get("prefix_msgs"), "new_msgs": d.get("new_msgs"), "prefix_chars": d.get("prefix_chars"),
        "new_chars": d.get("new_chars"), "tok_prefix": d.get("tok_prefix"), "tok_new": d.get("tok_new"),
        "template_lost": d.get("template_lost_tokens"), "diverge_msg": d.get("diverge_msg"),
        "diverge_kind": d.get("diverge_kind"), "diverge_prev_kind": d.get("diverge_prev_kind"),
        "tools_changed": d.get("tools_changed"), "tok_src": x.tok_src, "match_vllm": x.tok_match_vllm,
        "match_gold": x.tok_match_gold, "render_ms": x.render_ms, "encode_ms": x.encode_ms, "error": x.error,
        "dropped": x.dropped, "approx": x.approx, "msgs": x.msg_rows, "segs": [s.as_dict() for s in x.segs],
        # 长度超过 200 字符的「模板标记」片段：模板标记本身不会这么长，这样的片段是没能对应到任何消息的文本
        "unattributed": sum(s.c1 - s.c0 for s in x.segs if s.kind == "markup" and s.c1 - s.c0 > 200 and len(anatomy._residue(x.text[s.c0:s.c1])) > 200),  # 去掉控制符后仍超过 200 字的标记片段
        "tok_attr": x.tok_attr, "out": x.out, "out_echo": x.out_echo, "compact_from": x.compact_from,
        "compact_for": x.compact_for,
        "params": {k: v for k, v in x.params.items() if k not in ("model", "stream")},
        "text_chars": len(x.text) if x.text is not None else None,
    }


def session_dict(sid: str, xs: list[anatomy.ReqAnatomy]) -> dict:
    hd = xs[0].headers
    main = [x for x in xs if x.thread == "main"]
    toks = [x.n_tokens for x in xs if x.n_tokens]
    new = [x.diff.get("tok_new") for x in xs if x.diff.get("tok_new") is not None]
    tot_for_new = [x.n_tokens for x in xs if x.diff.get("tok_new") is not None]
    lost = sum(x.diff.get("template_lost_tokens") or 0 for x in xs)
    pt = sum(x.pt or 0 for x in xs if isinstance(x.pt, int))
    ct = sum(x.ct or 0 for x in xs if isinstance(x.ct, int))
    return {
        "id": sid, "client": pcap.client_family(hd.get("user-agent")), "ua": hd.get("user-agent"),
        "parent": hd.get("x-parent-session-id"), "model": xs[0].model, "n_req": len(xs),
        "n_text": sum(1 for x in xs if x.level == "text"), "n_main": len(main),
        "n_threads": len({x.thread for x in xs}), "first": tfmt(xs[0].ts), "last": tfmt(xs[-1].ts),
        "duration_s": round(xs[-1].ts - xs[0].ts, 3), "max_tok": max(toks) if toks else None,
        "sum_tok": sum(toks), "sum_new": sum(new) if new else None,
        "new_share": round(sum(new) / sum(tot_for_new), 4) if tot_for_new and sum(tot_for_new) else None,
        "template_lost": lost, "pt_sum": pt, "ct_sum": ct, "cache_hit": round(ct / pt, 4) if pt else None,
        "kinds": dict(collections.Counter(x.diff.get("kind") for x in xs)),
        "causes": dict(collections.Counter(x.diff.get("cause") for x in xs if x.diff.get("cause"))),
        "max_msgs": max((x.n_msgs or 0) for x in xs), "reqs": [req_dict(x) for x in xs],
        "gold": sum(1 for x in xs if x.gold_n is not None),
        "followed": sum(1 for x in xs if x.followed),
        "tok_ok": all(x.tok_match_vllm in (True, None) for x in xs),
        "n_attr": sum(1 for x in xs if x.tok_attr),  # 有 token 级归属（分词器偏移）的请求数
        "n_out": sum(1 for x in xs if x.out is not None),  # 含模型输出全文的请求数
        "errors": dict(collections.Counter(x.error[:80] for x in xs if x.error)),
    }


# ---------------------------------------------------------------- 总览（跨会话）
def fleet_stats(sessions: list[dict]) -> dict:
    """跨会话汇总。unit：有任何请求带 token 级归属时为 token，否则为 char（没有 --tokenizer 或渲染全部失败时，
    来源构成、消息大小、工具大小按字符统计；token 总数仍取 vLLM 返回值或 golden）。"""
    unit = "token" if any(r.get("tok_attr") for s in sessions for r in s["reqs"]) else "char"
    by_kind = collections.Counter()
    by_kind_c = collections.Counter()
    resend: dict[tuple, dict] = {}  # 工具结果被重发的次数：(会话, 消息哈希) → {n, size, tool, first}
    errors = collections.Counter()
    by_kind_msgs = collections.Counter()
    n_tok_total = 0
    new_sum = new_tot = 0
    lost = 0
    n_lost_req = 0
    deltas: dict[str, list[int]] = collections.defaultdict(list)
    tool_sizes: dict[str, list[int]] = collections.defaultdict(list)
    call_sizes: dict[str, list[int]] = collections.defaultdict(list)
    causes = collections.Counter()
    kinds = collections.Counter()
    pt = ct = 0
    render_ms: list[tuple[int, float]] = []
    reasoning_tok_total = 0
    reasoning_stripped_later = 0
    for s in sessions:
        seen_msg: set[tuple[str, str]] = set()
        for r in s["reqs"]:
            if r.get("error") and r["error"] != "meta_only":
                errors[r["error"]] += 1
            if r["tok"]:
                n_tok_total += r["tok"]
                for k, v in (r.get("by_kind") or {}).items():
                    by_kind[k] += v
            for k, v in (r.get("by_kind_c") or {}).items():
                by_kind_c[k] += v
            kinds[r["kind"]] += 1
            if r.get("cause"):
                causes[r["cause"]] += 1
            if r.get("tok_new") is not None and r["tok"]:
                new_sum += r["tok_new"]
                new_tot += r["tok"]
                deltas[r["kind"] or "?"].append(r["tok_new"])
            if r.get("template_lost"):
                lost += r["template_lost"]
                n_lost_req += 1
            if isinstance(r.get("pt"), int):
                pt += r["pt"]
                ct += r.get("ct") or 0
            if r.get("render_ms") is not None and r.get("n_msgs"):
                render_ms.append((r["n_msgs"], r["render_ms"]))
            for m in r.get("msgs") or []:
                key = (s["id"], m.get("h") or f"{r['id']}:{m['i']}")
                size = m["tok"] if unit == "token" else m["chars"]
                calls = m.get("calls") if unit == "token" else m.get("calls_c")
                if m["role"] == "tool" and m.get("status") != "reused":
                    tool_sizes[m.get("tool") or "?"].append(size)
                if m["role"] == "assistant" and calls and m.get("status") != "reused":
                    call_sizes[m.get("tool") or "?"].append(calls)
                if m["role"] == "tool" and m.get("h") and r["thread"] == "main":
                    e = resend.setdefault(key, {"n": 0, "size": size, "tool": m.get("tool"), "first": r["i"], "session": s["id"]})
                    e["n"] += 1
                    e["size"] = max(e["size"], size)
                if key not in seen_msg:
                    seen_msg.add(key)
                    by_kind_msgs[m["role"]] += 1
                if m["role"] == "assistant" and m.get("reasoning_dropped"):
                    reasoning_stripped_later += 1
            for m in r.get("msgs") or []:
                if m["role"] == "assistant":
                    reasoning_tok_total += m.get("reasoning") or 0

    def dist(xs):
        return {"n": len(xs), "sum": sum(xs), "p50": q(xs, .5), "p90": q(xs, .9), "max": max(xs) if xs else None}

    slope = None
    if len(render_ms) >= 4:
        xs = [x for x, _ in render_ms]
        ys = [y for _, y in render_ms]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        vx = sum((x - mx) ** 2 for x in xs)
        slope = round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx, 4) if vx else None
    # 「累计口径」的实例：被重发次数 × 大小 最大的一条工具结果
    ex = max(resend.values(), key=lambda e: e["n"] * e["size"], default=None)
    return {
        "n_sessions": len(sessions), "n_req": sum(s["n_req"] for s in sessions), "tok_total": n_tok_total,
        "unit": unit, "n_attr": sum(s.get("n_attr", 0) for s in sessions), "n_out": sum(s.get("n_out", 0) for s in sessions),
        "errors": dict(errors), "resend_example": ex,
        "by_kind": dict(by_kind) if unit == "token" else dict(by_kind_c), "by_kind_tok": dict(by_kind), "by_kind_c": dict(by_kind_c),
        "kinds": dict(kinds), "causes": dict(causes),
        "new_share": round(new_sum / new_tot, 4) if new_tot else None, "new_sum": new_sum, "new_tot": new_tot,
        "template_lost": lost, "template_lost_req": n_lost_req,
        "template_lost_share": round(lost / new_tot, 4) if new_tot else None,
        "template_lost_share_total": round(lost / n_tok_total, 4) if n_tok_total else None,
        "deltas": {k: dist(v) for k, v in deltas.items()},
        "tool_sizes": {k: dist(v) for k, v in sorted(tool_sizes.items(), key=lambda kv: -sum(kv[1]))},
        "call_sizes": {k: dist(v) for k, v in sorted(call_sizes.items(), key=lambda kv: -sum(kv[1]))},
        "pt": pt, "ct": ct, "cache_hit": round(ct / pt, 4) if pt else None,
        "render_slope_ms_per_msg": slope, "n_render": len(render_ms),
        "reasoning_tok_total": reasoning_tok_total, "reasoning_stripped_msgs": reasoning_stripped_later,
    }


# ---------------------------------------------------------------- Markdown 原始数据
def _clip(t: str, n: int) -> str:
    return t if len(t) <= n else t[:n] + f"\n…（共 {len(t)} 字符，此处截断）"


def write_md(path: str, s: dict, texts: dict[str, str], mode: str) -> None:
    L: list[str] = []
    w = L.append
    w(f"# 会话 {s['id']}\n")
    w(f"客户端 {s['client']}（{s['ua']}）；模型 {s['model']}；请求 {s['n_req']} 个（含全文 {s['n_text']} 个，含 golden {s['gold']} 个）；"
      f"时间 {s['first']} 至 {s['last']}（{s['duration_s']} s）；线程 {s['n_threads']} 条"
      + (f"；父会话 {s['parent']}" if s.get("parent") else "") + "\n")
    w("各请求依次列出：参数与 vLLM 返回值、prompt 的来源构成、与同线程上一请求的比较、消息表、模板后文本。"
      + ("模板后文本仅列出需重新处理的片段；位于公共前缀内的片段以一行说明代替。" if mode == "new" else "模板后文本完整列出。") + "\n")
    for r in s["reqs"]:
        w(f"\n## 请求 {r['i']}  {r['time']}" + (f"  (+{r['gap']} s)" if r.get("gap") is not None else "")
          + f"  线程={r['thread']} 第 {r['tt']} 步  kind={r['kind']}" + (f"  cause={r['cause']}" if r.get("cause") else "")
          + (f"  级别={r['level']}" if r["level"] != "text" else "") + (f"  错误={r['error']}" if r.get("error") else ""))
        p = r.get("params") or {}
        w(f"- 参数：ep={r['ep']} stream={r['stream']} " + " ".join(f"{k}={json.dumps(v, ensure_ascii=False)[:60]}"
                                                                  for k, v in p.items())
          + f"；tools={r['n_tools']}（{r['tools_bytes']} 字节）；请求体 {r['body_bytes']} 字节；消息 {r['n_msgs']} 条")
        w(f"- vLLM：prompt_tokens={r['pt']} cached_tokens={r['ct']} prefill={r['prefill_ms']} ms ttft={r['ttft_ms']} ms "
          f"e2e={r['e2e_ms']} ms 状态={r['status']} finish={r['finish']} 输出 {r['out_tok']} token"
          + (f"（content {r['out_chars']} 字符，reasoning {r['out_reasoning']} 字符，工具调用 {r['out_calls']} 个）"
             if r.get("out_chars") is not None else ""))
        if r.get("tok") and not r.get("tok_attr") and r.get("by_kind_c"):
            bk = r["by_kind_c"]
            w(f"- prompt 的来源构成（共 {r['tok']} token，取自 {r['tok_src']}；无分词器偏移，按字符统计）："
              + "，".join(f"{anatomy.KIND_LABEL[k]} {bk[k]} 字符" for k in anatomy.KINDS if bk.get(k)))
        elif r.get("tok"):
            bk = r["by_kind"]
            comp = "，".join(f"{anatomy.KIND_LABEL[k]} {bk[k]}" for k in anatomy.KINDS if bk.get(k))
            w(f"- prompt 的来源构成（共 {r['tok']} token，取自 {r['tok_src']}"
              + (f"，token 数与 prompt_tokens {'相等' if r['match_vllm'] else '不相等'}" if r.get("match_vllm") is not None else "")
              + (f"，token 序列与 golden {'逐个相同' if r['match_gold'] else '不同'}" if r.get("match_gold") is not None else "")
              + f"）：{comp}")
        if r["kind"] != "first" and r.get("prefix_msgs") is not None:
            line = (f"- 与上一请求相比：消息前 {r['prefix_msgs']} 条（{r['prefix_chars']} 字符）相同，新增 {r['new_msgs']} 条"
                    f"（{r['new_chars']} 字符）")
            if r.get("tok_prefix") is not None:
                prev_k = f"上一请求同一位置为 {r['diverge_prev_kind']}" if r.get("diverge_prev_kind") else "上一请求的 token 序列在此结束"
                line += (f"；token 公共前缀 {r['tok_prefix']} 个，需重新处理 {r['tok_new']} 个；分歧点位于消息 {r['diverge_msg']}"
                         f" 的 {r['diverge_kind']}（{prev_k}），分歧原因 {r['cause']}")
                if r.get("template_lost"):
                    line += f"；其中 {r['template_lost']} 个 token 所属消息内容未变，由模板改写历史造成"
            if r.get("tools_changed"):
                line += "；工具定义发生变更"
            w(line)
        if r.get("dropped"):
            w("- 客户端已发送、模板未渲染进 prompt 的片段：" + "，".join(f"消息 {d['msg']} 的 {d['kind']}（{d['chars']} 字符）" for d in r["dropped"]))
        if r.get("approx"):
            w("- 模板改写了内容、按首尾定位的片段：" + "，".join(f"消息 {d['msg']} 的 {d['kind']}（原文 {d['chars']} 字符，渲染后 {d['rendered_chars']} 字符）" for d in r["approx"]))
        if r.get("unattributed"):
            w(f"- 未能对应到任何消息的文本：{r['unattributed']} 字符（计入模板标记；模板标记片段去掉控制符后仍超过 200 字符即属此类）")
        if r.get("compact_from"):
            for cf in r["compact_from"]:
                w(f"- 历史压缩：上一主线程请求之后、本请求之前的旁路请求 {cf['req']}（{cf['n_msgs']} 条消息，prompt {cf['tok']} token"
                  + (f"，输出正文 {cf['out_chars']} 字符" if cf.get("out_chars") is not None else "，输出全文未采集") + "）；其输出正文"
                  + (f"出现在本请求的消息 {cf['msg']} 中" if cf.get("msg") is not None else "未出现在本请求的消息中"))
        if r.get("compact_for") is not None:
            w(f"- 本请求的输出正文出现在请求 {r['compact_for']}（历史压缩）的消息中")
        if r.get("out_echo"):
            oe = r["out_echo"]
            st = lambda v: "相同" if v is True else ("不同或未回传" if v is False else "输出中无此项")  # noqa: E731
            w(f"- 本请求的输出在同线程下一请求（请求 {oe['next']}"
              + (f"，消息 {oe['msg']}" if oe.get("msg") is not None else "，未找到对应的 assistant 消息") + f"）中的回传情况：正文 {st(oe['content'])}，"
              f"思考 {st(oe['reasoning'])}" + ("（客户端未回传思考）" if oe.get("reasoning_sent") is False else "") + f"，工具调用 {st(oe['tool_calls'])}")
        if r.get("msgs"):
            tok_unit = r.get("tok_attr")
            sizes = ["tok", "正文", "思考", "调用", "标记"] if tok_unit else [
                "字符", "正文(字符)", "思考(字符)", "调用(字符)", "标记(字符)"]
            columns = ["#", "role", "tool", *sizes, "状态", "预览"]
            w("\n| " + " | ".join(columns) + " |\n|" + "|".join("---" for _ in columns) + "|")
            for m in r["msgs"]:
                pv = m["preview"].replace("|", "\\|")[:90]
                sz = (m["tok"], m["content"], m["reasoning"], m["calls"], m["markup"]) if tok_unit else \
                    (m["chars"], m.get("content_c", 0), m.get("reasoning_c", 0), m.get("calls_c", 0), m.get("markup_c", 0))
                w(f"| {m['i']} | {m['role']} | {m.get('tool') or ''} | {sz[0]} | {sz[1]} | {sz[2]}"
                  f"{'（模板未渲染）' if m.get('reasoning_dropped') else ''} | {sz[3]} | {sz[4]} | "
                  f"{m.get('status') or ''} | {pv} |")
        if r.get("out"):
            o = r["out"]
            w(f"\n模型输出（D 返回给客户端的内容；正文 {len(o.get('content') or '')} 字符，思考 {len(o.get('reasoning') or '')} 字符，"
              f"工具调用 {len(o.get('tool_calls') or [])} 个）：")
            w("```text")
            if o.get("reasoning"):
                w("⟦reasoning⟧" + _clip(o["reasoning"], 3000))
            if o.get("content"):
                w("⟦content⟧" + _clip(o["content"], 3000))
            for c in o.get("tool_calls") or []:
                w(f"⟦tool_call {c.get('name')}⟧" + _clip(str(c.get("arguments") or ""), 1500))
            w("```")
        if r.get("segs") and mode != "none":
            w("\n```text")
            prefix_ok = r.get("tok_prefix") if r.get("tok_prefix") is not None else None
            skipped = 0
            for sg in r["segs"]:
                reused = prefix_ok is not None and sg["t1"] <= prefix_ok and sg["t1"] > sg["t0"]
                if mode == "new" and reused and sg["k"] not in ("gen_prompt",):
                    skipped += sg["t1"] - sg["t0"]
                    continue
                if skipped:
                    w(f"⟦与上一请求的公共前缀 {skipped} 个 token，未列出⟧")
                    skipped = 0
                tag = f"⟦{sg['k']}" + (f" msg{sg['m']}" if sg.get("m") is not None else "") \
                    + (f" {sg['tool']}" if sg.get("tool") else "") + f" {sg['t1'] - sg['t0']} tok⟧"
                w(tag + texts.get(sg["b"], "⟨原文未嵌入⟩"))
            if skipped:
                w(f"⟦与上一请求的公共前缀 {skipped} 个 token，未列出⟧")
            w("```")
    with private_open(path) as f:
        f.write("\n".join(L) + "\n")


# ---------------------------------------------------------------- HTML
STATUS = ["reused", "new", "stripped", "shifted"]


def pack_for_html(sd: dict) -> dict:
    """HTML 里嵌入的会话数据。每个请求都带着全部历史，所以消息行与片段在相邻请求之间几乎全部重复，原样嵌入时
    大小随步数的平方增长（180 步的会话约 10 MB）。这里按内容去重成两张表（mtab、stab），请求里只存下标；
    片段的字符区间、token 区间是首尾相接的，只存长度，由页面累加还原（session_view.js 的 unpack）。
    json、csv、md 输出不受影响。"""
    mtab: list[dict] = []
    stab: list[list] = []
    mkey: dict[str, int] = {}
    skey: dict[tuple, int] = {}
    reqs = []
    for r in sd["reqs"]:
        q = {k: v for k, v in r.items() if k not in ("msgs", "segs")}
        mp: list[int] = []
        for n, m in enumerate(r.get("msgs") or []):
            if m["i"] != n:  # 下标与位置不一致：不压缩这个请求
                mp = None
                break
            row = {k: v for k, v in m.items() if k not in ("i", "status")}
            key = json.dumps(row, sort_keys=True, ensure_ascii=False)
            j = mkey.get(key)
            if j is None:
                j = mkey[key] = len(mtab)
                mtab.append(row)
            mp += [j, STATUS.index(m["status"]) if m.get("status") in STATUS else -1]
        if mp is None:
            q["msgs"] = r.get("msgs")
        else:
            q["mp"] = mp
        sp: list[int] | None = []
        c = t = 0
        for sg in r.get("segs") or []:
            if sg["c0"] != c or sg["t0"] != t:  # 片段不是首尾相接：不压缩这个请求
                sp = None
                break
            k = (sg["k"], sg["m"], sg["tool"], sg["b"], sg["c1"] - sg["c0"], sg["t1"] - sg["t0"])
            j = skey.get(k)
            if j is None:
                j = skey[k] = len(stab)
                stab.append(list(k))
            sp.append(j)
            c, t = sg["c1"], sg["t1"]
        if sp is None:
            q["segs"] = r.get("segs")
        else:
            q["sp"] = sp
        reqs.append(q)
    return {**{k: v for k, v in sd.items() if k != "reqs"}, "reqs": reqs, "mtab": mtab, "stab": stab}


def write_html(path: str, payload: dict) -> None:
    with open(os.path.join(HERE, "session_view.js"), encoding="utf-8") as f:
        js = f.read()
    css = ""
    for name in ("report_base.css", "session_view.css"):  # 共用版式 + 本页专有样式
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            css += f.read() + "\n"
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    doc = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>会话分析报告</title><style>{css}</style></head><body><div id="app"></div>
<script id="data" type="application/json">{data}</script>
<script>{js}</script></body></html>"""
    with private_open(path) as f:
        f.write(doc)


# ---------------------------------------------------------------- 并行（--jobs）
_ANAT: anatomy.Anatomist | None = None


def make_anatomist(tokenizer: str | None, content_format: str, template_kwargs: str | None,
                   trust_remote_code: bool) -> None:
    global _ANAT
    renderer = None
    if tokenizer:
        renderer = pcap.Renderer(tokenizer, content_format, json.loads(template_kwargs) if template_kwargs else None,
                                 trust_remote_code=trust_remote_code)
    _ANAT = anatomy.Anatomist(renderer)


def compute_one(req: pcap.Req) -> anatomy.ReqAnatomy:
    return _ANAT.compute(req)


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="按会话解剖采集到的请求：组成、多轮变化、原文")
    ap.add_argument("data", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tokenizer", help="模型目录；没有就只有字符级数字")
    ap.add_argument("--trust-remote-code", action="store_true")
    ap.add_argument("--content-format", default="auto", choices=["auto", "string", "openai"])
    ap.add_argument("--template-kwargs")
    ap.add_argument("--session", nargs="*", help="会话 ID（可用前缀）；不给则按 --top 自动挑")
    ap.add_argument("--top", type=int, default=8, help="自动挑选：全文请求最多的前 N 个会话；0 = 全部会话")
    ap.add_argument("--min-requests", type=int, default=2)
    ap.add_argument("--client", help="只看某个客户端家族，如 opencode")
    ap.add_argument("--text-only", action="store_true", help="只挑全部请求都有全文的会话")
    ap.add_argument("--md", default="new", choices=["new", "full", "none"], help="md 里模板后文本的打印方式")
    ap.add_argument("--embed-mb", type=float, default=40.0, help="HTML 里最多嵌入多少 MB 原文（超出的会话只保留数字）")
    ap.add_argument("--no-html", action="store_true")
    ap.add_argument("--include-text", action="store_true", help="明确授权后才在报告中包含原文、输出和参数")
    ap.add_argument("--jobs", type=int, default=1,
                    help="并行进程数：每条请求的渲染、分词、片段归属放到子进程，结果与单进程相同（耗时列除外）")
    a = ap.parse_args()
    prepare_output_dir(a.out)
    t0 = time.time()
    by = collect_sessions(a.data, a)
    chosen = pick(by, a)
    if not chosen:
        print("[session_view] 没有可分析的会话", file=sys.stderr)
        return 1
    workers = pcap.Workers(a.jobs, make_anatomist, (a.tokenizer, a.content_format, a.template_kwargs,
                                                    a.trust_remote_code), tag="session_view")
    anat = _ANAT if a.jobs <= 1 else anatomy.Anatomist(None)  # 主进程只做与上一请求的比较，不需要分词器
    pg = pcap.Progress("session_view", sum(len(by[s]) for s in chosen))

    def computed(reqs: list[pcap.Req]):
        jobs = (pcap.detach(r) for r in anatomy.in_order(reqs)) if a.jobs > 1 else anatomy.in_order(reqs)
        for x in workers.imap(compute_one, jobs):
            yield x
            pg.step()

    sessions: list[dict] = []
    texts: dict[str, str] = {}
    embedded = 0
    limit = int(a.embed_mb * 2**20)
    req_rows: list[dict] = []
    msg_rows: list[dict] = []
    for n, sid in enumerate(chosen, 1):
        reqs = by[sid]
        print(f"[session_view] {n}/{len(chosen)} {sid} ({len(reqs)} 请求)", file=sys.stderr)
        xs = anatomy.assemble_session(reqs, anat, computed(reqs))
        sd = session_dict(sid, xs)
        if not a.include_text:
            sd["ua"] = None
            for row in sd["reqs"]:
                row["params"] = {}
                row["out"] = row["out_echo"] = None
                for message in row["msgs"]:
                    message["preview"] = ""
        local: dict[str, str] = {}
        for x in xs:
            if x.text is None or not a.include_text:
                continue
            for sg in x.segs:
                if sg.blob not in local and sg.blob not in texts:
                    local[sg.blob] = x.text[sg.c0:sg.c1]
        size = sum(len(v.encode("utf-8", "surrogatepass")) for v in local.values())
        sd["text_embedded"] = embedded + size <= limit
        if sd["text_embedded"]:
            texts.update(local)
            embedded += size
        sessions.append(sd)
        file_id = hashlib.sha256(sid.encode("utf-8", "surrogatepass")).hexdigest()[:24]
        write_md(os.path.join(a.out, f"session_{file_id}.md"), sd, {**texts, **local}, a.md if a.include_text else "none")
        with private_open(os.path.join(a.out, f"session_{file_id}.json")) as f:
            json.dump({"session": {k: v for k, v in sd.items() if k != "reqs"}, "requests": sd["reqs"]}, f,
                      ensure_ascii=False)
        for r in sd["reqs"]:
            row = {k: v for k, v in r.items() if k not in ("msgs", "segs", "by_kind", "by_kind_c", "params", "dropped",
                                                            "approx", "out", "out_echo", "compact_from")}
            if r.get("out_echo"):
                for k in ("content", "reasoning", "tool_calls"):
                    row["echo_" + k] = r["out_echo"].get(k)
            row["session"] = sid
            for k in anatomy.KINDS:
                row["k_" + k] = (r.get("by_kind") or {}).get(k)  # 各来源的 token 数
            req_rows.append(row)
            for m in r.get("msgs") or []:
                msg_rows.append({"session": sid, "req_i": r["i"], "req_id": r["id"], **{k: v for k, v in m.items()
                                                                                        if k != "preview"},
                                 "preview": m["preview"][:80]})
    workers.close()
    fleet = fleet_stats(sessions)
    req_cols = ["session", "i", "id", "time", "gap", "thread", "tt", "ep", "level", "stream", "kind", "cause", "n_msgs",
                "n_tools", "tools_bytes", "body_bytes", "text_chars", "tok", "pt", "ct", "tok_prefix", "tok_new",
                "template_lost", "diverge_msg", "diverge_kind", "diverge_prev_kind", "prefix_msgs", "new_msgs",
                "prefix_chars", "new_chars", *["k_" + k for k in anatomy.KINDS], "prefill_ms", "ttft_ms", "e2e_ms",
                "status", "finish", "out_tok", "out_chars", "out_reasoning", "out_calls", "echo_content", "echo_reasoning",
                "echo_tool_calls", "compact_for", "tok_src", "tok_attr", "match_vllm", "match_gold", "render_ms", "encode_ms",
                "tools_changed", "unattributed", "error"]
    with private_open(os.path.join(a.out, "session_requests.csv"), newline="") as f:
        w = csv.DictWriter(f, req_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(req_rows)
    msg_cols = ["session", "req_i", "req_id", "i", "role", "tool", "h", "status", "chars", "src_chars", "tok", "content",
                "reasoning", "calls", "markup", "content_c", "reasoning_c", "calls_c", "markup_c", "reasoning_dropped", "preview"]
    with private_open(os.path.join(a.out, "session_messages.csv"), newline="") as f:
        w = csv.DictWriter(f, msg_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(msg_rows)
    with private_open(os.path.join(a.out, "fleet.json")) as f:
        json.dump(fleet, f, ensure_ascii=False, indent=1)
    if not a.no_html:
        payload = {"meta": {"generated": tfmt(time.time()), "data": [os.path.abspath(p) for p in a.data],
                            "tokenizer": a.tokenizer, "sessions_total": len(by), "chosen": len(chosen),
                            "embedded_mb": round(embedded / 2**20, 2)},
                   "kinds": anatomy.KINDS, "kind_label": anatomy.KIND_LABEL,
                   "sessions": [pack_for_html(sd) for sd in sessions], "texts": texts,
                   "fleet": fleet}
        write_html(os.path.join(a.out, "sessions.html"), payload)
    print(f"[session_view] {len(sessions)} 会话，{sum(s['n_req'] for s in sessions)} 请求，嵌入原文 {embedded / 2**20:.1f} MB，"
          f"{time.time() - t0:.1f}s -> {a.out}", file=sys.stderr)
    print_fleet(fleet)
    return 0


def print_fleet(f: dict) -> None:
    print(f"\n总览：{f['n_sessions']} 个会话，{f['n_req']} 个请求，token 合计 {f['tok_total']:,}")
    if f.get("errors"):
        print("   [!] 渲染或分词失败的请求：" + "；".join(f"{k}（{v} 个）" for k, v in f["errors"].items()))
    if f["unit"] == "char":
        print("   [!] 没有 token 级归属：未提供 --tokenizer，或全部请求的渲染/分词失败。来源构成、消息大小、工具大小按字符统计；"
              "重新运行时加 --tokenizer <模型目录> 得到 token 级结果")
    unit = "token" if f["unit"] == "token" else "字符"
    tot = (f["tok_total"] if f["unit"] == "token" else sum(f["by_kind"].values())) or 1
    for k in anatomy.KINDS:
        v = f["by_kind"].get(k, 0)
        if v:
            print(f"   {anatomy.KIND_LABEL[k]:<12} {v:>12,} {unit}  {v / tot:6.1%}")
    if f.get("new_share") is not None:
        print(f"   有同线程上一请求且 token 可比较的请求中，需重新处理的 token 占比 {f['new_share']:.1%}；"
              f"该范围内按消息缓存分词结果可减少的分词量上限 {1 - f['new_share']:.1%}")
    if f.get("template_lost"):
        total_share = f"{f['template_lost_share_total']:.1%}" if f.get("template_lost_share_total") is not None else "未测"
        print(f"   模板改写历史造成的需重新处理 token：{f['template_lost']:,}（占有计数的全部 token 的 {total_share}，"
              f"占需重新处理 token 的 {f['template_lost'] / (f['new_sum'] or 1):.1%}；{f['template_lost_req']} 个请求）")
    if f.get("cache_hit") is not None:
        print(f"   vLLM 前缀缓存命中率 {f['cache_hit']:.1%}")
    for k, d in f["deltas"].items():
        print(f"   单个请求需重新处理的 token 数 [{k}] n={d['n']} p50={d['p50']:.0f} p90={d['p90']:.0f} max={d['max']}")
    for k, d in list(f["tool_sizes"].items())[:12]:
        print(f"   工具结果 [{k}] n={d['n']} sum={d['sum']:,} p50={d['p50']:.0f} p90={d['p90']:.0f} max={d['max']} ({unit})")


if __name__ == "__main__":
    sys.exit(main())
