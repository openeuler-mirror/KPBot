#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Behavioural regression checks for analyze_eplb_metrics.py.

Run: python3 evals/run_checks.py
Covers fixture verdicts, the multi-signal gate, threshold overrides, parser
inputs, CLI contracts and the utilisation confounder note.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "evals", "fixtures")
SCRIPT = os.path.join(ROOT, "scripts", "analyze_eplb_metrics.py")

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  | " + detail) if detail else ""))


def run(args):
    return subprocess.run([sys.executable, SCRIPT] + args, capture_output=True, text=True)


def analyse(args):
    r = run(args)
    try:
        return r, json.loads(r.stdout)
    except ValueError:
        return r, None


def main():
    tmp = tempfile.mkdtemp(prefix="eplb-analysis-checks-")
    try:
        # --- fixture verdicts ---
        for name, verdict, signals in (
            ("imbalanced", "imbalanced", 3),
            ("balanced", "balanced", 0),
            ("borderline", "borderline", 1),
            ("inconclusive", "inconclusive", 1),
        ):
            r, d = analyse(["--evidence", os.path.join(FIX, name + ".json")])
            ok = r.returncode == 0 and d and d["verdict"] == verdict and len(d["signals"]) == signals
            check("%s.json -> %s (%d signal)" % (name, verdict, signals), ok,
                  (d or {}).get("verdict", r.stderr.strip()))

        # --- multi-signal gate: one metric alone must not be enough ---
        r, d = analyse(["--eplb-log", os.path.join(FIX, "eplb_sample.log")])
        check("log only -> inconclusive（单指标不下结论）",
              d and d["verdict"] == "inconclusive" and d["signals"] == ["I1_balancedness_low"],
              json.dumps((d or {}).get("signals", [])))
        r, d = analyse(["--eplb-log", os.path.join(FIX, "eplb_sample.log"),
                        "--prometheus", os.path.join(FIX, "prometheus_sample.txt")])
        check("log + prometheus -> imbalanced",
              d and d["verdict"] == "imbalanced" and len(d["signals"]) >= 2,
              json.dumps((d or {}).get("signals", [])))

        # --- parsers ---
        if d:
            ind = d["indicators"]
            check("prometheus 直方图还原每个 engine 的 TTFT",
                  ind.get("ttft_skew") and abs(ind["ttft_skew"]["max_ms"] - 2380.0) < 5,
                  json.dumps(ind.get("ttft_skew")))
            check("prometheus gauge 还原在途请求倾斜",
                  ind.get("queue_skew") and ind["queue_skew"]["ratio"] > 2.0,
                  json.dumps(ind.get("queue_skew")))

        # --- threshold override flips the verdict ---
        r, d = analyse(["--evidence", os.path.join(FIX, "imbalanced.json")])
        check("默认阈值下 imbalanced", d and d["verdict"] == "imbalanced")
        ev = json.load(open(os.path.join(FIX, "imbalanced.json"), encoding="utf-8"))
        ev["thresholds"] = {"queue_ratio": 3.0, "ttft_ratio": 3.0}
        p = os.path.join(tmp, "relaxed.json")
        json.dump(ev, open(p, "w"))
        r, d = analyse(["--evidence", p])
        check("放宽阈值后降级为 borderline", d and d["verdict"] == "borderline",
              (d or {}).get("verdict", ""))

        # --- I4 prompt skew is an independent signal ---
        ev = {
            "balancedness": [0.97, 0.98, 0.97],
            "rank_waiting": [4, 4, 4, 4], "rank_running": [8, 8, 8, 8],
            "rank_ttft_ms": [905, 910, 900, 908],
            "rank_prompt_tokens": [30000, 12000, 12500, 11800],
        }
        p = os.path.join(tmp, "prompt.json")
        json.dump(ev, open(p, "w"))
        r, d = analyse(["--evidence", p])
        check("prompt token 倾斜单独也能成为信号",
              d and d["signals"] == ["I4_prompt_skew"] and d["verdict"] == "borderline",
              json.dumps((d or {}).get("signals", [])))

        # --- utilisation confounder ---
        ev = {"balancedness": [0.97, 0.98, 0.97], "rank_waiting": [4, 4, 4, 4],
              "rank_ttft_ms": [905, 910, 900, 908], "rank_util_pct": [97, 96, 98, 97]}
        p = os.path.join(tmp, "util.json")
        json.dump(ev, open(p, "w"))
        r, d = analyse(["--evidence", p])
        check("各 rank 打满且差异小 -> 提示非专家瓶颈",
              d and any("瓶颈" in n for n in d["notes"]),
              json.dumps((d or {}).get("notes", [])))

        # --- memory gate (M0) ---
        r, d = analyse(["--evidence", os.path.join(FIX, "imbalanced_low_memory.json")])
        check("显存无余量 -> free_memory_first",
              d and d["verdict"] == "imbalanced"
              and d["recommendation"]["action"] == "free_memory_first"
              and d["indicators"]["memory"]["verdict"] == "no_headroom",
              json.dumps((d or {}).get("recommendation", {}).get("action")))
        r, d = analyse(["--evidence", os.path.join(FIX, "imbalanced_tight_memory.json")])
        check("显存偏紧 -> optimize_with_cap 且副本按余量收敛",
              d and d["recommendation"]["action"] == "optimize_with_cap"
              and d["recommendation"]["handoff"]["redundant_experts"] == 16,
              json.dumps((d or {}).get("recommendation", {}).get("handoff")))
        r, d = analyse(["--eplb-log", os.path.join(FIX, "eplb_sample.log"),
                        "--prometheus", os.path.join(FIX, "prometheus_sample.txt")])
        check("从 /metrics 解析 KV 占用与换出",
              d and d["indicators"]["memory"]["verdict"] == "ample"
              and d["indicators"]["memory"]["kv_cache_usage_pct"] == 43.0,
              json.dumps((d or {}).get("indicators", {}).get("memory")))
        r, d = analyse(["--evidence", os.path.join(FIX, "imbalanced.json")])
        check("无显存证据时 memory=unknown 且不阻塞结论",
              d and d["indicators"]["memory"]["verdict"] == "unknown"
              and d["recommendation"]["action"] == "optimize_dynamic",
              (d or {}).get("indicators", {}).get("memory", {}).get("verdict", ""))

        # --- P2-1: cumulative preemption counter must not gate ---
        r, d = analyse(["--evidence", os.path.join(FIX, "imbalanced_cumulative_preemptions.json")])
        check("只有累计换出次数时不判 no_headroom",
              d and d["indicators"]["memory"]["verdict"] == "ample"
              and any("累计换出" in n for n in d["notes"]),
              json.dumps((d or {}).get("indicators", {}).get("memory", {})))
        import shutil as _sh
        base_text = open(os.path.join(FIX, "prometheus_sample.txt"), encoding="utf-8").read()
        prev = os.path.join(tmp, "prev.txt")
        open(prev, "w", encoding="utf-8").write(base_text)
        same = os.path.join(tmp, "cur_same.txt")
        open(same, "w", encoding="utf-8").write(base_text)
        r, d = analyse(["--prometheus", same, "--prometheus-prev", prev])
        check("两份快照差分为 0 时不判 no_headroom（累计值 >0 也不阻塞）",
              d and d["indicators"]["memory"]["preemptions_delta"] == 0
              and d["indicators"]["memory"]["verdict"] == "ample",
              json.dumps((d or {}).get("indicators", {}).get("memory", {})))
        cur = os.path.join(tmp, "cur_diff.txt")
        open(cur, "w", encoding="utf-8").write(
            base_text.replace('vllm:num_preemptions{model_name="dsv41",engine="2"} 0.0',
                              'vllm:num_preemptions{model_name="dsv41",engine="2"} 4.0'))
        r, d = analyse(["--prometheus", cur, "--prometheus-prev", prev])
        check("两份快照差分 >0 时判 no_headroom",
              d and d["indicators"]["memory"]["preemptions_delta"] == 4.0
              and d["indicators"]["memory"]["verdict"] == "no_headroom",
              json.dumps((d or {}).get("indicators", {}).get("memory", {})))

        # --- P2-2: tight without affordable must not hand out an uncapped number ---
        ev = {"balancedness": [0.78, 0.80, 0.79, 0.77], "rank_waiting": [20, 3, 4, 3, 3, 4, 3, 3],
              "rank_running": [8] * 8, "rank_ttft_ms": [2200, 900, 920, 890, 910, 900, 905, 915],
              "kv_cache_usage_pct": 88.0}
        p = os.path.join(tmp, "tight_noad.json")
        json.dump(ev, open(p, "w"))
        r, d = analyse(["--evidence", p])
        check("tight 但算不出 affordable -> 副本数待定",
              d and d["recommendation"]["action"] == "optimize_with_cap"
              and d["recommendation"]["handoff"]["redundant_experts"] is None
              and "先测" in d["recommendation"]["next"],
              json.dumps((d or {}).get("recommendation", {}).get("handoff")))
        r, d = analyse(["--evidence", os.path.join(FIX, "imbalanced.json")])
        check("memory=unknown 时交接明确要求先确认显存",
              "显存" in d["recommendation"]["next"],
              d["recommendation"]["next"][-60:])

        # --- P2: per-slot memory field name / suspicious magnitude ---
        ev = {"balancedness": [0.78, 0.80, 0.79, 0.77], "rank_waiting": [20, 3, 4, 3, 3, 4, 3, 3],
              "rank_running": [8] * 8, "rank_ttft_ms": [2200, 900, 920, 890, 910, 900, 905, 915],
              "kv_cache_usage_pct": 43.0, "free_mem_gib": 6, "ep_size": 16,
              "bytes_per_extra_expert_per_rank_gib": 0.043}
        p = os.path.join(tmp, "tiny_perslot.json")
        json.dump(ev, open(p, "w"))
        r, d = analyse(["--evidence", p])
        mem = d["indicators"]["memory"]
        check("per-slot 成本量级可疑 -> unknown 且忽略（防 OOM 误判）",
              mem["verdict"] == "unknown" and mem["affordable_redundant"] is None
              and "槽位" in (mem.get("note") or ""),
              json.dumps(mem, ensure_ascii=False)[:160])

        # --- P1: handoff is self-contained (no external skill/script) ---
        r, d = analyse(["--evidence", os.path.join(FIX, "imbalanced.json")])
        ho = d["recommendation"]["handoff"]
        check("handoff 携带模板改动清单且不引用外部技能",
              "template_changes" in ho and "skill" not in ho
              and any("--enable-eplb" in f for f in ho["template_changes"]["add_flags"]),
              json.dumps(ho["template_changes"], ensure_ascii=False)[:120])
        r, d = analyse(["--evidence", os.path.join(FIX, "borderline.json")])
        probe = d["recommendation"]["template_changes"]
        check("force 探针同样给出模板改动清单",
              probe["env"].get("VLLM_USE_V2_MODEL_RUNNER") == "0"
              and probe["unset_env"] == ["DYNAMIC_EPLB"]
              and probe["additional_config"].get("enable_force_eplb") is True,
              json.dumps(probe, ensure_ascii=False)[:120])

        # --- CLI contracts ---
        p = os.path.join(tmp, "out.json")
        r, d = analyse(["--evidence", os.path.join(FIX, "balanced.json"), "--out", p])
        check("--out 写出 verdict.json", r.returncode == 0 and os.path.isfile(p)
              and json.load(open(p))["verdict"] == "balanced")
        r = run(["--evidence", os.path.join(FIX, "balanced.json"), "--expect", "balanced"])
        check("--expect 匹配时 rc=0", r.returncode == 0)
        r = run(["--evidence", os.path.join(FIX, "balanced.json"), "--expect", "imbalanced"])
        check("--expect 不匹配时 rc=2", r.returncode == 2)
        r = run([])
        check("无输入时 rc=1 并报错", r.returncode == 1 and "ERROR" in r.stderr)
        bad = os.path.join(tmp, "bad.json")
        open(bad, "w").write("{not json")
        r = run(["--evidence", bad])
        check("损坏的证据 JSON -> rc=1", r.returncode == 1 and "cannot read" in r.stderr)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print("\n%d/%d checks passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
