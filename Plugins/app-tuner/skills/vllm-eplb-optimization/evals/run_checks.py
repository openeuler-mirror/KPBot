#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end regression checks for the eplb-optimization scripts.

Run: python3 evals/run_checks.py
Covers dry-run, mode switching, idempotency, off semantics, P/D and EP guards,
parameter validation, same-line refusal, symlink handling, EP-last-arg command
structure, ratio/memory analysis and dataset generation.
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


def write(p, text):
    open(p, "w", encoding="utf-8").write(text)


KV = '''    --kv-transfer-config '{
        "kv_connector":"MooncakeConnector",
        "kv_role":"kv_producer"
    }' \\\n'''


def base_template(body):
    return "#!/bin/bash\nexport DIST_TIMEOUT=1200\n" + body


def main():
    tmp = tempfile.mkdtemp(prefix="eplb-checks-")
    try:
        clean_src = read(os.path.join(FIXTURES, "run_dp_template_p_clean.sh"))

        # --- 1. dry-run: no write, no backup ---
        for mode in ("force", "dynamic"):
            p = os.path.join(tmp, "dry_%s.sh" % mode)
            shutil.copy2(os.path.join(FIXTURES, "run_dp_template_p_clean.sh"), p)
            before = read(p)
            r = run([APPLY, "--template", p, "--mode", mode, "--ep-size", "16", "--dry-run"])
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
        r3 = run([APPLY, "--template", p, "--mode", "dynamic", "--ep-size", "16"])
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
        write(d, base_template("vllm serve /models/x \\\n"
                               "    --kv-transfer-config '{\"kv_role\":\"kv_consumer\"}' \\\n"
                               "    --port 8000\n"))
        rd = run([APPLY, "--template", d, "--mode", "dynamic"])
        check("guard: 拒绝 D 节点模板 (rc=2)", rd.returncode == 2, rd.stdout.strip().splitlines()[0] if rd.stdout else "")

        norole = os.path.join(tmp, "no_role.sh")
        write(norole, base_template("vllm serve /models/x \\\n    --port 8000\n"))
        rnr = run([APPLY, "--template", norole, "--mode", "dynamic"])
        check("guard: 无 kv_role 的模板被拒绝", rnr.returncode == 2 and "kv-transfer-config" in rnr.stdout,
              rnr.stdout.strip().splitlines()[0] if rnr.stdout else "")

        no_ep = os.path.join(tmp, "no_ep.sh")
        write(no_ep, base_template("vllm serve /models/x \\\n" + KV + "    --port 8000\n"))
        rn = run([APPLY, "--template", no_ep, "--mode", "dynamic", "--ep-size", "16"])
        check("guard: 无 --enable-expert-parallel 时拒绝 dynamic",
              rn.returncode == 1 and "expert-parallel" in rn.stdout)

        no_ac = os.path.join(tmp, "no_ac.sh")
        write(no_ac, base_template("vllm serve /models/x \\\n" + KV +
                                   "    --enable-expert-parallel \\\n    --port 8000\n"))
        ra = run([APPLY, "--template", no_ac, "--mode", "force"])
        check("guard: 缺 additional-config 时拒绝 force",
              ra.returncode == 1 and "additional-config" in ra.stdout)
        # dynamic/off must work without --additional-config
        shutil.copy2(no_ac, os.path.join(tmp, "no_ac_dyn.sh"))
        rdn = run([APPLY, "--template", os.path.join(tmp, "no_ac_dyn.sh"), "--mode", "dynamic", "--ep-size", "16"])
        check("no additional-config: dynamic 成功",
              rdn.returncode == 0 and "--enable-eplb" in read(os.path.join(tmp, "no_ac_dyn.sh")))
        shutil.copy2(no_ac, os.path.join(tmp, "no_ac_off.sh"))
        roff = run([APPLY, "--template", os.path.join(tmp, "no_ac_off.sh"), "--mode", "off"])
        check("no additional-config: off 成功且幂等", roff.returncode == 0)

        # --- 5a2. stale-index regression: export lines must stay outside serve ---
        stale = os.path.join(tmp, "stale.sh")
        write(stale, base_template('export DYNAMIC_EPLB="true"\n'
                                   "vllm serve /models/x \\\n" + KV +
                                   "    --enable-expert-parallel \\\n    --port 8000\n"))
        rst = run([APPLY, "--template", stale, "--mode", "dynamic", "--ep-size", "16"])
        slines = read(stale).splitlines()
        serve_line = next(i for i, l in enumerate(slines) if l.strip().startswith("vllm serve"))
        export_lines = [i for i, l in enumerate(slines) if l.strip().startswith("export VLLM_USE_V2_MODEL_RUNNER")
                        or l.strip().startswith("export DYNAMIC_EPLB")]
        check("stale-index: export 都是独立行且在 serve 之前",
              rst.returncode == 0 and export_lines and all(i < serve_line for i in export_lines),
              str(export_lines) + " serve=" + str(serve_line))

        # --- 5b. same-line EPLB args are refused, file untouched ---
        same = os.path.join(tmp, "same_line.sh")
        write(same, base_template("vllm serve /models/x \\\n" + KV +
                                  "    --enable-expert-parallel \\\n    --enable-eplb --port 8100\n"))
        before = read(same)
        rs = run([APPLY, "--template", same, "--mode", "off"])
        check("same-line EPLB 参数被拒绝且不改文件",
              rs.returncode != 0 and read(same) == before and "独占一行" in rs.stdout)

        # --- 5c. EP is the last serve argument ---
        ep_last = os.path.join(tmp, "ep_last.sh")
        write(ep_last, base_template("vllm serve /models/x \\\n" + KV +
                                     "    --enable-expert-parallel\nbash /opt/start_proxy.sh\n"))
        re_ = run([APPLY, "--template", ep_last, "--mode", "dynamic", "--ep-size", "16"])
        lines = read(ep_last).splitlines()
        idx = next(i for i, l in enumerate(lines) if "--eplb-config.step_interval" in l)
        check("EP 为末参数: 末条 flag 不带悬挂续行符",
              re_.returncode == 0 and not lines[idx].rstrip().endswith("\\"), lines[idx])
        check("EP 为末参数: 后续脚本行未被拼进 serve",
              lines[idx + 1] == "bash /opt/start_proxy.sh", lines[idx + 1] if idx + 1 < len(lines) else "EOF")
        check("EP 为末参数: bash -n 通过", subprocess.run(["bash", "-n", ep_last]).returncode == 0)

        # --- 5d. symlink: edit the real file, keep the link ---
        real = os.path.join(tmp, "real.sh")
        link = os.path.join(tmp, "link.sh")
        shutil.copy2(os.path.join(FIXTURES, "run_dp_template_p_clean.sh"), real)
        os.symlink(real, link)
        rl = run([APPLY, "--template", link, "--mode", "dynamic", "--ep-size", "16"])
        check("symlink: 链接仍是链接", os.path.islink(link))
        check("symlink: 真实文件被改写", rl.returncode == 0 and 'export DYNAMIC_EPLB="true"' in read(real))
        check("symlink: 备份落在真实文件旁",
              any(f.startswith("real.sh.bak-eplb-dynamic") for f in os.listdir(tmp)))

        # --- 5e. off must not inject/flip V2 on templates without EPLB ---
        v1 = os.path.join(tmp, "v1_base.sh")
        write(v1, "#!/bin/bash\nexport VLLM_USE_V2_MODEL_RUNNER=0\nvllm serve /models/x \\\n" + KV + "    --port 8000\n")
        before = read(v1)
        rv = run([APPLY, "--template", v1, "--mode", "off"])
        check("off: V1 基线不被翻转", rv.returncode == 0 and read(v1) == before)

        nov2 = os.path.join(tmp, "no_v2.sh")
        write(nov2, "#!/bin/bash\nvllm serve /models/x \\\n" + KV + "    --port 8000\n")
        before = read(nov2)
        rn2 = run([APPLY, "--template", nov2, "--mode", "off"])
        check("off: 缺 V2 行时不注入", rn2.returncode == 0 and read(nov2) == before)

        # --- 5f. parameter validation ---
        val = os.path.join(tmp, "val.sh")
        shutil.copy2(os.path.join(FIXTURES, "run_dp_template_p_clean.sh"), val)
        before = read(val)
        rw = run([APPLY, "--template", val, "--mode", "dynamic", "--window-size", "0"])
        check("param: window-size=0 拒绝 (rc=2)", rw.returncode == 2 and read(val) == before)
        ri = run([APPLY, "--template", val, "--mode", "dynamic", "--step-interval", "-5"])
        check("param: step-interval<0 拒绝 (rc=2)", ri.returncode == 2 and read(val) == before)
        rz = run([APPLY, "--template", val, "--mode", "dynamic", "--redundant-experts", "0"])
        check("param: redundant=0 拒绝 (rc=2)", rz.returncode == 2 and read(val) == before)
        rr = run([APPLY, "--template", val, "--mode", "dynamic", "--ep-size", "16",
                  "--redundant-experts", "7"])
        check("param: 非整除被取整并提示",
              rr.returncode == 0 and "--eplb-config.num_redundant_experts 16" in read(val)
              and "取整" in rr.stdout)

        # dynamic without --ep-size must refuse (no unverified redundant value on disk)
        val2 = os.path.join(tmp, "val_ep_required.sh")
        shutil.copy2(os.path.join(FIXTURES, "run_dp_template_p_clean.sh"), val2)
        before = read(val2)
        rne = run([APPLY, "--template", val2, "--mode", "dynamic", "--redundant-experts", "7"])
        check("param: dynamic 缺 --ep-size 拒绝 (rc=2) 且不落盘",
              rne.returncode == 2 and read(val2) == before and "ep-size" in rne.stdout)

        # --- 5f2. boolean switch sharing a line with another flag is refused ---
        bsame = os.path.join(tmp, "bool_same_line.sh")
        write(bsame, base_template("vllm serve /models/x \\\n" + KV +
                                   "    --enable-expert-parallel \\\n"
                                   "    --eplb-config.log_balancedness --trust-remote-code \\\n"
                                   "    --port 8000\n"))
        before = read(bsame)
        rbs = run([APPLY, "--template", bsame, "--mode", "off"])
        check("same-line 布尔开关+其它 flag 被拒绝且不改文件",
              rbs.returncode != 0 and read(bsame) == before and "独占一行" in rbs.stdout)

        # --- 5f3. off ignores EPLB markers that only appear in comments ---
        v1c = os.path.join(tmp, "v1_comment.sh")
        write(v1c, '#!/bin/bash\n# legacy: export DYNAMIC_EPLB="true" used earlier\n'
                   "export VLLM_USE_V2_MODEL_RUNNER=0\nvllm serve /models/x \\\n" +
                   KV + "    --port 8000\n")
        before = read(v1c)
        rv1c = run([APPLY, "--template", v1c, "--mode", "off"])
        check("off: 仅注释提及 DYNAMIC_EPLB 的 V1 模板不被翻转",
              rv1c.returncode == 0 and read(v1c) == before)

        # --- 5g. check_state: loose markers must not be reported as dynamic ---
        bad = os.path.join(tmp, "bad_dyn.sh")
        write(bad, base_template('export VLLM_USE_V2_MODEL_RUNNER=1\nexport DYNAMIC_EPLB="false"\n'
                                 "vllm serve /models/x \\\n" + KV +
                                 "    --enable-expert-parallel \\\n    --enable-eplb\n"))
        cb = run([APPLY, "--template", bad, "--mode", "check"])
        check("check: DYNAMIC_EPLB=false 不算 dynamic", cb.returncode == 1 and "不明确" in cb.stdout)

        # --- 5h. safe write: path-safe backup tag, unique temp ---
        p2 = os.path.join(tmp, "safe.sh")
        shutil.copy2(os.path.join(FIXTURES, "run_dp_template_p_clean.sh"), p2)
        rs = run([APPLY, "--template", p2, "--mode", "dynamic", "--ep-size", "16", "--backup-tag", "../evil"])
        baks = [f for f in os.listdir(tmp) if f.startswith("safe.sh.bak-")]
        check("backup tag 被清洗，不能逃逸目录",
              rs.returncode == 0 and len(baks) == 1 and "/" not in baks[0], str(baks))
        check("写入后没有固定名 .tmp-write 残留",
              not [f for f in os.listdir(tmp) if ".tmp-write" in f])

        # --- 5i. redundant-expert ratio analysis ---
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
        r = run([REDUNDANCY, "--logical-experts", "100", "--ep-size", "8", "--redundant-experts", "8"])
        check("逻辑专家不能被 EP 整除时返回错误",
              r.returncode == 1 and "不能被 ep_size" in json.loads(r.stdout).get("error", ""),
              r.stdout.strip()[:80])
        r = run([REDUNDANCY, "--logical-experts", "100", "--ep-size", "1", "--redundant-experts", "14", "--strict"])
        check("--strict 对 high 档也返回 2", r.returncode == 2)
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

        out2 = os.path.join(tmp, "should_not_exist.jsonl")
        r0 = run([GEN, "--out", out2, "--num-requests", "0", "--target-tokens", "2000"])
        check("dataset: num-requests=0 报错且不创建文件",
              r0.returncode != 0 and not os.path.exists(out2))
        out3 = os.path.join(tmp, "should_not_exist2.jsonl")
        r3 = run([GEN, "--out", out3, "--num-requests", "5", "--target-tokens", "0"])
        check("dataset: target-tokens=0 报错且不创建文件",
              r3.returncode != 0 and not os.path.exists(out3))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print("\n%d/%d checks passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
