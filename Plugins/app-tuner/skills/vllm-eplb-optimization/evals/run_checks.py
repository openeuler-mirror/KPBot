#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end regression checks for the eplb-optimization scripts.

Run: python3 evals/run_checks.py
Covers dry-run (no write, no backup), mode switching, idempotency, off-restore,
P/D node guards, EP prerequisite guard and dataset generation.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "evals", "fixtures")
APPLY = os.path.join(ROOT, "scripts", "apply_eplb.py")
GEN = os.path.join(ROOT, "scripts", "gen_imbalanced_dataset.py")
REDUNDANCY = os.path.join(ROOT, "scripts", "recommend_redundancy.py")

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  | " + detail) if detail else ""))


def run(args):
    return subprocess.run([sys.executable] + args, capture_output=True, text=True)


def read(p):
    return open(p, encoding="utf-8").read()


def main():
    tmp = tempfile.mkdtemp(prefix="eplb-checks-")
    try:
        clean_src = read(os.path.join(FIXTURES, "run_dp_template_p_clean.sh"))

        # --- 1. dry-run: no write, no backup ---
        for mode in ("force", "dynamic"):
            p = os.path.join(tmp, "dry_%s.sh" % mode)
            shutil.copy2(os.path.join(FIXTURES, "run_dp_template_p_clean.sh"), p)
            before = read(p)
            r = run([APPLY, "--template", p, "--mode", mode, "--dry-run"])
            backups = [f for f in os.listdir(tmp) if ".bak-" in f and f.startswith("dry_%s.sh" % mode)]
            check("dry-run %s: rc=0" % mode, r.returncode == 0, r.stdout.strip().splitlines()[-1] if r.stdout else "")
            check("dry-run %s: 文件未被写入" % mode, read(p) == before)
            check("dry-run %s: 未创建备份" % mode, not backups, str(backups))
            check("dry-run %s: 输出含 unified diff" % mode,
                  ("@@" in r.stdout)
                  and any(ln.startswith("+") and not ln.startswith("+++") for ln in r.stdout.splitlines()))

        # --- 2. force: apply, check, idempotent ---
        p = os.path.join(tmp, "flow.sh")
        shutil.copy2(os.path.join(FIXTURES, "run_dp_template_p_clean.sh"), p)
        r1 = run([APPLY, "--template", p, "--mode", "force"])
        txt = read(p)
        check("force: apply rc=0", r1.returncode == 0)
        check("force: V2=0 且 unset DYNAMIC_EPLB", "VLLM_USE_V2_MODEL_RUNNER=0" in txt and "unset DYNAMIC_EPLB" in txt)
        check("force: enable_force_eplb 合并且只有 1 个 additional-config",
              '"enable_force_eplb":true' in txt and txt.count("--additional-config") == 1)
        check("force: 原有键保留",
              all(k in txt for k in ("enable_cpu_binding", "enable_fused_mc2",
                                     "enable_flashcomm1", "enable_shared_expert_dp")))
        r2 = run([APPLY, "--template", p, "--mode", "force"])
        check("force: 重复执行幂等", r2.returncode == 0 and "无需修改" in r2.stdout)
        c = run([APPLY, "--template", p, "--mode", "check"])
        check("check: 识别为 force", "状态: force EPLB" in c.stdout)
        check("force: bash -n 通过", subprocess.run(["bash", "-n", p]).returncode == 0)

        # --- 3. force -> dynamic ---
        r3 = run([APPLY, "--template", p, "--mode", "dynamic"])
        txt = read(p)
        check("force->dynamic: apply rc=0", r3.returncode == 0)
        check("force->dynamic: V2=1 且无 unset 残留",
              "VLLM_USE_V2_MODEL_RUNNER=1" in txt and "unset DYNAMIC_EPLB" not in txt)
        check("force->dynamic: 4 个 eplb 参数齐全",
              "--enable-eplb" in txt and
              all(("--eplb-config.%s %d" % (k, v)) in txt for k, v in
                  (("num_redundant_experts", 16), ("window_size", 1000), ("step_interval", 1000))))
        check("force->dynamic: enable_force_eplb 已移除", "enable_force_eplb" not in txt)
        check("force->dynamic: bash -n 通过", subprocess.run(["bash", "-n", p]).returncode == 0)
        c = run([APPLY, "--template", p, "--mode", "check"])
        check("check: 识别为 dynamic", "状态: dynamic EPLB" in c.stdout)

        # --- 4. dynamic -> off restores the original file ---
        r4 = run([APPLY, "--template", p, "--mode", "off"])
        check("dynamic->off: apply rc=0", r4.returncode == 0)
        check("dynamic->off: 与原始模板逐字节一致", read(p) == clean_src)
        c = run([APPLY, "--template", p, "--mode", "check"])
        check("check: 识别为 off", "状态: 关闭 EPLB" in c.stdout)

        # --- 5. guards ---
        d = os.path.join(tmp, "d_node.sh")
        open(d, "w").write("#!/bin/bash\nvllm serve /models/x \\\n"
                           "    --kv-transfer-config '{\"kv_role\":\"kv_consumer\"}' \\\n"
                           "    --port 8000\n")
        rd = run([APPLY, "--template", d, "--mode", "dynamic"])
        check("guard: 拒绝 D 节点模板 (rc=2)", rd.returncode == 2, rd.stdout.strip().splitlines()[0] if rd.stdout else "")

        no_ep = os.path.join(tmp, "no_ep.sh")
        open(no_ep, "w").write("#!/bin/bash\nexport VLLM_USE_V2_MODEL_RUNNER=1\n"
                               "vllm serve /models/x \\\n    --port 8000\n")
        rn = run([APPLY, "--template", no_ep, "--mode", "dynamic"])
        check("guard: 无 --enable-expert-parallel 时拒绝 dynamic",
              rn.returncode == 1 and "expert-parallel" in rn.stdout)

        no_ac = os.path.join(tmp, "no_ac.sh")
        open(no_ac, "w").write("#!/bin/bash\nexport VLLM_USE_V2_MODEL_RUNNER=1\n"
                               "vllm serve /models/x \\\n    --enable-expert-parallel \\\n    --port 8000\n")
        ra = run([APPLY, "--template", no_ac, "--mode", "force"])
        check("guard: 缺 additional-config 时拒绝 force",
              ra.returncode == 1 and "additional-config" in ra.stdout)

        # --- 5b. safe write: path-safe backup tag, no temp leftovers ---
        p2 = os.path.join(tmp, "safe.sh")
        shutil.copy2(os.path.join(FIXTURES, "run_dp_template_p_clean.sh"), p2)
        rs = run([APPLY, "--template", p2, "--mode", "dynamic", "--backup-tag", "../evil"])
        baks = [f for f in os.listdir(tmp) if f.startswith("safe.sh.bak-")]
        check("backup tag 被清洗，不能逃逸目录",
              rs.returncode == 0 and len(baks) == 1 and "/" not in baks[0],
              str(baks))
        check("写入后没有 .tmp-write 残留",
              not [f for f in os.listdir(tmp) if ".tmp-write" in f],
              str([f for f in os.listdir(tmp) if ".tmp-write" in f]))

        # --- 5c. redundant-expert ratio analysis ---
        r = run([REDUNDANCY, "--logical-experts", "384", "--ep-size", "16",
                 "--redundant-experts", "16", "--bytes-per-expert-gib", "2.4", "--free-gib", "6"])
        d = json.loads(r.stdout) if r.returncode == 0 else {}
        check("比例 16/384 = 4.17%，每 rank 1 个、+2.4 GiB",
              d.get("redundant_ratio_pct") == 4.17 and d.get("per_rank_extra_experts") == 1
              and d.get("memory", {}).get("extra_per_rank_gib") == 2.4
              and d.get("memory", {}).get("fits") is True,
              json.dumps(d.get("memory")))
        r = run([REDUNDANCY, "--logical-experts", "384", "--ep-size", "16", "--ratio", "0.05"])
        d = json.loads(r.stdout) if r.returncode == 0 else {}
        check("非整倍数向上取整到 ep_size",
              d.get("redundant_experts") == 32 and any("整数倍" in w for w in d.get("warnings", [])),
              str(d.get("warnings")))
        r = run([REDUNDANCY, "--logical-experts", "384", "--ep-size", "16",
                 "--redundant-experts", "32", "--bytes-per-expert-gib", "2.4", "--free-gib", "3"])
        d = json.loads(r.stdout) if r.returncode == 0 else {}
        check("显存不足时给出可负担上限 16",
              d.get("memory", {}).get("fits") is False
              and d.get("memory", {}).get("max_affordable_redundant_experts") == 16,
              json.dumps(d.get("memory")))
        r = run([REDUNDANCY, "--logical-experts", "384", "--ep-size", "16", "--redundant-experts", "16",
                 "--bytes-per-expert-per-layer-gib", "0.04", "--num-moe-layers", "60"])
        d = json.loads(r.stdout) if r.returncode == 0 else {}
        check("按层成本换算单副本 = 2.4 GiB",
              abs(d.get("memory", {}).get("bytes_per_extra_expert_per_rank_gib", 0) - 2.4) < 1e-6,
              str(d.get("memory")))
        r = run([REDUNDANCY, "--logical-experts", "384", "--ep-size", "16", "--redundant-experts", "64",
                 "--bytes-per-expert-gib", "2.4", "--free-gib", "1", "--strict"])
        check("--strict 超预算时 rc=2", r.returncode == 2)
        r = run([REDUNDANCY, "--logical-experts", "384", "--ep-size", "16", "--redundant-experts", "0"])
        d = json.loads(r.stdout) if r.returncode == 0 else {}
        check("0 副本 -> band=low 并提示只剩重排",
              d.get("band") == "low" and any("重排" in w for w in d.get("warnings", [])),
              str(d.get("warnings")))
        r = run([REDUNDANCY, "--logical-experts", "0", "--ep-size", "16"])
        check("非法输入 rc=1", r.returncode == 1 and "ERROR" in r.stderr)

        # --- 6. dataset generator ---
        out = os.path.join(tmp, "imb.jsonl")
        rg = run([GEN, "--out", out, "--num-requests", "5", "--target-tokens", "2000"])
        lines = [ln for ln in read(out).splitlines() if ln.strip()]
        ok_json = True
        for ln in lines:
            try:
                obj = json.loads(ln)
                assert obj["messages"][0]["content"]
            except Exception:
                ok_json = False
        check("dataset: 生成 5 行", rg.returncode == 0 and len(lines) == 5, "%d 行" % len(lines))
        check("dataset: 每行为合法 chat payload", ok_json)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print("\n%d/%d checks passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
