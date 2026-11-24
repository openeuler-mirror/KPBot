#!/usr/bin/env python3
"""对比两份 Ascend profiling 采集，输出瓶颈变化（步级 / 任意步 / 整窗降级）。

采集读取与度量逻辑在 kpc_core.py；本文件只做差异计算、CLI 与报告。

输入优先级:
  1. *_ascend_pt / ASCEND_PROFILER_OUTPUT（原生侧车）
  2. 裸 trace_view.json（复用已有 skill 的切 step / 分类逻辑）
  3. 任何可用数据 -> 整窗对比，绝不空报告

用法:
  compare.py BASE CAND [--out-html report.html] [--json result.json]
             [--base-step N --cand-step M] [--base-steps 0,1,2 --cand-steps 3,4,5]
             [--skip-warmup K] [--min-delta-ms 0.05] [--min-delta-pct 1.0]
             [--min-kernel-ms 0.02]
"""

import argparse
import json
import os
import re
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from envelope import to_envelope, validate
from kpc_core import (
    METRIC_DEFS,
    _class_family,
    _collapse,
    _dominant,
    _find_step,
    _kernel_avg,
    _metric_block,
    _pick_steps,
    _round,
    load_capture,
)


def _ratio(bm, cm):
    try:
        bm, cm = float(bm), float(cm)
    except (TypeError, ValueError):
        return 0.0
    if bm <= 0:
        return 0.0
    return cm / bm


def _kernel_diff(base, cand, base_steps, cand_steps, min_kernel_ms):
    b = _kernel_avg(base, base_steps)
    c = _kernel_avg(cand, cand_steps)
    rows, one_sided = [], []
    for name in set(b) | set(c):
        bv = b.get(name, {"dur": 0.0, "count": 0, "type": ""})
        cv = c.get(name, {"dur": 0.0, "count": 0, "type": ""})
        delta = bv["dur"] - cv["dur"]  # µs; 正 = 候选更省
        row = {
            "name": name,
            "type": bv["type"] or cv["type"],
            "base_ms": _round(bv["dur"] / 1000.0),
            "cand_ms": _round(cv["dur"] / 1000.0),
            "delta_ms": _round(delta / 1000.0),
            "count_base": bv["count"],
            "count_cand": cv["count"],
        }
        if bv["count"] and cv["count"]:
            if abs(delta) < min_kernel_ms * 1000.0:
                continue
            rows.append(row)
        else:
            row["present_in"] = "base" if bv["count"] else "cand"
            one_sided.append(row)
    improved = sorted([r for r in rows if r["delta_ms"] > 0], key=lambda r: -r["delta_ms"])[:10]
    regressed = sorted([r for r in rows if r["delta_ms"] < 0], key=lambda r: r["delta_ms"])[:10]
    one_sided.sort(key=lambda r: -abs(r["delta_ms"]))
    return {"improved": improved, "regressed": regressed, "one_sided": one_sided[:20]}


def _comm_diff(base, cand, base_steps, cand_steps):
    def agg(cap, steps):
        if not _has_comm(cap):
            return None
        n = len(steps) or 1
        out = {"elapse": 0.0, "wait": 0.0, "transit": 0.0, "sync": 0.0}
        found = False
        for s in steps:
            c = cap["comm"].get(s["id"])
            if not c:
                continue
            found = True
            for k in out:
                out[k] += c[k]
        if not found:
            return None
        return {k: v / n for k, v in out.items()}

    b = agg(base, base_steps)
    c = agg(cand, cand_steps)
    if not b and not c:
        return None
    b = b or {k: 0.0 for k in ("elapse", "wait", "transit", "sync")}
    c = c or {k: 0.0 for k in ("elapse", "wait", "transit", "sync")}
    return {
        k: {
            "base_ms": _round(b[k]),
            "cand_ms": _round(c[k]),
            "delta_ms": _round(b[k] - c[k]),
        }
        for k in ("elapse", "wait", "transit", "sync")
    }


def _has_comm(cap):
    """该采集是否真的带通信数据（不能用零值 dict 的真值代替）。"""
    return bool(cap.get("has_comm")) or bool(cap.get("comm"))


def _components_additive(cap):
    """分量是否满足可加性：comp + comm_no + free + prep ≈ 步耗时。

    多卡错误的“分量求和 + Stage 取 max”、或异常侧车会让分量合计超过步耗时，
    此时分量拆分不可比，只能比较步耗时。
    """
    for s in cap["steps"]:
        dur = float(s.get("dur") or 0)
        if dur <= 0:
            continue
        total = (float(s.get("comp", 0)) + float(s.get("comm_no", 0))
                 + float(s.get("free", 0)) + float(s.get("prep", 0)))
        if abs(total - dur) > max(1.0, dur * 0.02):
            return False
    return True


def _capture_total_steps(cap):
    return cap.get("_collapsed_from") or len(cap["steps"]) or 1


def _api_diff(base, cand, n_base, n_cand, comparable=True):
    if not base["api"] and not cand["api"]:
        return []
    nb = max(1, n_base)
    nc = max(1, n_cand)
    names = set(base["api"]) | set(cand["api"])
    rows = []
    for name in names:
        b = base["api"].get(name, {"time": 0.0, "count": 0})
        c = cand["api"].get(name, {"time": 0.0, "count": 0})
        b_has = name in base["api"]
        c_has = name in cand["api"]
        if comparable:
            bt = b["time"] / nb / 1000.0
            ct = c["time"] / nc / 1000.0
            delta = _round(bt - ct)
        else:
            # 两侧口径不同或仅一侧有侧车：缺失侧用 None，不输出 Δ。
            bt = b["time"] / 1000.0 if b_has else None
            ct = c["time"] / 1000.0 if c_has else None
            delta = None
        if comparable and abs(bt - ct) < 0.001 and b["count"] == c["count"]:
            continue
        rows.append(
            {
                "name": name,
                "base_ms_per_step": _round(bt) if bt is not None else None,
                "cand_ms_per_step": _round(ct) if ct is not None else None,
                "delta_ms": delta,
                "comparable": comparable,
                "count_base": b["count"],
                "count_cand": c["count"],
            }
        )
    rows.sort(key=lambda r: (0 if r["delta_ms"] is None else -abs(r["delta_ms"])))
    return rows[:12]


def compare_captures(base, cand, base_step=None, cand_step=None, base_steps=None,
                     cand_steps=None, skip_warmup=0, min_delta_ms=0.05,
                     min_delta_pct=1.0, min_kernel_ms=0.02):
    caveats = []
    degradations = []
    mode = "aligned"

    if (base_step is None) != (cand_step is None):
        raise ValueError("--base-step 与 --cand-step 必须同时提供")
    if base_step is not None and (base_steps or cand_steps):
        raise ValueError("--base-step/--cand-step 与 --base-steps/--cand-steps 不能同时使用")

    if base["whole_window"] or cand["whole_window"]:
        if base_step is not None or cand_step is not None or base_steps or cand_steps:
            raise ValueError(
                "至少一侧没有可靠 step 边界（whole_window），"
                "--base-step/--cand-step/--base-steps/--cand-steps 无法生效；"
                "请先用 kptools-ascend-profiling-analysis-nostack 的 detect_steps 切出 step（或等 step-split skill 合入后使用），或去掉显式 step 参数"
            )
        base = _collapse(base)
        cand = _collapse(cand)
        pairs = [(base["steps"][0], cand["steps"][0])]
        mode = "whole_window"
        caveats.append("至少一侧没有可靠 step 边界，已退化为整窗对比")
        degradations.append("至少一侧没有可靠 step 边界，已退化为整窗对比")
        if skip_warmup:
            caveats.append(
                f"整窗降级下 --skip-warmup {skip_warmup} 不生效：warmup 步已折回整窗，"
                "不能据此认为预热已排除"
            )
    elif base_step is not None:
        pairs = [(_find_step(base, base_step), _find_step(cand, cand_step))]
        mode = "single"
    elif base_steps or cand_steps:
        if not base_steps or not cand_steps:
            raise ValueError("--base-steps 与 --cand-steps 必须同时提供")
        if len(base_steps) != len(cand_steps):
            raise ValueError(
                "--base-steps 与 --cand-steps 数量必须一致，"
                f"否则会静默丢弃 step（{len(base_steps)} vs {len(cand_steps)}）"
            )
        pairs = list(zip(_pick_steps(base, base_steps), _pick_steps(cand, cand_steps)))
        mode = "group"
    else:
        bs = base["steps"][skip_warmup:]
        cs = cand["steps"][skip_warmup:]
        n = min(len(bs), len(cs))
        if n == 0:
            raise ValueError(
                "跳过 warmup/取交集后没有可比 step，停止性能判定"
                "（请减小 --skip-warmup 或检查 step 数）"
            )
        else:
            pairs = list(zip(bs[:n], cs[:n]))
            if len(bs) != len(cs):
                caveats.append(
                    f"step 数不一致（base {len(bs)} / cand {len(cs)}），只比较交集 {n} 对"
                )
                degradations.append("两侧 step 数不一致，只比较交集")

    n_base_total = _capture_total_steps(base)
    n_cand_total = _capture_total_steps(cand)
    api_one_sided = bool(base["api"]) != bool(cand["api"])
    api_comparable = (n_base_total == n_cand_total) and not api_one_sided
    api_rows = _api_diff(base, cand, n_base_total, n_cand_total, api_comparable)
    if api_rows and api_one_sided:
        caveats.append(
            "仅一侧提供 Host API 侧车（api_detail.csv/api_statistic.csv），"
            "缺失侧展示为「—」，API 差异不作为优化结论"
        )
    if api_rows and not api_comparable:
        caveats.append(
            f"整窗降级时两侧 Host API 归一化分母不同（base {n_base_total} 步 / "
            f"cand {n_cand_total} 个窗口），API 表只展示各侧合计、不输出 Δ"
        )
    elif api_rows and (len(pairs) != n_base_total or len(pairs) != n_cand_total):
        caveats.append(
            "Host API 为整份采集聚合，按采集总步数归一，未按所选 step/warmup 过滤"
        )

    if skip_warmup < 0:
        raise ValueError("--skip-warmup 必须是非负整数")
    if skip_warmup and (base_step is not None or base_steps or cand_steps):
        raise ValueError("--skip-warmup 只用于默认按位置对齐的模式")
    if skip_warmup and mode == "aligned":
        caveats.append(f"已跳过前 {skip_warmup} 步（warmup）")
    if base["has_stack"] != cand["has_stack"]:
        caveats.append(
            f"栈类型不同（base has_stack={base['has_stack']} / cand has_stack={cand['has_stack']}）"
        )
    if base["version"] and cand["version"] and base["version"] != cand["version"]:
        caveats.append(f"torch_npu 版本不同（{base['version']} vs {cand['version']}）")
    family_match = _class_family(base) == _class_family(cand)
    classes_available = base["class_source"] != "none" and cand["class_source"] != "none"
    additive = _components_additive(base) and _components_additive(cand)
    same_class = family_match and classes_available and additive
    if not classes_available:
        caveats.append(
            "至少一侧无法获得设备任务分类，仅比较步耗时；"
            "Computing/Communication/Free/Preparing 仅展示数值、不做判定"
        )
    elif not family_match:
        caveats.append(
            "四分类来源跨族"
            f"（{base['class_source']} vs {cand['class_source']}）："
            "仅比较步耗时，Computing/Communication/Free/Preparing 不做判定"
        )
    elif not additive:
        caveats.append(
            "分量不满足可加性（Computing + Communication(未重叠) + Free + Preparing "
            "≠ 步耗时）：仅比较步耗时，分量仅展示数值、不做判定"
        )
    if mode == "aligned" and any(b["id"] != c["id"] for b, c in pairs):
        caveats.append("按位置配对，部分两侧 step id 不一致，请确认语义一致")

    bsteps = [b for b, _ in pairs]
    csteps = [c for _, c in pairs]
    comm_diff = _comm_diff(base, cand, bsteps, csteps)
    if comm_diff:
        base_has_comm = _has_comm(base)
        cand_has_comm = _has_comm(cand)
        if base_has_comm != cand_has_comm:
            caveats.append(
                "仅一侧提供通信数据（communication.json / analysis.db:CommAnalyzerTime），"
                "通信差异把缺失侧按 0 展示，不代表真实收益，不能作为优化结论"
            )

    for cap in (base, cand):
        caveats.extend(cap.get("notes") or [])
    caveats = list(dict.fromkeys(caveats))

    components = {
        key: _metric_block(bsteps, csteps, key, min_delta_ms, min_delta_pct)
        for key, _ in METRIC_DEFS
    }
    if not same_class:
        for key in ("comp", "comm_no", "free", "prep"):
            components[key]["verdict"] = "info"
            components[key]["heterogeneous"] = False
            components[key]["sign_consistency"] = None
    dur = components["dur"]

    base_dom, base_pcts = _dominant(bsteps)
    cand_dom, cand_pcts = _dominant(csteps)
    migrated = (
        same_class
        and base_dom != cand_dom
        and dur["verdict"] in ("改善", "恶化")
    )
    if not classes_available:
        bottleneck_text = "四分类不可用，仅比较步耗时；不做瓶颈迁移结论"
    elif not (family_match and additive):
        reason = "跨族" if not family_match else "不可加"
        bottleneck_text = f"分量{reason}不可比，仅步耗时可比；当前候选以 {cand_dom} 为主"
    elif dur["verdict"] == "方向不一致":
        bottleneck_text = "步间变化方向不一致，不做瓶颈迁移结论"
    elif migrated:
        bottleneck_text = f"{base_dom} → {cand_dom}"
    elif dur["verdict"] == "在噪声内":
        bottleneck_text = "未观察到实质瓶颈迁移"
    else:
        bottleneck_text = f"仍以 {cand_dom} 为主"
    bottleneck = {
        "base": base_dom,
        "cand": cand_dom,
        "base_pcts": {k: _round(v, 2) for k, v in base_pcts.items()},
        "cand_pcts": {k: _round(v, 2) for k, v in cand_pcts.items()},
        "available": classes_available and family_match,
        "migrated": migrated,
        "text": bottleneck_text,
    }

    contributions = sorted(
        ((k, components[k]["delta_ms"]) for k in ("comp", "comm_no", "free", "prep")
         if components[k]["verdict"] in ("改善", "恶化")),
        key=lambda kv: -abs(kv[1]),
    )
    ratio = _ratio(dur["base_median_ms"], dur["cand_median_ms"])
    if dur["verdict"] == "方向不一致":
        overall_verdict = (
            f"步间变化方向不一致（{dur['n_improved']} 步改善 / {dur['n_regressed']} 步恶化），"
            "请按 prefill/decode 或阶段分组比较"
        )
    elif dur["verdict"] == "在噪声内":
        overall_verdict = "未观察到实质变化（在噪声内）"
    elif dur["verdict"] == "改善":
        overall_verdict = (
            f"候选更快：步耗时 {abs(dur['pct']):.1f}%"
            f"（逐对 Δ {dur['paired_delta_ms']:+.3f}ms，×{ratio:.2f}）"
        )
    else:
        overall_verdict = (
            f"候选更慢：步耗时 +{abs(dur['pct']):.1f}%"
            f"（逐对 Δ {dur['paired_delta_ms']:+.3f}ms，×{ratio:.2f}）"
        )

    return {
        "mode": mode,
        "base": {k: base[k] for k in ("label", "path", "kind", "step_source", "class_source",
                                      "kernel_source", "version", "cann_version", "rank", "has_stack",
                                      "has_comm", "sidecars", "degradations")},
        "cand": {k: cand[k] for k in ("label", "path", "kind", "step_source", "class_source",
                                      "kernel_source", "version", "cann_version", "rank", "has_stack",
                                      "has_comm", "sidecars", "degradations")},
        "pairs": [
            {
                "base_id": b["id"],
                "cand_id": c["id"],
                "base_dur_ms": _round(b["dur"] / 1000.0),
                "cand_dur_ms": _round(c["dur"] / 1000.0),
                "base": {k: _round(b[k] / 1000.0) for k in ("comp", "comm_no", "free", "prep")},
                "cand": {k: _round(c[k] / 1000.0) for k in ("comp", "comm_no", "free", "prep")},
                "delta_ms": {
                    "dur": _round((b["dur"] - c["dur"]) / 1000.0),
                    **{k: _round((b[k] - c[k]) / 1000.0) for k in ("comp", "comm_no", "free", "prep")},
                },
            }
            for b, c in pairs
        ],
        "summary": {
            "pair_count": len(pairs),
            "overall_verdict": overall_verdict,
            "dur": dur,
            "dur_ratio": _round(ratio, 3),
            "components": components,
            "components_comparable": same_class,
            "bottleneck": bottleneck,
            "contributions": [
                {"key": k, "name": dict(METRIC_DEFS).get(k, k), "delta_ms": _round(v)}
                for k, v in contributions
            ],
            "noise_rule": (
                f"max({min_delta_ms}ms, {min_delta_pct}%×base, MAD(逐对 Δ))"
            ),
        },
        "kernels": _kernel_diff(base, cand, bsteps, csteps, min_kernel_ms),
        "comm": comm_diff,
        "api": api_rows,
        "caveats": caveats,
        "degradations": degradations,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_id_list(text):
    if not text:
        return None
    return [int(x) for x in re.split(r"[,\s]+", str(text).strip()) if x.strip()]


def format_text(result):
    s = result["summary"]
    lines = []
    b, c = result["base"], result["cand"]
    lines.append("=" * 72)
    lines.append(f"base: {b['label']}  ({b['kind']}, {b['step_source']})")
    lines.append(f"cand: {c['label']}  ({c['kind']}, {c['step_source']})")
    lines.append(f"模式: {result['mode']} · 配对 {s['pair_count']} 对")
    lines.append(f"结论: {s['overall_verdict']}")
    lines.append("-" * 72)
    for key, name in METRIC_DEFS:
        m = s["components"][key]
        lines.append(
            f"{name:<26} base {m['base_median_ms']:>10.3f}ms  cand {m['cand_median_ms']:>10.3f}ms"
            f"  Δ {m['delta_ms']:>+9.3f}ms  ×{_ratio(m['base_median_ms'], m['cand_median_ms']):.2f}"
            f"  {m['verdict']}"
        )
    lines.append(f"瓶颈: {s['bottleneck']['text']}")
    if result["kernels"]["improved"]:
        lines.append("Top 变快算子:")
        for r in result["kernels"]["improved"][:5]:
            lines.append(f"  {r['name'][:60]:<60} {r['delta_ms']:+.3f}ms")
    if result["kernels"]["regressed"]:
        lines.append("Top 变慢算子:")
        for r in result["kernels"]["regressed"][:5]:
            lines.append(f"  {r['name'][:60]:<60} {r['delta_ms']:+.3f}ms")
    if result["kernels"].get("one_sided"):
        lines.append("仅单侧出现的算子（不计入 Top，前 5）:")
        for r in result["kernels"]["one_sided"][:5]:
            lines.append(
                f"  {r['name'][:60]:<60} base {r['count_base']:>4} / cand {r['count_cand']:>4}"
                f"  ({r['present_in']} only)"
            )
    if result["caveats"]:
        lines.append("注意:")
        for x in result["caveats"]:
            lines.append(f"  - {x}")
    return "\n".join(lines)


def _error_report(base_path, cand_path, exc, out_html):
    from report_html import render_error

    data = render_error(base_path, cand_path, f"{type(exc).__name__}: {exc}",
                        traceback.format_exc())
    Path(out_html).parent.mkdir(parents=True, exist_ok=True)
    Path(out_html).write_text(data, encoding="utf-8")
    print(f"对比失败，已输出错误报告: {out_html}", file=sys.stderr)


_SIDECAR_NAMES = (
    "trace_view.json", "analysis.db", "kernel_details.csv", "step_trace_time.csv",
    "communication.json", "api_statistic.csv", "api_detail.csv",
)


def _input_files(path):
    p = Path(path).expanduser().resolve()
    if p.is_file():
        return {p}
    out = set()
    if p.is_dir():
        for f in p.rglob("*"):
            if f.is_file() and (
                f.name in _SIDECAR_NAMES
                or f.name.startswith(("ascend_pytorch_profiler_", "profiler_info_"))
            ):
                out.add(f.resolve())
    return out


def _check_outputs(base, cand, out_html, out_json):
    outs = {"--out-html": Path(out_html).expanduser().resolve()}
    if out_json:
        outs["--json"] = Path(out_json).expanduser().resolve()
    if out_json and Path(out_json).expanduser().resolve() == Path(out_html).expanduser().resolve():
        raise ValueError("--out-html 与 --json 不能指向同一文件")
    inputs = _input_files(base) | (_input_files(cand) if cand else set())
    for flag, out in outs.items():
        if out in inputs:
            raise ValueError(f"{flag} 指向输入采集文件 {out}，拒绝覆盖原始数据")


def main(argv=None):
    ap = argparse.ArgumentParser(description="对比两份 Ascend profiling 的瓶颈变化")
    ap.add_argument("base", help="基线采集（目录/trace_view.json/ASCEND_PROFILER_OUTPUT）")
    ap.add_argument("cand", help="候选采集")
    ap.add_argument("--out-html", default=os.path.join(os.getcwd(), "profiling_compare_report.html"))
    ap.add_argument("--json", dest="out_json", default=None)
    ap.add_argument("--base-step", type=int, default=None)
    ap.add_argument("--cand-step", type=int, default=None)
    ap.add_argument("--base-steps", default=None, help="逗号分隔的 step id 列表")
    ap.add_argument("--cand-steps", default=None)
    ap.add_argument("--skip-warmup", type=int, default=0)
    ap.add_argument("--min-delta-ms", type=float, default=0.05)
    ap.add_argument("--min-delta-pct", type=float, default=1.0,
                    help="相对噪声下限（占 base 中位数的百分比，默认 1.0）")
    ap.add_argument("--min-kernel-ms", type=float, default=0.02)
    ap.add_argument("--label-base", default=None)
    ap.add_argument("--label-cand", default=None)
    args = ap.parse_args(argv)

    # 读取采集前先校验输出路径：拒绝与输入侧车同路径、拒绝 HTML/JSON 同名。
    # 失败时不写错误页（错误页本身也可能指向输入文件），直接非零退出。
    try:
        _check_outputs(args.base, args.cand, args.out_html, args.out_json)
    except ValueError as exc:
        print(f"输出路径不安全，拒绝覆盖：{exc}", file=sys.stderr)
        return 3

    try:
        base = load_capture(args.base, args.label_base)
        cand = load_capture(args.cand, args.label_cand)
        result = compare_captures(
            base,
            cand,
            base_step=args.base_step,
            cand_step=args.cand_step,
            base_steps=_parse_id_list(args.base_steps),
            cand_steps=_parse_id_list(args.cand_steps),
            skip_warmup=args.skip_warmup,
            min_delta_ms=args.min_delta_ms,
            min_delta_pct=args.min_delta_pct,
            min_kernel_ms=args.min_kernel_ms,
        )
    except Exception as exc:
        _error_report(args.base, args.cand, exc, args.out_html)
        return 3

    print(format_text(result))
    if args.out_json:
        env = to_envelope(result)
        errs = validate(env)
        if errs:
            print("警告：JSON 信封不符合 methodology §7：" + "; ".join(errs), file=sys.stderr)
        Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_json).write_text(
            json.dumps(env, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"JSON → {args.out_json}")

    from report_html import render

    try:
        html = render(result)
    except Exception as exc:
        _error_report(args.base, args.cand, exc, args.out_html)
        return 3
    Path(args.out_html).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_html).write_text(html, encoding="utf-8")
    print(f"HTML → {args.out_html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
