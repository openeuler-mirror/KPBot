#!/usr/bin/env python3
"""生成 profiling 对比 HTML 报告。"""

import html
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from envelope import confidence_of, degradations_of, sidecars_of

CSS_PATH = HERE.parent / "assets" / "report.css"


def esc(s):
    return html.escape(str(s), quote=True)


def _css():
    return CSS_PATH.read_text(encoding="utf-8") if CSS_PATH.exists() else ""


def _num(x, n=3):
    try:
        return f"{float(x):.{n}f}"
    except (TypeError, ValueError):
        return "0." + "0" * n


def _verdict_class(v):
    if v == "改善":
        return "v-dev"
    if v == "恶化":
        return "v-host"
    return "v-mid"


def _ratio(bm, cm):
    try:
        bm, cm = float(bm), float(cm)
    except (TypeError, ValueError):
        return 0.0
    return cm / bm if bm > 0 else 0.0


def _table(headers, rows):
    out = ["<table><tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr>"]
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
    out.append("</table>")
    return "".join(out)


def render(result):
    css = _css()
    s = result["summary"]
    b = result["base"]
    c = result["cand"]
    dur = s["dur"]
    mode_label = {
        "aligned": "默认对齐（step N ↔ step N）",
        "single": "指定两步对比",
        "group": "指定两组 step 对比",
        "whole_window": "整窗对比（无可靠 step 边界）",
    }.get(result["mode"], result["mode"])

    banner = ""
    if result["mode"] == "whole_window":
        banner = (
            '<div class="note" style="border-left:4px solid #B08968;padding-left:10px">'
            "未切出可靠 step，已退化为整窗对比；结论只反映整段采集，不含逐步波动信息。</div>"
        )
    elif dur.get("heterogeneous"):
        banner = (
            '<div class="note" style="border-left:4px solid #B08968;padding-left:10px">'
            f"步间变化方向不一致（{dur.get('n_improved')} 步改善 / {dur.get('n_regressed')} 步恶化），"
            "整体步耗时结论仅供参考，建议按 prefill/decode 或阶段分组比较。</div>"
        )
    elif "未观察到实质变化" in s["overall_verdict"]:
        banner = (
            '<div class="note" style="border-left:4px solid #A6BE9E;padding-left:10px">'
            "两次采集的差异在噪声带内，不能判定为优化或退化。</div>"
        )

    hero = (
        '<div class="hero">'
        f'<div class="htitle">步耗时变化 · <span class="judge">{esc(s["overall_verdict"])}</span></div>'
        f'<div class="hnums">Δ 中位数之差 <b>{dur["delta_ms"]:+.3f}ms</b>'
        f'<span class="sep">·</span>base {_num(dur["base_median_ms"])}ms'
        f'<span class="sep">→</span>cand {_num(dur["cand_median_ms"])}ms'
        f'<span class="sep">·</span>×{_ratio(dur["base_median_ms"], dur["cand_median_ms"]):.2f}'
        f'<span class="sep">·</span>配对 {s["pair_count"]} 步</div>'
        f'<div class="barleg">噪声带 ±{_num(dur["noise_ms"])}ms（{esc(s["noise_rule"])}）</div>'
        "</div>"
    )

    src = (
        f'<div class="kv">base: {esc(b["label"])} · {esc(b["kind"])} · '
        f'step={esc(b["step_source"])} · 四分类={esc(b["class_source"])} · '
        f'torch_npu={esc(b["version"] or "?")} · rank={esc(b["rank"] if b["rank"] is not None else "?")}</div>'
        f'<div class="kv">cand: {esc(c["label"])} · {esc(c["kind"])} · '
        f'step={esc(c["step_source"])} · 四分类={esc(c["class_source"])} · '
        f'torch_npu={esc(c["version"] or "?")} · rank={esc(c["rank"] if c["rank"] is not None else "?")}</div>'
        f'<div class="kv">模式: {esc(mode_label)}</div>'
    )
    quality = (
        f'<div class="kv">契约置信度: {esc(confidence_of(result))} · '
        f'分量可比: {"是" if s.get("components_comparable", True) else "否（仅步耗时）"} · '
        f'侧车: {esc(", ".join(sidecars_of(result)) or "无（仅 trace_view.json）")}</div>'
    )

    caveat_lines = list(result["caveats"])
    # 降级说明与 JSON 的 data_quality.degraded 同源：把仅出现在 degraded 里的条目补进 HTML。
    for msg in degradations_of(result):
        raw = msg.split(": ", 1)[1] if msg.startswith(("base: ", "cand: ")) else msg
        if msg not in caveat_lines and raw not in caveat_lines:
            caveat_lines.append(msg)
    caveats = ""
    if caveat_lines:
        caveats = (
            '<div class="sect"><h3>数据一致性与降级说明</h3><ul>'
            + "".join(f"<li>{esc(x)}</li>" for x in caveat_lines)
            + "</ul></div>"
        )

    comp_rows = []
    for key in ("dur", "comp", "comm_no", "free", "prep"):
        m = s["components"][key]
        name = {
            "dur": "步耗时",
            "comp": "Computing",
            "comm_no": "Communication(未重叠)",
            "free": "Free",
            "prep": "Preparing",
        }[key]
        comp_rows.append(
            [
                esc(name),
                f'{_num(m["base_median_ms"])}ms',
                f'{_num(m["cand_median_ms"])}ms',
                f'{m["delta_ms"]:+.3f}ms',
                f'×{_ratio(m["base_median_ms"], m["cand_median_ms"]):.2f}',
                f'<span class="{_verdict_class(m["verdict"])}">{esc(m["verdict"])}</span>',
            ]
        )
    comp_table = _table(
        ["指标", "base 中位数", "cand 中位数", "Δ(base−cand)", "候选/基线", "判定"], comp_rows
    )

    bt = s["bottleneck"]
    contrib = "、".join(
        f'{x["name"]} {x["delta_ms"]:+.3f}ms' for x in s["contributions"]
    ) or "无明显超噪声分量"

    def _pcts_text(pcts):
        order = ("Computing", "Communication", "Free", "Preparing")
        parts = [f"{k} {float(pcts.get(k, 0.0)):.2f}%" for k in order if k in pcts]
        return " / ".join(parts)

    dominant_html = ""
    if bt.get("available", True):
        dominant_html = (
            f'<div class="kv">base 主导: {esc(bt["base"])}（{esc(_pcts_text(bt["base_pcts"]))}）'
            f' · cand 主导: {esc(bt["cand"])}（{esc(_pcts_text(bt["cand_pcts"]))}）</div>'
        )
    bottleneck = (
        '<div class="sect"><h3>瓶颈迁移</h3>'
        f"{dominant_html}"
        f'<div class="kv">结论: <b>{esc(bt["text"])}</b></div>'
        f'<div class="kv">主要变化分量: {esc(contrib)}</div></div>'
    )

    pair_rows = []
    for p in result["pairs"]:
        pair_rows.append(
            [
                f'step{p["base_id"]} / step{p["cand_id"]}',
                f'{_num(p["base_dur_ms"])}ms',
                f'{_num(p["cand_dur_ms"])}ms',
                f'{p["delta_ms"]["dur"]:+.3f}ms',
                f'{p["delta_ms"]["comp"]:+.3f}ms',
                f'{p["delta_ms"]["comm_no"]:+.3f}ms',
                f'{p["delta_ms"]["free"]:+.3f}ms',
                f'{p["delta_ms"]["prep"]:+.3f}ms',
            ]
        )
    pair_table = _table(
        ["step (base/cand)", "base 步长", "cand 步长", "Δ步长", "ΔComputing",
         "ΔComm", "ΔFree", "ΔPreparing"],
        pair_rows,
    )

    def kernel_table(rows):
        if not rows:
            return '<div class="note">无。</div>'
        body = []
        for r in rows:
            body.append(
                [
                    esc(r["name"]),
                    esc(r["type"] or "—"),
                    f'{_num(r["base_ms"])}ms',
                    f'{_num(r["cand_ms"])}ms',
                    f'{r["delta_ms"]:+.3f}ms',
                    f'{r["count_base"]} / {r["count_cand"]}',
                ]
            )
        return _table(["算子", "类型", "base/步", "cand/步", "Δ(base−cand)", "次数 b/c"], body)

    kernels = (
        '<div class="sect"><h3>Top 变快算子（候选更省）</h3>'
        + kernel_table(result["kernels"]["improved"])
        + "</div>"
        '<div class="sect"><h3>Top 变慢算子（候选更耗时）</h3>'
        + kernel_table(result["kernels"]["regressed"])
        + "</div>"
    )
    one_sided = result["kernels"].get("one_sided") or []
    if one_sided:
        rows = [
            [
                esc(r["name"]),
                esc(r["type"] or "—"),
                f'{r["base_ms"]}ms',
                f'{r["cand_ms"]}ms',
                f'{r["count_base"]} / {r["count_cand"]}',
                esc(r.get("present_in", "—")),
            ]
            for r in one_sided
        ]
        kernels += (
            '<div class="sect"><h3>仅单侧出现的算子（不计入 Top）</h3>'
            + _table(["算子", "类型", "base/步", "cand/步", "次数 b/c", "仅出现于"], rows)
            + '<div class="note">只在单侧出现的算子可能代表融合/删除，也可能是命名或路径差异，'
            "不参与快慢排名。</div></div>"
        )

    comm_html = ""
    if result["comm"]:
        rows = []
        names = {"elapse": "Elapse", "wait": "Wait", "transit": "Transit", "sync": "Sync"}
        for k, v in result["comm"].items():
            rows.append(
                [
                    esc(names.get(k, k)),
                    f'{_num(v["base_ms"])}ms',
                    f'{_num(v["cand_ms"])}ms',
                    f'{v["delta_ms"]:+.3f}ms',
                ]
            )
        comm_html = '<div class="sect"><h3>通信变化（每步均值）</h3>' + _table(
            ["指标", "base", "cand", "Δ(base−cand)"], rows
        ) + "</div>"

    if not result["comm"] and not result["api"]:
        comm_html = (
            '<div class="sect"><h3>通信 / Host API</h3><div class="note">'
            "输入未提供 communication.json / api_statistic.csv，本报告不比较通信与 Host API。</div></div>"
        )

    api_html = ""
    if result["api"]:
        rows = []
        comparable = all(r.get("comparable", True) for r in result["api"])
        for r in result["api"]:
            delta = "—" if r.get("delta_ms") is None else f'{r["delta_ms"]:+.3f}ms'
            rows.append(
                [
                    esc(r["name"]),
                    f'{_num(r["base_ms_per_step"])}ms' if r["base_ms_per_step"] is not None else "—",
                    f'{_num(r["cand_ms_per_step"])}ms' if r["cand_ms_per_step"] is not None else "—",
                    delta,
                    f'{r["count_base"]} / {r["count_cand"]}',
                ]
            )
        title = ("Host API 变化（整份采集每步均值）" if comparable
                 else "Host API（两侧分母口径不同，仅展示合计、不做比较）")
        api_html = f'<div class="sect"><h3>{title}</h3>' + _table(
            ["API", "base", "cand", "Δ(base−cand)", "次数 b/c"], rows
        ) + "</div>"

    limits = (
        '<div class="sect"><h3>口径与限制</h3><div class="note">'
        "Δ = base − cand，正值表示候选更省；×列为 cand/base（<1 更快）。"
        "步耗时 = StepTraceTime 的 stage + preparing（≈STEP_TIME 跨度）；"
        "表中的 Δ 是中位数之差（JSON 的 delta），判定用逐对 Δ 的中位数（JSON 的 paired_delta）与 MAD 噪声带；"
        "分量占比 = 分量 / 步耗时，分母含 Preparing，因此大 Preparing 采集里 Preparing 可能成为主导分量；"
        "步间方向不一致时不下改善/退化结论。未做跨采集时间戳对齐，默认按 step 位置配对；"
        "kernel 按选中 step 数归一化、通信算子实例后缀已归一化，仅单侧出现的算子单独列出、不参与排名；"
        "API 按采集总步数归一化。四分类来源跨族（CANN 侧车 vs trace 自建）时仅比较步耗时。"
        "无栈 trace 的四分类来自设备任务类型自建，缺少 Python 栈，阶段/调用栈信息不可用。</div></div>"
    )

    return (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        "<title>Ascend Profiling 对比报告</title>"
        f"<style>{css}</style></head><body>"
        '<nav><button class="on">总览</button></nav>'
        '<div class="page on" id="home"><h1>Profiling 对比 · 瓶颈变化</h1>'
        f"{banner}{hero}{src}{quality}"
        '<div class="sect"><h3>分量对比</h3>' + comp_table + "</div>"
        f"{bottleneck}"
        '<div class="sect"><h3>逐步配对</h3>' + pair_table
        + '<div class="note">Δ = base − cand；正值=候选更省。</div></div>'
        f"{kernels}{comm_html}{api_html}{caveats}{limits}"
        "</div></body></html>"
    )


def render_error(base_path, cand_path, error, detail=""):
    css = _css()
    return (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        "<title>Profiling 对比失败</title>"
        f"<style>{css}</style></head><body>"
        '<div class="page on" id="home"><h1>Profiling 对比失败</h1>'
        '<div class="hero" style="border-left-color:#B08968">'
        '<div class="htitle">无法完成对比</div>'
        f'<div class="hnums">{esc(error)}</div></div>'
        f'<div class="kv">base: {esc(base_path)}</div>'
        f'<div class="kv">cand: {esc(cand_path)}</div>'
        f'<div class="note">排查建议：确认输入目录含 ASCEND_PROFILER_OUTPUT 或 trace_view.json；'
        "纯 trace 输入会复用已有 skill 切 step 与自建四分类。</div>"
        f'<div class="sect"><h3>详细堆栈</h3><pre>{esc(detail)}</pre></div>'
        "</div></body></html>"
    )
