#!/usr/bin/env python3
"""无栈 trace HTML 报告。

用法:
  report_html_nostack.py <trace_view.json> [--out report.html]

step 识别复用 detect_steps.py；四分类/算子/通信/Host API 来自同目录侧车。
"""

import argparse
import csv
import html
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import detect_steps as ds  # noqa: E402
import nostack_analysis as na  # noqa: E402

COMM_HINTS = ("hcom", "allreduce", "allgather", "reducescatter", "alltoall", "broadcast")


def esc(s):
    return html.escape(str(s), quote=True)


def pct(x, total):
    return 0.0 if not total else x / total * 100


def read_css():
    path = os.path.join(HERE, "..", "assets", "report.css")
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def read_step_trace(base):
    path = os.path.join(base, "step_trace_time.csv")
    rows = []
    if os.path.exists(path):
        for r in csv.DictReader(open(path, encoding="utf-8", errors="replace")):
            try:
                rows.append(
                    {
                        "step": int(r["Step"]),
                        "computing": float(r["Computing"]),
                        "comm_no": float(r["Communication(Not Overlapped)"]),
                        "comm": float(r["Communication"]),
                        "overlap": float(r["Overlapped"]),
                        "free": float(r["Free"]),
                        "stage": float(r["Stage"]),
                        "preparing": float(r["Preparing"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
    if rows:
        return {r["step"]: r for r in rows}, "step_trace_time.csv"
    adb = os.path.join(base, "analysis.db")
    if os.path.exists(adb):
        try:
            con = sqlite3.connect(f"file:{adb}?mode=ro", uri=True)
            for row in con.execute(
                "SELECT step,computing,communication_not_overlapped,communication,"
                "overlapped,free,stage,preparing FROM StepTraceTime"
            ):
                rows.append(
                    {
                        "step": int(row[0]),
                        "computing": float(row[1]),
                        "comm_no": float(row[2]),
                        "comm": float(row[3]),
                        "overlap": float(row[4]),
                        "free": float(row[5]),
                        "stage": float(row[6]),
                        "preparing": float(row[7]),
                    }
                )
            con.close()
        except Exception:
            rows = []
    return ({r["step"]: r for r in rows}, "analysis.db:StepTraceTime") if rows else ({}, None)


def read_kernels(base):
    path = os.path.join(base, "kernel_details.csv")
    if not os.path.exists(path):
        return None
    by_step = defaultdict(lambda: defaultdict(lambda: [0.0, 0, 0.0]))
    for r in csv.DictReader(open(path, encoding="utf-8", errors="replace")):
        try:
            sid = int((r.get("Step Id") or "").strip())
            name = r["Name"][:100]
            dur = float(r["Duration(us)"])
            wait = float(r.get("Wait Time(us)") or 0)
        except (TypeError, ValueError):
            continue
        x = by_step[sid][name]
        x[0] += dur
        x[1] += 1
        x[2] += wait
    return by_step


def is_comm(name):
    low = name.lower()
    return any(h in low for h in COMM_HINTS)


def read_comm(base):
    path = os.path.join(base, "communication.json")
    if not os.path.exists(path):
        return None
    data = json.load(open(path, encoding="utf-8"))
    out = {}
    for step, grp in data.items():
        total = (
            (grp.get("collective") or {})
            .get("Total Op Info", {})
            .get("Communication Time Info", {})
        )
        ops = []
        for kind in ("collective", "p2p"):
            for name, info in (grp.get(kind) or {}).items():
                if name == "Total Op Info":
                    continue
                ti = info.get("Communication Time Info", {})
                try:
                    ops.append(
                        {
                            "name": name,
                            "elapse": float(ti.get("Elapse Time(ms)", 0) or 0),
                            "wait": float(ti.get("Wait Time(ms)", 0) or 0),
                            "transit": float(ti.get("Transit Time(ms)", 0) or 0),
                        }
                    )
                except (TypeError, ValueError):
                    continue
        ops.sort(key=lambda x: -x["wait"])
        try:
            out[step] = {
                "elapse": float(total.get("Elapse Time(ms)", 0) or 0),
                "wait": float(total.get("Wait Time(ms)", 0) or 0),
                "transit": float(total.get("Transit Time(ms)", 0) or 0),
                "sync": float(total.get("Synchronization Time(ms)", 0) or 0),
                "ops": ops,
            }
        except (TypeError, ValueError):
            out[step] = {"elapse": 0, "wait": 0, "transit": 0, "sync": 0, "ops": ops}
    return out


def read_api(base):
    path = os.path.join(base, "api_statistic.csv")
    if not os.path.exists(path):
        return []
    rows = []
    for r in csv.DictReader(open(path, encoding="utf-8", errors="replace")):
        try:
            rows.append(
                {
                    "name": r.get("API Name") or r.get("Name") or "?",
                    "time": float(r.get("Time(us)") or 0),
                    "count": int(float(r.get("Count") or 0)),
                    "avg": float(r.get("Avg(us)") or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda x: -x["time"])
    return rows


def read_operators(base):
    path = os.path.join(base, "operator_details.csv")
    if not os.path.exists(path):
        return []
    agg = defaultdict(lambda: [0.0, 0.0, 0])
    for r in csv.DictReader(open(path, encoding="utf-8", errors="replace")):
        try:
            name = r["Name"][:100]
            agg[name][0] += float(r.get("Host Total Duration(us)") or 0)
            agg[name][1] += float(r.get("Device Total Duration(us)") or 0)
            agg[name][2] += 1
        except (TypeError, ValueError):
            continue
    rows = [{"name": n, "host": v[0], "device": v[1], "count": v[2]} for n, v in agg.items()]
    rows.sort(key=lambda x: -x["host"])
    return rows


def end_syncs(steps, events, uiz):
    syncs = sorted(
        [e for e in events if "SynchronizeDevice" in e[2] and e[1] > 0], key=lambda e: e[0]
    )
    out = []
    for s in steps:
        cand = [e for e in syncs if s["start_ns"] <= e[0] + e[1] <= s["end_ns"] + 1_000_000]
        best = min(cand, key=lambda e: abs((e[0] + e[1]) - s["end_ns"])) if cand else None
        out.append(best[1] / 1e6 if best else None)
    return out


def judge(comp, comm, free, sync):
    if comm >= 50:
        return "通信等待主导"
    if free >= 50:
        return "设备空闲/等待请求"
    if sync is not None and sync >= 2:
        return "设备积压（步尾同步等设备跑完）"
    return "设备基本跟上"


def bar(comp, comm, free, big=False, title=""):
    cls = "mbar big" if big else "mbar"
    return (
        f'<span class="{cls}" title="{esc(title)}">'
        f'<i style="width:{comp:.2f}%;background:#4A78A0"></i>'
        f'<i style="width:{comm:.2f}%;background:#B08968"></i>'
        f'<i style="width:{free:.2f}%;background:#A6BE9E"></i></span>'
    )


def cf_box_html(comp_pct, free_pct, title="Computing vs Free（重点）"):
    cf = comp_pct + free_pct
    comp_cf = comp_pct / cf * 100 if cf else 0.0
    free_cf = free_pct / cf * 100 if cf else 0.0
    return (
        f'<div class="hero" style="border-left-color:#4A78A0">'
        f'<div class="htitle">{esc(title)}</div>'
        f'<div class="hnums">Computing <b>{comp_cf:.1f}%</b><span class="sep">·</span>'
        f"Free <b>{free_cf:.1f}%</b>（占 Computing+Free）</div>"
        f'<div><span class="mbar big"><i style="width:{comp_cf:.2f}%;background:#4A78A0"></i>'
        f'<i style="width:{free_cf:.2f}%;background:#A6BE9E"></i></span></div>'
        f'<div class="barleg">占整步：Computing {comp_pct:.1f}% · Free {free_pct:.1f}%；'
        f"Communication 不在此框内</div></div>"
    )


def step_failure_page(trace, result):
    """无法定位任何 step 时，显式报错而不是输出 0 步的百分比结论。"""
    css = read_css()
    notes = " | ".join(result.get("notes") or []) or "无"
    return (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        "<title>Ascend 无栈 Profiling 报告 · step 定位失败</title>"
        f"<style>{css}</style></head><body>"
        '<div class="page on" id="home"><h1>Profiling 汇总</h1>'
        '<div class="hero" style="border-left-color:#B08968">'
        '<div class="htitle">step 定位失败</div>'
        '<div class="hnums">未能从 trace/侧车推导出任何 step 边界，'
        "不输出 0 步的四分类百分比结论，也不判断设备是否跟上</div></div>"
        f'<div class="kv">trace: {esc(trace)}</div>'
        f'<div class="kv">step 识别源: {esc(result.get("source"))} '
        f'({esc(result.get("confidence"))}, {esc(result.get("scope"))})</div>'
        f'<div class="note">notes: {esc(notes)}</div>'
        '<div class="note">排查建议：确认 trace 含设备同步（aclrtSynchronizeDevice / '
        "SynchronizeStream / SynchronizeEvent）或存在可用的设备活动间隔；也可提供 "
        "step_trace_time.csv、kernel_details.csv 或 ascend_pytorch_profiler_*.db 的 STEP_TIME "
        "侧车。communication.json 仅能给出步数，需 trace 边界数量与之一致才能对齐。</div>"
        "</div></body></html>"
    )


def build(
    trace,
    model=None,
    vllm_version=None,
    vllm_ascend_version=None,
    model_config=None,
    vllm_src=None,
    vllm_ascend_src=None,
    with_stack_trace=None,
):
    result = ds.detect(trace)
    steps = result["steps"]
    if not steps:
        return step_failure_page(trace, result)
    base = os.path.dirname(os.path.abspath(trace))
    stt, stt_src = read_step_trace(base)
    kernels = read_kernels(base)
    comm = read_comm(base)
    api = read_api(base)
    ops = read_operators(base)
    events, host_pid, uiz = ds.load_trace(trace)
    syncs = end_syncs(steps, events, uiz)

    kernel_rows = na.read_kernel_rows(base)
    classes = na.resolve_classes(events)
    class_src = classes[3]
    oa, oa_src = na.oa_classes(steps, events, classes)
    windows = {}
    for s in steps:
        w = na.phase_windows(s, kernel_rows.get(s["id"], []), events, host_pid)
        if w:
            windows[s["id"]] = w
    free_stats = na.free_by_phase(steps, events, windows, classes)
    free_attrs = {}
    calltrees = {}
    models = {}
    for i, s in enumerate(steps):
        sid = s["id"]
        if sid in windows:
            free_attrs[sid] = {
                p: na.free_attribution(sid, kernel_rows.get(sid, []), events, w, p, classes)
                for p, w in windows[sid].items()
            }
            calltrees[sid] = na.calltree_html(
                kernel_rows.get(sid, []), windows[sid], steps, syncs[i]
            )
        models[sid] = na.model_structure_html(
            kernel_rows.get(sid, []), windows.get(sid, {}).get("P2")
        )

    layer_counts = {}
    for sid, h in models.items():
        m = re.search(r"×([0-9]+)", h)
        if m:
            layer_counts[sid] = int(m.group(1))
    expected_layers = None
    if model_config and os.path.exists(model_config):
        try:
            cfg = json.load(open(model_config, encoding="utf-8"))
            tc = cfg.get("text_config") or cfg
            expected_layers = tc.get("num_hidden_layers") or cfg.get("num_hidden_layers")
        except Exception:
            pass

    rows = []
    for i, s in enumerate(steps):
        t = stt.get(i, {})
        # OA 可用时用 OA；OA 缺失时优先用 CANN step_trace_time 的正确四分类，
        # 连 CSV 也没有才退回设备任务类型自建。
        if class_src == "overlap_analysis" or not t:
            oc = oa.get(s["id"], {})
            stage = oc.get("stage", 0.0)
            comp = oc.get("comp", 0.0)
            comm_no = oc.get("comm_no", 0.0)
            comm_tot = oc.get("comm", 0.0)
            free = oc.get("free", 0.0)
            overlap = oc.get("overlap", 0.0)
        else:
            stage = t.get("stage", 0.0)
            comp = t.get("computing", 0.0)
            comm_no = t.get("comm_no", 0.0)
            comm_tot = t.get("comm", 0.0)
            free = t.get("free", 0.0)
            overlap = t.get("overlap", 0.0)
        prep = t.get("preparing", 0.0)
        sync = syncs[i]
        rows.append(
            {
                "id": s["id"],
                "start": s["start_ms"],
                "end": s["end_ms"],
                "dur": s["dur_ms"],
                "stage": stage,
                "comp": comp,
                "comm_no": comm_no,
                "comm": comm_tot,
                "overlap": overlap,
                "free": free,
                "prep": prep,
                "sync": sync,
                "comp_pct": pct(comp, stage),
                "comm_pct": pct(comm_no, stage),
                "free_pct": pct(free, stage),
                "judge": judge(pct(comp, stage), pct(comm_no, stage), pct(free, stage), sync),
            }
        )

    total_stage = sum(r["stage"] for r in rows)
    total_comp = sum(r["comp"] for r in rows)
    total_comm = sum(r["comm_no"] for r in rows)
    total_free = sum(r["free"] for r in rows)
    overall = {
        "comp": pct(total_comp, total_stage),
        "comm": pct(total_comm, total_stage),
        "free": pct(total_free, total_stage),
    }
    overall_judge = judge(
        overall["comp"],
        overall["comm"],
        overall["free"],
        max([r["sync"] or 0 for r in rows] or [0]),
    )
    total_elapse = sum((comm or {}).get(k, {}).get("elapse", 0) for k in (comm or {}))
    total_wait = sum((comm or {}).get(k, {}).get("wait", 0) for k in (comm or {}))
    worst = max(rows, key=lambda r: r["comm_pct"]) if rows else None

    css = read_css()
    nav = '<nav><button id="btn-home" class="on" onclick="show(\'home\')">首页 · 汇总</button>'
    for r in rows:
        nav += (
            f'<button id="btn-step{r["id"]}" onclick="show(\'step{r["id"]}\')">'
            f"step{r['id']}</button>"
        )
    nav += "</nav>"

    hero = (
        f'<div class="hero"><div class="htitle">整份 profiling · {len(rows)} 步 · '
        f"{sum(r['dur'] for r in rows):.1f}ms —— "
        f'<span class="judge">{esc(overall_judge)}</span></div>'
        f'<div class="hnums">NPU <b>Free {overall["free"]:.1f}%</b>'
        f'<span class="sep">·</span>Computing {overall["comp"]:.1f}%'
        f'<span class="sep">·</span>Communication {overall["comm"]:.1f}%</div>'
        f"<div>{bar(overall['comp'], overall['comm'], overall['free'], big=True)}</div>"
        '<div class="barleg">Computing<i style="background:#4A78A0"></i>'
        'Communication(未重叠)<i style="background:#B08968"></i>'
        'Free<i style="background:#A6BE9E"></i></div></div>'
    )

    cf_box = cf_box_html(overall["comp"], overall["free"])
    home = f'<div class="page on" id="home"><h1>Profiling 汇总</h1>{cf_box}{hero}'
    home += f'<div class="kv">trace: {esc(trace)}</div>'
    home += (
        f'<div class="kv">模型: {esc(model or "未提供")} · vllm: {esc(vllm_version or "未提供")} · '
        f"vllm-ascend: {esc(vllm_ascend_version or '未提供')}</div>"
    )
    if vllm_src or vllm_ascend_src:
        home += (
            f'<div class="kv">源码: vllm={esc(vllm_src or "未提供")} · '
            f"vllm-ascend={esc(vllm_ascend_src or '未提供')}</div>"
        )
    if with_stack_trace:
        home += f'<div class="kv">with-stack 参考: {esc(with_stack_trace)}</div>'
    missing = []
    if not model:
        missing.append("模型")
    if not vllm_version:
        missing.append("vllm 版本")
    if not vllm_ascend_version:
        missing.append("vllm-ascend 版本")
    if not vllm_src and not vllm_ascend_src:
        missing.append("源码路径")
    if not with_stack_trace:
        missing.append("with-stack 参考 trace")
    if missing:
        home += (
            f'<div class="note">未提供：{esc("、".join(missing))}；模型结构/阶段/源码函数名为默认 '
            "0.23.0 参考，mapping_confidence=low，建议补充后用 with-stack 校准。</div>"
        )
    elif vllm_version and vllm_version not in ("0.23.0", "0.23.0rc1"):
        home += (
            f'<div class="note">vllm 版本 {esc(vllm_version)} 与默认映射 0.23.0 不一致，'
            "源码函数名/阶段语义需重新校准。</div>"
        )
    else:
        home += '<div class="kv">mapping_confidence: 已提供版本/源码/with-stack（仍需确认版本匹配）</div>'
    if class_src == "overlap_analysis":
        class_src_text = "CANN Overlap Analysis 事件"
        free_src_text = "Overlap Analysis 事件"
    elif stt:
        class_src_text = f"{stt_src}（CANN 四分类）"
        free_src_text = "设备任务类型自建区间"
    else:
        class_src_text = "设备任务类型自建（无 Overlap Analysis / step_trace_time）"
        free_src_text = "设备任务类型自建区间"
    home += (
        f'<div class="kv">四分类来源: {esc(class_src_text)} · '
        f"Free 去向区间: {esc(free_src_text)}</div>"
    )
    home += (
        f'<div class="kv">step 识别: {esc(result["source"])} '
        f"({esc(result['confidence'])}, {esc(result['scope'])}) · 事件数 {len(events)}</div>"
    )
    if result["notes"]:
        home += f'<div class="note">{esc(" | ".join(result["notes"]))}</div>'

    home += (
        "<table><tr><th>step</th><th>步长</th><th>Computing</th><th>Comm 未重叠</th>"
        "<th>Comm 总计</th><th>Free</th><th>Preparing</th><th>步尾同步</th><th>判定</th></tr>"
    )
    for r in rows:
        rsync = "—" if r["sync"] is None else f"{r['sync']:.2f}ms"
        home += (
            f"<tr><td>step{r['id']}</td><td>{r['dur']:.1f}ms</td>"
            f"<td>{r['comp'] / 1000:.2f}ms / {r['comp_pct']:.1f}%</td>"
            f"<td>{r['comm_no'] / 1000:.2f}ms / {r['comm_pct']:.1f}%</td>"
            f"<td>{r['comm'] / 1000:.2f}ms</td>"
            f"<td>{r['free'] / 1000:.2f}ms / {r['free_pct']:.1f}%</td>"
            f"<td>{r['prep'] / 1000:.2f}ms</td>"
            f"<td>{rsync}</td>"
            f"<td>{esc(r['judge'])}</td></tr>"
        )
    home += (
        f"<tr><td>合计</td><td>{sum(r['dur'] for r in rows):.1f}ms</td>"
        f"<td>{total_comp / 1000:.2f}ms / {overall['comp']:.1f}%</td>"
        f"<td>{total_comm / 1000:.2f}ms / {overall['comm']:.1f}%</td>"
        f"<td>{sum(r['comm'] for r in rows) / 1000:.2f}ms</td>"
        f"<td>{total_free / 1000:.2f}ms / {overall['free']:.1f}%</td>"
        f"<td>{sum(r['prep'] for r in rows) / 1000:.2f}ms</td><td>—</td><td>—</td></tr></table>"
    )
    home += (
        f'<div class="note">口径：Free = 窗口 − (Computing ∪ Communication)，'
        "Computing + 未重叠 + Free = 100%；四分类来源见上方"
        f"（{esc(class_src_text)}），缺失 Overlap Analysis 时按设备任务类型自建，"
        "避免整窗被误判为 Free；"
        "步尾同步 = 每步末尾设备级同步耗时：长 = 设备还有任务没跑完，host 在等它；短 = 设备早已空闲。</div>"
    )

    if windows:
        home += (
            '<div class="sect"><h3>阶段与 Free 去向（近似）</h3>'
            "<table><tr><th>step</th><th>Free 最高阶段</th><th>P1 Free</th><th>P2 Free</th>"
            "<th>P3 Free</th><th>Top Free 去向</th></tr>"
        )
        for r in rows:
            sid = r["id"]
            if sid not in free_stats:
                continue
            fs = free_stats[sid]
            top_p = max(fs, key=lambda p: fs[p]["free"])
            top_list = free_attrs.get(sid, {}).get(top_p, {}).get("top", [])
            top_f = max(top_list, key=lambda kv: kv[1])[0] if top_list else "other"
            home += (
                f"<tr><td>step{sid}</td><td>{top_p} · {fs[top_p]['free'] / 1e6:.2f}ms</td>"
                f"<td>{fs.get('P1', {}).get('free', 0) / 1e6:.2f}ms</td>"
                f"<td>{fs.get('P2', {}).get('free', 0) / 1e6:.2f}ms</td>"
                f"<td>{fs.get('P3', {}).get('free', 0) / 1e6:.2f}ms</td>"
                f"<td>{esc(na.FUNC_LABELS.get(top_f, top_f))}</td></tr>"
            )
        home += (
            '</table><div class="note">Free 去向按“最近设备内核 → 源码函数”近似；'
            "无 Python 栈，不能等同于真实调用栈。</div></div>"
        )
        rep = max(rows, key=lambda r: r["free"])["id"] if rows else None
        model_rep = max(layer_counts, key=layer_counts.get) if layer_counts else rep
        if expected_layers:
            home += (
                f'<div class="kv">模型 config 期望层数: {expected_layers} · '
                f"观测最完整步: ×{layer_counts.get(model_rep, '?')}</div>"
            )
        if model_rep is not None and models.get(model_rep):
            home += '<div class="sect">' + models[model_rep]
            if layer_counts:
                home += "<table><tr><th>step</th><th>观测层数</th><th>说明</th></tr>"
                mx = max(layer_counts.values())
                for sid in sorted(layer_counts):
                    note = (
                        "完整"
                        if layer_counts[sid] == mx
                        else "本步执行路径/内核交错，少层不代表模型少层"
                    )
                    home += (
                        f"<tr><td>step{sid}</td><td>×{layer_counts[sid]}</td><td>{note}</td></tr>"
                    )
                home += "</table>"
            if expected_layers:
                if layer_counts and max(layer_counts.values()) != expected_layers:
                    home += (
                        f'<div class="note">注意：观测最完整 ×{max(layer_counts.values())} 与 config '
                        f"期望 {expected_layers} 不一致，需检查模型/config 是否匹配。</div>"
                    )
                else:
                    home += f'<div class="note">模型 config 期望 {expected_layers} 层，与观测最完整步一致。</div>'
            else:
                home += (
                    '<div class="note">未提供 model config：层数按最完整执行步观测值表示，'
                    "不能与静态模型定义做强校验；模型结构会随 step 的执行路径变化。</div>"
                )
            home += "</div>"
        if rep is not None and calltrees.get(rep):
            home += (
                '<div class="sect"><h3>最高 Free 步的近似调用栈</h3>' + calltrees[rep] + "</div>"
            )

    home += '<div class="sect"><h3>结论</h3><ul>'
    home += (
        f"<li>整体 Computing {overall['comp']:.1f}% / Communication {overall['comm']:.1f}% / "
        f"Free {overall['free']:.1f}%。</li>"
    )
    if total_elapse:
        home += (
            f"<li>通信总 Elapse {total_elapse:.2f}ms，其中 Wait {total_wait:.2f}ms "
            f"（{pct(total_wait, total_elapse):.1f}%）；Transit 接近 0 时优先看 rank 同步/串行化。</li>"
        )
    if worst:
        worst_sync = "—" if worst["sync"] is None else f"{worst['sync']:.2f}ms"
        home += (
            f"<li>通信占比最高的是 step{worst['id']}：{worst['comm_pct']:.1f}% "
            f"（{worst['comm_no'] / 1000:.2f}ms），步尾同步 {worst_sync}。</li>"
        )
    if windows and rows:
        rep = max(rows, key=lambda r: r["free"])
        sid = rep["id"]
        fs = free_stats.get(sid, {})
        if fs:
            top_p = max(fs, key=lambda p: fs[p]["free"])
            tl = free_attrs.get(sid, {}).get(top_p, {}).get("top", [])
            top_f = na.FUNC_LABELS.get(tl[0][0], tl[0][0]) if tl else "other"
            host_note = (
                "host 持续有事件，设备 Free 更可能来自下发节奏/依赖/通信等待"
                if free_attrs.get(sid, {}).get(top_p, {}).get("host_idle", 0) == 0
                else "host 存在空闲/阻塞"
            )
            home += (
                f"<li>Free 最高的是 step{sid}：{rep['free'] / 1000:.2f}ms，"
                f"{fs[top_p]['free'] / 1e6:.2f}ms（"
                f"{fs[top_p]['free'] / max(rep['free'] * 1000, 1) * 100:.0f}%）"
                f"落在 {top_p}，Top 去向 {esc(top_f)}；{esc(host_note)}。</li>"
            )
    home += (
        "<li>无 Python 栈，P1/P2/P3 和调用栈是“设备内核 + host 算子 + vllm 0.23.0 源码知识”的近似，"
        "不是真实 Python 栈；通信只作了解，不展开。</li>"
    )
    home += "</ul></div>"

    if api:
        home += (
            '<div class="sect"><h3>Host API Top</h3>'
            "<table><tr><th>API</th><th>总耗时</th><th>次数</th><th>平均</th></tr>"
        )
        for a in api[:10]:
            home += (
                f"<tr><td>{esc(a['name'])}</td><td>{a['time'] / 1000:.2f}ms</td>"
                f"<td>{a['count']}</td><td>{a['avg']:.2f}µs</td></tr>"
            )
        home += "</table></div>"

    if ops:
        home += (
            '<div class="sect"><h3>Host 算子 Top</h3>'
            "<table><tr><th>算子</th><th>Host 总耗时</th><th>Device 总耗时</th><th>次数</th></tr>"
        )
        for o in ops[:10]:
            home += (
                f"<tr><td>{esc(o['name'])}</td><td>{o['host'] / 1000:.2f}ms</td>"
                f"<td>{o['device'] / 1000:.2f}ms</td><td>{o['count']}</td></tr>"
            )
        home += '</table><div class="note">Host 总耗时包含嵌套调用，不能直接相加当墙钟，仅用于排序。</div></div>'

    home += (
        '<div class="sect"><h3>限制</h3><div class="note">'
        "无栈 trace 没有 python_function，无法生成火焰图和真实 Python 调用树；"
        "P1/P2/P3、模型结构和 Free 去向均为近似。"
        "step 边界来自 detect_steps.py 的侧车优先级（本报告优先 STEP_TIME）。"
        "</div></div></div>"
    )

    pages = ""
    for r in rows:
        sid = r["id"]
        page = (
            f'<div class="page" id="step{sid}"><h1>step{sid} 详细分析 '
            f'<span class="note">(窗口 {r["start"]:.3f}~{r["end"]:.3f}ms · '
            f"{r['dur']:.3f}ms · 判定: {esc(r['judge'])} · "
            f"四分类: {esc(class_src_text)})</span></h1>"
        )
        page += cf_box_html(r["comp_pct"], r["free_pct"], "Computing vs Free（本步重点）")
        page += (
            '<div class="stagebox"><h3>阶段总览</h3>'
            "<table><tr><th>阶段</th><th>耗时</th><th>占 step</th><th>Computing</th>"
            "<th>Communication</th><th>Comm 未重叠</th><th>Free</th><th>Preparing</th></tr>"
            f"<tr><td>全步</td><td>{r['dur']:.3f}ms</td><td>100.0%</td>"
            f"<td>{r['comp_pct']:.1f}%</td><td>{pct(r['comm'], r['stage']):.1f}%</td>"
            f"<td>{r['comm_pct']:.1f}%</td><td>{r['free_pct']:.1f}%</td>"
            f"<td>{r['prep'] / 1000:.2f}ms</td></tr></table>"
            f"<div>{bar(r['comp_pct'], r['comm_pct'], r['free_pct'])}</div>"
            '<div class="barleg">Computing<i style="background:#4A78A0"></i>'
            'Communication(未重叠)<i style="background:#B08968"></i>'
            'Free<i style="background:#A6BE9E"></i></div></div>'
        )

        w = windows.get(sid)
        if w:
            page += (
                '<div class="sect"><h3>阶段切分（设备内核顺序推导）</h3>'
                + na.phase_table_html(w, free_stats.get(sid, {}))
                + '<div class="note">P1=输入准备，P2=forward，P3=采样收尾；'
                "由首个 forward 内核、最后一个 logits MatMul / 首个采样内核推导。</div></div>"
            )
        if models.get(sid):
            page += '<div class="sect">' + models[sid]
            if layer_counts and sid in layer_counts:
                mx = max(layer_counts.values())
                if layer_counts[sid] != mx:
                    page += (
                        f'<div class="note">本步观测 ×{layer_counts[sid]}；完整结构见首页 ×{mx}。'
                        "少层来自本步执行路径/内核交错，不代表模型层数变化。</div>"
                    )
            page += "</div>"
        if calltrees.get(sid):
            page += (
                '<div class="sect"><h3>近似调用栈</h3>'
                + calltrees[sid]
                + '<div class="note">无 Python 栈；按 host 算子/设备内核 + vllm 0.23.0 源码知识推导，'
                "用于判断 host 在哪个阶段、哪类算子上。不是真实 Python 调用栈。</div></div>"
            )
        if free_attrs.get(sid):
            page += '<div class="sect"><h3>Free 去向（近似）</h3>'
            for p in ("P1", "P2", "P3"):
                a = free_attrs[sid].get(p)
                if a and a["total"] > 0:
                    page += f"<h4>{p}</h4>" + na.free_table_html(
                        a, used_total=free_stats[sid][p]["free"]
                    )
            page += "</div>"

        ks = (kernels or {}).get(sid, {})
        if ks:
            comp = sorted(
                [(n, v) for n, v in ks.items() if not is_comm(n)], key=lambda kv: -kv[1][0]
            )[:6]
            commk = sorted(
                [(n, v) for n, v in ks.items() if is_comm(n)], key=lambda kv: -kv[1][0]
            )[:5]
            if comp:
                page += (
                    '<div class="sect"><h3>计算算子 Top</h3>'
                    "<table><tr><th>算子</th><th>耗时</th><th>次数</th><th>Wait</th></tr>"
                )
                for n, v in comp:
                    page += (
                        f"<tr><td>{esc(n)}</td><td>{v[0] / 1000:.3f}ms</td>"
                        f"<td>{v[1]}</td><td>{v[2] / 1000:.3f}ms</td></tr>"
                    )
                page += "</table></div>"
            if commk:
                page += (
                    '<div class="sect"><h3>通信内核 Top</h3>'
                    "<table><tr><th>算子</th><th>耗时</th><th>次数</th><th>Wait</th></tr>"
                )
                for n, v in commk:
                    page += (
                        f"<tr><td>{esc(n)}</td><td>{v[0] / 1000:.3f}ms</td>"
                        f"<td>{v[1]}</td><td>{v[2] / 1000:.3f}ms</td></tr>"
                    )
                page += '</table><div class="note">多 stream 并行时内核耗时求和可超过 step 墙钟，用于排序。</div></div>'

        c = (comm or {}).get(f"step{sid}")
        if c:
            page += (
                '<div class="sect"><h3>通信明细</h3><table><tr><th>Elapse</th><th>Wait</th>'
                "<th>Transit</th><th>Sync</th></tr>"
                f"<tr><td>{c['elapse']:.3f}ms</td><td>{c['wait']:.3f}ms</td>"
                f"<td>{c['transit']:.3f}ms</td><td>{c['sync']:.3f}ms</td></tr></table>"
            )
            if c["ops"]:
                page += "<table><tr><th>通信算子</th><th>Wait</th><th>Elapse</th></tr>"
                for o in c["ops"][:5]:
                    page += (
                        f"<tr><td>{esc(o['name'])}</td><td>{o['wait']:.3f}ms</td>"
                        f"<td>{o['elapse']:.3f}ms</td></tr>"
                    )
                page += "</table>"
            page += "</div>"
        page += "</div>"
        pages += page

    return (
        f'<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        f"<title>Ascend 无栈 Profiling 报告</title><style>{css}</style></head><body>"
        f"{nav}{home}{pages}"
        "<script>function show(id){"
        'document.querySelectorAll(".page").forEach(p=>p.classList.remove("on"));'
        'document.querySelectorAll("nav button").forEach(b=>b.classList.remove("on"));'
        'document.getElementById(id).classList.add("on");'
        'document.getElementById("btn-"+id).classList.add("on");}'
        "</script></body></html>"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trace")
    ap.add_argument("--out", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--vllm-version", default=None)
    ap.add_argument("--vllm-ascend-version", default=None)
    ap.add_argument(
        "--model-config", default=None, help="模型 config.json（用于校验 num_hidden_layers）"
    )
    ap.add_argument("--vllm-src", default=None, help="vllm 源码目录")
    ap.add_argument("--vllm-ascend-src", default=None, help="vllm-ascend 源码目录")
    ap.add_argument(
        "--with-stack-trace", default=None, help="同模型/版本/场景的 with-stack trace（校准参考）"
    )
    args = ap.parse_args()
    out = args.out or os.path.splitext(args.trace)[0] + "_nostack_report.html"
    data = build(
        args.trace,
        args.model,
        args.vllm_version,
        args.vllm_ascend_version,
        args.model_config,
        args.vllm_src,
        args.vllm_ascend_src,
        args.with_stack_trace,
    )
    open(out, "w", encoding="utf-8").write(data)
    print(f"HTML 报告 → {out} ({len(data) / 1024:.1f}KB)")


if __name__ == "__main__":
    main()
