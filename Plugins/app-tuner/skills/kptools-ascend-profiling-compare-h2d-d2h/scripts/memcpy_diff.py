#!/usr/bin/env python3
"""H2D/D2H 数据搬运对比（h2d-d2h）。

对比两份 Ascend profiling 的拷贝行为：次数、字节、带宽、方向分布、与计算的重叠率、
同步（阻塞型）拷贝占比。输出 A.1 统一信封（9 字段）+ HTML。

用法:
  memcpy_diff.py <base> <cand> [--out-html r.html] [--json r.json]
                 [--min-delta-count 0] [--min-delta-bytes 0] [--min-delta-pct 1.0]
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

# DSA_SQE 是拷贝描述符，不属于计算 kernel；移出避免抬高 overlap 分子。
AI_TASK_TYPES = {"AI_CORE", "AI_VECTOR_CORE", "MIX_AIC", "MIX_AIV", "AI_CPU"}
SCHEMA_VERSION = 1

COPY_KEYWORDS = ("memcpy", "memset", "transdata", "h2d", "d2h", "d2d", "copy_")
SYNC_KEYWORDS = ("synchronize", "sync_stream", "syncdevice")
SIZE_KEYS = ("bytes", "size", "datasize", "length", "bytecount", "sizebyte", "sizeb", "bytesize")
DIR_KEYS = ("kind", "direction", "dir", "copytype", "copy_type")
# CANN AscendCL aclrtMemcpyKind 顺序枚举（acl_rt.h）：
#   0=HOST_TO_HOST, 1=HOST_TO_DEVICE, 2=DEVICE_TO_HOST, 3=DEVICE_TO_DEVICE
# 0（H2H）不是本 skill 关心的搬运方向，按 unknown 统计。
DIR_VALUES = {
    "1": "h2d", "2": "d2h", "3": "d2d",
    "h2d": "h2d", "host2device": "h2d", "host to device": "h2d",
    "d2h": "d2h", "device2host": "d2h", "device to host": "d2h",
    "d2d": "d2d", "device2device": "d2d", "device to device": "d2d",
}


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def round4(x, n=4):
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return 0.0


def median(xs):
    xs = sorted(float(x) for x in xs if x is not None)
    if not xs:
        return 0.0
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def mad(xs):
    xs = [float(x) for x in xs if x is not None]
    if not xs:
        return 0.0
    m = median(xs)
    return median([abs(x - m) for x in xs])


def union(ivs):
    out = []
    for a, b in sorted((float(s), float(e)) for s, e in ivs if e > s):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def cover(ivs, a, b):
    return sum(max(0.0, min(e, b) - max(s, a)) for s, e in union(ivs))


def cover_all(ivs):
    return sum(e - s for s, e in union(ivs))


def intersect_len(a_ivs, b_ivs):
    """两组区间的交集总长（双指针，O(n+m)）。"""
    A, B = union(a_ivs), union(b_ivs)
    i = j = 0
    total = 0.0
    while i < len(A) and j < len(B):
        a0, a1 = A[i]
        b0, b1 = B[j]
        total += max(0.0, min(a1, b1) - max(a0, b0))
        if a1 < b1:
            i += 1
        else:
            j += 1
    return total


# ---------------------------------------------------------------------------
# 采集定位与读取
# ---------------------------------------------------------------------------
def locate(path):
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"输入不存在: {p}")
    if p.is_file():
        return (p.parent, p) if p.name == "trace_view.json" else (p.parent, p.parent / "trace_view.json")
    direct = p / "ASCEND_PROFILER_OUTPUT"
    if direct.is_dir():
        return direct, direct / "trace_view.json"
    if (p / "kernel_details.csv").exists() or (p / "trace_view.json").exists():
        return p, p / "trace_view.json"
    outs = sorted(p.rglob("ASCEND_PROFILER_OUTPUT"), key=lambda x: len(x.parts))
    if outs:
        return outs[0], outs[0] / "trace_view.json"
    traces = sorted(p.rglob("trace_view.json"), key=lambda x: len(x.parts))
    if traces:
        return traces[0].parent, traces[0]
    return p, p / "trace_view.json"


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _us(ts, dur):
    """trace 的 ts/dur 统一按 µs 解释（torch_npu/CANN 的 chrome trace 规范）。"""
    return ts, dur


def _args(e):
    a = e.get("args")
    return a if isinstance(a, dict) else {}


def _size_of(args):
    for k, v in args.items():
        # 真实 trace 的 size 键带单位，如 "size(Byte)"；先剥掉括号再归一化。
        key = re.sub(r"\([^)]*\)", "", str(k)).strip().lower()
        key = re.sub(r"[^a-z0-9]", "", key)
        if key in SIZE_KEYS:
            n = _num(v)
            if n and n > 0:
                return n
    return None


def _dir_detail(name, args, allow_memset=True):
    """返回 (方向, 来源)。来源用于报告披露启发式，避免把主观假设当实测。"""
    low_name = str(name).lower().replace("_", " ")
    for k, v in args.items():
        if re.sub(r"[^a-z0-9]", "", str(k).lower()) in DIR_KEYS:
            key = re.sub(r"\s+", " ", str(v).strip().lower())
            if key in DIR_VALUES:
                return DIR_VALUES[key], "args"
            if type(v) is int:
                d = DIR_VALUES.get(str(v))
                return (d, "args") if d else (None, None)
    for key, val in DIR_VALUES.items():
        # 数值编码只用于 args；放进名称匹配会让任意地址/路径里的数字误判方向。
        if key.isdigit():
            continue
        if len(key) > 4:
            if key in low_name:
                return val, "name"
        elif re.search(rf"(^|[^a-z]){key}([^a-z]|$)", low_name):
            return val, "name"
    if allow_memset and "memset" in low_name:
        return "h2d", "memset-assumption"
    return None, None


def _dir_of(name, args):
    return _dir_detail(name, args)[0]


def _is_copy(name):
    low = str(name).lower()
    return any(k in low for k in COPY_KEYWORDS)


def _is_sync(name):
    low = str(name).lower()
    return any(k in low for k in SYNC_KEYWORDS)


def _task_type(e):
    a = _args(e)
    return str(a.get("Task Type") or a.get("task type") or "")


def _is_device_event(e):
    if _task_type(e):
        return True
    cat = str(e.get("cat") or "")
    return cat in AI_TASK_TYPES or cat in ("SDMA_SQE", "PCIE_DMA_SQE")


def _device_copy(name, e):
    """设备拷贝任务：名称命中拷贝关键字，或 task type 明确是 Memcpy。

    注意排除 SDMA_SQE/PCIE_DMA_SQE 描述符事件（名字是 SDMA_SQE，不是拷贝任务本体），
    否则同一拷贝会与 Memcpy/MEMCPY_ASYNC 本体重复计数。
    """
    tt = re.sub(r"[^a-z0-9]", "", _task_type(e).lower())
    return _is_copy(name) or tt == "memcpy"


def _host_copy_layer(name, cat):
    """host 侧拷贝事件的层级（仅在 trace 没有设备拷贝时兜底）。"""
    low = str(name).lower()
    c = str(cat or "")
    if c in ("enqueue", "dequeue"):
        return None
    if "acl_memcpy" in low or "acl_memset" in low:
        return 1
    if low.startswith("ascendcl@aclrtmemcpy") or low.startswith("ascendcl@aclrtmemset"):
        return 2
    if c == "python_function" and _is_copy(name):
        return 3
    return None


def _device_direction(name, args, api_dir):
    """设备拷贝方向：args/名称 → connection_id → 链路 → Memset 默认值（逐级兜底并记录来源）。"""
    d, src = _dir_detail(name, args, allow_memset=False)
    if d:
        return d, src
    conn = args.get("connection_id")
    if conn is not None and api_dir.get(conn):
        return api_dir[conn], "connection-id"
    link = str(args.get("link type") or args.get("link_type") or "").lower()
    transport = str(args.get("transport type") or args.get("transport_type") or "").lower()
    # ON_CHIP / SDMA 是设备内搬运，按 D2D 记（报告 notes 明示该启发式）。
    if "on_chip" in link or "onchip" in link or transport == "sdma":
        return "d2d", "onchip-sdma-assumption"
    if "memset" in str(name).lower():
        return "h2d", "memset-assumption"
    return None, None


def read_trace(trace_path, notes, degradations):
    if not trace_path or not Path(trace_path).exists():
        return None
    try:
        events = json.loads(Path(trace_path).read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        notes.append(f"trace_view.json 解析失败: {exc}")
        return None
    if not isinstance(events, list):
        return None

    spans = []
    for e in events:
        if not isinstance(e, dict) or e.get("ph") != "X":
            continue
        try:
            ts, dur = float(e.get("ts")), float(e.get("dur") or 0)
        except (TypeError, ValueError):
            continue
        name = str(e.get("name") or "")
        if dur > 0 and name.startswith("ProfilerStep#"):
            try:
                sid = int(name.split("#", 1)[1])
            except (IndexError, ValueError):
                sid = len(spans)
            spans.append((sid, ts, ts + dur))
    spans.sort(key=lambda x: x[1])
    if not spans:
        degradations.append("未找到 ProfilerStep#N，按整窗聚合")
    # 保留真实 ProfilerStep 编号，避免两侧被各自重编号成 0 后错配。
    steps = [sid for sid, _a, _b in spans] if spans else [0]

    # AscendCL@ 事件与设备任务共享 connection_id，可把方向传播到设备拷贝。
    api_dir = {}
    for e in events:
        if not isinstance(e, dict) or e.get("ph") != "X":
            continue
        name = str(e.get("name") or "")
        if not name.startswith("AscendCL@"):
            continue
        conn = _args(e).get("connection_id")
        d = _dir_of(name, {})
        if conn is not None and d:
            api_dir[conn] = d

    device_copies, host_candidates = [], []
    kernels, syncs = [], []
    outside = 0
    for e in events:
        if not isinstance(e, dict) or e.get("ph") != "X":
            continue
        try:
            ts, dur = float(e.get("ts")), float(e.get("dur") or 0)
        except (TypeError, ValueError):
            continue
        if dur <= 0:
            continue
        name = str(e.get("name") or "").strip()
        cat = str(e.get("cat") or "")
        ts_us, dur_us = _us(ts, dur)
        step = None
        for sid, a, b in spans:
            if a <= ts_us < b:
                step = sid
                break
        if step is None and not spans:
            step = 0

        if _is_device_event(e):
            args = _args(e)
            if _device_copy(name, e):
                if step is None:
                    outside += 1
                direction, dir_source = _device_direction(name, args, api_dir)
                device_copies.append({
                    "step": step,
                    "name": name[:160],
                    "start": ts_us,
                    "dur": dur_us,
                    "bytes": _size_of(args),
                    "direction": direction,
                    "dir_source": dir_source,
                    "start_us": ts_us,
                })
            else:
                tt = _task_type(e)
                is_kernel = (tt in AI_TASK_TYPES) if tt else (
                    cat in AI_TASK_TYPES or name.startswith("hcom"))
                if is_kernel:
                    kernels.append((ts_us, ts_us + dur_us))
        else:
            layer = _host_copy_layer(name, cat)
            if layer is not None:
                host_candidates.append((layer, name, cat, ts_us, dur_us, step))
        if _is_sync(name):
            syncs.append((ts_us, ts_us + dur_us))

    # 只在没有设备拷贝时用 host 层兜底，并且只选最高优先级的一层，避免
    # python_function → enqueue → dequeue → API 多层重复计数。
    copies = device_copies
    if not copies and host_candidates:
        best_layer = min(c[0] for c in host_candidates)
        for layer, name, cat, ts_us, dur_us, step in host_candidates:
            if layer != best_layer:
                continue
            args = {}
            copies.append({
                "step": step,
                "name": name[:160],
                "start": ts_us,
                "dur": dur_us,
                "bytes": None,
                "direction": _dir_of(name, args),
                "dir_source": "host-name",
                "start_us": ts_us,
            })
        notes.append(f"未发现设备拷贝任务，回退 host 层拷贝事件（layer={best_layer}）")

    # host 侧方向计数仅作参考，不并入设备层指标。
    ref = {"h2d": 0, "d2h": 0, "d2d": 0}
    for layer, name, cat, ts_us, dur_us, step in host_candidates:
        d = _dir_of(name, {})
        if d:
            ref[d] += 1
    if any(ref.values()):
        notes.append(
            "host 侧拷贝方向参考（不计入设备拷贝数）："
            f"H2D {ref['h2d']} / D2H {ref['d2h']} / D2D {ref['d2d']}"
        )
    if outside:
        notes.append(f"{outside} 个设备拷贝事件不在 ProfilerStep 窗口内，未计入 per-step")
    return {"copies": copies, "kernels": kernels, "syncs": syncs, "steps": steps}


def read_csv(output_dir, notes):
    path = output_dir / "kernel_details.csv"
    if not path.exists():
        return []
    import csv as _csv

    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        reader = _csv.DictReader(fh)
        if not reader.fieldnames:
            return []
        norm = {re.sub(r"[\s_()]+", " ", f.lower()).strip(): f for f in reader.fieldnames}
        c_name = norm.get("name")
        c_dur = norm.get("duration us") or norm.get("duration")
        c_start = norm.get("start time us") or norm.get("start time")
        c_step = norm.get("step id") or norm.get("step")
        if not c_name or not c_dur:
            return []
        for r in reader:
            name = (r.get(c_name) or "").strip()
            if not _is_copy(name):
                continue
            try:
                dur = float(r.get(c_dur) or 0)
            except (TypeError, ValueError):
                continue
            if dur <= 0:
                continue
            step_raw = (r.get(c_step) or "").strip() if c_step else "0"
            try:
                step = int(step_raw)
            except ValueError:
                step = 0
                notes.append(f"kernel_details.csv 有 Step Id 无法解析（{step_raw!r}），按 step0 归属")
            start = _num(r.get(c_start)) if c_start else None
            rows.append({"step": step, "name": name[:160], "start": start, "dur": dur,
                         "bytes": None, "direction": _dir_of(name, {}),
                         "dir_source": "csv-name", "start_us": start})
    if rows:
        notes.append(f"kernel_details.csv 补充 {len(rows)} 条拷贝行（无 size 字段，仅计次数）")
        if any(r_["start"] is None for r_ in rows):
            notes.append("kernel_details.csv 部分拷贝行缺 Start Time，重叠率按 0 计")
    return rows


def read_api_counts(output_dir):
    for name in ("api_statistic.csv", "api_detail.csv"):
        path = output_dir / name
        if not path.exists():
            continue
        import csv as _csv

        n = 0
        with open(path, encoding="utf-8", errors="replace") as fh:
            reader = _csv.DictReader(fh)
            for r in reader:
                api = (r.get("API Name") or r.get("Name") or "").lower()
                if "memcpy" in api or "memset" in api:
                    try:
                        n += int(float(r.get("Count") or 0))
                    except (TypeError, ValueError):
                        pass
        if n:
            return n, name
    return 0, None


def load_capture(path, notes, degradations):
    output_dir, trace_path = locate(path)
    tr = read_trace(trace_path, notes, degradations)
    trace_ok = tr is not None
    copies = list(tr["copies"]) if tr else []
    kernels = list(tr["kernels"]) if tr else []
    syncs = list(tr["syncs"]) if tr else []
    source = "trace_view.json" if tr else None

    if not copies:
        rows = read_csv(output_dir, notes)
        if rows:
            copies = rows
            source = "kernel_details.csv"
            degradations.append("无 trace 拷贝事件，改用 kernel_details.csv（仅计次数）")

    api_n, api_src = read_api_counts(output_dir)
    if api_n:
        notes.append(f"{api_src} 记录拷贝类 API {api_n} 次（aclrtMemcpy/aclrtMemcpyAsync 等 API 调用计数与设备 memcpy 事件非一一对应：异步提交、同笔搬运多 API 参与等，仅作交叉参考）")

    if not copies and not kernels:
        if trace_ok:
            # trace 可读但确实没有拷贝/设备事件：按“无拷贝”出 0 指标报告，不报错。
            degradations.append("trace 内未发现拷贝事件或设备 kernel，指标按 0 输出")
        else:
            raise FileNotFoundError(f"无法从 {path} 读取拷贝或设备事件")
    if not kernels:
        degradations.append("未发现设备 kernel，重叠率与同步占比不可判断")

    if tr:
        step_ids = list(tr["steps"])
    else:
        step_ids = sorted({c["step"] for c in copies if c.get("step") is not None}) or [0]
    return {"path": str(trace_path if source == "trace_view.json" else output_dir),
            "source": source or "trace_view.json", "copies": copies, "kernels": kernels,
            "syncs": syncs, "steps": step_ids}


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def stats(cap):
    copies = cap["copies"]
    busy_ivs = [(c["start"], c["start"] + c["dur"]) for c in copies if c["start"] is not None]
    busy = cover_all(busy_ivs)
    known = [c for c in copies if c.get("bytes")]
    total_bytes = sum(c["bytes"] for c in known)
    # 用“已知大小拷贝忙碌区间并集”作分母（重叠拷贝不重复计时），与 SKILL 口径一致。
    known_ivs = [(c["start"], c["start"] + c["dur"]) for c in known if c["start"] is not None]
    total_dur = cover_all(known_ivs) if known_ivs else sum(c["dur"] for c in known)
    bw = (total_bytes * 8.0) / (total_dur / 1e6) / 1e9 if total_dur > 0 else 0.0

    kernel_union = union(cap.get("kernels") or [])
    sync_union = union(cap.get("syncs") or [])
    overlap = intersect_len(busy_ivs, kernel_union) if busy > 0 else 0.0
    sync_hit = intersect_len(busy_ivs, sync_union) if busy > 0 else 0.0

    per_step = {}
    for s in cap["steps"]:
        rows = [c for c in copies if c["step"] == s]
        per_step[s] = {"count": len(rows), "bytes": sum((c.get("bytes") or 0) for c in rows)}

    directions = {"h2d": 0, "d2h": 0, "d2d": 0}
    for c in copies:
        if c.get("direction") in directions:
            directions[c["direction"]] += 1
    return {
        "count": len(copies),
        "bytes": total_bytes,
        "bandwidth_gbps": bw,
        "overlap_pct": (overlap / busy * 100.0) if busy > 0 else 0.0,
        "sync_pct": (sync_hit / busy * 100.0) if busy > 0 else 0.0,
        "directions": directions,
        "unknown_direction": sum(1 for c in copies if not c.get("direction")),
        "size_unknown": sum(1 for c in copies if not c.get("bytes")),
        "known_size": sum(1 for c in copies if c.get("bytes")),
        "memset_assumed": sum(1 for c in copies if c.get("dir_source") == "memset-assumption"),
        "onchip_sdma_assumed": sum(
            1 for c in copies if c.get("dir_source") == "onchip-sdma-assumption"
        ),
        "busy_us": busy,
        "per_step": per_step,
    }


def metric(name, unit, base, cand, noise, evidence, info=False, invert=False, pct=True,
           judge_delta=None):
    delta = base - cand
    judge = delta if judge_delta is None else judge_delta
    if info:
        verdict = "info"
    elif invert:
        verdict = "improved" if judge < -noise else ("regressed" if judge > noise else "noise")
    else:
        verdict = "improved" if judge > noise else ("regressed" if judge < -noise else "noise")
    return {
        "name": name, "unit": unit,
        "base": round4(base), "cand": round4(cand),
        "delta": round4(delta),
        "delta_pct": round4((delta / base * 100.0) if base else 0.0, 2) if pct else 0.0,
        "verdict": verdict,
        "noise_band": round4(noise),
        "evidence": evidence,
    }


def compare(base, cand, min_count, min_bytes, min_pct):
    bs, cs = stats(base), stats(cand)
    ev = f"base:{base['source']} / cand:{cand['source']}"

    b_cnt = [bs["per_step"][s]["count"] for s in bs["per_step"]]
    c_cnt = [cs["per_step"][s]["count"] for s in cs["per_step"]]
    b_byt = [bs["per_step"][s]["bytes"] for s in bs["per_step"]]
    c_byt = [cs["per_step"][s]["bytes"] for s in cs["per_step"]]

    # 全量只作描述；方向判定用共同 step 的逐对差值，避免两侧采样步数不同时误判。
    n_pair = min(len(b_cnt), len(c_cnt))
    paired_cnt = [b_cnt[i] - c_cnt[i] for i in range(n_pair)]
    paired_byt = [b_byt[i] - c_byt[i] for i in range(n_pair)]
    judge_cnt = median(paired_cnt) if paired_cnt else (bs["count"] - cs["count"])
    judge_byt = median(paired_byt) if paired_byt else (bs["bytes"] - cs["bytes"])
    noise_cnt = max(min_count, mad(paired_cnt))
    noise_byt = max(min_bytes, mad(paired_byt))
    noise_pct = max(0.0, min_pct)
    noise_sync = max(0.0, min_pct)
    bytes_comparable = bs["known_size"] == cs["known_size"]
    bytes_evidence = (
        f"{ev}；已知大小事件 base {bs['known_size']}/cand {cs['known_size']}，"
        f"未知大小 base {bs['size_unknown']}/cand {cs['size_unknown']}；"
        "方向判定用共同 step 逐对差值"
    )

    metrics = [
        metric("memcpy_count", "count", bs["count"], cs["count"], noise_cnt,
               f"{ev}；全量为描述值，方向判定用共同 step 逐对差值", judge_delta=judge_cnt),
        metric("memcpy_bytes", "bytes", bs["bytes"], cs["bytes"], noise_byt,
               bytes_evidence, info=not bytes_comparable, judge_delta=judge_byt),
        metric("memcpy_bandwidth_gbps", "Gbps", bs["bandwidth_gbps"], cs["bandwidth_gbps"],
               0.0, ev, info=True),
        metric("memcpy_overlap_pct", "pct", bs["overlap_pct"], cs["overlap_pct"],
               noise_pct, ev, invert=True),
        metric("memcpy_sync_pct", "pct", bs["sync_pct"], cs["sync_pct"], noise_sync, ev),
        metric("memcpy_h2d_count", "count", bs["directions"]["h2d"], cs["directions"]["h2d"],
               noise_cnt, ev, info=True),
        metric("memcpy_d2h_count", "count", bs["directions"]["d2h"], cs["directions"]["d2h"],
               noise_cnt, ev, info=True),
        metric("memcpy_d2d_count", "count", bs["directions"]["d2d"], cs["directions"]["d2d"],
               noise_cnt, ev, info=True),
        metric("memcpy_size_unknown_count", "count", bs["size_unknown"], cs["size_unknown"],
               0.0, ev, info=True, pct=False),
        metric("memcpy_unknown_direction_count", "count", bs["unknown_direction"],
               cs["unknown_direction"], 0.0, ev, info=True, pct=False),
    ]
    if not base["copies"] and not cand["copies"]:
        summary = "两侧均未发现拷贝事件"
    else:
        m = {x["name"]: x for x in metrics}
        parts = [f"拷贝 {m['memcpy_count']['base']:.0f} → {m['memcpy_count']['cand']:.0f} 次"]
        if m["memcpy_bytes"]["base"] or m["memcpy_bytes"]["cand"]:
            parts.append(f"字节 {m['memcpy_bytes']['base']:.0f} → {m['memcpy_bytes']['cand']:.0f}")
        parts.append(f"重叠率 {m['memcpy_overlap_pct']['base']:.1f}% → {m['memcpy_overlap_pct']['cand']:.1f}%")
        summary = "；".join(parts)

    per_step = []
    # 按真实/共同 step id 对齐；缺对应样本的步骤保留并显式给 null，不重编号错配。
    for sid in sorted(set(base["steps"]) | set(cand["steps"])):
        b_row = bs["per_step"].get(sid)
        c_row = cs["per_step"].get(sid)
        b_cnt_v = b_row["count"] if b_row else None
        c_cnt_v = c_row["count"] if c_row else None
        per_step.append({
            "step": sid,
            "cand_step": sid if c_row else None,
            "base": b_cnt_v,
            "cand": c_cnt_v,
            "delta": (b_cnt_v - c_cnt_v) if (b_cnt_v is not None and c_cnt_v is not None) else None,
            "base_bytes": b_row["bytes"] if b_row else None,
            "cand_bytes": c_row["bytes"] if c_row else None,
        })

    dir_table = {
        k: (bs["directions"][k], cs["directions"][k])
        for k in ("h2d", "d2h", "d2d")
    }
    return {"metrics": metrics, "per_step": per_step, "summary": summary,
            "dir_table": dir_table, "base_stats": bs, "cand_stats": cs}


def envelope(base, cand, result, notes_b, notes_c, deg_b, deg_c):
    degraded = [f"base: {x}" for x in deg_b] + [f"cand: {x}" for x in deg_c]
    sidecars = sorted({s for s in (base["source"], cand["source"]) if s and s != "none"})
    notes = [f"base: {x}" for x in notes_b] + [f"cand: {x}" for x in notes_c]
    notes.append("Δ = base − cand；拷贝次数/字节越少通常越好，重叠率越高越好（判定已按语义取反）")
    for tag, cap in (("base", base), ("cand", cand)):
        if not cap["copies"]:
            degraded.append(f"{tag}: 未发现拷贝事件")
    for tag, st in (("base", result["base_stats"]), ("cand", result["cand_stats"])):
        if st["count"] and st["size_unknown"] >= st["count"]:
            degraded.append(f"{tag}: 拷贝 size 全部未知，字节/带宽不可用")
        if st["count"] and st["unknown_direction"] >= st["count"]:
            notes.append(f"{tag}: 拷贝方向全部未知，方向分布不可用")
        if st["memset_assumed"]:
            notes.append(
                f"{tag}: {st['memset_assumed']} 个 Memset 事件按 host 发起假设归 H2D（启发式，非实测方向）"
            )
        if st["onchip_sdma_assumed"]:
            notes.append(
                f"{tag}: {st['onchip_sdma_assumed']} 个 ON_CHIP/SDMA 事件按设备内搬运假设归 D2D（启发式，非实测方向）"
            )
    if any("未找到 ProfilerStep" in x for x in degraded):
        confidence = "low"
    elif degraded:
        confidence = "medium"
    else:
        confidence = "high"
    return {
        "skill": "kptools-ascend-profiling-compare-h2d-d2h",
        "schema_version": SCHEMA_VERSION,
        "mode": "pair",
        "inputs": {"base": base["path"], "cand": cand["path"],
                   "steps": f"per_step={len(result['per_step'])}"},
        "data_quality": {"sidecars": sidecars, "degraded": degraded, "confidence": confidence},
        "metrics": result["metrics"],
        "per_step": result["per_step"],
        "summary": result["summary"],
        "notes": notes,
    }


def validate(env):
    errs = []
    for k in ("skill", "schema_version", "mode", "inputs", "data_quality",
              "metrics", "per_step", "summary", "notes"):
        if k not in env:
            errs.append(f"缺字段 {k}")
    if env.get("mode") not in ("pair", "single"):
        errs.append("mode 非法")
    dq = env.get("data_quality") or {}
    if dq.get("confidence") not in ("high", "medium", "low"):
        errs.append("confidence 非法")
    for i, m in enumerate(env.get("metrics") or []):
        if m.get("verdict") not in ("improved", "regressed", "noise", "info"):
            errs.append(f"metrics[{i}].verdict 非法")
        if not m.get("evidence"):
            errs.append(f"metrics[{i}] 缺 evidence")
    if not env.get("metrics"):
        errs.append("metrics 为空")
    return errs


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _cls(v):
    return {"improved": "v-dev", "regressed": "v-host"}.get(v, "v-mid")


def render_html(env, result, css_path):
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    rows = "".join(
        f"<tr><td>{esc(m['name'])}</td><td>{m['base']}</td><td>{m['cand']}</td>"
        f"<td>{m['delta']:+.3f}</td><td>{m['delta_pct']:+.1f}%</td>"
        f"<td><span class='verdict {_cls(m['verdict'])}'>{esc(m['verdict'])}</span></td>"
        f"<td>±{m['noise_band']}</td><td class='note'>{esc(m['evidence'])}</td></tr>"
        for m in env["metrics"]
    )
    table = ("<div class='sect'><h3>指标对比</h3><table><tr><th>指标</th><th>base</th>"
             "<th>cand</th><th>Δ</th><th>变化</th><th>判定</th><th>噪声带</th><th>证据</th></tr>"
             + rows + "</table></div>")

    d = result["dir_table"]
    dir_rows = "".join(
        f"<tr><td>{k.upper()}</td><td>{d[k][0]}</td><td>{d[k][1]}</td>"
        f"<td>{d[k][0] - d[k][1]:+d}</td></tr>" for k in ("h2d", "d2h", "d2d")
    )
    dir_html = ("<div class='sect'><h3>方向分布（设备层次数）</h3><table><tr><th>方向</th>"
                "<th>base</th><th>cand</th><th>Δ</th></tr>" + dir_rows + "</table>"
                "<div class='note'>方向由设备事件 args/名称判定；ON_CHIP/SDMA 按设备内搬运假设归 D2D，"
                "Memset 在无 connection_id/链路证据时按 host 发起假设归 H2D（启发式，见 notes），"
                "通用 PCIe DMA 无法判方向时计入 unknown。host 侧方向计数见下方口径说明"
                "（不计入设备拷贝数）。</div></div>")

    top = []
    for tag, st in (("base", result["base_stats"]), ("cand", result["cand_stats"])):
        if st["unknown_direction"]:
            top.append(f"{tag} 有 {st['unknown_direction']} 个事件方向未识别")
        if st["size_unknown"]:
            top.append(f"{tag} 有 {st['size_unknown']} 个事件大小未知（不计入字节/带宽）")
    notes_html = ("<div class='sect'><h3>降级说明与口径</h3><ul>"
                  + "".join(f"<li>{esc(x)}</li>" for x in (env["data_quality"]["degraded"] + env["notes"]))
                  + "</ul></div>")
    if top:
        notes_html = ("<div class='sect'><h3>数据完整性提示</h3><ul>"
                      + "".join(f"<li>{esc(x)}</li>" for x in top) + "</ul></div>") + notes_html

    hero = ("<div class='hero'>"
            f"<div class='htitle'>拷贝对比 · <span class='judge'>{esc(env['summary'])}</span></div>"
            f"<div class='hnums'>base {esc(env['inputs']['base'])}<span class='sep'>→</span>"
            f"cand {esc(env['inputs']['cand'])}</div>"
            f"<div class='kv'>契约置信度: {esc(env['data_quality']['confidence'])} · 数据源: "
            f"{esc(', '.join(env['data_quality']['sidecars']) or '无')}</div></div>")
    return ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'><title>拷贝对比</title>"
            f"<style>{css}</style></head><body><div class='page on'><h1>H2D/D2H 拷贝对比</h1>"
            f"{hero}{table}{dir_html}{notes_html}</div></body></html>")


def render_error(msg, base_path, cand_path, css_path):
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    return ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'><title>拷贝对比失败</title>"
            f"<style>{css}</style></head><body><div class='page on'><h1>拷贝对比失败</h1>"
            f"<div class='hero'><div class='htitle'>无法完成对比</div>"
            f"<div class='hnums'>{esc(msg)}</div></div>"
            f"<div class='kv'>base: {esc(base_path)}</div><div class='kv'>cand: {esc(cand_path)}</div>"
            "</div></body></html>")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="两份 Ascend profiling 的拷贝（H2D/D2H）对比")
    ap.add_argument("base")
    ap.add_argument("cand")
    ap.add_argument("--out-html", default="memcpy_report.html")
    ap.add_argument("--json", dest="out_json", default=None)
    ap.add_argument("--min-delta-count", type=float, default=0.0)
    ap.add_argument("--min-delta-bytes", type=float, default=0.0)
    ap.add_argument("--min-delta-pct", type=float, default=1.0)
    args = ap.parse_args(argv)

    css_path = Path(__file__).resolve().parent.parent / "assets" / "report.css"
    nb, nc, db, dc = [], [], [], []
    try:
        base = load_capture(args.base, nb, db)
        cand = load_capture(args.cand, nc, dc)
        result = compare(base, cand, args.min_delta_count, args.min_delta_bytes, args.min_delta_pct)
    except Exception as exc:
        Path(args.out_html).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_html).write_text(render_error(f"{type(exc).__name__}: {exc}",
                                                    args.base, args.cand, css_path), encoding="utf-8")
        print(f"对比失败: {exc}", file=sys.stderr)
        return 3

    env = envelope(base, cand, result, nb, nc, db, dc)
    errs = validate(env)
    if errs:
        print("警告：JSON 不符合 A.1: " + "; ".join(errs), file=sys.stderr)
    if args.out_json:
        Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_json).write_text(json.dumps(env, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON → {args.out_json}")
    Path(args.out_html).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_html).write_text(render_html(env, result, css_path), encoding="utf-8")
    print(f"HTML → {args.out_html}")
    print(f"结论: {env['summary']}")
    for m in env["metrics"][:5]:
        print(f"  {m['name']:<32} base {m['base']:>12} cand {m['cand']:>12} "
              f"Δ {m['delta']:+.3f} {m['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
