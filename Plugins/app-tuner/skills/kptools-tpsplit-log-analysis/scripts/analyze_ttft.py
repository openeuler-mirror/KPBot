#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_ttft.py — TTFT breakdown 日志解析出表（v3 口径）

输入 P/D/proxy/压测端四类日志，解析为逐请求 TTFT 拆解，回填分析层真实值，
输出 Excel（8 Sheet，全部毫秒）+ 终端文字结论。

用法（单 P）：
  python analyze_ttft.py "P:prefill.log" -o TTFT.xlsx --one-burst \
      --decode "D:decode.log" --kvxfer "P:prefill.log" \
      --lb "PROXY:load_balance.log" \
      --detailed "BENCH:manual_xxx_dumpjsonl.json"

多 P 合并（--merge-p 把多台 P 的日志合成一套完整请求，明细新增 IP 列）：
  python analyze_ttft.py "102:P102_prefill.log" "103:P103_prefill.log" -o TTFT_full.xlsx \
      --merge-p --one-burst --decode "D:D103_decode.log" \
      --kvxfer "102:P102_prefill.log" --kvxfer "103:P103_prefill.log" \
      --lb "PROXY:load_balance.log" --detailed "BENCH:bench.json"

约定：
  * 输入形如 "LABEL:path"。LABEL 是机器标识（P 节点常用 IP，如 102/103；
    D/PROXY/BENCH 为角色名）。不带 LABEL 时 P 日志用文件名做机器标识。
  * 解析全部按键 `(?:^|\\s)key=value`，与字段顺序无关；旧日志缺列按 0。
  * 主守恒式：ttft = tokenize_render + dispatch + add_req + mm_hash + queue_wait
    + wait_exec + load_kv + forward + mtp + sample + save_kv + step_other
    + out_delivery + residual，恒等式 conserv_err = Σ主列 - ttft >= 0。
"""

import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 openpyxl（xlsx 输出必需）：python -m pip install openpyxl")

# ---------------------------------------------------------------- 常量与口径

# 13 个主列分量（residual 单独）
MAIN_COMPS = [
    "tokenize_render", "dispatch", "add_req", "mm_hash", "queue_wait",
    "wait_exec", "load_kv", "forward", "mtp", "sample", "save_kv",
    "step_other", "out_delivery",
]
RESIDUAL = "residual"

# INFO 列（日志直读，不参与守恒式）
INFO_COMPS = [
    "lb_dispatch", "hash", "ext_lookup", "alloc", "overhead",
    "sched_total", "update", "emit", "step_exec",
]
DEQ_SUBS = ["main", "draft", "take_draft", "aux"]

SHEET_ORDER = [
    "总览", "Burst各环节", "逐请求明细", "机器对比delta",
    "D节点Decode", "P节点KV传输", "LB分发", "回传对账",
]

# ttft_table.csv 契约（references/ttft_table_contract.md，仓内权威定义；
# 下游消费方（TP 拆分收益对比等）暂在仓外）：
# ttft = net_lb + fe_inputq + fe_addreq + sched_qwait + sched_total + fwd_pure
#      + draft + kv_xfer + shm_dq + other
CSV_HEADER = ["ts", "request_id", "input_len", "ttft_ms", "net_lb_ms",
              "fe_inputq_ms", "fe_addreq_ms", "sched_qwait_ms", "sched_total_ms",
              "fwd_pure_ms", "draft_ms", "kv_xfer_ms", "shm_dq_ms", "other_ms"]

KV_RE = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")


def parse_kv(line):
    return dict(KV_RE.findall(line))


def fnum(v, default=0.0):
    """容错的浮点解析：None/坏值/带 ms 后缀都处理，缺列按 0。"""
    if v is None:
        return default
    s = str(v).strip().lower()
    if s.endswith("ms"):
        s = s[:-2]
    try:
        f = float(s)
        return f if math.isfinite(f) else default
    except ValueError:
        return default


def split_label(spec, default_label=None):
    """'LABEL:path' 拆分。Windows 盘符启发式仅在本机为 Windows 时启用：
    'C:\\x' / 'C:/x'（单字母后跟 / 或 \\）视为路径。Linux/macOS 下
    'D:/abs/path' 一律拆成 LABEL='D' + POSIX 路径——否则文档化用法在
    Linux 直接 FileNotFoundError（检视意见 huangsibeier）。
    """
    spec = spec.strip().strip('"')
    if ":" in spec:
        head, rest = spec.split(":", 1)
        drive_like = len(head) == 1 and rest[:1] in ("/", "\\")
        if head and (os.name != "nt" or not drive_like):
            return head, os.path.normpath(rest)
    return default_label or os.path.splitext(os.path.basename(spec))[0], os.path.normpath(spec)


def stats(values):
    """均值 / p50 / p90 / p99 / max / min。空集返回全 0。"""
    if not values:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p90": 0.0, "p99": 0.0,
                "max": 0.0, "min": 0.0}
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p90": 0.0, "p99": 0.0,
                "max": 0.0, "min": 0.0}

    def pct(p):
        k = max(0, min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1)))))
        return xs[k]

    return {"n": len(xs), "mean": sum(xs) / len(xs), "p50": pct(50),
            "p90": pct(90), "p99": pct(99), "max": xs[-1], "min": xs[0]}


# ---------------------------------------------------------------- 日志解析

def load_breakdown(path, machine):
    """P 节点 [TTFT][BREAKDOWN] 行 → 逐请求 dict 列表。"""
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "[TTFT][BREAKDOWN]" not in line:
                continue
            kv = parse_kv(line)
            if "req" not in kv:
                continue
            row = {
                "machine": machine, "ip": None, "req": kv["req"],
                "batch": kv.get("batch", ""),
                "ts": None, "burst": None,
                "ttft": fnum(kv.get("ttft")),
                # input_len 缺失（打点行未携带）保持 None → CSV 输出空单元格；
                # 有值但坏仍按 0 容错（与既有 fnum 口径一致）
                "_input_len": (None if kv.get("input_len") is None
                                else fnum(kv.get("input_len"))),
            }
            for c in MAIN_COMPS + [RESIDUAL] + INFO_COMPS:
                row[c] = fnum(kv.get(c))
            # 日志原值固化（回填增量 = 回填值 - 原值，由 finalize 的 step_other 吸收）
            for c in ("load_kv", "mtp", "save_kv"):
                row["_orig_" + c] = row[c]
            for c in ("dequeue", "return", "dequeue_main", "dequeue_draft",
                      "dequeue_take_draft", "dequeue_aux",
                      "loadkv_real", "mtp_real", "savekv_real"):
                row[c] = None
            for c in ("preempt", "fwd_tokens", "fwd_steps"):
                row[c] = fnum(kv.get(c))
            for c in ("add_req_iq", "add_req_proc"):
                row[c] = fnum(kv.get(c))
            row["fwd_ms_per_kT"] = 0.0
            row["conserv_err"] = 0.0
            row["lb_src"] = "engine"
            row["cold_step"] = 0
            for tkey in ("ts", "t", "arrival", "mono"):
                if kv.get(tkey) is not None:
                    ts = fnum(kv.get(tkey))
                    # 毫秒 epoch 自适应（>1e11 不可能是秒）：统一归一到秒，
                    # 否则 --burst-gap（秒）与 CSV ts 格式语义错 1000 倍
                    if ts > 1e11:
                        ts /= 1000.0
                    row["ts"] = ts
                    break
            rows.append(row)
    return rows


def load_aux(path, machine):
    """从一份日志里解析全部辅助行：kvxfer put / pool_worker load / shm / draft。"""
    puts, pool_loads, shm_batches, shm_agg, drafts = [], [], [], [], []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "[TTFT_KVXFER][put]" in line:
                kv = parse_kv(line)
                if "req" in kv:
                    puts.append({"machine": machine, "req": kv["req"],
                                 "total": fnum(kv.get("total")),
                                 "build_key": fnum(kv.get("build_key")),
                                 "lookup": fnum(kv.get("lookup")),
                                 "prepare": fnum(kv.get("prepare")),
                                 "sync": fnum(kv.get("sync")),
                                 "put": fnum(kv.get("put")),
                                 "finalize": fnum(kv.get("finalize"))})
            elif "start_load_kv" in line and "cost" in line:
                kv = parse_kv(line)
                m = re.search(r"cost\s+([0-9.]+)", line)
                cost = fnum(m.group(1)) if m else fnum(kv.get("cost"))
                tp = kv.get("tp", "-1")
                m2 = re.match(r"(\d+)", str(tp))
                pool_loads.append({
                    "machine": machine, "batch": kv.get("batch", ""),
                    "reqs": fnum(kv.get("reqs")),
                    "tp": int(m2.group(1)) if m2 else -1, "cost_ms": cost})
            elif "[SHM_DEQUEUE_BATCH]" in line:
                kv = parse_kv(line)
                btag = kv.get("batch", "")
                batch, step = btag, 1
                if "-" in btag:
                    b0, s0 = btag.rsplit("-", 1)
                    if s0.isdigit():
                        batch, step = b0, int(s0)
                tp = kv.get("tp", "-1")
                m2 = re.match(r"(\d+)", str(tp))
                shm_batches.append({
                    "machine": machine, "batch": batch, "step": step,
                    "tp": int(m2.group(1)) if m2 else -1,
                    "wait": fnum(kv.get("wait")), "read": fnum(kv.get("read")),
                    "nreq": fnum(kv.get("nreq")), "sub": kv.get("sub", "main")})
            elif "[SHM_DEQUEUE]" in line:
                kv = parse_kv(line)
                shm_agg.append({"machine": machine, "t": fnum(kv.get("t")),
                                "wait": fnum(kv.get("wait")),
                                "read": fnum(kv.get("read")),
                                "total": fnum(kv.get("total")),
                                "count": fnum(kv.get("count")),
                                "avg": fnum(kv.get("avg"))})
            elif "[TTFT_DRAFT]" in line:
                kv = parse_kv(line)
                tp = kv.get("tp", "-1")
                m2 = re.match(r"(\d+)", str(tp))
                drafts.append({"machine": machine, "batch": kv.get("batch", ""),
                               "draft_wall": fnum(kv.get("draft_wall")),
                               "reqs": fnum(kv.get("reqs")),
                               "tp": int(m2.group(1)) if m2 else -1})
    return {"puts": puts, "pool_loads": pool_loads, "shm_batches": shm_batches,
            "shm_agg": shm_agg, "drafts": drafts}


def load_decode(path, machine):
    """D 节点 [TTFT_DECODE] 行。"""
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if "[TTFT_DECODE]" not in line:
                continue
            kv = parse_kv(line)
            rows.append({
                "machine": machine, "idx": len(rows) + 1,
                "ts": fnum(kv.get("ts", kv.get("t", 0.0))),
                "decode_forward": fnum(kv.get("decode_forward")),
                "load": fnum(kv.get("load")), "save": fnum(kv.get("save")),
                "sample": fnum(kv.get("sample")),
                "num_total": fnum(kv.get("num_total")),
                "num_gen": fnum(kv.get("num_gen")),
                "sched_total": fnum(kv.get("sched_total")),
                "decode_only": fnum(kv.get("decode_only"))})
    return rows


def load_lb(path, machine):
    """proxy [LB_DISPATCH] 行。"""
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "[LB_DISPATCH]" not in line:
                continue
            kv = parse_kv(line)
            if "req" not in kv:
                continue
            rows.append({
                "machine": machine, "req": kv["req"],
                "wall_recv": fnum(kv.get("wall_recv")),
                "client_send": fnum(kv.get("client_send")),
                "client2proxy_ms": fnum(kv.get("client2proxy_ms")),
                "dispatch_ms": fnum(kv.get("dispatch_ms")),
                "wait_p_ms": fnum(kv.get("wait_p_ms")),
                "first_token_ms": fnum(kv.get("first_token_ms")),
                "prefiller": kv.get("prefiller", ""),
                "decoder": kv.get("decoder", "")})
    return rows


def load_bench(path, machine):
    """压测端 --save-detailed 结果（jsonl 或 JSON 数组）。

    每请求需要 client_send_ts 与首 token 墙钟（first_token_ts / client_ttft）。
    解析失败显式报错（含文件与行号），不静默吞掉坏行。
    """
    records = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read().strip()
    if not text:
        return records
    items = []
    if text.startswith("["):
        try:
            items = json.loads(text)
        except json.JSONDecodeError as e:
            sys.exit(f"[错误] 压测结果 JSON 数组解析失败：{path}（{e}）")
    else:
        for i, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as e:
                sys.exit(f"[错误] 压测结果 jsonl 第 {i} 行解析失败：{path}"
                         f"（{e}；行内容前 80 字符：{line[:80]}）")
    for it in items:
        if not isinstance(it, dict):
            continue
        req = it.get("req", it.get("request_id", it.get("rid", "")))
        send = fnum(it.get("client_send_ts"))
        ftts = it.get("first_token_ts")
        cttft = it.get("client_ttft", it.get("ttft_ms"))
        if cttft is None and ftts is not None:
            cttft = fnum(ftts) - send
        records.append({"machine": machine, "req": str(req),
                        "client_send_ts": send,
                        "first_token_ts": fnum(ftts) if ftts is not None else None,
                        "client_ttft": fnum(cttft) if cttft is not None else None})
    return records


# ---------------------------------------------------------------- Burst 分段

def assign_bursts(rows, one_burst, burst_gap_s):
    """按时间戳分段；无 ts 或 --one-burst 时整段一个 burst。

    混合输入（部分行有 ts、部分缺）：缺 ts 的行归入最后一个 burst，
    并在返回的说明里注明归组方式，避免 None 参与数值运算。
    """
    if not rows:
        return 0, "no rows"
    have_ts = [r for r in rows if r["ts"] is not None]
    no_ts = [r for r in rows if r["ts"] is None]
    if one_burst or not have_ts:
        for r in rows:
            r["burst"] = 1
        return 1, ("one-burst" if one_burst else "no-ts (fallback one-burst)")
    have_ts.sort(key=lambda r: r["ts"])
    burst, prev = 1, None
    for r in have_ts:
        if prev is not None and r["ts"] - prev > burst_gap_s:
            burst += 1
        r["burst"] = burst
        prev = r["ts"]
    note = f"burst-gap {burst_gap_s}s"
    if no_ts:
        for r in no_ts:
            r["burst"] = burst
        note += f"；{len(no_ts)} 条缺时间戳请求归入最后一个 burst"
    return burst, note


def mark_cold_steps(rows):
    """cold_step=1：add_req<1ms 且 forward>2×中位数（首步冷启动）。"""
    if not rows:
        return
    med = stats([r["forward"] for r in rows])["p50"]
    for r in rows:
        r["cold_step"] = 1 if (r["add_req"] < 1.0 and r["forward"] > 2 * med) else 0


# ---------------------------------------------------------------- 回填层

def _keyed_max(items, keyfields, valfield):
    """按 keyfields 分组取 valfield 最大值 → {(keys...): max}。"""
    out = defaultdict(float)
    for it in items:
        k = tuple(it[f] for f in keyfields)
        if it[valfield] > out[k]:
            out[k] = it[valfield]
    return out


def _requests_by_batch(rows):
    byb = defaultdict(list)
    for r in rows:
        if r.get("batch"):
            byb[(r["machine"], r["batch"])].append(r)
    return byb


def _apply_real_loadkv(rows, pool_loads, report):
    """按 batch 取全 TP 最大 cost 覆盖 load_kv（MLA 下 KV 跨 TP 复制，各 rank 加载完整批）。"""
    if not pool_loads:
        return
    bmax = _keyed_max(pool_loads, ("machine", "batch"), "cost_ms")
    byb = _requests_by_batch(rows)
    n = 0
    for (machine, batch), mx in bmax.items():
        for r in byb.get((machine, batch), []):
            r["loadkv_real"] = mx
            r["load_kv"] = mx
            n += 1
    report["loadkv_backfilled"] = n


def _apply_draft_mtp(rows, drafts, report):
    """[TTFT_DRAFT] 批内 TP-max → mtp。"""
    if not drafts:
        return
    bmax = _keyed_max(drafts, ("machine", "batch"), "draft_wall")
    byb = _requests_by_batch(rows)
    n = 0
    for (machine, batch), mx in bmax.items():
        for r in byb.get((machine, batch), []):
            r["mtp_real"] = mx
            r["mtp"] = mx
            n += 1
    report["mtp_backfilled"] = n


def _apply_real_savekv(rows, puts, report):
    """[TTFT_KVXFER][put] total 同 req 全 TP max → save_kv。"""
    if not puts:
        return
    rmax = _keyed_max(puts, ("machine", "req"), "total")
    idx = {(r["machine"], r["req"]): r for r in rows}
    n = 0
    for (machine, req), mx in rmax.items():
        r = idx.get((machine, req))
        if r is not None:
            r["savekv_real"] = mx
            r["save_kv"] = mx
            n += 1
    report["savekv_backfilled"] = n


def _apply_real_dequeue(rows, shm_batches, report):
    """[SHM_DEQUEUE_BATCH] 批内全 TP wait max，跨步累加（fwd_steps 步内）；
    截断到 step_other，子拆分按比例缩放。"""
    if not shm_batches:
        return
    # (machine, batch, step, sub) → TP-max wait
    smax = _keyed_max(shm_batches, ("machine", "batch", "step", "sub"), "wait")
    # (machine, batch) → {step: {sub: wait}}
    agg = defaultdict(lambda: defaultdict(dict))
    for (machine, batch, step, sub), w in smax.items():
        agg[(machine, batch)][step][sub] = w
    byb = _requests_by_batch(rows)
    n_cov = 0
    for (machine, batch), steps in agg.items():
        targets = byb.get((machine, batch), [])
        if not targets:
            continue
        for r in targets:
            nsteps = max(1, int(r.get("fwd_steps") or 1))
            total, subs = 0.0, defaultdict(float)
            for step in sorted(steps)[:nsteps]:
                for sub, w in steps[step].items():
                    total += w
                    subs[sub] += w
            step_other_orig = r["step_other"]
            if total > step_other_orig:
                scale = step_other_orig / total if total > 0 else 0.0
                total = step_other_orig
                for k in list(subs):
                    subs[k] *= scale
            r["dequeue"] = total
            for sub in DEQ_SUBS:
                r["dequeue_" + sub] = subs.get(sub, 0.0)
            r["dequeue_raw"] = total
            n_cov += 1
    report["dequeue_covered"] = n_cov


def _finalize_step_other(rows):
    """信封重推导：step_other 吸收回填增量 → 拆 dequeue/return → 重算 residual。

    正常路径下 step_other_new = step_other_log - Σ回填增量（clamp ≥ 0），
    主列加总不变；增量超出 step_other 时下限截断，溢出部分记入 conserv_err。
    回填增量 = 回填值 - 日志原值（原值在解析时已固化为 _orig_*）。
    """
    for r in rows:
        delta = 0.0
        for col, real in (("load_kv", "loadkv_real"), ("mtp", "mtp_real"),
                          ("save_kv", "savekv_real")):
            if r.get(real) is not None:
                delta += r[real] - r["_orig_" + col]
        if "_step_other_log" not in r:
            r["_step_other_log"] = r["step_other"]
        step_new = max(0.0, r["_step_other_log"] - delta)
        # dequeue 截断到新信封，子拆分按比例缩放；return = step_other - dequeue
        deq = r.get("dequeue") or 0.0
        if deq > step_new:
            scale = step_new / deq if deq > 0 else 0.0
            for sub in DEQ_SUBS:
                r["dequeue_" + sub] = (r.get("dequeue_" + sub) or 0.0) * scale
            deq = step_new
        r["dequeue"] = deq
        for sub in DEQ_SUBS:
            if r.get("dequeue_" + sub) is None:
                r["dequeue_" + sub] = 0.0
        r["return"] = max(0.0, step_new - deq)
        r["step_other"] = step_new
        # 重算 residual / conserv_err（守恒式）
        s13 = sum(r[c] for c in MAIN_COMPS)
        r[RESIDUAL] = max(0.0, r["ttft"] - s13)
        r["conserv_err"] = s13 + r[RESIDUAL] - r["ttft"]
        # 派生列
        r["fwd_ms_per_kT"] = (r["forward"] * 1000.0 / r["fwd_tokens"]
                              if r["fwd_tokens"] > 0 else 0.0)


def apply_lb_backfill(rows, lb_rows, report):
    """--lb 提供时用 proxy 单机口径回填 lb_dispatch = client2proxy_ms + dispatch_ms。"""
    if not lb_rows:
        return
    idx = defaultdict(list)
    for l in lb_rows:
        idx[l["req"]].append(l)
    n = 0
    for r in rows:
        cands = idx.get(r["req"])
        if cands:
            l = cands[0]
            r["lb_dispatch"] = l["client2proxy_ms"] + l["dispatch_ms"]
            r["lb_src"] = "proxy"
            n += 1
    report["lb_backfilled"] = n


# ---------------------------------------------------------------- Excel 输出

HEADER_FILL = PatternFill(start_color="DDE6F0", end_color="DDE6F0", fill_type="solid")


def dump_sheet(ws, columns, rows_dicts, num_fmt="0.000", cap=42):
    """把 list[dict] 写入 sheet；columns = [(key, header)]。"""
    ws.append([h for _, h in columns])
    for c in range(1, len(columns) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")
    for r in rows_dicts:
        ws.append([r.get(k, "") for k, _ in columns])
    for ri in range(2, len(rows_dicts) + 2):
        for ci, (k, _) in enumerate(columns, start=1):
            v = ws.cell(row=ri, column=ci).value
            if isinstance(v, float):
                ws.cell(row=ri, column=ci).number_format = num_fmt
    for ci, (k, h) in enumerate(columns, start=1):
        w = len(str(h)) + 2
        for r in rows_dicts[:200]:
            v = r.get(k, "")
            if v is not None:
                w = max(w, min(cap, len(str(v)) + 2))
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.freeze_panes = "A2"


def kv_block(ws, title, pairs, start=1):
    """Overview 用的 key-value 块。"""
    ws.cell(row=start, column=1, value=title).font = Font(bold=True, size=12)
    r = start + 1
    for k, v in pairs:
        ws.cell(row=r, column=1, value=k).font = Font(bold=True)
        c = ws.cell(row=r, column=2, value=v)
        if isinstance(v, float):
            c.number_format = "0.000"
        r += 1
    return r + 1


def build_detail_columns(merge_p):
    ident = [("machine", "机器")]
    if merge_p:
        ident.append(("ip", "IP"))
    ident += [("burst", "Burst"), ("req", "req"), ("batch", "batch"),
              ("ttft", "ttft"), ("cold_step", "cold_step")]
    cols = ident
    cols += [(c, c) for c in MAIN_COMPS] + [(RESIDUAL, RESIDUAL)]
    cols += [(c, c) for c in INFO_COMPS]
    cols += [("dequeue", "dequeue")] + [("dequeue_" + s, "dequeue_" + s) for s in DEQ_SUBS]
    cols += [("return", "return"), ("add_req_iq", "add_req_iq"),
             ("add_req_proc", "add_req_proc")]
    cols += [("conserv_err", "conserv_err"), ("preempt", "preempt"),
             ("fwd_tokens", "fwd_tokens"), ("fwd_steps", "fwd_steps"),
             ("fwd_ms_per_kT", "fwd_ms_per_kT"), ("lb_src", "lb_src")]
    return cols


def build_burst_sheet(ws, rows):
    cols = [("burst", "burst"), ("n", "n_req"),
            ("ttft_mean", "ttft_mean"), ("ttft_p50", "ttft_p50"),
            ("ttft_p90", "ttft_p90"), ("ttft_p99", "ttft_p99"),
            ("ttft_max", "ttft_max")]
    for c in MAIN_COMPS + [RESIDUAL]:
        cols += [(c + "_mean", c + "_mean"), (c + "_share%", c + "_share%")]
    out = []
    byb = defaultdict(list)
    for r in rows:
        byb[r["burst"]].append(r)
    for b in sorted(byb):
        g = byb[b]
        st = stats([r["ttft"] for r in g])
        row = {"burst": b, "n": st["n"], "ttft_mean": st["mean"],
               "ttft_p50": st["p50"], "ttft_p90": st["p90"],
               "ttft_p99": st["p99"], "ttft_max": st["max"]}
        tmean = st["mean"] or 1.0
        for c in MAIN_COMPS + [RESIDUAL]:
            m = stats([r[c] for r in g])["mean"]
            row[c + "_mean"] = m
            row[c + "_share%"] = 100.0 * m / tmean
        out.append(row)
    dump_sheet(ws, cols, out)


def build_machine_sheet(ws, rows):
    machines = sorted({r["machine"] for r in rows})
    cols = [("machine", "机器"), ("n", "n_req"), ("ttft_mean", "ttft_mean"),
            ("ttft_p50", "ttft_p50"), ("ttft_p90", "ttft_p90")]
    for c in MAIN_COMPS + [RESIDUAL]:
        cols += [(c + "_mean", c + "_mean")]
    for c in MAIN_COMPS + [RESIDUAL]:
        cols += [(c + "_delta", c + "_delta")]
    bym = defaultdict(list)
    for r in rows:
        bym[r["machine"]].append(r)
    overall = {c: stats([r[c] for r in rows])["mean"]
               for c in MAIN_COMPS + [RESIDUAL]}
    out = []
    for m in machines:
        g = bym[m]
        st = stats([r["ttft"] for r in g])
        row = {"machine": m, "n": st["n"], "ttft_mean": st["mean"],
               "ttft_p50": st["p50"], "ttft_p90": st["p90"]}
        for c in MAIN_COMPS + [RESIDUAL]:
            mc = stats([r[c] for r in g])["mean"]
            row[c + "_mean"] = mc
            row[c + "_delta"] = mc - overall[c]
        out.append(row)
    dump_sheet(ws, cols, out)


def build_decode_sheet(ws, decode_rows):
    cols = [("machine", "机器"), ("idx", "step"), ("ts", "ts"),
            ("decode_forward", "decode_forward"), ("load", "load"),
            ("save", "save"), ("sample", "sample"), ("num_total", "num_total"),
            ("num_gen", "num_gen"), ("sched_total", "sched_total"),
            ("decode_only", "decode_only")]
    dump_sheet(ws, cols, decode_rows)


def build_kv_sheet(ws, aux):
    """两段：[TTFT_KVXFER][put] 逐请求（TP-max）；pool_worker start_load_kv 逐 TP。"""
    # --- 段 1：put 逐请求
    ws.cell(row=1, column=1,
            value="[TTFT_KVXFER][put] 逐请求（同 req 全 TP 取 max → 回填 save_kv）"
            ).font = Font(bold=True, size=12)
    cols = ["机器", "req", "total", "build_key", "lookup", "prepare", "sync",
            "put", "finalize"]
    hr = 3
    for ci, h in enumerate(cols, start=1):
        c = ws.cell(row=hr, column=ci, value=h)
        c.font = Font(bold=True)
        c.fill = HEADER_FILL
    putmax = _keyed_max(aux["puts"], ("machine", "req"), "total")
    putidx = defaultdict(list)
    for p in aux["puts"]:
        putidx[(p["machine"], p["req"])].append(p)
    rr = hr + 1
    for (m, rq), v in sorted(putmax.items()):
        best = max(putidx[(m, rq)], key=lambda p: p["total"])
        vals = [m, rq, v, best["build_key"], best["lookup"], best["prepare"],
                best["sync"], best["put"], best["finalize"]]
        for ci, val in enumerate(vals, start=1):
            c = ws.cell(row=rr, column=ci, value=val)
            if ci >= 3:
                c.number_format = "0.000"
        rr += 1
    for ci, h in enumerate(cols, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = max(10, len(h) + 2)

    # --- 段 2：pool_worker start_load_kv 逐 TP
    r0 = rr + 2
    ws.cell(row=r0, column=1,
            value="pool_worker start_load_kv 逐 TP（批内取 TP-max → 回填 load_kv）"
            ).font = Font(bold=True, size=12)
    cols2 = ["机器", "batch", "tp", "reqs", "cost_ms"]
    hr2 = r0 + 2
    for ci, h in enumerate(cols2, start=1):
        c = ws.cell(row=hr2, column=ci, value=h)
        c.font = Font(bold=True)
        c.fill = HEADER_FILL
    rr2 = hr2 + 1
    for p in sorted(aux["pool_loads"],
                    key=lambda x: (str(x["machine"]), str(x["batch"]), x["tp"])):
        vals = [p["machine"], p["batch"], p["tp"], p["reqs"], p["cost_ms"]]
        for ci, val in enumerate(vals, start=1):
            c = ws.cell(row=rr2, column=ci, value=val)
            if ci in (4, 5):
                c.number_format = "0.000"
        rr2 += 1
    if aux["shm_agg"]:
        r1 = rr2 + 2
        ws.cell(row=r1, column=1,
                value="[SHM_DEQUEUE] 聚合观测（不参与回填，统计看 p50/p90）"
                ).font = Font(bold=True, size=12)
        cols3 = ["t", "wait", "read", "total", "count", "avg"]
        hr3 = r1 + 2
        for ci, h in enumerate(cols3, start=1):
            c = ws.cell(row=hr3, column=ci, value=h)
            c.font = Font(bold=True)
            c.fill = HEADER_FILL
        rr3 = hr3 + 1
        for s in aux["shm_agg"][:5000]:
            vals = [s["t"], s["wait"], s["read"], s["total"], s["count"], s["avg"]]
            for ci, val in enumerate(vals, start=1):
                c = ws.cell(row=rr3, column=ci, value=val)
                c.number_format = "0.000"
            rr3 += 1


def build_lb_sheet(ws, lb_rows):
    cols = [("machine", "机器"), ("req", "req"), ("wall_recv", "wall_recv"),
            ("client_send", "client_send_ts"),
            ("client2proxy_ms", "client2proxy_ms"),
            ("dispatch_ms", "dispatch_ms"), ("wait_p_ms", "wait_p_ms"),
            ("first_token_ms", "first_token_ms"), ("prefiller", "prefiller"),
            ("decoder", "decoder")]
    dump_sheet(ws, cols, lb_rows)


def build_recon_sheet(ws, recon_rows):
    cols = [("req", "req"), ("client_send_ts", "client_send_ts"),
            ("client_ttft", "client_ttft"),
            ("client2proxy_ms", "client2proxy_ms"),
            ("dispatch_ms", "dispatch_ms"),
            ("first_token_ms", "first_token_ms"),
            ("return_ms", "return_ms"),
            ("sum_id", "c2p+ft+ret"), ("err_ms", "err_ms"),
            ("joined", "joined")]
    dump_sheet(ws, cols, recon_rows)


def fmt_ts(ts):
    """CSV 的 ts 列：epoch 秒 → 可读时间；相对时钟（小数值）原样输出。"""
    if ts is None:
        return ""
    if ts > 1e9:  # unix epoch
        import datetime
        dt = datetime.datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{int((ts % 1) * 1000):03d}"
    return f"{ts:.3f}"


def write_csv(rows, path):
    """写 ttft_table.csv（契约见 references/ttft_table_contract.md）。

    v3 列 → 10 阶段映射：
      net_lb=lb_dispatch / fe_inputq=add_req_iq / fe_addreq=add_req_proc /
      sched_qwait=queue_wait / sched_total=sched_total / fwd_pure=forward /
      draft=mtp / kv_xfer=load_kv+save_kv / shm_dq=dequeue /
      other=引擎侧 ttft−Σ(引擎内 8 阶段)（tokenize/dispatch/add_req/mm_hash/
      wait_exec/sample/return/out_delivery/residual 兜底）。
    时间边界（与阶段和一致）：ttft_ms = net_lb + 引擎侧 ttft，因此
    Σ10 阶段 = ttft_ms 精确成立（回填增量超出 step_other 信封的行除外：
    other 钳 0，溢出量表现为守恒差值 |Σ10 − ttft_ms|，供下游质量检查；
    此类行对应 v3 口径 conserv_err>0，写 CSV 时打 stderr 告警）。
    ttft_ms 与 BREAKDOWN 行的 ttft= 相差 net_lb（对账/证据链引用时注意）。
    """
    import csv
    import sys as _sys
    n_neg = 0
    # ts 形态检测：整轮全部为相对秒（≤1e9，与 fmt_ts 的 epoch 判定同口径）时提示
    # 下游限制——下游 tpsplit-report 只解析日期时间，相对秒形态下其时间过滤
    # （如 --warmup-seconds 预热过滤）不生效，整轮将保留预热请求
    ts_vals = [r["ts"] for r in rows if r["ts"] is not None]
    if ts_vals and all(t <= 1e9 for t in ts_vals):
        print("[warn] ts 为相对秒形态（非 unix epoch）：下游 kptools-tpsplit-report "
              "只解析日期时间，--warmup-seconds 等时间过滤不适用于相对秒，整轮将保留"
              "预热请求；需要预热过滤时请在解析端完成或确认 ts 为绝对时间",
              file=_sys.stderr)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        for r in sorted(rows, key=lambda x: (x["ts"] is None, x["ts"] or 0)):
            stages = {
                "net_lb": r["lb_dispatch"],
                "fe_inputq": r["add_req_iq"],
                "fe_addreq": r["add_req_proc"],
                "sched_qwait": r["queue_wait"],
                "sched_total": r["sched_total"],
                "fwd_pure": r["forward"],
                "draft": r["mtp"],
                "kv_xfer": r["load_kv"] + r["save_kv"],
                "shm_dq": r["dequeue"],
            }
            engine_sum = sum(v for k, v in stages.items() if k != "net_lb")
            other = r["ttft"] - engine_sum
            if other < 0:
                # 回填增量超出 step_other 信封（对应 v3 conserv_err>0 行）：
                # 按契约 other 钳 0，溢出量保留在守恒差值里给下游质量检查
                n_neg += 1
                print(f"[warn] CSV other<0 已钳 0（req={r['req']}，"
                      f"other={other:.3f}，回填增量超出 step_other 信封，"
                      f"守恒差值={-other:.3f}）", file=_sys.stderr)
                other = 0.0
            ttft_total = r["ttft"] + stages["net_lb"]
            w.writerow([
                fmt_ts(r["ts"]), r["req"],
                # input_len 缺失输出空单元格（缺失约定与 ts 一致，见契约 §4：
                # 下游不得把空值当有效长度，0 仅在打点确实为 0 时出现）
                ("" if r.get("_input_len") is None else fnum(r.get("_input_len"))),
                f"{ttft_total:.2f}",
                f"{stages['net_lb']:.2f}", f"{stages['fe_inputq']:.2f}",
                f"{stages['fe_addreq']:.2f}", f"{stages['sched_qwait']:.2f}",
                f"{stages['sched_total']:.2f}", f"{stages['fwd_pure']:.2f}",
                f"{stages['draft']:.2f}", f"{stages['kv_xfer']:.2f}",
                f"{stages['shm_dq']:.2f}", f"{other:.2f}",
            ])
    if n_neg:
        print(f"[warn] 共 {n_neg} 行 other<0 已钳 0（回填增量超出 step_other "
              f"信封，守恒差值暴露给下游质量检查）", file=_sys.stderr)


# ---------------------------------------------------------------- 对账

def reconcile(lb_rows, bench_rows):
    """按 client_send_ts 逐请求对账（±2ms 容差），算回传 return。

    client_ttft ≈ client2proxy(网络) + first_token(分发+P处理+P→D交接+D首token)
    + return(proxy→client)；同机回环 return≈0。

    匹配规则（防串算）：
    * 排序仅按时间戳键（不同请求同 ts 时不再比较字典，避免 TypeError）；
    * 已被匹配的 LB 记录从候选中排除，两条 bench 不会命中同一条 LB；
    * 多候选时优先精确时间戳相等，其次用请求 ID 消歧；
    * 仍无法唯一匹配的行标 joined=ambiguous（不算 joined，不参与 return 统计）。
    """
    import bisect
    from collections import defaultdict

    recon = []
    used = set()  # 已匹配 LB 行的 id()，防串算
    by_req = defaultdict(list)
    for l in lb_rows:
        if l["req"]:
            by_req[str(l["req"])].append(l)
    sends = sorted(((l["client_send"], l) for l in lb_rows
                    if l["client_send"] > 0), key=lambda t: t[0])
    keys = [s for s, _ in sends]

    def pick(b, avail):
        """从候选中选唯一匹配：精确 ts > 请求 ID 消歧；返回 (row, why)。"""
        if not avail:
            return None, "unmatched"
        exact = [c for c in avail if c["client_send"] == b["client_send_ts"]]
        pool = exact if exact else avail
        if len(pool) > 1 and b["req"]:
            req_hits = [c for c in pool if str(c["req"]) == str(b["req"])]
            if req_hits:
                pool = req_hits
        if len(pool) == 1:
            return pool[0], ""
        if len(pool) > 1:
            return None, "ambiguous"
        return None, "unmatched"

    for b in bench_rows:
        row = {"req": b["req"], "client_send_ts": b["client_send_ts"],
               "client_ttft": b["client_ttft"] or 0.0,
               "client2proxy_ms": None, "dispatch_ms": None,
               "first_token_ms": None, "return_ms": None,
               "sum_id": None, "err_ms": None, "joined": ""}
        if b["client_send_ts"] > 0 and sends:
            i = bisect.bisect_left(keys, b["client_send_ts"] - 2)
            window = [sends[j][1] for j in range(i, len(keys))
                      if keys[j] <= b["client_send_ts"] + 2
                      and id(sends[j][1]) not in used]
            match, why = pick(b, window)
        else:
            # 无时间戳可比时按请求 ID 兜底（同样排除已用记录，防串算）
            match, why = pick(
                b, [l for l in by_req.get(str(b["req"]), [])
                    if id(l) not in used])
        if match is None:
            row["joined"] = "" if why == "unmatched" else why
            recon.append(row)
            continue
        used.add(id(match))
        row.update({"client2proxy_ms": match["client2proxy_ms"],
                    "dispatch_ms": match["dispatch_ms"],
                    "first_token_ms": match["first_token_ms"],
                    "joined": "lb"})
        ret = row["client_ttft"] - match["client2proxy_ms"] - match["first_token_ms"]
        row["return_ms"] = ret
        row["sum_id"] = match["client2proxy_ms"] + match["first_token_ms"] + ret
        row["err_ms"] = row["client_ttft"] - row["sum_id"]
        recon.append(row)
    return recon, sum(1 for r in recon if r["joined"] == "lb")


# ---------------------------------------------------------------- 文字结论

def fmt_ms(v):
    return f"{v:.1f}"


def print_conclusions(ctx):
    rows = ctx["rows"]
    p = print
    p("")
    p("=" * 72)
    p("TTFT 解析结论（单位 ms）")
    p("=" * 72)
    if not rows:
        p("（无 [TTFT][BREAKDOWN] 请求数据——请检查 P 日志是否打点生效）")
        return
    st = stats([r["ttft"] for r in rows])
    p(f"请求总数 {st['n']}，Burst 数 {ctx['n_burst']}（分段方式：{ctx['burst_mode']}）")
    p(f"ttft: mean={fmt_ms(st['mean'])} p50={fmt_ms(st['p50'])} "
      f"p90={fmt_ms(st['p90'])} p99={fmt_ms(st['p99'])} max={fmt_ms(st['max'])}")

    p("-" * 72)
    p("各环节（全请求 mean，按占比排序）：")
    comp_means = [(c, stats([r[c] for r in rows])["mean"]) for c in MAIN_COMPS]
    comp_means.sort(key=lambda x: -x[1])
    for c, m in comp_means:
        if m > 0.0005:
            p(f"  {c:<16} mean={fmt_ms(m):>9}  share={100.0 * m / max(st['mean'], 1e-9):5.1f}%")

    p("-" * 72)
    p("恒等式 / 守恒：")
    ce = stats([r["conserv_err"] for r in rows])
    n_ok = sum(1 for r in rows if abs(r["conserv_err"]) < 0.01)
    p(f"  conserv_err: {n_ok}/{len(rows)} 行为 0，max={fmt_ms(ce['max'])} "
      f"p99={fmt_ms(ce['p99'])}（v3 正常应≈0；fwd_steps>1 时可数十 ms）")
    n_env = sum(1 for r in rows
                if abs(r["step_other"] - r["dequeue"] - r["return"]) > 0.01)
    p(f"  step_other = dequeue + return 违反行数：{n_env}（应恒为 0）")
    ar_err = stats([abs(r["add_req"] - r["add_req_iq"] - r["add_req_proc"])
                    for r in rows])
    p(f"  |add_req - (iq+proc)| p99={fmt_ms(ar_err['p99'])}")

    p("-" * 72)
    rep = ctx["report"]
    p("回填（分析层，TP-max）：")
    p(f"  load_kv : {rep.get('loadkv_backfilled', 0)} 行被 pool_worker 真实值覆盖")
    p(f"  mtp     : {rep.get('mtp_backfilled', 0)} 行由 [TTFT_DRAFT] 回填")
    p(f"  save_kv : {rep.get('savekv_backfilled', 0)} 行由 [TTFT_KVXFER][put] 回填")
    p(f"  dequeue : {rep.get('dequeue_covered', 0)} 行有 [SHM_DEQUEUE_BATCH] 归因")

    p("-" * 72)
    warm = sum(1 for r in rows if r["fwd_tokens"] == 12)
    p(f"预热：fwd_tokens==12 的请求 {warm}/{len(rows)}"
      f"（明显 >12 说明前缀池未预热，该轮不可比）")
    cold = sum(1 for r in rows if r["cold_step"] == 1)
    p(f"冷启动：cold_step=1 共 {cold} 行（首步 kernel/图开销，建议剔除或单列看）")
    fs = sum(1 for r in rows if r["fwd_steps"] > 1)
    p(f"chunked prefill（fwd_steps>1）：{fs} 行（多步墙钟超出首步信封部分落 conserv_err）")

    if ctx["decode_rows"]:
        dr = stats([d["decode_forward"] for d in ctx["decode_rows"]])
        p("-" * 72)
        p(f"D 节点 decode：{len(ctx['decode_rows'])} 步，decode_forward mean="
          f"{fmt_ms(dr['mean'])} p90={fmt_ms(dr['p90'])}"
          f"（num_total 常为 0 属正常，EngineCore 墙钟兜底）")

    if ctx["lb_rows"]:
        lr = ctx["lb_rows"]
        for key in ("client2proxy_ms", "dispatch_ms", "wait_p_ms", "first_token_ms"):
            s = stats([l[key] for l in lr])
            p(f"LB {key}: mean={fmt_ms(s['mean'])} p90={fmt_ms(s['p90'])}")
        pf = defaultdict(int)
        for l in lr:
            pf[l["prefiller"]] += 1
        p("LB prefiller 分布: " + ", ".join(f"{k}={v}" for k, v in sorted(pf.items())))

    if ctx["recon"]:
        joined = [r for r in ctx["recon"] if r["joined"]]
        if joined:
            rt = stats([r["return_ms"] for r in joined])
            p("-" * 72)
            p(f"回传对账：{len(joined)}/{len(ctx['recon'])} 条对上 "
              f"（client_send_ts ±2ms）")
            p(f"  return: mean={fmt_ms(rt['mean'])} p90={fmt_ms(rt['p90'])} "
              f"max={fmt_ms(rt['max'])}（同机回环应≈0；偏大=钟偏/真实网络）")
        else:
            p("回传对账：0 条对上（检查压测端 --save-detailed 与 proxy 日志时间戳口径）")
    p("=" * 72)


# ---------------------------------------------------------------- 主流程

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="TTFT breakdown 日志解析 → Excel（8 Sheet）+ 文字结论（单位 ms）")
    ap.add_argument("plogs", nargs="+", help='P 节点日志，形如 "LABEL:path"（多 P 各一个）')
    ap.add_argument("-o", "--out", required=True, help="输出 xlsx 路径")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--one-burst", action="store_true",
                   help="整份日志当一个 burst（默认）")
    g.add_argument("--burst-gap", type=float, metavar="N",
                   help="按请求到达间隔 >N 秒切 burst（0 = 不聚合，逐请求成段；"
                        "默认 300s，且仅显式传入时生效）")
    ap.add_argument("--decode", help='"D:decode.log" D 节点日志')
    ap.add_argument("--kvxfer", action="append", default=[],
                    help='"102:P102.log" KVXFER/pool_worker/shm/draft 来源（可多次）')
    ap.add_argument("--lb", help='"PROXY:load_balance.log" proxy 日志')
    ap.add_argument("--detailed", help='"BENCH:xxx_dumpjsonl.json" 压测端结果')
    ap.add_argument("--merge-p", action="store_true",
                    help="多 P 合并成一套完整请求，明细新增 IP 列")
    ap.add_argument("--csv-out", metavar="PATH",
                    help="额外输出 ttft_table.csv（契约见 references/"
                         "ttft_table_contract.md，仓内权威定义；"
                         "ts/request_id/input_len/ttft_ms + 10 阶段列）")
    args = ap.parse_args(argv)

    # ---- 载入 P 日志
    rows = []
    p_machines = []
    for spec in args.plogs:
        label, path = split_label(spec)
        if not os.path.isfile(path):
            sys.exit(f"[错误] P 日志不存在：{path}")
        p_machines.append(label)
        rows.extend(load_breakdown(path, label))
    if not rows:
        sys.exit("[错误] 未解析到任何 [TTFT][BREAKDOWN] 行——打点未生效或日志不对")

    # ---- 载入辅助来源（--kvxfer 缺省 = P 日志本身）
    kv_sources = args.kvxfer or list(args.plogs)
    aux = {"puts": [], "pool_loads": [], "shm_batches": [], "shm_agg": [], "drafts": []}
    for spec in kv_sources:
        label, path = split_label(spec, default_label=None)
        # 对齐机器标签：kvxfer 的 label 需与 P 日志 label 一致才能按机器 join
        if label not in p_machines and len(p_machines) == 1:
            label = p_machines[0]
        elif label not in p_machines:
            # 标签对不上又无法唯一对齐时显式告警，避免回填静默失效
            print(f"[warn] --kvxfer 标签 {label!r} 与 P 日志机器 {p_machines} 不匹配，"
                  f"该来源的回填行不会命中任何请求：{path}", file=sys.stderr)
        a = load_aux(path, label)
        for k in aux:
            aux[k].extend(a[k])

    decode_rows = []
    if args.decode:
        label, path = split_label(args.decode, "D")
        decode_rows = load_decode(path, label)
    lb_rows = []
    if args.lb:
        label, path = split_label(args.lb, "PROXY")
        lb_rows = load_lb(path, label)
    bench_rows = []
    if args.detailed:
        label, path = split_label(args.detailed, "BENCH")
        bench_rows = load_bench(path, label)

    # ---- merge-p 的 IP 列
    if args.merge_p:
        for r in rows:
            r["ip"] = r["machine"]

    # ---- burst 分段 + 冷启动标记（--burst-gap 0 是合法的“不聚合”语义，
    #      不得被 or 静默替换为默认值）
    burst_gap = args.burst_gap if args.burst_gap is not None else 300.0
    n_burst, burst_mode = assign_bursts(
        rows, args.one_burst or (args.burst_gap is None), burst_gap)
    mark_cold_steps(rows)

    # ---- 回填管线（顺序固定：loadkv → draft → savekv → dequeue → finalize）
    report = {}
    _apply_real_loadkv(rows, aux["pool_loads"], report)
    _apply_draft_mtp(rows, aux["drafts"], report)
    _apply_real_savekv(rows, aux["puts"], report)
    _apply_real_dequeue(rows, aux["shm_batches"], report)
    _finalize_step_other(rows)
    apply_lb_backfill(rows, lb_rows, report)

    # ---- 同轮对齐软告警（承接采集侧 kptools-tpsplit-log-collector 的口径：
    #      P 侧请求数 vs 压测记录数不一致 → 四份日志可能不是同一次压测，
    #      对账与守恒口径将失真；软告警不改变退出码，核对请回采集侧）
    if bench_rows and rows and len(rows) != len(bench_rows):
        print(f"[warn] 同轮对齐告警：P 侧解析出 {len(rows)} 条请求 vs 压测记录 "
              f"{len(bench_rows)} 条（差 {abs(len(rows) - len(bench_rows))}）——"
              f"四份日志可能不是同一次压测，对账结果无意义；请回采集侧核对",
              file=sys.stderr)

    # ---- 对账
    recon, joined_n = ([], 0)
    if lb_rows and bench_rows:
        recon, joined_n = reconcile(lb_rows, bench_rows)

    # ---- CSV（契约：references/ttft_table_contract.md）
    if args.csv_out:
        write_csv(rows, args.csv_out)
        print(f"CSV 已写出：{os.path.abspath(args.csv_out)}（ttft_table 契约，"
              f"{len(rows)} 行）")

    # ---- 写 Excel
    wb = Workbook()
    wb.remove(wb.active)
    ws_over = wb.create_sheet("总览")
    ws_burst = wb.create_sheet("Burst各环节")
    ws_detail = wb.create_sheet("逐请求明细")
    ws_machine = wb.create_sheet("机器对比delta")
    ws_decode = wb.create_sheet("D节点Decode")
    ws_kv = wb.create_sheet("P节点KV传输")
    ws_lb = wb.create_sheet("LB分发")
    ws_recon = wb.create_sheet("回传对账")

    build_burst_sheet(ws_burst, rows)
    dump_sheet(ws_detail, build_detail_columns(args.merge_p), rows)
    build_machine_sheet(ws_machine, rows)
    build_decode_sheet(ws_decode, decode_rows)
    build_kv_sheet(ws_kv, aux)
    build_lb_sheet(ws_lb, lb_rows)
    build_recon_sheet(ws_recon, recon)

    # 总览
    st = stats([r["ttft"] for r in rows])
    ce = stats([r["conserv_err"] for r in rows])
    r0 = 1
    r0 = kv_block(ws_over, "输入与规模", [
        ("P 日志", ", ".join(p_machines)),
        ("请求数", st["n"]),
        ("Burst 数", n_burst),
        ("分段方式", burst_mode),
        ("merge-p", "yes" if args.merge_p else "no"),
        ("输出", os.path.abspath(args.out)),
    ], r0)
    r0 = kv_block(ws_over, "TTFT 统计（ms）", [
        ("mean", st["mean"]), ("p50", st["p50"]), ("p90", st["p90"]),
        ("p99", st["p99"]), ("max", st["max"]),
    ], r0)
    pairs = []
    for c in MAIN_COMPS + [RESIDUAL]:
        pairs.append((c, stats([r[c] for r in rows])["mean"]))
    r0 = kv_block(ws_over, "各环节 mean（ms）", pairs, r0)
    n_ok = sum(1 for r in rows if abs(r["conserv_err"]) < 0.01)
    r0 = kv_block(ws_over, "守恒与信封", [
        ("conserv_err==0 行数", f"{n_ok}/{len(rows)}"),
        ("conserv_err max", ce["max"]),
        ("step_other 信封违反", sum(1 for r in rows
                                    if abs(r["step_other"] - r["dequeue"] - r["return"]) > 0.01)),
        ("warmup fwd_tokens==12", f"{sum(1 for r in rows if r['fwd_tokens'] == 12)}/{len(rows)}"),
        ("cold_step=1", sum(1 for r in rows if r["cold_step"] == 1)),
        ("fwd_steps>1", sum(1 for r in rows if r["fwd_steps"] > 1)),
        ("lb_src=proxy 行数", report.get("lb_backfilled", 0)),
    ], r0)
    r0 = kv_block(ws_over, "回填（TP-max）", [
        ("load_kv 回填行数", report.get("loadkv_backfilled", 0)),
        ("mtp 回填行数", report.get("mtp_backfilled", 0)),
        ("save_kv 回填行数", report.get("savekv_backfilled", 0)),
        ("dequeue 归因行数", report.get("dequeue_covered", 0)),
        ("D decode 步数", len(decode_rows)),
        ("LB 请求数", len(lb_rows)),
        ("对账 joined", joined_n),
    ], r0)
    for ws in (ws_over, ws_burst, ws_detail, ws_machine, ws_decode, ws_kv,
               ws_lb, ws_recon):
        for col in ("A", "B"):
            ws.column_dimensions[col].width = max(ws.column_dimensions[col].width or 0, 22)

    wb.save(args.out)

    # ---- 文字结论
    ctx = {"rows": rows, "n_burst": n_burst, "burst_mode": burst_mode,
           "report": report, "decode_rows": decode_rows, "lb_rows": lb_rows,
           "recon": recon, "bench_rows": bench_rows}
    print_conclusions(ctx)
    print(f"\nExcel 已写出：{os.path.abspath(args.out)}（{len(SHEET_ORDER)} Sheet）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
