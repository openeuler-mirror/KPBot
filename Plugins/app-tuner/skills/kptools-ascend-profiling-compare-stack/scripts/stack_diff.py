#!/usr/bin/env python3
"""kptools-ascend-profiling-compare-stack：两份有栈 trace 的 Python 调用树 diff。

流程：先用上游 analysis skill 的 build_index.py（subprocess，允许）把 trace 建成
sqlite 索引；再直接读索引 events 表（cat='python_function'）按 (pid,tid) 重建树，
折叠机械层，按「self = dur − Σ直接子节点 dur」聚合到归一化函数名，最后做双向 diff。

区间折叠规则 COLLAPSE_SUBSTR 复制自
Plugins/app-tuner/skills/ascend_kunpeng/kptools-ascend-profiling-analysis/scripts/calltree.py，
索引缓存 key 写法参考 kptools-ascend-profiling-compare/scripts/kpc_core.py::_index_trace。
本 skill 不 import 任何其他 skill 的脚本。
"""

import argparse
import bisect
import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import sys

# 真实调用链可能深达上千层；默认递归上限 1000 会把合法输入误判为不可读。
sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
from pathlib import Path

SKILL_NAME = "kptools-ascend-profiling-compare-stack"
SCHEMA_VERSION = 1
HERE = Path(__file__).resolve().parent
SKILLS_DIR = HERE.parent.parent
CSS_PATH = HERE.parent / "assets" / "report.css"
CACHE_DIR = Path(
    os.environ.get(
        "KPBOT_STACK_CACHE_DIR",
        os.path.expanduser("~/.cache/kpbot_profiling_stack"),
    )
)

# 复制自 analysis/scripts/calltree.py::COLLAPSE_SUBSTR（机械层折叠）
COLLAPSE_SUBSTR = (
    ": _call_impl",
    "torch/_ops.py",
    "<built-in method",
    "_record_function_enter_new",
    "_record_function_exit",
    "profiler_interface.py",
    ": decorate_context",
    "_dispatcher",
    "logging/__init__.py",
    "logger.py",
    "): cast",
    ": req_ids",
    ": num_reqs",
    "autograd/profiler.py",
    "_has_torch_function",
    "torch/_tensor.py",
    "_get_device_index",
)

RE_TRAIL_FILE_LINE = re.compile(r"\s*\([^()]*:\d+\)\s*$")
RE_PY_LINE = re.compile(r"(\.pyc?)\(\d+\)")
RE_DIR_PREFIX = re.compile(
    r"(?:[A-Za-z]:)?[/\\]?(?:[\w.\-+@]+[/\\])+(?=[\w.\-+@]+\.pyc?)"
)


# ---------------------------------------------------------------------------
# 通用工具（复制自 kpc_core.py，按需裁剪）
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
    vals = [float(x) for x in xs if x is not None]
    if not vals:
        return 0.0
    m = _median(vals)
    return _median([abs(x - m) for x in vals])


def collapsed(name):
    return any(p in name for p in COLLAPSE_SUBSTR)


def normalize_name(name, strict=False):
    """去掉文件/行号噪声，保留「文件.py(*): 函数」形态；strict 时原样返回。"""
    text = str(name or "").strip()
    if strict:
        return text
    text = RE_TRAIL_FILE_LINE.sub("", text)
    text = RE_PY_LINE.sub(r"\1(*)", text)
    text = RE_DIR_PREFIX.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _unresolved(norm, strict):
    if strict:
        return False
    if not norm or not re.search(r"[A-Za-z0-9_]", norm):
        return True
    return bool(re.search(r"[\\/]", norm)) or bool(RE_PY_LINE.search(norm))


# ---------------------------------------------------------------------------
# 输入定位与索引
# ---------------------------------------------------------------------------
def locate_trace(path):
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"输入不存在: {p}")
    if p.is_file():
        if p.name == "trace_view.json":
            return p
        candidate = p.parent / "trace_view.json"
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"给定文件不是 trace_view.json 且同目录无 trace: {p}")
    for candidate in (
        p / "trace_view.json",
        p / "ASCEND_PROFILER_OUTPUT" / "trace_view.json",
    ):
        if candidate.is_file():
            return candidate
    def rank_key(hit):
        import re as _re
        rank = None
        for part in hit.parts:
            m = _re.search(r"(?:rank|device)[_-]?(\d+)$", part, _re.IGNORECASE)
            if m:
                rank = int(m.group(1))
        return (rank if rank is not None else 10**9, str(hit))
    hits = sorted(p.rglob("trace_view.json"), key=rank_key)
    if hits:
        return hits[0]
    raise FileNotFoundError(f"目录下未找到 trace_view.json: {p}")


def find_build_index(skills_dir=None):
    """兼容 skills/<name>/ 与 skills/<domain>/<name>/ 两种布局。"""
    root = Path(skills_dir) if skills_dir else SKILLS_DIR
    target = "kptools-ascend-profiling-analysis/scripts/build_index.py"
    direct = root / target
    if direct.is_file():
        return direct
    for pattern in (f"*/{target}", f"*/*/{target}"):
        hits = sorted(root.glob(pattern))
        if hits:
            return hits[0]
    return None


def _trace_has_python_function(trace_path):
    needle = b"python_function"
    tail = b""
    with open(trace_path, "rb") as fh:
        while True:
            chunk = fh.read(4 * 1024 * 1024)
            if not chunk:
                return False
            if needle in tail + chunk:
                return True
            tail = chunk[-len(needle):]


def _db_ready(path):
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = con.execute("SELECT value FROM meta WHERE key='has_stack'").fetchone()
        con.close()
        return bool(row)
    except Exception:  # noqa: BLE001 - 坏缓存直接重建
        return False


def build_cache_key(trace_path):
    st = os.stat(trace_path)
    raw = f"{os.path.abspath(trace_path)}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _index_trace(trace_path, timeout=900):
    build_index = find_build_index()
    if build_index is None:
        return None, "缺少 kptools-ascend-profiling-analysis/scripts/build_index.py"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    db = CACHE_DIR / f"{build_cache_key(trace_path)}.db"
    if db.exists() and db.stat().st_size > 0 and _db_ready(db):
        return str(db), None
    try:
        run = subprocess.run(
            [sys.executable, str(build_index), str(trace_path), "-o", str(db)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"build_index 超时（>{timeout}s）"
    except OSError as exc:
        return None, f"build_index 无法执行: {exc}"
    if run.returncode != 0 or not db.exists():
        tail = (run.stderr or run.stdout or "").strip().splitlines()[-3:]
        return None, "build_index 失败: " + " | ".join(tail)
    return str(db), None


# ---------------------------------------------------------------------------
# 建树 / 折叠 / 聚合
# ---------------------------------------------------------------------------
def build_tree(rows):
    """rows: [(ts, dur, name)] 已按 ts 升序；返回根节点列表。"""
    roots, stack = [], []
    for ts, dur, name in rows:
        end = ts + dur
        node = {"name": name, "start": ts, "end": end, "self": 0, "children": []}
        while stack and stack[-1]["end"] <= ts:
            stack.pop()
        i = len(stack) - 1
        while i >= 0 and stack[i]["end"] < end:
            i -= 1
        if i + 1 < len(stack):
            del stack[i + 1:]
        if i >= 0:
            stack[i]["children"].append(node)
        else:
            roots.append(node)
        stack.append(node)
    return roots


def _exclusive_intervals(start, end, intervals):
    """在 [start, end) 内扣除子帧区间后的剩余区间（子帧区间取并集，可重叠）。"""
    clipped = sorted(
        (max(start, s), min(end, e))
        for s, e in intervals
        if e > start and s < end
    )
    result, cur = [], start
    for s, e in clipped:
        if e <= cur:
            continue
        if s > cur:
            result.append((cur, s))
        cur = max(cur, e)
        if cur >= end:
            break
    if cur < end:
        result.append((cur, end))
    return result


def keep_tree(node):
    """折叠机械层：被折叠节点的子节点上提，自身时间归父。

    为保留跨步帧的独占时间，节点的 self 存为扣除直接子帧后的**区间列表**，
    供逐步交集分摊使用；self 标量等于这些区间长度之和。
    """
    kept_children = []
    for child in node["children"]:
        kept_children.extend(keep_tree(child))
    if collapsed(node["name"]):
        return kept_children
    node["children"] = kept_children
    node["excl"] = _exclusive_intervals(
        node["start"], node["end"],
        [(c["start"], c["end"]) for c in kept_children],
    )
    node["self"] = sum(e - s for s, e in node["excl"])
    return [node]


def count_collapsed(roots):
    total = 0
    stack = list(roots)
    while stack:
        node = stack.pop()
        if collapsed(node["name"]):
            total += 1
        stack.extend(node["children"])
    return total


def aggregate(roots, strict=False):
    agg = {}
    points = []
    unresolved = [0]

    def walk(node, ancestors):
        dur = node["end"] - node["start"]
        norm = normalize_name(node["name"], strict)
        if _unresolved(norm, strict):
            unresolved[0] += 1
            norm = "unresolved"
        entry = agg.setdefault(
            norm,
            {"self": 0, "total": 0, "count": 0, "display": node["name"], "aliases": set(),
             "paths": []},
        )
        entry["aliases"].add(node["name"])
        entry["self"] += node["self"]
        entry["total"] += dur
        entry["count"] += 1
        path = ancestors + [node["name"]]
        if not entry["paths"] or len(path) > len(entry["paths"][0]):
            entry["paths"] = [path]
        for seg_start, seg_end in node["excl"]:
            points.append((seg_start, seg_end - seg_start, norm))
        for child in node["children"]:
            walk(child, ancestors + [node["name"]])

    for root in roots:
        walk(root, [])
    return {
        "functions": agg,
        "points": points,
        "unresolved": unresolved[0],
        "aliases": sum(1 for e in agg.values() if len(e["aliases"]) > 1),
    }


def _per_step_totals(points, steps):
    """把每段独占时间按与各 step 窗口的交集分摊，跨步帧不再整体归到起始步。"""
    if not steps:
        return [], {}
    starts = [s for _i, s, _e in steps]
    sums = [0.0] * len(steps)
    by_name = {}
    for seg_start, seg_dur, norm in points:
        seg_end = seg_start + seg_dur
        if seg_dur <= 0:
            continue
        i = max(0, bisect.bisect_right(starts, seg_start) - 1)
        while i < len(steps):
            step_start, step_end = steps[i][1], steps[i][2]
            if step_start >= seg_end:
                break
            overlap = min(seg_end, step_end) - max(seg_start, step_start)
            if overlap > 0:
                sums[i] += overlap
                by_name.setdefault(norm, [0.0] * len(steps))[i] += overlap
            i += 1
    return [
        {"step": steps[i][0], "start": steps[i][1], "end": steps[i][2], "self": sums[i]}
        for i in range(len(steps))
    ], by_name


def load_profile(path, strict=False):
    trace = locate_trace(path)
    profile = {
        "path": str(Path(path).expanduser().resolve()),
        "trace": str(trace),
        "status": "ok",
        "reason": None,
        "degraded": [],
        "functions": {},
        "steps": [],
        "per_step": [],
        "func_step": {},
        "total_self": 0.0,
        "func_count": 0,
        "collapse_hits": 0,
        "unresolved": 0,
        "aliases": 0,
        "confidence": "medium",
        "cache_db": None,
    }
    if not _trace_has_python_function(trace):
        profile.update(
            status="no_stack",
            reason="trace_view.json 中未发现 cat=python_function 事件（未开启 with_stack）",
            confidence="low",
        )
        profile["degraded"].append(profile["reason"])
        return profile

    db, err = _index_trace(trace)
    if err:
        profile.update(
            status="no_index" if "缺少" in err else "failed",
            reason="索引失败（缺少可用 build_index/索引）：无法读取 with_stack 调用栈（上游 build_index 异常已省略，"
                   "请检查 trace 是否为纯 host / 是否含 python_function）",
            confidence="low",
        )
        profile["degraded"].append(err)
        return profile
    profile["cache_db"] = db

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        steps = con.execute(
            "SELECT id,start_ns,end_ns FROM steps ORDER BY start_ns"
        ).fetchall()
        raw = con.execute(
            "SELECT pid,tid,ts_ns,dur_ns,name FROM events "
            "WHERE cat='python_function' AND dur_ns>0 ORDER BY pid,tid,ts_ns,dur_ns DESC"
        ).fetchall()
        con.close()
    except Exception as exc:  # noqa: BLE001 - 索引异常按 failed 处理
        profile.update(status="failed", reason=f"索引读取失败: {exc}", confidence="low")
        profile["degraded"].append(profile["reason"])
        return profile

    if not raw:
        profile.update(
            status="no_stack",
            reason="索引中 python_function 事件数为 0，无法比较调用栈",
            confidence="low",
        )
        profile["degraded"].append(profile["reason"])
        return profile

    groups = {}
    for pid, tid, ts, dur, name in raw:
        groups.setdefault((pid, tid), []).append((ts, dur, name))
    roots = []
    for _key, group_rows in groups.items():
        roots.extend(build_tree(group_rows))

    collapse_hits = count_collapsed(roots)
    kept = []
    for root in roots:
        kept.extend(keep_tree(root))
    data = aggregate(kept, strict)
    per_step, func_step = _per_step_totals(data["points"], steps)
    total_self = sum(entry["self"] for entry in data["functions"].values())

    profile.update(
        functions=data["functions"],
        steps=[{"step": s, "start": a, "end": b} for s, a, b in steps],
        per_step=per_step,
        func_step=func_step,
        total_self=total_self,
        func_count=len(data["functions"]),
        collapse_hits=collapse_hits,
        unresolved=data["unresolved"],
        aliases=data["aliases"],
        confidence="medium",
    )
    return profile


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------
METRIC_DEFS = (
    ("stack_func_count", "count", "去重函数数"),
    ("stack_total_self_ms", "ms", "所有函数 self 时间和"),
    ("stack_top_self_ms", "ms", "变化最大函数的 self 时间"),
    ("stack_new_funcs", "count", "候选新增函数数"),
    ("stack_gone_funcs", "count", "基线消失函数数"),
    ("stack_changed_funcs", "count", "self 变化超阈值的函数数"),
)

COUNT_METRICS = {"stack_new_funcs", "stack_gone_funcs", "stack_changed_funcs"}


def _self_ms(profile, name):
    entry = profile["functions"].get(name)
    return (entry["self"] / 1e6) if entry else 0.0


def _per_step_ms(profile):
    return [p["self"] / 1e6 for p in profile["per_step"]]


def _func_step_ms(profile, name):
    return [v / 1e6 for v in profile["func_step"].get(name, [])]


def _paired_func_delta(base, cand, name):
    """同名函数逐 step self 的配对差值（按较短侧截齐）。"""
    b, c = _func_step_ms(base, name), _func_step_ms(cand, name)
    n = min(len(b), len(c))
    return [b[i] - c[i] for i in range(n)]


def _top_name(base, cand):
    # unresolved 是归一化失败的混合组，函数身份不可信，不参与「变化最大」排名。
    names = (set(base["functions"]) | set(cand["functions"])) - {"unresolved"}
    if not names:
        return None
    return min(sorted(names), key=lambda n: (-abs(_self_ms(base, n) - _self_ms(cand, n)), n))


def build_result(base, cand, strict, min_delta_ms):
    comparable = base["status"] == "ok" and cand["status"] == "ok"
    reasons = []
    if base["status"] != "ok":
        reasons.append(f"base: {base['reason']}")
    if cand["status"] != "ok":
        reasons.append(f"cand: {cand['reason']}")

    degraded = [f"base: {x}" for x in base["degraded"]]
    degraded += [f"cand: {x}" for x in cand["degraded"]]

    metrics = []
    details = {
        "new": [], "gone": [], "increased": [], "decreased": [], "path_examples": [],
    }

    if comparable:
        bnames = {n for n in base["functions"] if n != "unresolved"}
        cnames = {n for n in cand["functions"] if n != "unresolved"}
        common = bnames & cnames
        new_names = cnames - bnames
        gone_names = bnames - cnames
        deltas = {n: _self_ms(base, n) - _self_ms(cand, n) for n in common}

        top = _top_name(base, cand)
        top_base = _self_ms(base, top) if top else 0.0
        top_cand = _self_ms(cand, top) if top else 0.0
        paired_top = _paired_func_delta(base, cand, top) if top else []
        top_noise = (
            max(min_delta_ms, _mad(paired_top))
            if paired_top
            else (
                max(
                    min_delta_ms,
                    _mad(_func_step_ms(base, top)) + _mad(_func_step_ms(cand, top)),
                )
                if top
                else 0.0
            )
        )

        total_base = base["total_self"] / 1e6
        total_cand = cand["total_self"] / 1e6
        total_delta = total_base - total_cand
        bps, cps = _per_step_ms(base), _per_step_ms(cand)
        nps = min(len(bps), len(cps))
        paired_ps = [bps[i] - cps[i] for i in range(nps)]
        total_noise = max(
            min_delta_ms, _mad(paired_ps) if paired_ps else 0.0
        )

        changed = [n for n, d in deltas.items() if abs(d) > min_delta_ms]

        def verdict(delta, noise):
            if abs(delta) <= noise:
                return "noise"
            return "improved" if delta > 0 else "regressed"

        top_verdict = (
            verdict(_median(paired_top) if paired_top else (top_base - top_cand), top_noise)
            if top
            else "info"
        )

        metrics = [
            {
                "name": "stack_func_count",
                "unit": "count",
                "base": base["func_count"],
                "cand": cand["func_count"],
                "delta": base["func_count"] - cand["func_count"],
                "delta_pct": None,
                "verdict": "info",
                "noise_band": 0.0,
                "evidence": "sqlite:events(cat=python_function) 归一化函数名去重",
            },
            {
                "name": "stack_total_self_ms",
                "unit": "ms",
                "base": _round(total_base),
                "cand": _round(total_cand),
                "delta": _round(total_delta),
                "delta_pct": _round(total_delta / total_base * 100) if total_base else None,
                "verdict": verdict(
                    (_median(paired_ps) if paired_ps else total_delta), total_noise
                ),
                "noise_band": _round(total_noise),
                "evidence": "全量 self 仅作描述；判定用逐 step self 的逐对 Δ 中位数与 MAD",
            },
            {
                "name": "stack_top_self_ms",
                "unit": "ms",
                "base": _round(top_base),
                "cand": _round(top_cand),
                "delta": _round(top_base - top_cand),
                "delta_pct": (
                    _round((top_base - top_cand) / top_base * 100) if top_base else None
                ),
                "verdict": top_verdict,
                "noise_band": _round(top_noise),
                "evidence": (
                    f"函数: {top}; 判定用该函数逐 step self 的逐对 Δ 中位数与 MAD，"
                    "base/cand 展示全量"
                    if top else "无函数"
                ),
            },
            {
                "name": "stack_new_funcs",
                "unit": "count",
                "base": 0,
                "cand": len(new_names),
                "delta": -len(new_names),
                "delta_pct": None,
                "verdict": "info",
                "noise_band": 0.0,
                "evidence": "归一化函数名集合差（cand − base）",
            },
            {
                "name": "stack_gone_funcs",
                "unit": "count",
                "base": 0,
                "cand": len(gone_names),
                "delta": -len(gone_names),
                "delta_pct": None,
                "verdict": "info",
                "noise_band": 0.0,
                "evidence": "归一化函数名集合差（base − cand）",
            },
            {
                "name": "stack_changed_funcs",
                "unit": "count",
                "base": 0,
                "cand": len(changed),
                "delta": -len(changed),
                "delta_pct": None,
                "verdict": "info",
                "noise_band": 0.0,
                "evidence": f"|self(base)-self(cand)| > {min_delta_ms}ms",
            },
        ]

        def row(n, delta):
            return {
                "name": n,
                "display": base["functions"].get(n, cand["functions"].get(n))["display"],
                "base": _self_ms(base, n),
                "cand": _self_ms(cand, n),
                "delta": delta,
            }

        details["new"] = sorted(
            (row(n, -_self_ms(cand, n)) for n in new_names),
            key=lambda r: -r["cand"],
        )
        details["gone"] = sorted(
            (row(n, _self_ms(base, n)) for n in gone_names),
            key=lambda r: -r["base"],
        )
        details["increased"] = sorted(
            (row(n, deltas[n]) for n in common if deltas[n] < 0),
            key=lambda r: r["delta"],
        )
        details["decreased"] = sorted(
            (row(n, deltas[n]) for n in common if deltas[n] > 0),
            key=lambda r: -r["delta"],
        )
        key_funcs = sorted(common, key=lambda n: -abs(deltas[n]))[:5]
        details["path_examples"] = [
            {
                "name": n,
                "display": base["functions"].get(n, cand["functions"].get(n))["display"],
                "base_path": _deepest_path(base, n),
                "cand_path": _deepest_path(cand, n),
                "delta": deltas[n],
            }
            for n in key_funcs
        ]
        summary = (
            f"函数 {base['func_count']}→{cand['func_count']}；"
            f"self 总和 {total_base:.3f}→{total_cand:.3f}ms（Δ {total_delta:+.3f}ms）；"
            f"新增 {len(new_names)} / 消失 {len(gone_names)} / 变化 {len(changed)}"
        )
        per_step = []
        for i in range(max(len(base["per_step"]), len(cand["per_step"]))):
            b = base["per_step"][i]["self"] / 1e6 if i < len(base["per_step"]) else None
            c = cand["per_step"][i]["self"] / 1e6 if i < len(cand["per_step"]) else None
            step_id = (
                base["per_step"][i]["step"] if i < len(base["per_step"]) else cand["per_step"][i]["step"]
            )
            per_step.append(
                {
                    "step": step_id,
                    "base": _round(b) if b is not None else None,
                    "cand": _round(c) if c is not None else None,
                    "delta": _round(b - c) if (b is not None and c is not None) else None,
                }
            )
    else:
        summary = "无法比较调用栈：" + "；".join(reasons)
        per_step = []
        # 只给可用一侧的画像；不可比时所有指标不做 Δ
        def side_value(key, side, field=None):
            if side["status"] != "ok":
                return None
            if key == "func_count":
                return side["func_count"]
            return _round(side["total_self"] / 1e6)

        for key, unit, _label in METRIC_DEFS:
            if key in COUNT_METRICS:
                metrics.append(
                    {
                        "name": key, "unit": unit, "base": None, "cand": None, "delta": None,
                        "delta_pct": None, "verdict": "info", "noise_band": 0.0,
                        "evidence": "不可比：两侧未同时具备 python_function",
                    }
                )
                continue
            if key == "stack_func_count":
                b, c = side_value("func_count", base), side_value("func_count", cand)
            elif key == "stack_total_self_ms":
                b, c = side_value("total", base), side_value("total", cand)
            else:
                b = c = None
            metrics.append(
                {
                    "name": key, "unit": unit, "base": b, "cand": c,
                    "delta": None, "delta_pct": None, "verdict": "info",
                    "noise_band": 0.0,
                    "evidence": "不可比：两侧未同时具备 python_function" if key != "stack_top_self_ms"
                    else "不可比：无法确定变化最大函数",
                }
            )

    notes = []
    for label, prof in (("base", base), ("cand", cand)):
        if prof.get("cache_db"):
            notes.append(f"{label} 索引缓存: {prof['cache_db']}")
    notes.append(
        "self = 帧区间扣除直接子帧区间后的独占经过时间（跨线程累计，含阻塞/等待，不是 CPU 时间；"
        "判断 CPU 占用需线程 CPU 时钟或调度证据）；total = 节点自身区间时长"
    )
    notes.append("跨 step 的独占时间按其与各步窗口的交集分摊，逐步总量、函数统计与噪声带同源")
    notes.append(
        f"折叠规则: COLLAPSE_SUBSTR {len(COLLAPSE_SUBSTR)} 条机械层，子节点上提、折叠帧时间归父"
    )
    notes.append(
        "名称归一化: 去掉 (file:line) 后缀、file.py(N)→file.py(*)、目录前缀"
        + ("（--strict-line 已关闭归一化）" if strict else "")
    )
    if comparable:
        notes.append(
            f"归一化合并别名 {base['aliases'] + cand['aliases']} 组；"
            f"unresolved {base['unresolved'] + cand['unresolved']} 个"
        )
        notes.append("新增/消失/changed 是集合差异，不是量的 Δ（base 固定 0，verdict=info）")
        notes.append("Δ = base − cand，正值为候选更省；时间类指标 lower is better")
        if len(base["per_step"]) != len(cand["per_step"]):
            notes.append(
                f"两份 step 数不一致（base={len(base['per_step'])}, "
                f"cand={len(cand['per_step'])}），per_step 按序号对齐"
            )
    else:
        notes.append("不可比：不退化用 cpu_op 树硬比，不伪造 Δ")

    confidence = "low"
    if comparable and not degraded:
        confidence = "medium"
    if not comparable:
        degraded = degraded or reasons

    result = {
        "skill": SKILL_NAME,
        "schema_version": SCHEMA_VERSION,
        "mode": "pair",
        "inputs": {
            "base": base["path"],
            "cand": cand["path"],
            "steps": f"base={len(base['per_step'])}, cand={len(cand['per_step'])}",
        },
        "data_quality": {
            "sidecars": [],
            "degraded": degraded,
            "confidence": confidence,
        },
        "metrics": metrics,
        "per_step": per_step,
        "summary": summary,
        "notes": notes,
        "comparable": comparable,
        "_base": base,
        "_cand": cand,
        "details": {
            "new": details["new"][:10],
            "gone": details["gone"][:10],
            "increased": details["increased"][:10],
            "decreased": details["decreased"][:10],
        },
        "_details": details,
        "_reasons": reasons,
    }
    return result


def _deepest_path(profile, name):
    entry = profile["functions"].get(name)
    if not entry or not entry["paths"]:
        return []
    return max(entry["paths"], key=len)[-8:]


def public_result(result):
    return {k: v for k, v in result.items() if not k.startswith("_")}


def validate(env):
    errs = []
    for key in ("skill", "schema_version", "mode", "inputs", "data_quality",
                "metrics", "per_step", "summary", "notes"):
        if key not in env:
            errs.append(f"缺字段 {key}")
    if env.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version 应为 {SCHEMA_VERSION}")
    if env.get("mode") not in ("pair", "single"):
        errs.append("mode 必须是 pair | single")
    dq = env.get("data_quality") or {}
    for key in ("sidecars", "degraded", "confidence"):
        if key not in dq:
            errs.append(f"data_quality 缺字段 {key}")
    if dq.get("confidence") not in ("high", "medium", "low"):
        errs.append("confidence 必须是 high | medium | low")
    if not isinstance(dq.get("sidecars"), list):
        errs.append("data_quality.sidecars 必须是数组")
    if not isinstance(dq.get("degraded"), list):
        errs.append("data_quality.degraded 必须是数组")
    metrics = env.get("metrics") or []
    if not metrics:
        errs.append("metrics 不能为空")
    for i, m in enumerate(metrics):
        for key in ("name", "unit", "base", "cand", "delta", "delta_pct",
                    "verdict", "noise_band", "evidence"):
            if key not in m:
                errs.append(f"metrics[{i}] 缺字段 {key}")
        if m.get("verdict") not in ("improved", "regressed", "noise", "info"):
            errs.append(f"metrics[{i}].verdict 非法: {m.get('verdict')}")
        if not m.get("evidence"):
            errs.append(f"metrics[{i}].evidence 不能为空")
        if not str(m.get("name", "")).startswith("stack_"):
            errs.append(f"metrics[{i}].name 前缀非 stack_: {m.get('name')}")
    if not isinstance(env.get("per_step"), list):
        errs.append("per_step 必须是数组")
    if not isinstance(env.get("summary"), str) or not env.get("summary"):
        errs.append("summary 必须是非空字符串")
    if not isinstance(env.get("notes"), list):
        errs.append("notes 必须是数组")
    return errs


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def esc(value):
    return html.escape(str(value), quote=True)


def _css():
    return CSS_PATH.read_text(encoding="utf-8") if CSS_PATH.exists() else ""


EXTRA_CSS = """
.top2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:900px){.top2{grid-template-columns:1fr}}
.diffpos{color:#A65D57;font-weight:600}
.diffneg{color:#6E8B6E;font-weight:600}
.pathcell{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px;word-break:break-all}
.reason{border-left:4px solid #A65D57;padding-left:10px}
"""


def _fmt(x, digits=3, suffix=""):
    if x is None:
        return "—"
    try:
        return f"{float(x):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return esc(x)


def _table(headers, rows):
    out = ["<table><tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr>"]
    for row in rows:
        out.append("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _delta_cell(value):
    cls = "diffpos" if value > 0 else ("diffneg" if value < 0 else "")
    return f'<span class="{cls}">{value:+.3f}</span>'


def _top_rows(rows, limit):
    out = []
    for r in rows[:limit]:
        out.append(
            [
                f'<span class="pathcell">{esc(r["display"])}</span>',
                _fmt(r["base"]),
                _fmt(r["cand"]),
                _delta_cell(r["delta"]),
            ]
        )
    return out


def _section(title, body, cls="s-P2"):
    return f'<div class="sect"><h2 class="{cls}">{esc(title)}</h2>{body}</div>'


def _html_profile(label, profile):
    if profile["status"] != "ok":
        return (
            f'<div class="note reason">{esc(label)}: 不可用 — {esc(profile["reason"])}</div>'
        )
    return (
        f'<div class="kv">{esc(label)}: {esc(profile["trace"])}</div>'
        f'<div class="kv">函数 {profile["func_count"]} · '
        f'self 总和 {_fmt(profile["total_self"] / 1e6)}ms · '
        f'collapse {profile["collapse_hits"]} · unresolved {profile["unresolved"]} · '
        f'别名合并 {profile["aliases"]}</div>'
    )


def render_html(result):
    base, cand = result["_base"], result["_cand"]
    details = result["_details"]
    top = 10 if not result.get("_top") else result["_top"]
    comparable = result["comparable"]

    hero = (
        '<div class="hero">'
        f'<div class="htitle">调用栈 diff · {"可比较" if comparable else "不可比"}'
        f'<span class="judge"> {esc(result["summary"])}</span></div>'
        f'<div class="hnums">函数 <b>{base["func_count"]}</b> → <b>{cand["func_count"]}</b>'
        f'<span class="sep">·</span>self 总和 '
        f'<b>{_fmt(base["total_self"] / 1e6)}</b> → <b>{_fmt(cand["total_self"] / 1e6)}</b> ms'
        f'<span class="sep">·</span>置信度 {esc(result["data_quality"]["confidence"])}</div>'
        "</div>"
    )

    parts = [hero]
    parts.append(_section("输入与数据质量",
                          _html_profile("base", base) + _html_profile("cand", cand), "s-P1"))
    if not comparable:
        reason = "".join(f"<li>{esc(x)}</li>" for x in result["_reasons"])
        parts.append(
            _section(
                "不可比原因",
                '<div class="note reason">两侧未同时具备 python_function，'
                "按约定不退化成 cpu_op 树硬比，也不输出 Δ。</div>"
                f"<ul>{reason}</ul>",
                "s-P3",
            )
        )

    new_rows = _top_rows(details["new"], top)
    gone_rows = _top_rows(details["gone"], top)
    parts.append(
        _section(
            "新增 / 消失函数",
            '<div class="top2">'
            + '<div><h3>候选新增（Top %d）</h3>' % top
            + (_table(["函数", "base", "cand", "Δ"], new_rows)
               if new_rows else '<div class="note">无新增函数。</div>')
            + "</div>"
            + '<div><h3>基线消失（Top %d）</h3>' % top
            + (_table(["函数", "base", "cand", "Δ"], gone_rows)
               if gone_rows else '<div class="note">无消失函数。</div>')
            + "</div></div>",
            "s-P1",
        )
    )

    inc_rows = _top_rows(details["increased"], top)
    dec_rows = _top_rows(details["decreased"], top)
    parts.append(
        _section(
            "self 时间变化（双向 Top %d）" % top,
            '<div class="top2">'
            + "<div><h3>候选变多（self 增大）</h3>"
            + (_table(["函数", "base ms", "cand ms", "Δ"], inc_rows)
               if inc_rows else '<div class="note">无 self 增大的函数。</div>')
            + "</div>"
            + "<div><h3>候选变少（self 减小）</h3>"
            + (_table(["函数", "base ms", "cand ms", "Δ"], dec_rows)
               if dec_rows else '<div class="note">无 self 减小的函数。</div>')
            + "</div></div>",
            "s-P2",
        )
    )

    path_rows = [
        [
            f'<span class="pathcell">{esc(p["display"])}</span>',
            f'<span class="pathcell">{esc(" &gt; ".join(p["base_path"]) or "—")}</span>',
            f'<span class="pathcell">{esc(" &gt; ".join(p["cand_path"]) or "—")}</span>',
            _delta_cell(p["delta"]),
        ]
        for p in details["path_examples"]
    ]
    parts.append(
        _section(
            "关键调用路径变化示例",
            _table(["函数", "base 路径", "cand 路径", "Δ self"], path_rows)
            if path_rows else '<div class="note">无可对齐的共有函数。</div>',
            "s-P2",
        )
    )

    metric_rows = []
    for m in result["metrics"]:
        metric_rows.append(
            [
                esc(m["name"]),
                _fmt(m["base"]),
                _fmt(m["cand"]),
                _fmt(m["delta"]),
                _fmt(m["noise_band"]),
                esc(m["verdict"]),
                f'<span class="pathcell">{esc(m["evidence"])}</span>',
            ]
        )
    parts.append(_section("指标", _table(
        ["指标", "base", "cand", "Δ", "噪声带", "判定", "证据"], metric_rows), "s-P1"))

    step_rows = [
        [str(p["step"]), _fmt(p.get("base")), _fmt(p.get("cand")), _fmt(p.get("delta"))]
        for p in result["per_step"]
    ]
    parts.append(
        _section(
            "逐 step self 总和",
            _table(["step", "base ms", "cand ms", "Δ"], step_rows)
            if step_rows else '<div class="note">无 step 边界或不可比。</div>',
            "s-P2",
        )
    )

    degrade = "".join(f"<li>{esc(x)}</li>" for x in result["data_quality"]["degraded"])
    notes = "".join(f"<li>{esc(x)}</li>" for x in result["notes"])
    parts.append(
        _section(
            "降级说明与口径",
            (f"<h3>降级项</h3><ul>{degrade}</ul>" if degrade
             else '<div class="note">无降级。</div>')
            + f"<h3>归一化/折叠/self 口径</h3><ul>{notes}</ul>",
            "s-P3",
        )
    )

    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>NPU profiling 调用栈 diff</title><style>"
        + _css()
        + EXTRA_CSS
        + "</style></head><body><div class=\"page on\">"
        + "<h1>Ascend Profiling 调用栈 diff（python_function 调用树）</h1>"
        + "".join(parts)
        + "</div></body></html>"
    )


def render_error(err):
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>调用栈 diff 失败</title><style>"
        + _css()
        + '</style></head><body><div class="page on"><h1>调用栈 diff 失败</h1>'
        '<div class="sect"><div class="note reason">输入不可读或无法定位 trace_view.json。</div>'
        f"<pre>{esc(err)}</pre></div></div></body></html>"
    )


def error_result(base, cand, err):
    metrics = [
        {"name": key, "unit": unit, "base": 0, "cand": 0, "delta": 0,
         "delta_pct": None, "verdict": "info", "noise_band": 0.0,
         "evidence": f"输入不可读: {err}"}
        for key, unit, _label in METRIC_DEFS
    ]
    return {
        "skill": SKILL_NAME,
        "schema_version": SCHEMA_VERSION,
        "mode": "pair",
        "inputs": {"base": str(base), "cand": str(cand), "steps": "不可读"},
        "data_quality": {
            "sidecars": [],
            "degraded": [f"输入不可读: {err}"],
            "confidence": "low",
        },
        "metrics": metrics,
        "per_step": [],
        "summary": f"调用栈 diff 失败: {err}",
        "notes": ["输入不可读时仍输出 HTML 错误页与 JSON，退出码 3"],
        "comparable": False,
    }


def run(base_path, cand_path, strict=False, min_delta_ms=0.05, top=10):
    base = load_profile(base_path, strict)
    cand = load_profile(cand_path, strict)
    result = build_result(base, cand, strict, min_delta_ms)
    result["_top"] = top
    errs = validate(public_result(result))
    if errs:
        result["notes"].extend(f"A.1 自检: {e}" for e in errs)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="两份有栈 NPU trace 的 Python 调用树 diff"
    )
    ap.add_argument("base")
    ap.add_argument("cand")
    ap.add_argument("--out-html", default="stack_diff.html")
    ap.add_argument("--json", dest="json_out", default="stack_diff.json")
    ap.add_argument("--strict-line", action="store_true",
                    help="保留原始函数名（不做行号/路径归一化）")
    ap.add_argument("--min-delta-ms", type=float, default=0.05)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args(argv)

    try:
        result = run(args.base, args.cand, args.strict_line, args.min_delta_ms, args.top)
        html_text = render_html(result)
        rc = 0
    except RecursionError:
        msg = "调用栈过深（超过递归上限），建议先用 keep-depth 裁剪或改用更浅的采集"
        print(f"错误: {msg}", file=sys.stderr)
        try:
            Path(args.out_html).write_text(render_error(msg), encoding="utf-8")
        except Exception:
            pass
        return 3
    except Exception as exc:  # noqa: BLE001 - 保证一定产出错误页
        result = error_result(args.base, args.cand, exc)
        html_text = render_error(exc)
        rc = 3

    out_html = Path(args.out_html)
    out_json = Path(args.json_out)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(html_text, encoding="utf-8")
    out_json.write_text(
        json.dumps(public_result(result), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(result["summary"])
    print(f"HTML: {out_html}")
    print(f"JSON: {out_json}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
