#!/usr/bin/env python3
"""对比体系编排器（hub）：调度各专项 skill，聚合为统一报告。

hub 不实现任何指标；只做：
  1. 按 provider 表逐项调用子 skill（独立进程 + 超时隔离）
  2. 收集 A.1 JSON，做缺项 / 失败 / 坏 JSON / 超时降级
  3. 输出统一 HTML（总览 + 章节 + 数据来源矩阵）与 A.1 信封

用法:
  hub.py <base> [<cand>] [--out-html r.html] [--json r.json]
         [--only opstats,bubble] [--skip stack] [--timeout 300]
         [--skills-root DIR] [--base-steps 0,1 --cand-steps 5,6] [--min-delta-ms 0.05]
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_TIMEOUT = 300

# A.1 信封契约常量；真源为 kptools-ascend-profiling-compare/scripts/envelope.py。
# hub 保持独立安装，故在此忠实复制校验口径，不得弱化。
CONFIDENCE_VALUES = ("high", "medium", "low")
VERDICT_VALUES = ("improved", "regressed", "noise", "info")
METRIC_FIELDS = ("name", "unit", "base", "cand", "delta", "delta_pct",
                 "verdict", "noise_band", "evidence")
ENVELOPE_FIELDS = ("skill", "schema_version", "mode", "inputs", "data_quality",
                   "metrics", "per_step", "summary", "notes")

# provider 表：chapter 顺序即报告章节顺序
PROVIDERS = (
    {"skill": "kptools-ascend-profiling-compare", "script": "scripts/compare.py",
     "chapter": "总览：步耗时与四分类", "mode": "pair", "required": True, "prefix": "",
     "intro": "步耗时、Computing/Comm/Free/Preparing、瓶颈迁移、Top 算子时长差"},
    {"skill": "kptools-ascend-profiling-compare-opstats", "script": "scripts/opstats.py",
     "chapter": "算子维度：种类/次数/shape/分位数", "mode": "pair", "required": False, "prefix": "op_",
     "intro": "算子种类、每步调用次数、shape 漂移、p50/p95/p99、新增与消失"},
    {"skill": "kptools-ascend-profiling-compare-h2d-d2h", "script": "scripts/memcpy_diff.py",
     "chapter": "数据搬运：H2D/D2H/D2D", "mode": "pair", "required": False, "prefix": "memcpy_",
     "intro": "拷贝次数、字节、带宽、方向分布、与计算重叠率、同步占比"},
    {"skill": "kptools-ascend-profiling-stream", "script": "scripts/stream.py",
     "chapter": "流结构：忙碌占比与并发度", "mode": "both", "required": False, "prefix": "stream_",
     "intro": "每流忙碌占比、并发度时间线、跨流依赖"},
    {"skill": "kptools-ascend-profiling-compare-bubble", "script": "scripts/bubble.py",
     "chapter": "设备气泡：窗口与分类", "mode": "both", "required": False, "prefix": "bubble_",
     "intro": "空闲窗口位置/时长、三类归因（下发不足 unsupported）"},
    {"skill": "kptools-ascend-profiling-compare-free", "script": "scripts/free_breakdown.py",
     "chapter": "Free 构成：六类归因", "mode": "both", "required": False, "prefix": "free_",
     "intro": "每步 Free 的 host_busy/sync_wait/dispatch_gap/comm_align/memcpy_gap/unknown 构成"},
    {"skill": "kptools-ascend-profiling-compare-enqueue", "script": "scripts/enqueue.py",
     "chapter": "Host 下发：耗时与启动延迟", "mode": "pair", "required": False, "prefix": "enqueue_",
     "intro": "下发耗时分布、下发→设备启动延迟、每步下发总量、下发与执行重叠"},
    {"skill": "kptools-ascend-profiling-compare-dequeue", "script": "scripts/dequeue.py",
     "chapter": "Host 排队：下发完成→启动等待", "mode": "pair", "required": False, "prefix": "dequeue_",
     "intro": "排队等待分布、每步等待总量、等待占运行时间比例"},
    {"skill": "kptools-ascend-profiling-compare-fusion", "script": "scripts/fusion_diff.py",
     "chapter": "融合证据与归因", "mode": "pair", "required": False, "prefix": "fusion_",
     "intro": "N→1 合并证据（confirmed/suspected/unproven）、kernel 时长与数量归因"},
    {"skill": "kptools-ascend-profiling-compare-stack", "script": "scripts/stack_diff.py",
     "chapter": "调用栈差异", "mode": "pair", "required": False, "prefix": "stack_",
     "intro": "host 函数新增/消失、self/total 变化 Top"},
    {"skill": "kptools-ascend-profiling-compare-steps", "script": "scripts/step_groups.py",
     "chapter": "同一采集内 step 组对比", "mode": "single", "required": False, "prefix": "group_",
     "intro": "单份采集内两组 step 的同名指标对比（需 --base-steps/--cand-steps）"},
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _provider_tokens(provider):
    toks = {provider["skill"], provider["prefix"]}
    short = provider["prefix"].rstrip("_")
    if short:
        toks.add(short)
    return toks


def _match_filter(provider, tokens):
    return bool(_provider_tokens(provider) & set(tokens or ()))


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def round4(x, n=4):
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return 0.0


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _verdict_cls(v):
    return {"improved": "v-dev", "regressed": "v-host"}.get(v, "v-mid")


def validate_env(env):
    """校验 A.1 必需字段与类型（本函数只做结构/类型检查，不复制 envelope.validate 的全部语义）。"""
    if not isinstance(env, dict):
        return ["信封必须是 JSON 对象"]
    errs = []
    for k in ENVELOPE_FIELDS:
        if k not in env:
            errs.append(f"缺 {k}")
    if env.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version 应为 {SCHEMA_VERSION}")
    if env.get("mode") not in ("pair", "single"):
        errs.append("mode 非法")
    dq = env.get("data_quality")
    if not isinstance(dq, dict):
        errs.append("data_quality 非对象")
        dq = {}
    for k in ("sidecars", "degraded", "confidence"):
        if k not in dq:
            errs.append(f"data_quality 缺 {k}")
    if dq.get("confidence") not in CONFIDENCE_VALUES:
        errs.append("confidence 非法")
    if not isinstance(dq.get("sidecars"), list):
        errs.append("sidecars 非数组")
    if not isinstance(dq.get("degraded"), list):
        errs.append("degraded 非数组")
    metrics = env.get("metrics")
    if not isinstance(metrics, list):
        errs.append("metrics 非数组")
    elif not metrics:
        errs.append("metrics 不能为空")
    else:
        for i, m in enumerate(metrics):
            if not isinstance(m, dict):
                errs.append(f"metrics[{i}] 非对象")
                continue
            for k in METRIC_FIELDS:
                if k not in m:
                    errs.append(f"metrics[{i}] 缺 {k}")
            if m.get("verdict") not in VERDICT_VALUES:
                errs.append(f"metrics[{i}].verdict 非法")
            if not m.get("evidence"):
                errs.append(f"metrics[{i}].evidence 不能为空")
            for k in ("base", "cand", "delta", "delta_pct", "noise_band"):
                v = m.get(k)
                if v is not None and not _is_number(v):
                    errs.append(f"metrics[{i}].{k} 非数值")
    if not isinstance(env.get("per_step"), list):
        errs.append("per_step 非数组")
    if not isinstance(env.get("summary"), str) or not env.get("summary"):
        errs.append("summary 必须是非空字符串")
    if not isinstance(env.get("notes"), list):
        errs.append("notes 非数组")
    return errs


# ---------------------------------------------------------------------------
# provider 执行
# ---------------------------------------------------------------------------
def _build_cmd(skill_dir, provider, base, cand, opts, json_path, html_path):
    cmd = [sys.executable, str(skill_dir / provider["script"])]
    if provider["mode"] == "single" or not cand:
        cmd.append(base)
    else:
        cmd += [base, cand]
    cmd += ["--json", str(json_path), "--out-html", str(html_path)]
    if opts.get("min_delta_ms") is not None and provider["prefix"] in ("", "op_", "group_"):
        cmd += ["--min-delta-ms", str(opts["min_delta_ms"])]
    if provider["prefix"] in ("", "group_"):
        if opts.get("base_steps"):
            cmd += ["--base-steps", opts["base_steps"]]
        if opts.get("cand_steps"):
            cmd += ["--cand-steps", opts["cand_steps"]]
    return cmd


def run_provider(provider, skills_root, base, cand, opts, workdir, timeout):
    skill_dir = skills_root / provider["skill"]
    script = skill_dir / provider["script"]
    if not script.is_file():
        return {"status": "skipped", "reason": "not_installed", "env": None, "html": None}
    hint = ""
    if provider["mode"] == "single" and cand:
        hint = "使用 base 采集（单份模式）"
    if provider["mode"] == "pair" and not cand:
        return {"status": "skipped", "reason": "needs_two_captures", "env": None, "html": None}
    if provider["skill"].endswith("-steps") and not (opts.get("base_steps") and opts.get("cand_steps")):
        return {"status": "skipped", "reason": "needs_step_groups", "env": None, "html": None}

    json_path = workdir / f"{provider['skill']}.json"
    html_path = workdir / f"{provider['skill']}.html"
    cmd = _build_cmd(skill_dir, provider, base, cand, opts, json_path, html_path)
    try:
        run = subprocess.run(cmd, cwd=str(skill_dir), capture_output=True, text=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "failed", "reason": f"timeout({timeout}s)", "env": None, "html": None}
    except Exception as exc:
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}", "env": None, "html": None}
    if run.returncode != 0:
        tail = (run.stderr or run.stdout or "").strip().splitlines()[-1:] or [""]
        return {"status": "failed", "reason": f"rc={run.returncode}: {tail[0][:200]}",
                "env": None, "html": str(html_path) if html_path.exists() else None}
    if not json_path.exists():
        return {"status": "failed", "reason": "无 JSON 输出", "env": None,
                "html": str(html_path) if html_path.exists() else None}
    try:
        env = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "failed", "reason": f"JSON 解析失败: {exc}", "env": None,
                "html": str(html_path) if html_path.exists() else None}
    errs = validate_env(env)
    if errs:
        return {"status": "failed", "reason": "A.1 不合规: " + "; ".join(errs[:3]),
                "env": None, "html": str(html_path) if html_path.exists() else None}
    return {"status": "ok", "reason": hint, "env": env,
            "html": str(html_path) if html_path.exists() else None}


def orchestrate(base, cand, skills_root, only, skip, opts, workdir, timeout):
    rows = []
    for p in PROVIDERS:
        if only and not _match_filter(p, only):
            continue
        if _match_filter(p, skip):
            rows.append({**p, "status": "skipped", "reason": "user_skipped", "env": None, "html": None})
            continue
        result = run_provider(p, skills_root, base, cand, opts, workdir, timeout)
        rows.append({**p, **result})
    return rows


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------
def aggregate(rows, base, cand, opts):
    overview = next((r for r in rows if r["required"]), None)
    ok_rows = [r for r in rows if r["status"] == "ok"]
    degraded = []
    for r in rows:
        if r["status"] != "ok":
            degraded.append(f"{r['chapter']}[{r['skill']}]: {r['reason']}")

    metrics = []
    per_step = []
    if overview and overview["status"] == "ok":
        metrics = [dict(m, evidence=f"{overview['skill']} / {m.get('evidence', '')}")
                   for m in overview["env"].get("metrics") or []]
        per_step = overview["env"].get("per_step") or []
        summary = overview["env"].get("summary") or "总览可用"
        sidecars = list(overview["env"].get("data_quality", {}).get("sidecars") or [])
        conf = overview["env"].get("data_quality", {}).get("confidence", "medium")
    else:
        metrics = [{
            "name": "hub_chapter_ok", "unit": "count",
            "base": 0, "cand": len(ok_rows), "delta": -len(ok_rows),
            "delta_pct": 0.0, "verdict": "info", "noise_band": 0.0,
            "evidence": "provider 执行矩阵（总览不可用时的状态计数，不是性能指标）",
        }]
        per_step = []
        summary = f"总览不可用；{len(ok_rows)}/{len(rows)} 个章节成功，详见章节矩阵"
        sidecars = []
        conf = "low" if not ok_rows else "medium"

    for r in ok_rows:
        dq = r["env"].get("data_quality") or {}
        for s in dq.get("sidecars") or []:
            if s not in sidecars:
                sidecars.append(s)
        for d in dq.get("degraded") or []:
            tag = f"{r['chapter']}: {d}"
            if tag not in degraded:
                degraded.append(tag)

    order = {"high": 2, "medium": 1, "low": 0}
    for r in ok_rows:
        c = (r["env"].get("data_quality") or {}).get("confidence", "medium")
        if order.get(c, 1) < order.get(conf, 1):
            conf = c

    notes = [
        f"provider {len(ok_rows)}/{len(rows)} 成功",
        "hub 不重算任何指标，总览来自 kptools-ascend-profiling-compare",
    ]
    for r in ok_rows:
        for n in (r["env"].get("notes") or []):
            notes.append(f"{r['chapter']}: {n}")
    if opts.get("only"):
        notes.append("本次使用 --only 仅运行指定章节，其余章节未执行")
    if opts.get("filter_unmatched"):
        notes.append("未匹配任何章节的过滤项: " + ", ".join(opts["filter_unmatched"]))
    env = {
        "skill": "kptools-ascend-profiling-compare-hub",
        "schema_version": SCHEMA_VERSION,
        "mode": "pair" if cand else "single",
        "inputs": {"base": str(base), "cand": str(cand) if cand else None,
                   "steps": opts.get("steps_label") or "auto"},
        "data_quality": {"sidecars": sorted(sidecars), "degraded": degraded, "confidence": conf},
        "metrics": metrics,
        "per_step": per_step,
        "summary": summary,
        "notes": notes,
    }
    return env, rows


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def render_html(env, rows, css_path):
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    status_rows = "".join(
        f"<tr><td>{esc(r['chapter'])}</td><td>{esc(r['skill'])}</td>"
        f"<td><span class='verdict {'v-dev' if r['status']=='ok' else ('v-mid' if r['status']=='skipped' else 'v-host')}'>"
        f"{esc(r['status'])}</span></td><td>{esc(r['reason'] or '—')}</td>"
        f"<td>{esc(r['env'].get('summary', '')) if r['env'] else '—'}</td></tr>"
        for r in rows
    )
    matrix = ("<div class='sect'><h3>章节执行矩阵</h3><table><tr><th>章节</th><th>skill</th>"
              "<th>状态</th><th>原因</th><th>子结论</th></tr>" + status_rows + "</table></div>")

    chapters = []
    for r in rows:
        if r["status"] != "ok":
            chapters.append(
                f"<div class='sect'><h3>{esc(r['chapter'])}</h3>"
                f"<div class='note'>本章 skipped/failed：{esc(r['reason'] or 'unknown')}。"
                f"总报告不受影响。</div></div>")
            continue
        env_c = r["env"]
        mrows = "".join(
            f"<tr><td>{esc(m.get('name'))}</td><td>{esc(m.get('unit'))}</td>"
            f"<td>{esc(m.get('base'))}</td><td>{esc(m.get('cand'))}</td><td>{esc(m.get('delta'))}</td>"
            f"<td><span class='verdict {_verdict_cls(m.get('verdict'))}'>{esc(m.get('verdict'))}</span></td>"
            f"<td class='note'>{esc(m.get('evidence', ''))}</td></tr>"
            for m in env_c.get("metrics") or []
        )
        conf = (env_c.get("data_quality") or {}).get("confidence", "?")
        child_notes = (r.get("env") or {}).get("notes") or []
        note_html = "".join(
            f"<li>{esc(n)}</li>" for n in child_notes
        )
        notes_block = f"<ul class='note'>{note_html}</ul>" if note_html else ""
        chapters.append(
            f"<div class='sect'><h3>{esc(r['chapter'])}</h3>"
            f"<div class='kv'>{esc(r['intro'])}</div>"
            f"<div class='kv'>结论：<b>{esc(env_c.get('summary', ''))}</b> · 置信度 {esc(conf)}</div>"
            f"<table><tr><th>指标</th><th>单位</th><th>base</th><th>cand</th><th>Δ</th>"
            f"<th>判定</th><th>证据</th></tr>{mrows}</table>{notes_block}</div>")

    degraded = env["data_quality"]["degraded"]
    deg_html = ("<div class='sect'><h3>数据来源与降级</h3>"
                f"<div class='kv'>侧车：{esc(', '.join(env['data_quality']['sidecars']) or '无')}</div>"
                f"<div class='kv'>总体置信度：{esc(env['data_quality']['confidence'])}</div>"
                + ("<ul>" + "".join(f"<li>{esc(x)}</li>" for x in degraded) + "</ul>"
                   if degraded else "<div class='note'>无降级。</div>")
                + "</div>")
    hero = ("<div class='hero'>"
            f"<div class='htitle'>Profiling 对比总报告 · <span class='judge'>{esc(env['summary'])}</span></div>"
            f"<div class='hnums'>base {esc(env['inputs']['base'])}"
            f"<span class='sep'>→</span>cand {esc(env['inputs']['cand'] or '（单份）')}</div>"
            f"<div class='kv'>章节 {sum(1 for r in rows if r['status']=='ok')}/{len(rows)} 成功 · "
            f"契约置信度 {esc(env['data_quality']['confidence'])}</div></div>")
    return ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
            f"<title>Profiling 对比总报告</title><style>{css}</style></head><body>"
            f"<div class='page on'><h1>Profiling 对比总报告</h1>{hero}{matrix}"
            + "".join(chapters) + f"{deg_html}</div></body></html>")


def render_error(msg, base, cand, css_path):
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    return ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'><title>对比失败</title>"
            f"<style>{css}</style></head><body><div class='page on'><h1>对比失败</h1>"
            f"<div class='hero'><div class='htitle'>无法生成总报告</div>"
            f"<div class='hnums'>{esc(msg)}</div></div>"
            f"<div class='kv'>base: {esc(base)}</div><div class='kv'>cand: {esc(cand)}</div>"
            "</div></body></html>")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _split_list(text):
    return {x.strip() for x in (text or "").split(",") if x.strip()}


def _input_files(*roots):
    names = ("trace_view.json", "analysis.db", "kernel_details.csv", "step_trace_time.csv",
             "communication.json", "api_statistic.csv", "api_detail.csv")
    out = set()
    for root in roots:
        if not root:
            continue
        p = Path(root).expanduser()
        if p.is_file():
            out.add(p.resolve())
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and (f.name in names or f.name.startswith(
                        ("ascend_pytorch_profiler_", "profiler_info_"))):
                    out.add(f.resolve())
    return out


def _check_outputs(base, cand, out_html, out_json):
    inputs = _input_files(base, cand)
    if out_json and Path(out_html).expanduser().resolve() == Path(out_json).expanduser().resolve():
        raise ValueError("--out-html 与 --json 不能指向同一文件")
    for flag, path in (("--out-html", out_html), ("--json", out_json)):
        if not path:
            continue
        p = Path(path).expanduser()
        if p.is_symlink():
            raise ValueError(f"{flag} {p} 是符号链接，拒绝写入")
        if p.resolve() in inputs:
            raise ValueError(f"{flag} {p} 指向输入采集文件，拒绝覆盖")


def main(argv=None):
    ap = argparse.ArgumentParser(description="NPU profiling 对比体系编排器")
    ap.add_argument("base")
    ap.add_argument("cand", nargs="?", default=None)
    ap.add_argument("--out-html", default="profiling_hub_report.html")
    ap.add_argument("--json", dest="out_json", default=None)
    ap.add_argument("--only", default=None, help="逗号分隔：skill 名或指标前缀")
    ap.add_argument("--skip", default=None)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--skills-root", default=None,
                    help="子 skill 所在目录（默认取本 skill 的上级目录，测试用）")
    ap.add_argument("--workdir", default=None,
                    help="子报告与中间 JSON 的输出目录（默认临时目录；相对路径按当前 cwd 绝对化）")
    ap.add_argument("--base-steps", default=None)
    ap.add_argument("--cand-steps", default=None)
    ap.add_argument("--min-delta-ms", type=float, default=None)
    args = ap.parse_args(argv)

    try:
        _check_outputs(args.base, args.cand, args.out_html, args.out_json)
    except ValueError as exc:
        print(f"输出路径不安全，拒绝写入：{exc}", file=sys.stderr)
        return 3

    # 子进程 cwd 会切到各自 skill 目录，输入必须先用绝对路径
    args.base = os.path.abspath(os.path.expanduser(args.base))
    if args.cand:
        args.cand = os.path.abspath(os.path.expanduser(args.cand))

    here = Path(__file__).resolve().parent
    css_path = here.parent / "assets" / "report.css"
    skills_root = Path(args.skills_root).resolve() if args.skills_root else here.parent.parent

    import tempfile
    root_workdir = Path(args.workdir).expanduser().resolve() if args.workdir else Path(tempfile.mkdtemp())
    root_workdir.mkdir(parents=True, exist_ok=True)
    # 每轮独立子目录，避免复用 workdir 时命中上一轮同名 JSON。
    workdir = Path(tempfile.mkdtemp(prefix="run-", dir=str(root_workdir)))

    only_tokens = _split_list(args.only)
    skip_tokens = _split_list(args.skip)
    known_tokens = set()
    for p in PROVIDERS:
        known_tokens |= _provider_tokens(p)
    unmatched = sorted(t for t in (only_tokens | skip_tokens) if t not in known_tokens)
    if unmatched:
        print(f"警告：--only/--skip 未匹配任何章节：{', '.join(unmatched)}", file=sys.stderr)

    opts = {
        "only": only_tokens,
        "base_steps": args.base_steps,
        "cand_steps": args.cand_steps,
        "min_delta_ms": args.min_delta_ms,
        "filter_unmatched": unmatched,
        "steps_label": (f"base-steps={args.base_steps} cand-steps={args.cand_steps}"
                        if args.base_steps or args.cand_steps else None),
    }

    try:
        rows = orchestrate(args.base, args.cand, skills_root, only_tokens,
                           skip_tokens, opts, workdir, args.timeout)
        env, rows = aggregate(rows, args.base, args.cand, opts)
        self_errs = validate_env(env)
        if self_errs:
            env["data_quality"]["degraded"].append(
                "hub 自身 A.1 自检失败: " + "; ".join(self_errs[:3]))
            env["data_quality"]["confidence"] = "low"
            print("警告：hub 输出不符合 A.1：" + "; ".join(self_errs[:3]), file=sys.stderr)
    except Exception as exc:
        Path(args.out_html).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_html).write_text(render_error(f"{type(exc).__name__}: {exc}",
                                                    args.base, args.cand, css_path), encoding="utf-8")
        print(f"编排失败: {exc}", file=sys.stderr)
        return 3
    if args.out_json:
        Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_json).write_text(json.dumps(env, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON → {args.out_json}")
    Path(args.out_html).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_html).write_text(render_html(env, rows, css_path), encoding="utf-8")
    print(f"HTML → {args.out_html}")
    print(f"Workdir → {workdir}")
    for r in rows:
        print(f"  {r['chapter']:<28} {r['status']:<8} {r['reason']}")
    print(f"结论: {env['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
