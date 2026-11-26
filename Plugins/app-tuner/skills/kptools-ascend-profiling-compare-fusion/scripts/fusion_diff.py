#!/usr/bin/env python3
"""算子融合前后对比（fusion）。

对比两份 Ascend profiling 的 kernel 序列：算子数量变化、合并证据（N→1）、
收益归因。证据不足输出 unproven，不硬凑结论。输出 A.1 信封 + HTML。

用法:
  fusion_diff.py <base> <cand> [--out-html r.html] [--json r.json]
                 [--fused-ops FILE] [--window-tol-ms 0.05] [--min-delta-ms 0.05]
"""

import argparse
import csv as _csv
import json
import re
import sys
from pathlib import Path

AI_TASK_TYPES = {"AI_CORE", "AI_VECTOR_CORE", "MIX_AIC", "MIX_AIV", "AI_CPU", "DSA_SQE"}
SCHEMA_VERSION = 1
MAX_SEQ = 1500


# ---------------------------------------------------------------------------
# 工具
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


def tokens(name):
    n = re.sub(r"[^A-Za-z0-9]+", " ", str(name))
    n = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", n)
    out = set()
    for t in n.lower().split():
        t = re.sub(r"\d+$", "", t)
        if t and not t.isdigit():
            out.add(t)
    return out


def norm_name(name):
    """对齐用的名字：去掉**格式已知的实例编号后缀**，保留名称主体里的数字。

    只剥离以分隔符开头的尾部数字组（如 `_98_1` / `.23.4`），因此
    `hcom_x_98_1` 与 `hcom_x_293_1` 归一相同，而 `Conv2D` 与 `Conv3D` 保持不同。
    """
    text = str(name).lower()
    text = re.sub(r"(?:[._\-]+\d+)+$", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)


# ---------------------------------------------------------------------------
# 读取
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
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def read_csv(output_dir, notes, degradations):
    path = output_dir / "kernel_details.csv"
    if not path.exists():
        return None
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        reader = _csv.DictReader(fh)
        if not reader.fieldnames:
            return None
        norm = {re.sub(r"[\s_()]+", " ", f.lower()).strip(): f for f in reader.fieldnames}
        c_name = norm.get("name")
        c_dur = norm.get("duration us") or norm.get("duration")
        c_start = norm.get("start time us") or norm.get("start time")
        c_step = norm.get("step id") or norm.get("step")
        if not c_name or not c_dur:
            return None
        for r in reader:
            name = (r.get(c_name) or "").strip()
            dur = _num(r.get(c_dur))
            start = _num(r.get(c_start)) if c_start else None
            if not name or not dur or dur <= 0:
                continue
            try:
                step = int((r.get(c_step) or "0").strip()) if c_step else 0
            except ValueError:
                step = 0
            rows.append({"step": step, "name": name[:160], "dur": dur,
                         "start": start, "step_start": 0.0})
    if not rows:
        return None
    if not c_start:
        degradations.append("kernel_details.csv 无 Start Time，序列按文件顺序近似")
    if not c_step:
        degradations.append("kernel_details.csv 缺 Step Id，按整窗处理（无 per-step）")
    missing_start = sum(1 for r in rows if r["start"] is None)
    if missing_start:
        notes.append(
            f"kernel_details.csv 有 {missing_start} 行缺 Start Time：保留缺测状态，"
            "时间窗重叠证据不可验证（相应合并/拆分裂为 unproven）"
        )
    return rows, "kernel_details.csv"


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
        if isinstance(e, dict) and e.get("ph") == "X" \
                and str(e.get("name") or "").startswith("ProfilerStep#"):
            try:
                ts, dur = float(e.get("ts")), float(e.get("dur") or 0)
            except (TypeError, ValueError):
                continue
            if dur > 0:
                try:
                    sid = int(str(e.get("name")).split("#", 1)[1])
                except (IndexError, ValueError):
                    sid = len(spans)
                spans.append((sid, ts, ts + dur))
    spans.sort(key=lambda x: x[1])
    if not spans:
        degradations.append("未找到 ProfilerStep#N，按整窗聚合")
    rows = []
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
        args = e.get("args") if isinstance(e.get("args"), dict) else {}
        tt = str(args.get("Task Type") or args.get("task type") or "")
        # 拷贝任务（SDMA_SQE/PCIE_DMA_SQE）属于 h2d-d2h 口径，不计入 kernel 序列。
        is_dev = name.startswith("hcom") or ((tt in AI_TASK_TYPES) if tt else (cat in AI_TASK_TYPES))
        if not is_dev:
            continue
        if spans:
            step = None
            step_start = 0.0
            for sid, a, b in spans:
                if a <= ts < b:
                    step, step_start = sid, a
                    break
            if step is None:
                outside += 1
                continue
        else:
            # 没有任何 step 标记时才回退到整窗；有 step 时窗外事件不归第 0 步。
            step, step_start = 0, 0.0
        rows.append({"step": step, "name": name[:160], "dur": dur, "start": ts,
                     "step_start": step_start})
    if outside:
        notes.append(f"{outside} 个 kernel 落在所有 ProfilerStep 窗口之外，已排除（不归入第 0 步）")
    if not rows:
        return None
    degradations.append("使用 trace_view.json 设备事件")
    return rows, "trace_view.json"


def load_capture(path, fused_ops, notes, degradations):
    output_dir, trace_path = locate(path)
    got = read_csv(output_dir, notes, degradations)
    if got is None:
        got = read_trace(trace_path, notes, degradations)
    if got is None:
        raise FileNotFoundError(f"无法从 {path} 读取 kernel 序列")
    rows, source = got
    steps = {}
    for r in rows:
        steps.setdefault(r["step"], []).append(r)
    for sid in steps:
        steps[sid].sort(
            key=lambda x: (x["start"] is None, x["start"] if x["start"] is not None else 0.0)
        )
    step_starts = {
        sid: min((r.get("step_start", 0.0) for r in rows_), default=0.0)
        for sid, rows_ in steps.items()
    }
    return {"path": str(output_dir), "source": source, "steps": steps,
            "step_ids": sorted(steps), "step_starts": step_starts, "fused_ops": fused_ops}


# ---------------------------------------------------------------------------
# 对齐与合并检测
# ---------------------------------------------------------------------------
def lcs_indices(a, b):
    n, m = len(a), len(b)
    if n > MAX_SEQ or m > MAX_SEQ:
        return None
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if a[i] == b[j] else max(dp[i + 1][j], dp[i][j + 1])
    i = j = 0
    pairs = []
    while i < n and j < m:
        if a[i] == b[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _span(rows):
    """返回时间窗；缺 Start Time 时返回 None（缺测不能被当作从 0 开始）。"""
    if not rows or any(r.get("start") is None for r in rows):
        return None
    start = min(r["start"] for r in rows)
    end = max(r["start"] + r["dur"] for r in rows)
    return start, end


def _overlap(a, b, tol):
    return not (a[1] + tol < b[0] or b[1] + tol < a[0])


def _contains_all(old_rows, new_row):
    new_tokens = tokens(new_row["name"])
    if not new_tokens:
        return False
    for r in old_rows:
        t = tokens(r["name"])
        if t and not (t & new_tokens):
            return False
    return True


def classify_gap(old_rows, new_rows, tol, fused_ops):
    """返回 (kind, level, reason)。kind: merge/split/change。"""
    k, m = len(old_rows), len(new_rows)
    if k >= 2 and m == 1:
        kind = "merge"
    elif k == 1 and m >= 2:
        kind = "split"
    else:
        kind = "change"
    if not old_rows or not new_rows:
        return kind, "unproven", "一侧为空，不构成合并/拆分"
    old_span, new_span = _span(old_rows), _span(new_rows)
    if old_span is None or new_span is None:
        return kind, "unproven", "缺少 Start Time，无法验证时间窗重叠"
    if not _overlap(old_span, new_span, tol):
        return kind, "unproven", "时间窗不重叠"
    if kind == "merge" and _contains_all(old_rows, new_rows[0]):
        return kind, "confirmed", "时间窗重叠，且新名包含全部旧算子 token"
    if any(r["name"] in fused_ops for r in new_rows):
        return kind, "confirmed", "时间窗重叠，且命中 --fused-ops 清单"
    return kind, "suspected", "时间窗重叠，但名称无明确包含关系"


def _relative(rows, origin):
    """按 step 窗口起点平移，跨采集只比较相对时间窗（缺 Start Time 保持 None）。"""
    return [
        {**r, "start": (r["start"] - origin) if r.get("start") is not None else None}
        for r in rows
    ]


def analyse_step(base_rows, cand_rows, tol, fused_ops):
    a = [norm_name(r["name"]) for r in base_rows]
    b = [norm_name(r["name"]) for r in cand_rows]
    pairs = lcs_indices(a, b)
    too_long = pairs is None
    gaps = []
    if pairs is None:
        gaps.append((base_rows, cand_rows))
    else:
        prev_i = prev_j = 0
        for i, j in pairs + [(len(a), len(b))]:
            if i > prev_i or j > prev_j:
                gaps.append((base_rows[prev_i:i], cand_rows[prev_j:j]))
            prev_i, prev_j = i + 1, j + 1
    out = []
    for old_rows, new_rows in gaps:
        if not old_rows and not new_rows:
            continue
        kind, level, reason = classify_gap(old_rows, new_rows, tol, fused_ops)
        if too_long:
            # 序列过长时放弃 LCS，整序列缺口不能凭 token/时间窗凑成 confirmed。
            level, reason = "unproven", "序列过长（>MAX_SEQ），已放弃 LCS，仅比较数量与时长"
        out.append({"kind": kind, "level": level, "reason": reason,
                    "old": [r["name"] for r in old_rows],
                    "new": [r["name"] for r in new_rows],
                    "old_span": _span(old_rows), "new_span": _span(new_rows)})
    return out


def compare_captures(base, cand, tol, min_delta_ms=0.05):
    notes = []
    common = [s for s in base["step_ids"] if s in cand["steps"]]
    if common:
        pairs = [(s, s) for s in common]
    else:
        notes.append("两侧无相同 step id，按首组 step 对齐")
        pairs = [(base["step_ids"][0], cand["step_ids"][0])]

    details = []
    counts = {"confirmed": 0, "suspected": 0, "unproven": 0}
    too_long_seen = False
    splits = 0
    for b_id, c_id in pairs:
        gaps = analyse_step(
            _relative(base["steps"][b_id], base["step_starts"].get(b_id, 0.0)),
            _relative(cand["steps"][c_id], cand["step_starts"].get(c_id, 0.0)),
            tol, base["fused_ops"])
        for g in gaps:
            if "序列过长" in (g.get("reason") or ""):
                too_long_seen = True
            if g["kind"] == "split":
                splits += 1
            elif g["kind"] == "merge":
                counts[g["level"]] += 1
            details.append({"step": b_id, **g})

    # 方向性指标与噪声只用共同配对步骤；候选多采的步骤单列说明，不参与均值。
    paired_base_ids = [b_id for b_id, _c in pairs]
    paired_cand_ids = [c_id for _b, c_id in pairs]
    n_pair = max(1, len(pairs))

    b_count = sum(len(base["steps"][s]) for s in paired_base_ids)
    c_count = sum(len(cand["steps"][s]) for s in paired_cand_ids)
    b_time = sum(r["dur"] for s in paired_base_ids for r in base["steps"][s]) / 1000.0
    c_time = sum(r["dur"] for s in paired_cand_ids for r in cand["steps"][s]) / 1000.0
    nb = nc = n_pair

    b_series = [sum(r["dur"] for r in base["steps"][s]) / 1000.0 for s in paired_base_ids]
    c_series = [sum(r["dur"] for r in cand["steps"][s]) / 1000.0 for s in paired_cand_ids]
    # 时长类指标：噪声带含 --min-delta-ms 下限；计数类无 ms 单位下限。
    noise = max(min_delta_ms, mad(b_series) + mad(c_series))

    b_cnt_series = [len(base["steps"][s]) for s in paired_base_ids]
    c_cnt_series = [len(cand["steps"][s]) for s in paired_cand_ids]
    count_noise = max(0.0, mad(b_cnt_series) + mad(c_cnt_series))

    if too_long_seen:
        notes.append("部分 step kernel 数超过 MAX_SEQ，已放弃 LCS，相关缺口全部标 unproven（仅比较数量与时长）")
    unpaired_base = [s for s in base["step_ids"] if s not in cand["steps"]]
    unpaired_cand = [s for s in cand["step_ids"] if s not in base["steps"]]
    if unpaired_base or unpaired_cand:
        notes.append(
            f"未配对 step 单列、不计入方向性指标与噪声：base={unpaired_base}, "
            f"cand={unpaired_cand}"
        )

    metrics = [
        _metric("fusion_op_count", "count", b_count / nb, c_count / nc, count_noise, "info"),
        _metric("fusion_kernel_ms", "ms", b_time / nb, c_time / nc, noise, None),
        _metric_pair("fusion_merge_confirmed", "count", counts["confirmed"]),
        _metric_pair("fusion_merge_suspected", "count", counts["suspected"]),
        _metric_pair("fusion_merge_unproven", "count", counts["unproven"]),
        _metric("fusion_attribution_kernel_ms", "ms", b_time / nb, c_time / nc, noise, "info"),
        _metric("fusion_attribution_count", "count", b_count / nb, c_count / nc,
                count_noise, "info"),
    ]

    if counts["confirmed"] or counts["suspected"]:
        summary = (f"检测到合并证据 confirmed {counts['confirmed']} / suspected {counts['suspected']}；"
                   f"每步算子 {b_count / nb:.2f} → {c_count / nc:.2f}，"
                   f"kernel 时长 {b_time / nb:.3f} → {c_time / nc:.3f}ms")
    elif splits:
        summary = f"未发现融合证据（检测到 {splits} 处拆分）；每步算子 {b_count / nb:.2f} → {c_count / nc:.2f}"
    else:
        summary = "无法证明融合：未检测到 N→1 合并证据"
    if splits:
        notes.append(f"检测到 {splits} 处拆分（1→N），不计入融合收益")
    notes.append(
        f"合并证据计数（对级，非逐侧）：confirmed {counts['confirmed']} / "
        f"suspected {counts['suspected']} / unproven {counts['unproven']}"
    )
    notes.append("间隙 Δ 依赖 host 下发数据（本批 unsupported），收益归因只含 kernel 时长与算子数量")

    per_step = []
    for b_id, c_id in pairs:
        b_ms = sum(r["dur"] for r in base["steps"][b_id]) / 1000.0
        c_ms = sum(r["dur"] for r in cand["steps"][c_id]) / 1000.0
        bc, cc = len(base["steps"][b_id]), len(cand["steps"][c_id])
        per_step.append({"step": b_id, "cand_step": c_id,
                         "base": bc, "cand": cc, "delta": bc - cc,
                         "base_time_ms": round4(b_ms), "cand_time_ms": round4(c_ms)})

    return {"metrics": metrics, "per_step": per_step, "summary": summary,
            "details": details, "counts": counts, "splits": splits, "notes": notes}


def _metric_pair(name, unit, value):
    return {"name": name, "unit": unit, "base": None, "cand": value,
            "delta": None, "delta_pct": None, "verdict": "info", "noise_band": 0.0,
            "evidence": "对级计数：合并/拆分证据不是逐侧指标，base 固定为 null"}


def _metric(name, unit, base, cand, noise, forced):
    delta = base - cand
    if forced:
        verdict = forced
    elif delta > noise:
        verdict = "improved"
    elif delta < -noise:
        verdict = "regressed"
    else:
        verdict = "noise"
    return {"name": name, "unit": unit, "base": round4(base), "cand": round4(cand),
            "delta": round4(delta),
            "delta_pct": round4((delta / base * 100.0) if base else 0.0, 2),
            "verdict": verdict, "noise_band": round4(noise),
            "evidence": "kernel_details.csv / trace_view.json（见 data_quality.sidecars）"}


def envelope(base, cand, result, notes_b, notes_c, deg_b, deg_c):
    degraded = [f"base: {x}" for x in deg_b] + [f"cand: {x}" for x in deg_c]
    notes = [f"base: {x}" for x in notes_b] + [f"cand: {x}" for x in notes_c] + result["notes"]
    if any(("ProfilerStep" in x) or ("Step Id" in x) for x in degraded):
        confidence = "low"
    elif degraded:
        confidence = "medium"
    else:
        confidence = "high"
    return {
        "skill": "kptools-ascend-profiling-compare-fusion",
        "schema_version": SCHEMA_VERSION,
        "mode": "pair",
        "inputs": {"base": base["path"], "cand": cand["path"],
                   "steps": f"per_step={len(result['per_step'])}"},
        "data_quality": {"sidecars": sorted({base["source"], cand["source"]}),
                         "degraded": degraded, "confidence": confidence},
        "metrics": result["metrics"],
        "per_step": result["per_step"],
        "summary": result["summary"],
        "notes": notes,
    }


def validate(env):
    """A.1 信封自检，口径与 compare/scripts/envelope.py 的 validate 一致。"""
    errs = []
    for k in ("skill", "schema_version", "mode", "inputs", "data_quality",
              "metrics", "per_step", "summary", "notes"):
        if k not in env:
            errs.append(f"缺字段 {k}")
    if env.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version 应为 {SCHEMA_VERSION}")
    if env.get("mode") not in ("pair", "single"):
        errs.append("mode 非法")
    dq = env.get("data_quality") or {}
    for k in ("sidecars", "degraded", "confidence"):
        if k not in dq:
            errs.append(f"data_quality 缺 {k}")
    if dq.get("confidence") not in ("high", "medium", "low"):
        errs.append("confidence 非法")
    if not isinstance(dq.get("sidecars"), list):
        errs.append("sidecars 非数组")
    if not isinstance(dq.get("degraded"), list):
        errs.append("degraded 非数组")
    metrics = env.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        errs.append("metrics 必须是非空数组")
        metrics = metrics if isinstance(metrics, list) else []
    for i, m in enumerate(metrics):
        if not isinstance(m, dict):
            errs.append(f"metrics[{i}] 非对象")
            continue
        for k in ("name", "unit", "base", "cand", "delta", "delta_pct",
                  "verdict", "noise_band", "evidence"):
            if k not in m:
                errs.append(f"metrics[{i}] 缺 {k}")
        if m.get("verdict") not in ("improved", "regressed", "noise", "info"):
            errs.append(f"metrics[{i}].verdict 非法")
        if not m.get("evidence"):
            errs.append(f"metrics[{i}].evidence 不能为空")
    if not isinstance(env.get("per_step"), list):
        errs.append("per_step 非数组")
    if not isinstance(env.get("summary"), str) or not env.get("summary"):
        errs.append("summary 必须是非空字符串")
    if not isinstance(env.get("notes"), list):
        errs.append("notes 非数组")
    return errs


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _cls(v):
    return {"improved": "v-dev", "regressed": "v-host"}.get(v, "v-mid")


def _m(result, name):
    return next(m for m in result["metrics"] if m["name"] == name)


def _fmt(value, digits=4):
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return esc(value)


def render_html(env, result, css_path):
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    rows = "".join(
        f"<tr><td>{esc(m['name'])}</td><td>{_fmt(m['base'])}</td><td>{_fmt(m['cand'])}</td>"
        f"<td>{'—' if m['delta'] is None else format(m['delta'], '+.3f')}</td>"
        f"<td><span class='verdict {_cls(m['verdict'])}'>"
        f"{esc(m['verdict'])}</span></td><td>±{m['noise_band']}</td></tr>"
        for m in env["metrics"]
    )
    table = ("<div class='sect'><h3>指标对比</h3><table><tr><th>指标</th><th>base</th>"
             "<th>cand</th><th>Δ</th><th>判定</th><th>噪声带</th></tr>" + rows + "</table></div>")

    ev_rows = "".join(
        f"<tr><td>step{d['step']}</td><td>{esc(' + '.join(d['old']) or '(无)')}</td>"
        f"<td>{esc(' + '.join(d['new']) or '(无)')}</td><td>{esc(d['kind'])}</td>"
        f"<td><b>{esc(d['level'])}</b></td><td>{esc(d['reason'])}</td></tr>"
        for d in result["details"] if d["kind"] in ("merge", "split")
    )
    ev_html = ("<div class='sect'><h3>合并/拆分证据</h3>"
               + (f"<table><tr><th>step</th><th>旧算子</th><th>新算子</th><th>类型</th>"
                  f"<th>证据等级</th><th>依据</th></tr>{ev_rows}</table>"
                  if ev_rows else "<div class='note'>未检测到合并或拆分缺口。</div>")
               + "</div>")

    attribution = (
        "<div class='sect'><h3>收益归因</h3><table>"
        "<tr><th>分量</th><th>数值</th></tr>"
        f"<tr><td>kernel 时长差分量</td><td>{_m(result, 'fusion_attribution_kernel_ms')['delta']:+.3f} ms/step</td></tr>"
        f"<tr><td>算子数量差分量</td><td>{_m(result, 'fusion_attribution_count')['delta']:+.2f} 个/step</td></tr>"
        "<tr><td>间隙 Δ</td><td><b>unsupported</b>（依赖 host 下发数据，本批不做）</td></tr>"
        "</table><div class='note'>正值表示候选侧更省。间隙变化无法在本批可靠归因，"
        "因此不计入收益。</div></div>"
    )

    notes_html = ("<div class='sect'><h3>降级说明与口径</h3><ul>"
                  + "".join(f"<li>{esc(x)}</li>" for x in (env["data_quality"]["degraded"] + env["notes"]))
                  + "</ul></div>")
    hero = ("<div class='hero'>"
            f"<div class='htitle'>融合前后对比 · <span class='judge'>{esc(env['summary'])}</span></div>"
            f"<div class='hnums'>base {esc(env['inputs']['base'])}<span class='sep'>→</span>"
            f"cand {esc(env['inputs']['cand'])}</div>"
            f"<div class='kv'>契约置信度: {esc(env['data_quality']['confidence'])}</div></div>")
    return ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'><title>融合对比</title>"
            f"<style>{css}</style></head><body><div class='page on'><h1>算子融合前后对比</h1>"
            f"{hero}{table}{ev_html}{attribution}{notes_html}</div></body></html>")


def render_error(msg, base_path, cand_path, css_path):
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    return ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'><title>融合对比失败</title>"
            f"<style>{css}</style></head><body><div class='page on'><h1>融合对比失败</h1>"
            f"<div class='hero'><div class='htitle'>无法完成对比</div>"
            f"<div class='hnums'>{esc(msg)}</div></div>"
            f"<div class='kv'>base: {esc(base_path)}</div><div class='kv'>cand: {esc(cand_path)}</div>"
            "</div></body></html>")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="两份 Ascend profiling 的算子融合前后对比")
    ap.add_argument("base")
    ap.add_argument("cand")
    ap.add_argument("--out-html", default="fusion_report.html")
    ap.add_argument("--json", dest="out_json", default=None)
    ap.add_argument("--fused-ops", default=None, help="每行一个融合算子名")
    ap.add_argument("--window-tol-ms", type=float, default=0.05)
    ap.add_argument("--min-delta-ms", type=float, default=0.05)
    args = ap.parse_args(argv)

    fused_ops = set()
    if args.fused_ops:
        try:
            fused_ops = {l.strip() for l in Path(args.fused_ops).read_text(
                encoding="utf-8", errors="replace").splitlines() if l.strip()}
        except OSError as exc:
            print(f"警告：读取 --fused-ops 失败: {exc}", file=sys.stderr)

    css_path = Path(__file__).resolve().parent.parent / "assets" / "report.css"
    nb, nc, db, dc = [], [], [], []
    try:
        base = load_capture(args.base, fused_ops, nb, db)
        cand = load_capture(args.cand, fused_ops, nc, dc)
        result = compare_captures(base, cand, args.window_tol_ms * 1000.0, args.min_delta_ms)
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
    for m in env["metrics"][:3]:
        base = "—" if m["base"] is None else m["base"]
        cand = "—" if m["cand"] is None else m["cand"]
        delta = "—" if m["delta"] is None else format(m["delta"], "+.3f")
        print(f"  {m['name']:<30} base {base:>10} cand {cand:>10} "
              f"Δ {delta} {m['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
