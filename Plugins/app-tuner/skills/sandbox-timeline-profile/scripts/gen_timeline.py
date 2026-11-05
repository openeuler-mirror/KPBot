#!/usr/bin/env python3
"""Generate a nested timeline SVG from CubeSandbox profile results.

Reads stage-latency data from profile-results-*.xlsx (or stage-latency.csv)
and draws a Gantt-style timeline using the EXACT stage_name from the data.

Positions are computed recursively from the stage topology: each child bar is
scaled to fit inside its parent's bar, so containment always holds visually
even when avg durations don't compose across components.

Usage:
    python gen_timeline.py [path/to/profile-results.xlsx] [output.svg]
"""

import math
import sys
from pathlib import Path
from openpyxl import load_workbook

# ── SVG constants ─────────────────────────────────────────
BAR_H, GAP = 24, 10
LEFT, RIGHT, TOP, BOTTOM = 280, 60, 40, 30
TOTAL_W = 1280
MARGIN_FRAC = 0.02  # inset on each side of a parent's window for its children

COMPONENT_COLORS = {
    "API":        "#4ECDC4",
    "CubeMaster": "#FF6B6B",
    "Cubelet":    "#45B7D1",
    "CubeShim":   "#96CEB4",
    "GuestAgent": "#DDA0DD",
    "Derived":    "#FFEAA7",
}

# ── data loading ──────────────────────────────────────────
def load_data(excel_path):
    """Return {stage_id: {name, component, avg_ms, parent_id, depth, notes}}."""
    wb = load_workbook(excel_path)
    ws = wb["Stage Latency"]
    stages = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        depth, comp, sid, sname, psid, cnt, avg, *rest = row
        if sid:
            stages[sid] = {
                "stage_name": sname or "",
                "component": comp or "",
                "avg_ms": round(float(avg), 2) if avg else 0,
                "parent_id": psid or "",
                "depth": int(depth) if depth is not None else 0,
                "notes": rest[-1] if rest else "",
            }
    return stages

# ── layout definition ─────────────────────────────────────
# Each entry: (stage_id, est_offset, kind)
#   est_offset: ESTIMATED absolute ms from api.create start. Only used to
#     establish relative order/offset between siblings; actual drawn position
#     is computed by fitting children into the parent's window.
#   kind: "bar" | "parallel" | "same-call" | "skip" | "section"
#   "same-call" = same gRPC call, different timer → draws dashed

LAYOUT = [
    # ── Top-level chain ──
    ("api.create",                       0,     "bar"),
    ("master.cube-e2e",                  16,    "bar"),
    ("cubelet.cubebox-service",          23,    "bar"),
    ("cubelet.cubebox",                  38,    "bar"),
    # ── Inside cubebox ──
    ("cubelet.sandbox-start",            45,    "bar"),
    ("cubelet.sandbox-create",           46,    "bar"),
    ("shim.CreatePodSandbox",            50,    "same-call"),
    # Shim 子阶段（顺序: LaunchVmm → RestoreVm → ResetVm → CreateSandbox → CreateContainer）
    ("shim.LaunchVmm",                   51,    "bar"),
    ("shim.RestoreVm",                   52,    "bar"),
    ("shim.ResetVm",                     63,    "bar"),
    ("shim.CreateSandbox",               67,    "bar"),
    ("shim.CreateContainer",             68,    "bar"),
    # CreateContainer 子阶段（顺序：先 receive → 再 restore path）
    ("guest.receive-to-restore-branch",  68.0,  "sub"),
    ("derived.restore-path-unresolved",  69.0,  "sub"),
    # storage + network ∥ sandbox-start
    ("cubelet.storage",                  45,    "parallel"),
    ("cubelet.network",                  45,    "parallel"),
    # Probe
    ("cubelet.sandbox-probe",            85,    "bar"),
    ("master.all-probe",                 85,    "bar"),
]

def build_bars(stages):
    """Build bar list from layout + data, including depth for indentation."""
    bars = []
    for stage_id, est_offset, kind in LAYOUT:
        s = stages.get(stage_id)
        if not s or kind == "skip":
            continue
        note = ""
        if kind == "same-call":
            note = "← 同一 gRPC 调用，两端各自计时"
        elif kind == "parallel":
            note = "⚡ 并行执行"
        elif kind == "sub":
            note = "parent: CreateContainer"
        elif kind == "span":
            note = "累积/跨阶段标记"
        bars.append({
            "id": stage_id, "label": s["stage_name"], "comp": s["component"],
            "est": float(est_offset), "dur": s["avg_ms"], "kind": kind,
            "depth": s["depth"], "parent": s["parent_id"], "note": note,
            "x": 0.0, "w": 0.0,
        })
    return bars

# ── recursive position fit ────────────────────────────────
def compute_positions(bars, stages):
    """Fit every bar inside its parent's window.

    Estimated offsets (est) define sibling order and rough placement; each
    level is uniformly scaled + shifted so that all children land inside the
    parent's drawn window. Bar width therefore represents the stage's share
    of its parent, while the label still shows the true avg_ms.
    """
    by_id = {b["id"]: b for b in bars}

    def nearest_drawn(pid):
        seen = set()
        while pid and pid not in by_id and pid not in seen:
            seen.add(pid)
            pid = stages.get(pid, {}).get("parent_id", "")
        return pid if pid in by_id else None

    children, roots = {}, []
    for b in bars:
        p = nearest_drawn(b["parent"])
        if p:
            children.setdefault(p, []).append(b)
        else:
            roots.append(b)

    def place(b, start, dur):
        b["x"], b["w"] = start, max(dur, 0.3)
        kids = children.get(b["id"], [])
        if not kids:
            return
        lo = min(k["est"] for k in kids)
        hi = max(k["est"] + k["dur"] for k in kids)
        span = max(hi - lo, 1e-6)
        inner = dur * (1 - 2 * MARGIN_FRAC)
        scale = inner / span
        off = start + dur * MARGIN_FRAC - lo * scale
        for k in sorted(kids, key=lambda k: k["est"]):
            place(k, off + k["est"] * scale, k["dur"] * scale)

    for r in roots:
        place(r, 0.0, r["dur"])

# ── render ───────────────────────────────────────────────
def render_svg(bars, output_path):
    max_end = max((b["x"] + b["w"] for b in bars), default=0)
    total_max_ms = max(int(math.ceil(max_end / 10.0) * 10), 10)
    scale = (TOTAL_W - LEFT - RIGHT) / total_max_ms

    def x_for(ms_val): return LEFT + ms_val * scale
    def w_for(ms_val): return max(ms_val * scale, 2)

    total_h = TOP + len(bars) * (BAR_H + GAP) + BOTTOM

    lines = []
    def add(s): lines.append(s)

    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{TOTAL_W}" height="{total_h}" font-family="Consolas,Monaco,monospace">')
    add(f'<rect width="{TOTAL_W}" height="{total_h}" fill="#1e1e2e"/>')

    # Grid lines
    for t in range(0, total_max_ms + 1, 10):
        cx = x_for(t)
        add(f'<line x1="{cx:.1f}" y1="{TOP}" x2="{cx:.1f}" y2="{total_h - BOTTOM}" stroke="#333355" stroke-width="1"/>')
        add(f'<text x="{cx:.1f}" y="{total_h - 10}" fill="#8888aa" font-size="10" text-anchor="middle">{t}ms</text>')

    # Bars
    DEPTH_STEP = 18  # pixels per depth level for label indentation
    LABEL_BASE = 6   # base x for depth=0 labels

    for i, b in enumerate(bars):
        label, comp, kind, note, depth = b["label"], b["comp"], b["kind"], b["note"], b["depth"]
        y = TOP + i * (BAR_H + GAP)
        bx = x_for(b["x"])
        bw = w_for(b["w"])
        dur = b["dur"]
        color = COMPONENT_COLORS.get(comp, "#888888")

        rx = min(4, bw / 2)
        opacity = "0.85"
        dash = ""
        bar_h = BAR_H
        label_font = 11
        label_offset = depth * DEPTH_STEP  # left-aligned label x

        if kind == "same-call":
            opacity = "0.5"
            dash = ' stroke-dasharray="4,3"'
        elif kind == "parallel":
            opacity = "0.7"
        elif kind == "span":
            opacity = "0.4"
            dash = ' stroke-dasharray="2,4"'
            bar_h = 14
            label_font = 9
        elif kind == "sub":
            opacity = "0.75"
            bar_h = 18
            label_font = 9
            label_offset += 10  # extra indent for sub-stages
            # connector line from parent
            cx = LABEL_BASE + label_offset + 2
            add(f'<line x1="{cx:.1f}" y1="{y - GAP}" x2="{cx:.1f}" y2="{y}" stroke="#555577" stroke-width="1"/>')
            add(f'<line x1="{cx:.1f}" y1="{y}" x2="{bx:.1f}" y2="{y}" stroke="#555577" stroke-width="1"/>')

        add(f'<rect x="{bx:.1f}" y="{y}" width="{bw:.1f}" height="{bar_h}" rx="{rx}" fill="{color}" opacity="{opacity}"{dash}/>')

        # stage_name: left-aligned at fixed x per depth, prevents label overlap
        lx = LABEL_BASE + label_offset
        add(f'<text x="{lx:.1f}" y="{y + bar_h - 7}" fill="#cdd6f4" font-size="{label_font}" text-anchor="start">{label}</text>')
        # component tag below the name
        comp_y = y + 11 if bar_h >= 20 else y + bar_h + 2
        add(f'<text x="{lx:.1f}" y="{comp_y}" fill="{color}" font-size="8" text-anchor="start">{comp}</text>')

        # duration inside bar (only if wide enough); shows true avg_ms
        if bw > 35 and dur >= 0.5:
            txt_y = y + bar_h - 7
            add(f'<text x="{bx + bw/2:.1f}" y="{txt_y}" fill="#1e1e2e" font-size="10" text-anchor="middle" font-weight="bold">{dur}ms</text>')

        # note on the right
        if note:
            nx = bx + bw + 8
            add(f'<text x="{nx}" y="{y + bar_h - 7}" fill="#a6adc8" font-size="9" text-anchor="start">{note}</text>')

    # Legend (top-left, above all bars)
    lx, ly = LEFT, 4
    for comp, color in COMPONENT_COLORS.items():
        add(f'<rect x="{lx}" y="{ly}" width="10" height="10" rx="2" fill="{color}"/>')
        add(f'<text x="{lx + 14}" y="{ly + 9}" fill="#a6adc8" font-size="9">{comp}</text>')
        lx += 90
    # dash legend
    add(f'<rect x="{lx}" y="{ly}" width="20" height="10" rx="2" fill="#96CEB4" opacity="0.5" stroke-dasharray="4,3" stroke="#96CEB4"/>')
    add(f'<text x="{lx + 24}" y="{ly + 9}" fill="#a6adc8" font-size="9">同调用对端</text>')

    # footnote
    add(f'<text x="{TOTAL_W - RIGHT}" y="14" fill="#666688" font-size="9" text-anchor="end">'
        f'子阶段宽度按父阶段窗口等比缩放，标签为真实 avg_ms</text>')

    add('</svg>')
    Path(output_path).write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved: {output_path}  ({len(bars)} stages, axis 0-{total_max_ms}ms)")

# ── main ─────────────────────────────────────────────────
if __name__ == "__main__":
    script_dir = Path(__file__).resolve().parent

    if len(sys.argv) > 1:
        xlsx = Path(sys.argv[1])
    else:
        # Auto-detect
        candidates = sorted(script_dir.glob("profile-results-*.xlsx"))
        xlsx = candidates[0] if candidates else script_dir / "profile-results-90.90-v2.xlsx"

    if not xlsx.exists():
        print(f"ERROR: {xlsx} not found")
        sys.exit(1)

    out = Path(sys.argv[2]) if len(sys.argv) > 2 else script_dir / "timeline.svg"

    stages = load_data(str(xlsx))
    print(f"Loaded {len(stages)} stages from {xlsx.name}")

    bars = build_bars(stages)
    compute_positions(bars, stages)
    render_svg(bars, str(out))
