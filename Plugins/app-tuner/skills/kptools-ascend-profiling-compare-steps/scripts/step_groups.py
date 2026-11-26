#!/usr/bin/env python3
"""同一份 Ascend profiling 内的 step / step 组对比（kptools-ascend-profiling-compare-steps）。

只回答「同一份采集里不同 step 组谁快谁慢、坏 step 在哪」；跨采集对比归
kptools-ascend-profiling-compare（v1），切 step 归 kptools-ascend-profiling-step-split。

自包含实现：仅使用 python3 标准库，不 import 其它 skill 的脚本。
采集定位与侧车读取、MAD 噪声口径复制并改写自：
  kptools-ascend-profiling-compare/scripts/kpc_core.py

用法:
  step_groups.py CAPTURE --out-html r.html --json r.json \
    [--base-steps 0,1,2 --cand-steps 5,6,7 | --base-step 3 --cand-step 7] \
    [--skip-warmup K] [--min-delta-ms 0.05]
"""

import argparse
import csv
import json
import html
import os
import re
import sqlite3
import sys
import traceback
from pathlib import Path

SCHEMA_VERSION = 1
SKILL_NAME = "kptools-ascend-profiling-compare-steps"

MIN_DELTA_DEFAULT = 0.05

# (内部 key, 指标名, 中文名)
METRIC_DEFS = (
    ("step_time", "group_step_time_ms", "步耗时"),
    ("comp", "group_computing_ms", "Computing"),
    ("comm_no", "group_communication_ms", "Communication(未重叠)"),
    ("free", "group_free_ms", "Free"),
    ("prep", "group_preparing_ms", "Preparing"),
)

VERDICT_LABELS = {
    "improved": "改善",
    "regressed": "恶化",
    "noise": "在噪声内",
    "info": "信息",
}


class SelectionError(Exception):
    """step 选择错误（退出码 2）。"""


# ---------------------------------------------------------------------------
# 通用工具（复制自 kpc_core.py）
# ---------------------------------------------------------------------------
def _round(x, n=4):
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return 0.0


def _median(xs):
    xs = sorted(float(x) for x in xs if x is not None)
    if not xs:
        return 0.0
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _mad(xs):
    if not xs:
        return 0.0
    m = _median(xs)
    return _median([abs(x - m) for x in xs])


def _verdict(delta, noise):
    if delta > noise:
        return "improved"
    if delta < -noise:
        return "regressed"
    return "noise"


# ---------------------------------------------------------------------------
# 采集定位（复制自 kpc_core.py）
# ---------------------------------------------------------------------------
def locate_capture(path):
    """返回 (output_dir, profiler_root, trace_path)。"""
    p = Path(path).expanduser().resolve()
    if p.is_file():
        if p.name == "trace_view.json":
            return p.parent, p.parent.parent, p
        return p.parent, p.parent.parent, p.parent / "trace_view.json"
    if not p.exists():
        raise FileNotFoundError(f"输入不存在: {p}")

    direct = p / "ASCEND_PROFILER_OUTPUT"
    if direct.is_dir():
        return direct, p, direct / "trace_view.json"
    if (p / "analysis.db").exists() or (p / "step_trace_time.csv").exists() or (
        p / "trace_view.json"
    ).exists():
        return p, p.parent, p / "trace_view.json"

    outs = sorted(p.rglob("ASCEND_PROFILER_OUTPUT"), key=lambda x: len(x.parts))
    if outs:
        return outs[0], outs[0].parent, outs[0] / "trace_view.json"
    traces = sorted(p.rglob("trace_view.json"), key=lambda x: len(x.parts))
    if traces:
        return traces[0].parent, traces[0].parent.parent, traces[0]
    return p, p.parent, p / "trace_view.json"


# ---------------------------------------------------------------------------
# 侧车读取
# ---------------------------------------------------------------------------
_STEP_COLUMNS = {
    "step": ("step", "stepid"),
    "comp": ("computing",),
    "comm_no": ("communicationnotoverlapped", "communication(notoverlapped)",
                "communication_not_overlapped"),
    "comm": ("communication",),
    "free": ("free",),
    "stage": ("stage", "steptime"),
    "prep": ("preparing", "prepare"),
}


def _norm_key(name):
    return re.sub(r"[\s_]+", "", str(name or "")).lower()


def _pick_column(columns, key):
    aliases = {_norm_key(a) for a in _STEP_COLUMNS[key]}
    for name in columns:
        if _norm_key(name) in aliases:
            return name
    return None


def _pick_device_column(columns):
    for name in columns:
        if _norm_key(name) in ("deviceid", "device"):
            return name
    return None


def _accumulate_step(rows, sid, vals, device):
    """同一 step 的多 device 行：Computing/Communication 求和，Stage/Preparing 取 max。

    Free 在 finalize 阶段对多 device 重新计算（= max Stage − ΣComputing − ΣComm(未重叠)）。
    """
    default = {k: 0.0 for k in _STEP_COLUMNS if k != "step"}
    default["_devices"] = set()
    slot = rows.setdefault(sid, default)
    slot["comp"] += vals.get("comp", 0.0)
    slot["comm_no"] += vals.get("comm_no", 0.0)
    slot["comm"] += vals.get("comm", 0.0)
    slot["free"] += vals.get("free", 0.0)
    slot["stage"] = max(slot["stage"], vals.get("stage", 0.0))
    slot["prep"] = max(slot["prep"], vals.get("prep", 0.0))
    slot["_devices"].add(str(device))


def _finalize_steps(rows, notes):
    multi = 0
    for slot in rows.values():
        devices = slot.pop("_devices", {"0"})
        if len(devices) > 1:
            # Free 与 comp/comm 同口径：逐 device free 求和，保证可加性。
            multi += 1
    if multi:
        notes.append(
            f"{multi} 个 step 含多个 Device_id：Computing/Communication/Free 按 device 求和，"
            "Stage/Preparing 取 max；Free 不再用 max(stage) 减跨 device 求和"
        )


_FOUR_CLASS_KEYS = ("comp", "comm_no", "free", "prep")


def _read_step_metrics(output_dir):
    """读取 analysis.db:StepTraceTime 或 step_trace_time.csv（µs）。

    返回 (rows, source, notes, available)。rows: {step_id: {key: µs}}；
    available 逐指标记录该侧车是否真的存在对应列（缺列不得当作实测 0）。
    """
    notes = []
    rows, source, available = {}, None, {}
    keys = [k for k in _STEP_COLUMNS if k != "step"]

    def _mark(picks):
        state = {key: bool(picks.get(key)) for key in keys}
        missing = [key for key in _FOUR_CLASS_KEYS if not state.get(key)]
        if missing:
            notes.append(
                "缺少列 " + ", ".join(missing) + "：对应指标不可用（不参与判定，展示值记 0）"
            )
        return state

    adb = output_dir / "analysis.db"
    if adb.exists():
        try:
            con = sqlite3.connect(f"file:{adb}?mode=ro", uri=True)
            exists = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='StepTraceTime'"
            ).fetchone()
            columns = [
                r[1] for r in con.execute("PRAGMA table_info(StepTraceTime)").fetchall()
            ] if exists else []
            if columns:
                picks = {key: _pick_column(columns, key) for key in _STEP_COLUMNS}
                dev_col = _pick_device_column(columns)
                if picks["step"]:
                    sel = [f'"{picks["step"]}"']
                    if dev_col:
                        sel.append(f'"{dev_col}"')
                    sel.extend(f'"{picks[k]}"' if picks[k] else "0" for k in keys)
                    data = con.execute(
                        "SELECT " + ", ".join(sel) + " FROM StepTraceTime"
                    ).fetchall()
                    base_idx = 2 if dev_col else 1
                    for row in data:
                        try:
                            sid = int(row[0])
                        except (TypeError, ValueError):
                            continue
                        device = str(row[1]) if dev_col else "0"
                        vals = {}
                        for idx, key in enumerate(keys):
                            try:
                                vals[key] = float(row[base_idx + idx] or 0)
                            except (TypeError, ValueError):
                                vals[key] = 0.0
                        _accumulate_step(rows, sid, vals, device)
                    source = "analysis.db:StepTraceTime"
                    available = _mark(picks)
            con.close()
        except Exception as exc:
            notes.append(f"读取 analysis.db 失败: {exc}")
            rows = {}

    if not rows:
        csv_path = output_dir / "step_trace_time.csv"
        if csv_path.exists():
            with open(csv_path, encoding="utf-8", errors="replace") as fh:
                reader = csv.DictReader(fh)
                columns = reader.fieldnames or []
                picks = {key: _pick_column(columns, key) for key in _STEP_COLUMNS}
                dev_col = _pick_device_column(columns)
                if picks["step"]:
                    for r in reader:
                        try:
                            sid = int(str(r.get(picks["step"]) or "").strip())
                        except (TypeError, ValueError):
                            continue
                        device = str(r.get(dev_col) or "0").strip() if dev_col else "0"
                        vals = {}
                        for key in keys:
                            col = picks[key]
                            try:
                                vals[key] = float(r.get(col) or 0) if col else 0.0
                            except (TypeError, ValueError):
                                vals[key] = 0.0
                        _accumulate_step(rows, sid, vals, device)
                    source = "step_trace_time.csv"
                    available = _mark(picks)
    if rows:
        _finalize_steps(rows, notes)
    return rows, source, notes, available


def _read_step_time_db(output_dir):
    """读取 ascend_pytorch_profiler_*.db:STEP_TIME 绝对边界（ns）。"""
    best, best_db = {}, None
    for db in sorted(output_dir.glob("ascend_pytorch_profiler_*.db")):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute("SELECT id,startNs,endNs FROM STEP_TIME ORDER BY id").fetchall()
            con.close()
            rows = [(int(i), int(s), int(e)) for i, s, e in rows if e > s]
        except Exception:
            continue
        if len(rows) > len(best):
            best = {i: (s, e) for i, s, e in rows}
            best_db = db.name
    return best, best_db


# ---------------------------------------------------------------------------
# 采集加载
# ---------------------------------------------------------------------------
def load_capture(path):
    output_dir, root, trace_path = locate_capture(path)
    sidecars = []
    degradations = []
    notes = []

    rows, source, read_notes, available = _read_step_metrics(output_dir)
    notes.extend(read_notes)
    if source:
        sidecars.append(source.split(":")[0])

    step_time, step_db = _read_step_time_db(output_dir)
    if step_db:
        sidecars.append(step_db)

    steps = []
    step_time_source = None
    span_used = 0
    span_mismatch = 0
    if rows or step_time:
        bounds_map = dict(step_time)
        if rows and step_time and set(step_time) != set(rows) and len(step_time) == len(rows):
            # id 错位（如 STEP_TIME 0..N-1 对侧车 1..N）：按顺序配对，并显式降级。
            bounds_map = {sid: step_time[tid] for sid, tid in zip(sorted(rows), sorted(step_time))}
            degradations.append("STEP_TIME id 与侧车 step id 不一致，已按顺序配对")
        # 侧车与 STEP_TIME 的 step id 取并集：仅有 STEP_TIME 跨度的步也可参与步耗时比较。
        for sid in sorted(set(rows) | set(bounds_map)):
            m = rows.get(sid)
            bounds = bounds_map.get(sid)
            if m is None:
                if not bounds:
                    continue
                dur = (bounds[1] - bounds[0]) / 1000.0
                span_used += 1
                steps.append(
                    {
                        "id": sid,
                        "step_time": _round(dur / 1000.0),
                        "comp": 0.0,
                        "comm_no": 0.0,
                        "comm": 0.0,
                        "free": 0.0,
                        "prep": 0.0,
                        "sidecar": False,
                    }
                )
                continue
            stage = m.get("stage", 0.0)
            stage_plus_prep = stage + m.get("prep", 0.0) if stage > 0 else 0.0
            if bounds:
                # STEP_TIME 是权威 step 跨度（≈ stage+prep），也避免 CSV 的 Stage/Free
                # 与 analysis.db 口径不同导致同一采集因侧车不同而结论不同。
                dur = (bounds[1] - bounds[0]) / 1000.0
                span_used += 1
                if stage_plus_prep > 0 and abs(stage_plus_prep - dur) / dur > 0.02:
                    span_mismatch += 1
            else:
                dur = stage_plus_prep
            if dur <= 0:
                dur = m.get("comp", 0.0) + m.get("comm_no", 0.0) + m.get("free", 0.0) + m.get(
                    "prep", 0.0
                )
            steps.append(
                {
                    "id": sid,
                    "step_time": _round(dur / 1000.0),
                    "comp": _round(m.get("comp", 0.0) / 1000.0),
                    "comm_no": _round(m.get("comm_no", 0.0) / 1000.0),
                    "comm": _round(m.get("comm", 0.0) / 1000.0),
                    "free": _round(m.get("free", 0.0) / 1000.0),
                    "prep": _round(m.get("prep", 0.0) / 1000.0),
                    "sidecar": True,
                }
            )
        if span_used:
            step_time_source = "ascend_pytorch_profiler_*.db:STEP_TIME[startNs,endNs]"
            if span_used < len(steps):
                step_time_source += "（部分步缺失，缺步用 stage+preparing）"
            if span_mismatch:
                notes.append(
                    f"{span_mismatch} 步的 stage+preparing 与 STEP_TIME 跨度差异 >2%："
                    "步耗时以 STEP_TIME 为准，分量仍来自侧车"
                )
        if not rows:
            degradations.append(
                "缺少 StepTraceTime/step_trace_time.csv，仅 STEP_TIME 步耗时；四分类缺失"
            )
        elif not all(available.get(k) for k in _FOUR_CLASS_KEYS):
            degradations.append(
                "四分类不完整：StepTraceTime/CSV 缺少 Computing/Communication(未重叠)/Free/Preparing 中至少一列"
            )
    else:
        degradations.append("无可用 step 数据（缺 StepTraceTime / step_trace_time.csv / STEP_TIME）")

    four_class = bool(rows) and all(available.get(k) for k in _FOUR_CLASS_KEYS)
    metric_available = {
        "step_time": bool(steps),
        "comp": bool(available.get("comp")),
        "comm_no": bool(available.get("comm_no")),
        "free": bool(available.get("free")),
        "prep": bool(available.get("prep")),
    }

    if Path(trace_path).exists():
        sidecars.append(Path(trace_path).name)
    for info in sorted(output_dir.glob("profiler_info_*.json")) + sorted(
        root.glob("profiler_info_*.json")
    ):
        sidecars.append(info.name)
        break

    confidence = "high" if (steps and four_class) else "low"
    if not steps:
        notes.append("无可用 step 数据，无法计算组指标；不伪造数字")

    return {
        "path": str(output_dir),
        "sidecars": list(dict.fromkeys(sidecars)),
        "steps": steps,
        "step_source": source,
        "step_time_source": step_time_source,
        "four_class": four_class,
        "available": metric_available,
        "confidence": confidence,
        "notes": notes,
        "degradations": degradations,
    }


# ---------------------------------------------------------------------------
# step 组选择与对比
# ---------------------------------------------------------------------------
def _parse_ids(text):
    if text is None:
        return None
    parts = [x for x in re.split(r"[,\s]+", str(text).strip()) if x]
    if not parts:
        return None
    try:
        return [int(x) for x in parts]
    except ValueError:
        raise SelectionError(f"step id 必须是整数: {text}")


def _ids_label(ids):
    ids = list(ids)
    if not ids:
        return "—"
    if len(ids) > 1 and all(ids[i + 1] == ids[i] + 1 for i in range(len(ids) - 1)):
        return f"step{ids[0]}-{ids[-1]}"
    return "step" + ",".join(str(i) for i in ids)


def _available_text(cap):
    ids = [s["id"] for s in cap["steps"]]
    if not ids:
        return "无可用 step id"
    return f"可用 step id: [{', '.join(str(i) for i in ids)}]"


def select_groups(cap, base_spec, cand_spec, skip_warmup=0):
    """返回 (base_steps, cand_steps, notes)。选择错误抛 SelectionError。"""
    ids = [s["id"] for s in cap["steps"]]
    if not ids:
        return [], [], []
    available = set(ids)

    if base_spec is None and cand_spec is None:
        if len(ids) < 2:
            raise SelectionError("可用 step 少于 2，无法做组对比；请显式指定两组 step")
        mid = max(1, len(ids) // 2)
        base_spec = ids[:mid]
        cand_spec = ids[mid:] or ids[-1:]
        auto = True
    else:
        auto = False

    for spec in (base_spec, cand_spec):
        missing = [i for i in spec if i not in available]
        if missing:
            raise SelectionError(
                f"指定的 step id 不存在: {missing}；{_available_text(cap)}"
            )

    base_ids = list(base_spec)[skip_warmup:]
    cand_ids = list(cand_spec)[skip_warmup:]
    if not base_ids or not cand_ids:
        raise SelectionError(
            f"--skip-warmup {skip_warmup} 后 step 组为空；{_available_text(cap)}"
        )

    notes = []
    if auto:
        notes.append("未指定 step 组，默认按 step 顺序前后对半切分")
    notes.append(f"基线组 {_ids_label(base_ids)}（{len(base_ids)} 步）；候选组 {_ids_label(cand_ids)}（{len(cand_ids)} 步）")
    notes.append("配对方式：两组内按位置配对（第 i 个基线 step ↔ 第 i 个候选 step）")
    if len(base_ids) != len(cand_ids):
        notes.append(
            f"两组步数不同（基线 {len(base_ids)} / 候选 {len(cand_ids)}），只比较前 "
            f"{min(len(base_ids), len(cand_ids))} 对"
        )
    if skip_warmup:
        notes.append(f"两组各自开头已去掉 {skip_warmup} 个 step（--skip-warmup）")
    if min(len(base_ids), len(cand_ids)) < 3:
        notes.append("组大小 < 3，MAD 不可靠，判定主要由 --min-delta-ms 决定")
    return base_ids, cand_ids, notes


def _metric_block(b_steps, c_steps, key, min_delta_ms, min_delta_pct=1.0):
    # 先在共同配对样本上截齐，再算展示值与方向判定，避免「只比首对、摘要取全量」自相矛盾。
    n = min(len(b_steps), len(c_steps))
    b = [float(s[key]) for s in b_steps[:n]]
    c = [float(s[key]) for s in c_steps[:n]]
    bm, cm = _median(b), _median(c)
    delta = bm - cm
    paired = [b[i] - c[i] for i in range(n)]
    noise = max(min_delta_ms, abs(bm) * min_delta_pct / 100.0, _mad(paired))
    pos = sum(1 for d in paired if d > 1e-9)
    neg = sum(1 for d in paired if d < -1e-9)
    consistency = (max(pos, neg) / (pos + neg)) if (pos + neg) else 1.0
    if pos + neg >= 2 and consistency < 0.75:
        verdict = "info"
    else:
        verdict = _verdict(_median(paired) if paired else delta, noise)
    return {
        "base": _round(bm),
        "cand": _round(cm),
        "delta": _round(delta),
        "delta_pct": _round(delta / bm * 100.0, 2) if bm else 0.0,
        "noise": _round(noise),
        "verdict": verdict,
        "sign_consistency": _round(consistency, 3),
    }


def compare_groups(cap, base_ids, cand_ids, min_delta_ms=MIN_DELTA_DEFAULT):
    if not cap["steps"]:
        metrics, per_step, summary = [], [], "无可用 step 数据"
        notes = list(cap["notes"])
        notes.append("未计算任何组指标（无 step 数据）")
        return _result(cap, metrics, per_step, summary, notes, {"base": [], "cand": []})

    by_id = {s["id"]: s for s in cap["steps"]}
    b_steps = [by_id[i] for i in base_ids]
    c_steps = [by_id[i] for i in cand_ids]
    n = min(len(b_steps), len(c_steps))

    available = cap.get("available") or {}
    metrics = []
    for key, name, _label in METRIC_DEFS:
        m = _metric_block(b_steps, c_steps, key, min_delta_ms)
        # 逐指标判断可用性：缺列 / 缺该步侧车行时不给 noise，否则会被读成“无变化”。
        usable = bool(available.get(key, False))
        if key != "step_time":
            no_sidecar = [s["id"] for s in b_steps + c_steps if not s.get("sidecar", True)]
            if no_sidecar:
                usable = False
                m = dict(m)
                m["verdict"] = "info"
        # 四分类整体不可用时（STEP_TIME-only）也不下方向结论。
        verdict = m["verdict"] if usable else "info"
        evidence = _evidence(cap, key)
        if not usable:
            evidence += "；该指标不可用：缺少对应列或该步无侧车数据，展示值记 0 且不参与判定"
        metrics.append(
            {
                "name": name,
                "unit": "ms",
                "base": m["base"],
                "cand": m["cand"],
                "delta": m["delta"],
                "delta_pct": m["delta_pct"],
                "verdict": verdict,
                "noise_band": m["noise"],
                "evidence": evidence,
            }
        )

    pairs = []
    for i in range(n):
        b, c = b_steps[i], c_steps[i]
        pairs.append(
            {
                "step": b["id"],
                "cand_step": c["id"],
                "base": b["step_time"],
                "cand": c["step_time"],
                "delta": _round(b["step_time"] - c["step_time"]),
            }
        )
    worst = min(pairs, key=lambda p: p["delta"]) if pairs else None
    if worst and worst["delta"] < 0:
        worst["worst"] = True
    else:
        worst = None

    step_time_metric = metrics[0]
    verdict_zh = VERDICT_LABELS.get(step_time_metric["verdict"], step_time_metric["verdict"])
    summary = (
        f"基线组 {_ids_label(base_ids)}（{len(base_ids)} 步）vs 候选组 "
        f"{_ids_label(cand_ids)}（{len(cand_ids)} 步）：步耗时中位数 "
        f"{step_time_metric['base']:.3f}ms → {step_time_metric['cand']:.3f}ms"
        f"（Δ {step_time_metric['delta']:+.3f}ms，{verdict_zh}）"
    )
    if worst and worst["delta"] < 0:
        summary += (
            f"；最大劣化 step{worst['cand_step']} 比 step{worst['step']} "
            f"慢 {abs(worst['delta']):.3f}ms"
        )

    notes = list(cap["notes"])
    notes.append("Δ = base − cand，正值表示候选组更快/更省")
    return _result(cap, metrics, pairs, summary, notes, {
        "base": base_ids,
        "cand": cand_ids,
    }, worst=worst, min_delta_ms=min_delta_ms, base_steps=b_steps,
        cand_steps=c_steps)


def _evidence(cap, key):
    if key == "step_time":
        if cap.get("step_time_source"):
            return cap["step_time_source"]
        if not cap["step_source"]:
            return "四分类缺失（STEP_TIME-only），该项记 0"
        return f"{cap['step_source']}:stage+preparing"
    if not cap["step_source"]:
        return "四分类缺失（STEP_TIME-only），该项记 0"
    source = cap["step_source"]
    field = {
        "comp": "computing",
        "comm_no": "communication_not_overlapped",
        "free": "free",
        "prep": "preparing",
    }[key]
    return f"{source}:{field}"


def _result(cap, metrics, per_step, summary, notes, groups, worst=None,
            min_delta_ms=MIN_DELTA_DEFAULT, base_steps=None, cand_steps=None):
    degradations = list(cap["degradations"])
    if metrics:
        notes.append(
            f"噪声判定: noise = max(--min-delta-ms={min_delta_ms}, |base组中位数|×1%, MAD(逐对Δ))；"
            "展示与方向判定均在共同配对样本上计算；≥2 对且方向一致性<0.75 判 info"
        )
    return {
        "skill": SKILL_NAME,
        "schema_version": SCHEMA_VERSION,
        "mode": "single",
        "inputs": {
            "base": cap["path"],
            "cand": cap["path"],
            "steps": (
                f"base={groups.get('base')}; cand={groups.get('cand')}"
            ),
        },
        "data_quality": {
            "sidecars": cap["sidecars"],
            "degraded": degradations,
            "confidence": cap["confidence"] if metrics else "low",
        },
        "metrics": metrics,
        "per_step": per_step,
        "summary": summary,
        "notes": notes,
        "degradations": degradations,
        "groups": {
            "base_ids": groups.get("base") or [],
            "cand_ids": groups.get("cand") or [],
            "base_steps": base_steps or [],
            "cand_steps": cand_steps or [],
        },
        "worst": worst or {},
        "min_delta_ms": min_delta_ms,
    }


def validate(env):
    """A.1 信封自检（规则复制自 kpc_core/envelope.py 的 validate）。"""
    errs = []
    for key in (
        "skill",
        "schema_version",
        "mode",
        "inputs",
        "data_quality",
        "metrics",
        "per_step",
        "summary",
        "notes",
    ):
        if key not in env:
            errs.append(f"缺字段 {key}")
    if env.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version 应为 {SCHEMA_VERSION}")
    if env.get("mode") != "single":
        errs.append("mode 必须是 single")
    dq = env.get("data_quality") or {}
    for key in ("sidecars", "degraded", "confidence"):
        if key not in dq:
            errs.append(f"data_quality 缺字段 {key}")
    if dq.get("confidence") not in ("high", "medium", "low"):
        errs.append("data_quality.confidence 必须是 high | medium | low")
    for i, m in enumerate(env.get("metrics") or []):
        for key in (
            "name",
            "unit",
            "base",
            "cand",
            "delta",
            "delta_pct",
            "verdict",
            "noise_band",
            "evidence",
        ):
            if key not in m:
                errs.append(f"metrics[{i}] 缺字段 {key}")
        if m.get("verdict") not in ("improved", "regressed", "noise", "info"):
            errs.append(f"metrics[{i}].verdict 非法: {m.get('verdict')}")
        if not m.get("evidence"):
            errs.append(f"metrics[{i}].evidence 不能为空")
    if not isinstance(env.get("per_step"), list):
        errs.append("per_step 必须是数组")
    if not isinstance(env.get("summary"), str) or not env.get("summary"):
        errs.append("summary 必须是非空字符串")
    if not isinstance(env.get("notes"), list):
        errs.append("notes 必须是数组")
    return errs


# ---------------------------------------------------------------------------
# HTML 报告
# ---------------------------------------------------------------------------
CSS_PATH = Path(__file__).resolve().parent.parent / "assets" / "report.css"

EXTRA_CSS = """
.worst td{background:#FBEFEF !important}
.badge{display:inline-block;padding:1px 7px;border-radius:3px;background:#A65D57;
       color:#fff;font-size:11px;margin-left:6px}
.groupbox{display:flex;gap:12px;flex-wrap:wrap;margin:8px 0}
.groupcard{flex:1 1 260px;border:1px solid #E4E0D9;border-radius:8px;padding:10px 14px;
           background:#FAF8F5}
.groupcard b{color:#3B4750}
"""


def _esc(s):
    return html.escape(str(s), quote=True)


def _css():
    try:
        return CSS_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def _num(x, n=3):
    try:
        return f"{float(x):.{n}f}"
    except (TypeError, ValueError):
        return "—"


def _verdict_cls(v):
    return {"improved": "v-dev", "regressed": "v-host", "noise": "v-mid"}.get(v, "v-mid")


def _table(headers, rows, row_classes=None):
    row_classes = row_classes or {}
    out = ["<table><tr>" + "".join(f"<th>{_esc(h)}</th>" for h in headers) + "</tr>"]
    for idx, row in enumerate(rows):
        cls = row_classes.get(idx)
        attr = f' class="{cls}"' if cls else ""
        out.append(f"<tr{attr}>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
    out.append("</table>")
    return "".join(out)


def render_html(result):
    css = _css()
    metrics = result["metrics"]
    base_ids = result["groups"]["base_ids"]
    cand_ids = result["groups"]["cand_ids"]

    banner = ""
    if not metrics:
        banner = (
            '<div class="note" style="border-left:4px solid #B08968;padding-left:10px">'
            "无可用 step 数据：未计算任何组指标，也未伪造数字。请检查采集是否包含 "
            "analysis.db:StepTraceTime、step_trace_time.csv 或 ascend_pytorch_profiler_*.db:STEP_TIME。</div>"
        )

    hero = (
        '<div class="hero"><div class="htitle">同一份 profiling · step 组对比 · '
        '<span class="judge">单份模式</span></div>'
        f'<div class="hnums">{_esc(result["summary"])}</div>'
        f'<div class="barleg">噪声带规则: noise = max(--min-delta-ms={result["min_delta_ms"]}, '
        "|base中位数|×1%, MAD(逐对Δ))；≥2 对且方向一致性&lt;0.75 判 info</div></div>"
    )

    source = (
        f'<div class="kv">采集: {_esc(result["inputs"]["base"])}</div>'
        f'<div class="kv">契约置信度: {_esc(result["data_quality"]["confidence"])} · '
        f'侧车: {_esc(", ".join(result["data_quality"]["sidecars"]) or "无")}</div>'
    )

    groups = (
        '<div class="groupbox">'
        f'<div class="groupcard"><b>基线组</b><br>{_esc(_ids_label(base_ids))}'
        f"（{len(base_ids)} 步）</div>"
        f'<div class="groupcard"><b>候选组</b><br>{_esc(_ids_label(cand_ids))}'
        f"（{len(cand_ids)} 步）</div>"
        "</div>"
    )

    metric_rows = []
    for m in metrics:
        metric_rows.append(
            [
                _esc(m["name"]),
                _num(m["base"]),
                _num(m["cand"]),
                f'{m["delta"]:+.3f}',
                f'{m["delta_pct"]:+.1f}%',
                _num(m["noise_band"]),
                f'<span class="{_verdict_cls(m["verdict"])}">'
                f'{_esc(VERDICT_LABELS.get(m["verdict"], m["verdict"]))}</span>',
            ]
        )
    if metric_rows:
        metric_table = _table(
            ["指标", "base 中位数", "cand 中位数", "Δ(base−cand)", "变化", "噪声带", "判定"],
            metric_rows,
        )
    else:
        metric_table = '<div class="note">无组指标。</div>'

    pair_rows = []
    row_classes = {}
    worst_id = None
    if result.get("worst"):
        worst_id = result["worst"].get("cand_step")
    for idx, p in enumerate(result["per_step"]):
        is_worst = bool(p.get("worst"))
        if is_worst:
            row_classes[idx] = "worst"
        mark = '<span class="badge">最大劣化</span>' if is_worst else ""
        pair_rows.append(
            [
                f'step{p["step"]}',
                f'step{p["cand_step"]}{mark}',
                _num(p["base"]),
                _num(p["cand"]),
                f'{p["delta"]:+.3f}',
            ]
        )
    if pair_rows:
        pair_table = _table(
            ["基线 step", "候选 step", "base (ms)", "cand (ms)", "Δ(base−cand)"],
            pair_rows,
            row_classes,
        )
    else:
        pair_table = '<div class="note">无可配对的 step（组为空或只有一侧）。</div>'

    noise_rows = []
    for m in metrics:
        noise_rows.append(
            [_esc(m["name"]), _num(m["noise_band"]), _esc(m["evidence"])]
        )
    noise_html = (
        '<div class="sect"><h3>噪声带说明</h3>'
        f'<div class="kv">判定规则：先在共同配对样本上截齐，Δ = base组中位数 − cand组中位数；'
        f'noise = max(--min-delta-ms={result["min_delta_ms"]}, |base组中位数|×1%, MAD(逐对Δ))；'
        "|Δ| ≤ noise → 在噪声内，不下优化/退化结论；≥2 对且方向一致性&lt;0.75 判 info。"
        "组大小 &lt; 3 时 MAD 不可靠，判定主要由 --min-delta-ms 决定。</div>"
        + (_table(["指标", "噪声带 (ms)", "数据来源"], noise_rows) if noise_rows else "")
        + "</div>"
    )

    if result["degradations"]:
        deg_html = (
            '<div class="sect"><h3>降级说明</h3><ul>'
            + "".join(f"<li>{_esc(x)}</li>" for x in result["degradations"])
            + "</ul></div>"
        )
    else:
        deg_html = (
            '<div class="sect"><h3>降级说明</h3>'
            '<div class="note">无降级：读取到完整四分类数据（来源见侧车）。</div></div>'
        )

    notes_html = (
        '<div class="sect"><h3>口径与限制</h3><ul>'
        + "".join(f"<li>{_esc(x)}</li>" for x in result["notes"])
        + "</ul></div>"
    )

    worst_note = ""
    if worst_id is None and result["per_step"]:
        worst_note = (
            '<div class="note">逐 step 未出现候选劣化（Δ 均 ≥ 0），因此没有标记最大劣化步。</div>'
        )

    return (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        "<title>Ascend Profiling step 组对比</title>"
        f"<style>{css}{EXTRA_CSS}</style></head><body>"
        '<nav><button class="on">step 组对比</button></nav>'
        '<div class="page on" id="home"><h1>Profiling 单采集 · step 组对比</h1>'
        f"{banner}{hero}{source}{groups}"
        '<div class="sect"><h3>两组指标对比</h3>' + metric_table + "</div>"
        '<div class="sect"><h3>逐 step 配对</h3>' + pair_table + worst_note
        + '<div class="note">Δ = base − cand，正值表示候选 step 更快/更省。</div></div>'
        f"{noise_html}{deg_html}{notes_html}"
        "</div></body></html>"
    )


def render_error(capture_path, error, detail="", available=""):
    extra = f'<div class="kv">{_esc(available)}</div>' if available else ""
    return (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        "<title>Step 组对比失败</title>"
        f"<style>{_css()}{EXTRA_CSS}</style></head><body>"
        '<div class="page on" id="home"><h1>Step 组对比失败</h1>'
        '<div class="hero" style="border-left-color:#B08968">'
        '<div class="htitle">无法完成 step 组对比</div>'
        f'<div class="hnums">{_esc(error)}</div></div>'
        f'<div class="kv">采集: {_esc(capture_path)}</div>'
        f"{extra}"
        '<div class="note">排查建议：确认采集目录含 ASCEND_PROFILER_OUTPUT 与 '
        "analysis.db:StepTraceTime（或 step_trace_time.csv / STEP_TIME）；"
        "step id 需在可用列表内。</div>"
        f'<div class="sect"><h3>详细堆栈</h3><pre>{_esc(detail)}</pre></div>'
        "</div></body></html>"
    )


def error_envelope(capture_path, reason, detail="", summary="Step 组对比失败",
                   available=""):
    notes = [reason]
    if available:
        notes.append(available)
    if detail:
        notes.append(detail)
    return {
        "skill": SKILL_NAME,
        "schema_version": SCHEMA_VERSION,
        "mode": "single",
        "inputs": {"base": str(capture_path), "cand": str(capture_path), "steps": "n/a"},
        "data_quality": {
            "sidecars": [],
            "degraded": [reason],
            "confidence": "low",
        },
        "metrics": [],
        "per_step": [],
        "summary": summary,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _write(path, text):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def format_text(result):
    lines = ["=" * 72, result["summary"], "-" * 72]
    for m in result["metrics"]:
        lines.append(
            f'{m["name"]:<26} base {m["base"]:>9.3f}ms  cand {m["cand"]:>9.3f}ms'
            f'  Δ {m["delta"]:>+8.3f}ms ({m["delta_pct"]:+.1f}%)'
            f'  {VERDICT_LABELS.get(m["verdict"], m["verdict"])}'
        )
    for note in result["notes"]:
        lines.append(f"  - {note}")
    return "\n".join(lines)


def _selection_error(args, exc):
    reason = str(exc)
    detail = traceback.format_exc()
    _write(args.out_html, render_error(args.capture, reason, detail))
    if args.out_json:
        _write(
            args.out_json,
            json.dumps(
                error_envelope(args.capture, reason, detail, summary="step 选择失败"),
                ensure_ascii=False,
                indent=2,
            ),
        )
    print(f"step 选择失败: {reason}", file=sys.stderr)
    return 2


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="同一份 Ascend profiling 内的 step / step 组对比"
    )
    ap.add_argument("capture", help="单个采集（目录/trace_view.json/ASCEND_PROFILER_OUTPUT）")
    ap.add_argument("--out-html", default=os.path.join(os.getcwd(), "profiling_steps_report.html"))
    ap.add_argument("--json", dest="out_json", default=None)
    ap.add_argument("--base-steps", default=None, help="基线组 step id，逗号/空格分隔")
    ap.add_argument("--cand-steps", default=None, help="候选组 step id，逗号/空格分隔")
    ap.add_argument("--base-step", type=int, default=None)
    ap.add_argument("--cand-step", type=int, default=None)
    ap.add_argument("--skip-warmup", type=int, default=0)
    ap.add_argument("--min-delta-ms", type=float, default=MIN_DELTA_DEFAULT)
    args = ap.parse_args(argv)

    if (args.base_steps is None) != (args.cand_steps is None):
        return _selection_error(
            args, SelectionError("--base-steps 与 --cand-steps 必须同时提供")
        )
    if (args.base_step is None) != (args.cand_step is None):
        return _selection_error(
            args, SelectionError("--base-step 与 --cand-step 必须同时提供")
        )
    if (args.base_steps is not None or args.cand_steps is not None) and (
        args.base_step is not None or args.cand_step is not None
    ):
        return _selection_error(
            args,
            SelectionError("组选项（--*-steps）与单步选项（--*-step）不能同时使用"),
        )

    # 先解析并校验选择参数：无 step 数据时也不能静默放过非法输入。
    if args.base_steps is not None:
        try:
            base_spec, cand_spec = _parse_ids(args.base_steps), _parse_ids(args.cand_steps)
        except SelectionError as exc:
            return _selection_error(args, exc)
        if not base_spec or not cand_spec:
            return _selection_error(
                args, SelectionError("--base-steps/--cand-steps 不能为空")
            )
    elif args.base_step is not None:
        base_spec, cand_spec = [args.base_step], [args.cand_step]
    else:
        base_spec, cand_spec = None, None

    try:
        cap = load_capture(args.capture)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        detail = traceback.format_exc()
        _write(args.out_html, render_error(args.capture, reason, detail))
        if args.out_json:
            _write(
                args.out_json,
                json.dumps(
                    error_envelope(args.capture, reason, detail),
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        print(f"step 组对比失败，已输出错误报告: {args.out_html}", file=sys.stderr)
        return 3

    if not cap["steps"]:
        result = compare_groups(cap, [], [], args.min_delta_ms)
        _write(args.out_html, render_html(result))
        print(format_text(result))
        if args.out_json:
            _write(args.out_json, json.dumps(result, ensure_ascii=False, indent=2))
            print(f"JSON → {args.out_json}")
        print(f"HTML → {args.out_html}")
        return 0

    try:
        base_ids, cand_ids, notes = select_groups(
            cap, base_spec, cand_spec, max(0, args.skip_warmup)
        )
    except SelectionError as exc:
        return _selection_error(args, exc)

    result = compare_groups(cap, base_ids, cand_ids, args.min_delta_ms)
    result["notes"] = list(dict.fromkeys(result["notes"] + notes))
    result["inputs"]["steps"] = f"base={base_ids}; cand={cand_ids}"

    print(format_text(result))
    errs = validate(result)
    if errs:
        print("警告：JSON 信封不符合 A.1：" + "; ".join(errs), file=sys.stderr)
    _write(args.out_html, render_html(result))
    if args.out_json:
        _write(args.out_json, json.dumps(result, ensure_ascii=False, indent=2))
        print(f"JSON → {args.out_json}")
    print(f"HTML → {args.out_html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
