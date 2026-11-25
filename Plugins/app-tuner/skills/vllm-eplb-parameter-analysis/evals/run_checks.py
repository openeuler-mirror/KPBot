#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression checks for vllm-eplb-parameter-analysis.

Run: python3 evals/run_checks.py
Covers recommend_redundancy.py (ratio/memory/divisibility/strict) and
plan_eplb_params.py (verdict -> plan mapping, rounding, memory cap, window cap,
upstream branch gate).
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "evals", "fixtures")
PLAN = os.path.join(ROOT, "scripts", "plan_eplb_params.py")
RECOMMEND = os.path.join(ROOT, "scripts", "recommend_redundancy.py")

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  | " + detail) if detail else ""))


def run(args):
    return subprocess.run([sys.executable] + args, capture_output=True, text=True)


def plan(args):
    r = run([PLAN] + args)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        d = {}
    return r, d


def verdict(name):
    return os.path.join(FIXTURES, name)


def main():
    tmp = tempfile.mkdtemp(prefix="eplb-param-")
    try:
        # --- recommend_redundancy.py ---
        r = run([RECOMMEND, "--logical-experts", "384", "--ep-size", "16",
                 "--redundant-experts", "16", "--bytes-per-expert-gib", "2.4", "--free-gib", "6"])
        d = json.loads(r.stdout) if r.returncode == 0 else {}
        check("recommend: 16/384=4.17%、每 rank 1 个、+2.4GiB",
              d.get("redundant_ratio_pct") == 4.17 and d.get("per_rank_extra_experts") == 1
              and d.get("memory", {}).get("extra_per_rank_gib") == 2.4, json.dumps(d.get("memory")))

        r = run([RECOMMEND, "--logical-experts", "384", "--ep-size", "16", "--ratio", "0.05"])
        d = json.loads(r.stdout) if r.returncode == 0 else {}
        check("recommend: 非整倍向上取整",
              d.get("redundant_experts") == 32 and any("整数倍" in w for w in d.get("warnings", [])),
              str(d.get("warnings")))

        r = run([RECOMMEND, "--logical-experts", "384", "--ep-size", "16",
                 "--redundant-experts", "64", "--bytes-per-expert-gib", "2.4", "--free-gib", "1"])
        d = json.loads(r.stdout) if r.returncode == 0 else {}
        check("recommend: 显存不足给出可负担上限",
              d.get("memory", {}).get("fits") is False
              and d.get("memory", {}).get("max_affordable_redundant_experts") == 0,
              json.dumps(d.get("memory")))

        r = run([RECOMMEND, "--logical-experts", "100", "--ep-size", "8", "--redundant-experts", "8"])
        check("recommend: 逻辑专家不可被 EP 整除报错",
              r.returncode == 1 and "不能被 ep_size" in json.loads(r.stdout).get("error", ""))

        r = run([RECOMMEND, "--logical-experts", "100", "--ep-size", "1",
                 "--redundant-experts", "14", "--strict"])
        check("recommend: --strict 对 high 档返回 2", r.returncode == 2)

        # --- plan_eplb_params.py ---
        r, d = plan(["--verdict", verdict("verdict_optimize_dynamic.json"),
                     "--ep-size", "16", "--logical-experts", "384", "--branch", "site-fork",
                     "--bytes-per-expert-gib", "2.4", "--free-gib", "6"])
        check("plan: imbalanced -> dynamic 且参数来自 handoff",
              r.returncode == 0 and d.get("mode") == "dynamic"
              and d.get("redundant_experts") == 16 and d.get("window_size") == 1000
              and d.get("step_interval") == 1000)
        tc = d.get("template_changes") or {}
        check("plan: dynamic template_changes 含 env/flags",
              tc.get("env", {}).get("DYNAMIC_EPLB") == "true"
              and "--enable-eplb" in tc.get("add_flags", []))

        r, d = plan(["--verdict", verdict("verdict_optimize_dynamic.json"),
                     "--logical-experts", "384", "--branch", "site-fork"])
        check("plan: 缺 ep-size 时 dynamic 报错 (rc=2)", r.returncode == 2)

        # candidate rounding: handoff 7 on ep 16 -> 16
        vround = os.path.join(tmp, "verdict_round.json")
        json.dump({"verdict": "imbalanced",
                   "recommendation": {"action": "optimize_dynamic",
                                      "handoff": {"redundant_experts": 7,
                                                  "window_size": 1000, "step_interval": 1000}}},
                  open(vround, "w"))
        r, d = plan(["--verdict", vround, "--ep-size", "16", "--logical-experts", "384",
                     "--branch", "site-fork", "--bytes-per-expert-gib", "2.4", "--free-gib", "6"])
        check("plan: 非整倍冗余数向上取整到 16", d.get("redundant_experts") == 16)

        # memory cap: 32 -> 16 when free=3
        vcap = os.path.join(tmp, "verdict_cap.json")
        json.dump({"verdict": "imbalanced",
                   "recommendation": {"action": "optimize_with_cap",
                                      "memory": {"verdict": "tight", "affordable_redundant": 16},
                                      "handoff": {"redundant_experts": 32,
                                                  "window_size": 1000, "step_interval": 1000}}},
                  open(vcap, "w"))
        r, d = plan(["--verdict", vcap, "--ep-size", "16", "--logical-experts", "384",
                     "--branch", "site-fork", "--bytes-per-expert-gib", "2.4", "--free-gib", "3"])
        check("plan: 显存不足时按上限收敛到 16", d.get("redundant_experts") == 16)

        # benchmark step cap
        r, d = plan(["--verdict", verdict("verdict_optimize_dynamic.json"),
                     "--ep-size", "16", "--logical-experts", "384", "--branch", "site-fork",
                     "--bytes-per-expert-gib", "2.4", "--free-gib", "6",
                     "--benchmark-steps", "1000"])
        check("plan: window/step 超过压测步数 1/4 时被收敛",
              d.get("step_interval") == 250 and d.get("window_size") == 250,
              "w=%s s=%s" % (d.get("window_size"), d.get("step_interval")))

        # verdict mapping
        r, d = plan(["--verdict", verdict("verdict_balanced.json")])
        check("plan: balanced -> off 且无 template_changes",
              d.get("mode") == "off" and not (d.get("template_changes") or {}).get("add_flags"))

        r, d = plan(["--verdict", verdict("verdict_borderline.json"), "--branch", "site-fork"])
        check("plan: borderline -> force 合并 enable_force_eplb",
              d.get("mode") == "force"
              and (d.get("template_changes") or {}).get("additional_config", {}).get("enable_force_eplb") is True)

        r, d = plan(["--verdict", verdict("verdict_free_memory.json")])
        check("plan: free_memory_first -> off", d.get("mode") == "off")

        # upstream branch gate
        r, d = plan(["--verdict", verdict("verdict_optimize_dynamic.json"),
                     "--ep-size", "16", "--logical-experts", "384", "--branch", "upstream"])
        check("plan: upstream 分支 dynamic 被判 off 并告警",
              d.get("mode") == "off" and any("上游" in w for w in d.get("warnings", [])))

        # branch must be explicit
        r, d = plan(["--verdict", verdict("verdict_optimize_dynamic.json"),
                     "--ep-size", "16", "--logical-experts", "384",
                     "--bytes-per-expert-gib", "2.4", "--free-gib", "6"])
        check("plan: 分支未确认时 dynamic 转 hold",
              d.get("mode") == "hold" and any("分支" in w for w in d.get("warnings", [])))

        # verdict/action conflict
        vconf = os.path.join(tmp, "verdict_conflict.json")
        json.dump({"verdict": "balanced",
                   "recommendation": {"action": "optimize_dynamic",
                                      "handoff": {"redundant_experts": 16}}}, open(vconf, "w"))
        r, d = plan(["--verdict", vconf, "--ep-size", "16", "--logical-experts", "384",
                     "--branch", "site-fork", "--bytes-per-expert-gib", "2.4", "--free-gib", "6"])
        check("plan: verdict/action 冲突时 hold",
              d.get("mode") == "hold" and any("冲突" in w for w in d.get("warnings", [])))

        # dynamic without memory evidence must hold
        r, d = plan(["--verdict", verdict("verdict_optimize_dynamic.json"),
                     "--ep-size", "16", "--logical-experts", "384", "--branch", "site-fork"])
        check("plan: 缺显存证据时 dynamic 转 hold",
              d.get("mode") == "hold" and any("显存" in w for w in d.get("warnings", [])))

        # upstream memory warning / next must be preserved
        vmem = os.path.join(tmp, "verdict_mem_unknown.json")
        json.dump({"verdict": "imbalanced",
                   "recommendation": {"action": "optimize_dynamic",
                                      "memory": {"verdict": "unknown"},
                                      "next": "上线前先确认 KV 占用与剩余显存",
                                      "handoff": {"redundant_experts": 16,
                                                  "window_size": 1000, "step_interval": 1000}}},
                  open(vmem, "w"))
        r, d = plan(["--verdict", vmem, "--ep-size", "16", "--logical-experts", "384",
                     "--branch", "site-fork", "--bytes-per-expert-gib", "2.4", "--free-gib", "6"])
        check("plan: 保留上游显存 unknown 告警与 next",
              d.get("mode") == "dynamic"
              and any("unknown" in w for w in d.get("warnings", []))
              and (d.get("source") or {}).get("analysis_next"))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [x for x in results if not x[1]]
    print("\n%d/%d checks passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
