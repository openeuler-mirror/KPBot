#!/usr/bin/env python3
"""对比体系统一 JSON 信封 v1。

字段定义见 references/compare-methodology.md §7（本 PR 内的权威定义）；
本模块的 `validate()` 是同一契约的可执行校验实现，也可直接运行：
    python3 scripts/envelope.py result.json

下游 hub 只消费 `metrics` / `summary` / `data_quality`；`per_step` 供深挖。
内部结果结构保持原样，本模块只做投影，不参与任何计算。
"""

import argparse
import json
import sys

SCHEMA_VERSION = 1

_MODE = {
    "aligned": "pair",
    "group": "pair",
    "whole_window": "pair",
    # 内部 single 指“两份采集指定单步对比”，仍是两份采集，统一信封下应为 pair；
    # 只有单份采集分析（本 skill 不提供）才是 single。
    "single": "pair",
}

_VERDICT = {
    "改善": "improved",
    "恶化": "regressed",
    "在噪声内": "noise",
    "方向不一致": "info",
}

_METRIC_NAMES = (
    ("dur", "step_time_ms"),
    ("comp", "computing_ms"),
    ("comm_no", "communication_not_overlapped_ms"),
    ("free", "free_ms"),
    ("prep", "preparing_ms"),
)

_VERDICT_VALUES = ("improved", "regressed", "noise", "info")
_CONFIDENCE_VALUES = ("high", "medium", "low")


def verdict_of(text):
    return _VERDICT.get(text, "info")


def mode_of(result):
    return _MODE.get(result.get("mode"), "pair")


def sidecars_of(result):
    out = []
    for side in ("base", "cand"):
        for name in result.get(side, {}).get("sidecars") or []:
            if name not in out:
                out.append(name)
    return out


def degradations_of(result):
    out = []
    for side in ("base", "cand"):
        for msg in result.get(side, {}).get("degradations") or []:
            tagged = f"{side}: {msg}"
            if tagged not in out:
                out.append(tagged)
    for msg in result.get("degradations") or []:
        if msg not in out:
            out.append(msg)
    return out


def confidence_of(result):
    """high = 双侧原生侧车、无降级、步间一致且步数足够；low = 整窗或步间方向不一致。"""
    if result.get("mode") == "whole_window":
        return "low"
    summary = result.get("summary") or {}
    dur = summary.get("dur") or {}
    if dur.get("heterogeneous"):
        return "low"
    kinds = {result.get("base", {}).get("kind"), result.get("cand", {}).get("kind")}
    if kinds != {"native"}:
        return "medium"
    if degradations_of(result):
        return "medium"
    if (summary.get("pair_count") or 0) < 3:
        return "medium"
    return "high"


def _evidence(result, key):
    base, cand = result["base"], result["cand"]
    if key == "dur":
        return f"step_source: {base['step_source']} / {cand['step_source']}"
    return f"class_source: {base['class_source']} / {cand['class_source']}"


def metrics_of(result):
    out = []
    for key, name in _METRIC_NAMES:
        m = result["summary"]["components"][key]
        out.append(
            {
                "name": name,
                "unit": "ms",
                "base": m["base_median_ms"],
                "cand": m["cand_median_ms"],
                "delta": m["delta_ms"],
                "delta_pct": m["pct"],
                "verdict": verdict_of(m["verdict"]),
                "noise_band": m["noise_ms"],
                # verdict 由逐对 Δ 的中位数判定；delta 是中位数之差，二者含义不同。
                "paired_delta": m.get("paired_delta_ms"),
                "sign_consistency": m.get("sign_consistency"),
                "evidence": _evidence(result, key),
            }
        )
    return out


def per_step_of(result):
    return [
        {
            "step": p["base_id"],
            "cand_step": p["cand_id"],
            "base": p["base_dur_ms"],
            "cand": p["cand_dur_ms"],
            "delta": p["delta_ms"]["dur"],
        }
        for p in result.get("pairs") or []
    ]


def to_envelope(result):
    notes = list(result.get("caveats") or [])
    noise_rule = result["summary"].get("noise_rule")
    if noise_rule:
        notes.append(f"噪声判定: noise = {noise_rule}")
    dur = result["summary"].get("dur") or {}
    if dur.get("heterogeneous"):
        notes.append(
            "步间变化方向不一致（"
            f"{dur.get('n_improved')} 步改善 / {dur.get('n_regressed')} 步恶化），"
            "步耗时结论仅供参考，建议按阶段分组比较"
        )
    if not result["summary"].get("components_comparable", True):
        notes.append("四分类来源跨族，分量指标仅展示数值，不做判定")
    return {
        "skill": "kptools-ascend-profiling-compare",
        "schema_version": SCHEMA_VERSION,
        "mode": mode_of(result),
        "inputs": {
            "base": result["base"]["path"],
            "cand": result["cand"]["path"],
            "steps": f"mode={result.get('mode')}, pairs={len(result.get('pairs') or [])}",
        },
        "data_quality": {
            "sidecars": sidecars_of(result),
            "degraded": degradations_of(result),
            "confidence": confidence_of(result),
        },
        "metrics": metrics_of(result),
        "per_step": per_step_of(result),
        "summary": result["summary"]["overall_verdict"],
        "notes": notes,
    }


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_str_list(x):
    return isinstance(x, list) and all(isinstance(v, str) for v in x)


def validate(env):
    """按 references/compare-methodology.md §7 校验信封，返回错误列表。"""
    errs = []
    if not isinstance(env, dict):
        return ["信封必须是 JSON 对象"]
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
    if not isinstance(env.get("skill"), str) or not env.get("skill"):
        errs.append("skill 必须是非空字符串")
    if env.get("schema_version") != SCHEMA_VERSION or isinstance(env.get("schema_version"), bool):
        errs.append(f"schema_version 应为整数 {SCHEMA_VERSION}")
    if env.get("mode") not in ("pair", "single"):
        errs.append("mode 必须是 pair | single")
    if env.get("mode") == "pair" and not (
        isinstance(env.get("inputs"), dict) and isinstance(env["inputs"].get("cand"), str)
        and env["inputs"].get("cand")
    ):
        errs.append("mode=pair 时 inputs.cand 必须是非空字符串")

    inputs = env.get("inputs")
    if not isinstance(inputs, dict):
        errs.append("inputs 必须是对象")
    else:
        for key in ("base", "cand", "steps"):
            if key not in inputs:
                errs.append(f"inputs 缺字段 {key}")
        for key in ("base", "steps"):
            if not isinstance(inputs.get(key), str) or not inputs.get(key):
                errs.append(f"inputs.{key} 必须是非空字符串")
        if inputs.get("cand") is not None and not isinstance(inputs.get("cand"), str):
            errs.append("inputs.cand 必须是字符串或 null")

    dq = env.get("data_quality") or {}
    if not isinstance(dq, dict):
        errs.append("data_quality 必须是对象")
    else:
        for key in ("sidecars", "degraded", "confidence"):
            if key not in dq:
                errs.append(f"data_quality 缺字段 {key}")
        if dq.get("confidence") not in _CONFIDENCE_VALUES:
            errs.append("data_quality.confidence 必须是 high | medium | low")
        if not _is_str_list(dq.get("sidecars")):
            errs.append("data_quality.sidecars 必须是字符串数组")
        if not _is_str_list(dq.get("degraded")):
            errs.append("data_quality.degraded 必须是字符串数组")

    metrics = env.get("metrics") or []
    if not isinstance(metrics, list) or not metrics:
        errs.append("metrics 必须是非空数组")
    else:
        seen = set()
        for i, m in enumerate(metrics):
            if not isinstance(m, dict):
                errs.append(f"metrics[{i}] 必须是对象")
                continue
            for key in (
                "name", "unit", "base", "cand", "delta", "delta_pct",
                "verdict", "noise_band", "evidence",
            ):
                if key not in m:
                    errs.append(f"metrics[{i}] 缺字段 {key}")
            name = m.get("name")
            if not isinstance(name, str) or not name:
                errs.append(f"metrics[{i}].name 必须是非空字符串")
            elif name in seen:
                errs.append(f"metrics[{i}].name 重复: {name}")
            else:
                seen.add(name)
            if not isinstance(m.get("unit"), str) or not m.get("unit"):
                errs.append(f"metrics[{i}].unit 必须是非空字符串")
            for key in ("base", "cand", "delta", "delta_pct", "noise_band"):
                if not _is_num(m.get(key)):
                    errs.append(f"metrics[{i}].{key} 必须是数值")
            if m.get("verdict") not in _VERDICT_VALUES:
                errs.append(f"metrics[{i}].verdict 非法: {m.get('verdict')}")
            if not isinstance(m.get("evidence"), str) or not m.get("evidence"):
                errs.append(f"metrics[{i}].evidence 不能为空")
            for key in ("paired_delta", "sign_consistency"):
                if m.get(key) is not None and not _is_num(m.get(key)):
                    errs.append(f"metrics[{i}].{key} 必须是数值或 null")
            sc = m.get("sign_consistency")
            if sc is not None and _is_num(sc) and not (0.0 <= float(sc) <= 1.0):
                errs.append(f"metrics[{i}].sign_consistency 必须在 [0,1]")
            if _is_num(m.get("base")) and _is_num(m.get("cand")) and _is_num(m.get("delta")):
                if abs((m["base"] - m["cand"]) - m["delta"]) > 0.01:
                    errs.append(f"metrics[{i}].delta 与 base−cand 不一致")
            if _is_num(m.get("base")) and _is_num(m.get("delta")) and _is_num(m.get("delta_pct")):
                # 小基数下 3 位小数舍入会显著放大 pct 误差，只在 base ≥ 0.1ms 时校验。
                if abs(m["base"]) >= 0.1:
                    expect = m["delta"] / m["base"] * 100.0
                    if abs(expect - m["delta_pct"]) > max(0.5, abs(expect) * 0.02):
                        errs.append(f"metrics[{i}].delta_pct 与 delta/base 不一致")

    if not isinstance(env.get("per_step"), list):
        errs.append("per_step 必须是数组")
    else:
        for i, p in enumerate(env["per_step"]):
            if not isinstance(p, dict):
                errs.append(f"per_step[{i}] 必须是对象")
                continue
            for key in ("step", "cand_step", "base", "cand", "delta"):
                if key not in p:
                    errs.append(f"per_step[{i}] 缺字段 {key}")
            for key in ("step", "cand_step"):
                if not isinstance(p.get(key), int) or isinstance(p.get(key), bool):
                    errs.append(f"per_step[{i}].{key} 必须是整数")
            for key in ("base", "cand", "delta"):
                if not _is_num(p.get(key)):
                    errs.append(f"per_step[{i}].{key} 必须是数值")
            if all(_is_num(p.get(k)) for k in ("base", "cand", "delta")):
                if abs((p["base"] - p["cand"]) - p["delta"]) > 0.01:
                    errs.append(f"per_step[{i}].delta 与 base−cand 不一致")

    if not isinstance(env.get("summary"), str) or not env.get("summary"):
        errs.append("summary 必须是非空字符串")
    if not _is_str_list(env.get("notes")):
        errs.append("notes 必须是字符串数组")
    return errs


def main(argv=None):
    ap = argparse.ArgumentParser(description="校验对比 JSON 信封（v1）")
    ap.add_argument("json", help="待校验的 JSON 文件")
    args = ap.parse_args(argv)
    try:
        with open(args.json, encoding="utf-8") as fh:
            env = json.load(fh)
    except Exception as exc:
        print(f"读取失败: {exc}", file=sys.stderr)
        return 2
    errs = validate(env)
    if errs:
        for e in errs:
            print(f"invalid: {e}", file=sys.stderr)
        return 1
    print("OK: 信封符合 compare-methodology.md §7")
    return 0


if __name__ == "__main__":
    sys.exit(main())
