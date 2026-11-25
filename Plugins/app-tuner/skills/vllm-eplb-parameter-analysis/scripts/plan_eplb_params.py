#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""plan_eplb_params.py -- turn an EPLB verdict into an executable parameter plan.

Consumes the ``verdict.json`` produced by ``vllm-eplb-analysis`` plus site facts
(ep_size, logical experts, per-slot memory cost, free memory, planned benchmark
steps, target branch) and emits ``eplb_parameter_plan.json`` for
``vllm-eplb-deploy-validate``.

This script does not touch any launch template and does not run benchmarks.
"""
import argparse
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recommend_redundancy as rr  # noqa: E402

SCHEMA_VERSION = 1


def die(msg, code=2):
    print("ERROR: %s" % msg, file=sys.stderr)
    sys.exit(code)


def template_changes_dynamic(extra, window, interval):
    return {
        "env": {"VLLM_USE_V2_MODEL_RUNNER": "1", "DYNAMIC_EPLB": "true"},
        "unset_env": [],
        "add_flags": [
            "--enable-eplb",
            "--eplb-config.num_redundant_experts %d" % extra,
            "--eplb-config.window_size %d" % window,
            "--eplb-config.step_interval %d" % interval,
        ],
        "additional_config": {},
    }


def template_changes_force():
    return {
        "env": {"VLLM_USE_V2_MODEL_RUNNER": "0"},
        "unset_env": ["DYNAMIC_EPLB"],
        "add_flags": [],
        "additional_config": {"enable_force_eplb": True},
    }


def _cost_from_args(args):
    if args.bytes_per_expert_per_layer_gib is not None:
        if args.num_moe_layers is None:
            return None
        return args.bytes_per_expert_per_layer_gib * args.num_moe_layers
    return args.bytes_per_expert_gib


def _analyse(args, n, e, redundant):
    ns = SimpleNamespace(
        logical_experts=n,
        ep_size=e,
        redundant_experts=redundant,
        ratio=None,
        bytes_per_expert_gib=args.bytes_per_expert_gib,
        bytes_per_expert_per_layer_gib=args.bytes_per_expert_per_layer_gib,
        num_moe_layers=args.num_moe_layers,
        free_gib=args.free_gib,
    )
    return rr.analyse(ns)


def _cap_for_benchmark(window, interval, benchmark_steps, warnings):
    if not benchmark_steps or benchmark_steps <= 0:
        return window, interval
    cap = max(1, benchmark_steps // 4)  # aim for >= ~4 rebalance windows
    if interval > cap:
        warnings.append(
            "step_interval=%d 大于压测计划步数 %d 的 1/4，压测期内可能不触发重平衡；"
            "已收敛到 %d（window_size 同步不超过它）" % (interval, benchmark_steps, cap))
        interval = cap
        if window > cap:
            window = cap
    return window, interval



def _resolve_mode(verdict_v, action_in, rationale, warnings):
    """Map verdict/action to a mode and reject conflicting fields."""
    expected = {
        "imbalanced": "optimize_dynamic",
        "borderline": "probe_ceiling_first",
        "balanced": "do_not_enable",
        "inconclusive": "collect_more",
    }.get(verdict_v)
    action = action_in
    if action is None:
        action = expected or "collect_more"
        rationale.append("verdict=%s 未带 action，按 verdict 映射为 %s" % (verdict_v or "缺失", action))

    dynamic_actions = ("optimize_dynamic", "optimize_with_cap")
    if action in dynamic_actions and verdict_v != "imbalanced":
        warnings.append("字段冲突：action=%s 但 verdict=%s；按 hold 处理，需重新确认失衡证据"
                        % (action, verdict_v or "缺失"))
        return action, "hold"
    if action == "do_not_enable" and verdict_v == "imbalanced":
        warnings.append("字段冲突：verdict=imbalanced 但 action=do_not_enable；按 hold 处理")
        return action, "hold"

    if action in ("do_not_enable", "free_memory_first"):
        if action == "free_memory_first":
            warnings.append("显存无余量：先释放/测量显存或降低 KV 压力，再回到参数分析")
        return action, "off"
    if action == "collect_more":
        rationale.append("analysis 证据不足（inconclusive/collect_more），先补采证据")
        return action, "hold"
    if action == "probe_ceiling_first":
        rationale.append("analysis 仅单一/不稳定信号：先做 force 上限探针，测完还原 off")
        return action, "force"
    if action in dynamic_actions:
        rationale.append("analysis 判定失衡（action=%s）" % action)
        return action, "dynamic"
    warnings.append("未知 action=%r，按 hold 处理，请人工确认" % (action,))
    return action, "hold"


def build_plan(verdict, args):
    rec = verdict.get("recommendation") or {}
    verdict_v = str(verdict.get("verdict") or "").lower()
    handoff = rec.get("handoff") or {}
    mem_hint = rec.get("memory") or {}
    rationale, warnings = [], []
    action, mode = _resolve_mode(verdict_v, rec.get("action"), rationale, warnings)

    # The branch determines whether DYNAMIC_EPLB / enable_force_eplb exist at all.
    if mode in ("dynamic", "force"):
        if args.branch == "upstream":
            warnings.append("目标为上游 vLLM：不含 DYNAMIC_EPLB/enable_force_eplb，"
                            "dynamic/force 会静默无效；改为 off")
            rationale.append("branch=upstream 强制 off")
            mode = "off"
        elif args.branch != "site-fork":
            warnings.append("未确认站点分支（--branch 需显式给 site-fork）：dynamic/force 依赖站点开关，"
                            "按 hold 处理")
            rationale.append("branch 未确认，停在参数分析交接")
            mode = "hold"

    plan = {
        "schema_version": SCHEMA_VERSION,
        "verdict": verdict.get("verdict"),
        "action": action,
        "mode": mode,
        "ep_size": args.ep_size,
        "redundant_experts": None,
        "ratio_pct": None,
        "per_rank_extra": None,
        "memory": None,
        "window_size": None,
        "step_interval": None,
        "rationale": rationale,
        "warnings": warnings,
        "template_changes": {"env": {}, "unset_env": [], "add_flags": [],
                             "additional_config": {}},
        "source": {"analysis_memory_verdict": mem_hint.get("verdict"),
                   "analysis_next": rec.get("next"),
                   "analysis_notes": rec.get("notes") or []},
    }

    # Never drop the upstream memory gate / OOM warning.
    if mem_hint.get("verdict") == "unknown":
        warnings.append("上游显存余量=unknown：上线前必须确认 KV 占用与剩余显存，"
                        "否则可能 OOM 或频繁换出")
    elif mem_hint.get("verdict") == "no_headroom":
        warnings.append("上游判定显存无余量：先释放/降低 KV 压力，不要直接加副本")

    if mode == "force":
        plan["rationale"].append("force 仅用于给 dynamic 的剩余空间定标，不可上线")
        plan["template_changes"] = template_changes_force()
        return plan
    if mode in ("off", "hold"):
        return plan

    # --- dynamic: resolve and validate parameters ---
    n = args.logical_experts
    e = args.ep_size
    if not n or n <= 0:
        die("--mode dynamic 需要 --logical-experts（>0）")
    if not e or e <= 0:
        die("--mode dynamic 需要 --ep-size（>0）")
    if n % e != 0:
        die("logical_experts (%d) 不能被 ep_size (%d) 整除，无法均分到各 rank" % (n, e))

    cost = _cost_from_args(args)
    redundant = handoff.get("redundant_experts")
    affordable = mem_hint.get("affordable_redundant")
    if redundant is None:
        if affordable is not None:
            redundant = int(affordable)
            warnings.append("handoff 未给冗余专家数：采用 analysis 可负担上限 %d" % redundant)
        elif args.free_gib is not None and cost:
            per_rank = int(args.free_gib // cost)
            redundant = per_rank * e
            warnings.append("handoff 未给冗余专家数：按显存余量估算为 %d（%d/rank）"
                           % (redundant, per_rank))
        else:
            die("handoff 未给 redundant_experts，且缺少 --free-gib/单副本成本；"
                "请补显存事实或显式给 candidate")

    # Dynamic writes real memory-hungry copies; without evidence we must not ship it.
    if args.free_gib is None or not cost:
        warnings.append("缺少显存证据（--free-gib 与单副本成本），无法核实预算；按 hold 处理")
        plan["rationale"].append("显存预算未知，停在参数分析交接，不要直接开 dynamic")
        plan["mode"] = "hold"
        return plan

    if affordable is not None and redundant > affordable:
        capped = (int(affordable) // e) * e
        warnings.append("按 analysis 可负担上限把冗余专家从 %d 收敛到 %d" % (redundant, capped))
        redundant = capped

    res = _analyse(args, n, e, int(redundant))
    if res.get("error"):
        die(res["error"])
    redundant = res["redundant_experts"]
    warnings.extend(res.get("warnings") or [])

    if redundant == 0:
        plan["rationale"].append("冗余专家为 0，不构成削峰能力；按 off 处理")
        plan["mode"] = "off"
        return plan

    memory = res.get("memory")
    if memory and memory.get("fits") is False:
        cap = memory.get("max_affordable_redundant_experts")
        if cap is not None:
            warnings.append("显存不足：冗余专家从 %d 收敛到可负担上限 %d" % (redundant, cap))
            redundant = cap
            res = _analyse(args, n, e, redundant)
            if res.get("error") or redundant == 0:
                plan["mode"] = "off"
                plan["rationale"].append("显存无法容纳任何冗余副本，按 off 处理")
                return plan
            memory = res.get("memory")

    window = handoff.get("window_size") or 1000
    interval = handoff.get("step_interval") or window
    window, interval = _cap_for_benchmark(window, interval, args.benchmark_steps, warnings)

    plan.update({
        "redundant_experts": redundant,
        "ratio_pct": res.get("redundant_ratio_pct"),
        "per_rank_extra": res.get("per_rank_extra_experts"),
        "memory": memory,
        "window_size": window,
        "step_interval": interval,
        "template_changes": template_changes_dynamic(redundant, window, interval),
    })
    plan["rationale"].append(
        "冗余专家 %d（%s%%），每 rank 多 %s 个；window/step=%d/%d"
        % (redundant, res.get("redundant_ratio_pct"), res.get("per_rank_extra_experts"),
           window, interval))
    if memory and memory.get("fits") is False:
        warnings.append("当前配置仍不适配显存，请先降 KV 压力")
    return plan

def main():
    ap = argparse.ArgumentParser(description="Turn an EPLB verdict into a parameter plan")
    ap.add_argument("--verdict", required=True, help="verdict.json from vllm-eplb-analysis")
    ap.add_argument("--ep-size", type=int, default=None)
    ap.add_argument("--logical-experts", type=int, default=None)
    ap.add_argument("--bytes-per-expert-gib", type=float, default=None,
                    help="per-slot cost on each EP rank (DeepSeekV3 ~2.4)")
    ap.add_argument("--bytes-per-expert-per-layer-gib", type=float, default=None)
    ap.add_argument("--num-moe-layers", type=int, default=None)
    ap.add_argument("--free-gib", type=float, default=None)
    ap.add_argument("--benchmark-steps", type=int, default=None,
                    help="planned benchmark length in steps; used to cap window/step")
    ap.add_argument("--branch", choices=["site-fork", "upstream", "unknown"], default="unknown",
                    help="target vLLM branch; dynamic/force require an explicit site-fork")
    ap.add_argument("--out", help="write eplb_parameter_plan.json here")
    args = ap.parse_args()

    if not os.path.isfile(args.verdict):
        die("verdict not found: %s" % args.verdict)
    try:
        verdict = json.load(open(args.verdict, encoding="utf-8"))
    except ValueError as exc:
        die("cannot parse verdict JSON: %s" % exc)

    plan = build_plan(verdict, args)
    text = json.dumps(plan, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
