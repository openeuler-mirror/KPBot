#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end regression checks for vllm-eplb-deploy-validate.

Run: python3 evals/run_checks.py
Covers dry-run, mode switching, idempotency, off semantics, P/D and EP guards,
parameter validation, same-line refusal, symlink handling, EP-last-arg command
structure, dataset generation and run-manifest emission.
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
MANIFEST = os.path.join(ROOT, "scripts", "make_run_manifest.py")

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

        # --- 5c. EP is the last serve argument: dynamic/off/force must not swallow the next command ---
        ep_last = os.path.join(tmp, "ep_last.sh")
        write(ep_last, base_template("vllm serve /models/x \\\n" + KV +
                                     "    --enable-expert-parallel\nbash /opt/start_proxy.sh\n"))
        re_ = run([APPLY, "--template", ep_last, "--mode", "dynamic", "--ep-size", "16"])
        lines = read(ep_last).splitlines()
        body = "\n".join(lines)
        bash_i = lines.index("bash /opt/start_proxy.sh")
        check("EP 为末参数: dynamic 成功且 4 个 eplb 参数齐全",
              re_.returncode == 0 and "--enable-eplb" in body and
              all(("--eplb-config.%s %d" % (k, v)) in body for k, v in
                  (("num_redundant_experts", 16), ("window_size", 1000), ("step_interval", 1000))))
        check("EP 为末参数: serve 末行不悬挂续行符、后续脚本行未被拼进 serve",
              bash_i > 0 and not lines[bash_i - 1].rstrip().endswith("\\"),
              lines[bash_i - 1] if bash_i > 0 else "EOF")
        check("EP 为末参数: bash -n 通过", subprocess.run(["bash", "-n", ep_last]).returncode == 0)

        rd2 = run([APPLY, "--template", ep_last, "--mode", "dynamic", "--ep-size", "16"])
        lines2 = read(ep_last).splitlines()
        bash_i2 = lines2.index("bash /opt/start_proxy.sh")
        check("EP 为末参数: dynamic 再执行幂等且不吞下一行",
              rd2.returncode == 0 and "无需修改" in rd2.stdout
              and not lines2[bash_i2 - 1].rstrip().endswith("\\")
              and sum(1 for l in lines2 if "--enable-eplb" in l) == 1)

        roff_ep = run([APPLY, "--template", ep_last, "--mode", "off"])
        lines3 = read(ep_last).splitlines()
        bash_i3 = lines3.index("bash /opt/start_proxy.sh")
        check("EP 为末参数: dynamic->off 不悬挂续行符、不吞下一行",
              roff_ep.returncode == 0 and not lines3[bash_i3 - 1].rstrip().endswith("\\")
              and not any("--enable-eplb" in l or "--eplb-config." in l for l in lines3))

        # dynamic -> force on an EP-last fixture that carries --additional-config
        ep_last_ac = os.path.join(tmp, "ep_last_ac.sh")
        write(ep_last_ac, base_template("vllm serve /models/x \\\n" + KV +
                                        "    --additional-config '{\"a\":1}' \\\n"
                                        "    --enable-expert-parallel\nbash /opt/start_proxy.sh\n"))
        run([APPLY, "--template", ep_last_ac, "--mode", "dynamic", "--ep-size", "16"])
        rf = run([APPLY, "--template", ep_last_ac, "--mode", "force"])
        linesf = read(ep_last_ac).splitlines()
        bash_if = linesf.index("bash /opt/start_proxy.sh")
        check("EP 为末参数: dynamic->force 不悬挂续行符、不吞下一行",
              rf.returncode == 0 and not linesf[bash_if - 1].rstrip().endswith("\\")
              and '"enable_force_eplb":true' in "\n".join(linesf))

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

        # --- 5j. review regressions: JSON bool, multi-serve, export multi-var, dangling continuation ---
        fk = os.path.join(tmp, "force_space.sh")
        write(fk, base_template("vllm serve /models/x \\\n" + KV +
                                "    --additional-config '{\"enable_force_eplb\": true}' \\\n"
                                "    --port 8000\n"))
        fk0 = '#!/bin/bash\nexport VLLM_USE_V2_MODEL_RUNNER=0\nunset DYNAMIC_EPLB\n' + read(fk).split("\n", 1)[1]
        write(fk, fk0)
        cf = run([APPLY, "--template", fk, "--mode", "check"])
        check("check: 识别带空格的 enable_force_eplb: true",
              "状态: force EPLB" in cf.stdout, cf.stdout.strip().splitlines()[-1] if cf.stdout else "")

        multi = os.path.join(tmp, "multi_serve.sh")
        write(multi, "#!/bin/bash\nvllm serve /a \\\n" + KV +
                     "    --enable-expert-parallel\nvllm serve /b \\\n" +
                     KV + "    --port 8000\n")
        rm = run([APPLY, "--template", multi, "--mode", "dynamic", "--ep-size", "16"])
        check("guard: 多条 vllm serve 拒绝改写",
              rm.returncode != 0 and "vllm serve" in rm.stdout, rm.stdout.strip().splitlines()[0] if rm.stdout else "")

        mv = os.path.join(tmp, "export_multi.sh")
        write(mv, '#!/bin/bash\nexport DYNAMIC_EPLB="true" MODEL_PATH=/models/current\nexport VLLM_USE_V2_MODEL_RUNNER=1\n'
                  "vllm serve /models/x \\\n" + KV + "    --enable-expert-parallel\n")
        before = read(mv)
        rm = run([APPLY, "--template", mv, "--mode", "off"])
        check("guard: export 同行多赋值拒绝改写且不改文件",
              rm.returncode != 0 and read(mv) == before and "export" in rm.stdout)

        dang = os.path.join(tmp, "dangling.sh")
        write(dang, base_template("vllm serve /models/x \\\n" + KV +
                                  "    --enable-expert-parallel \\\n    --enable-eplb\nbash /opt/start_proxy.sh\n"))
        rd = run([APPLY, "--template", dang, "--mode", "off"])
        dl = read(dang).splitlines()
        bi = dl.index("bash /opt/start_proxy.sh") if "bash /opt/start_proxy.sh" in dl else -1
        check("off: 删除末尾参数时修正悬空续行符",
              rd.returncode == 0 and bi > 0 and not dl[bi - 1].rstrip().endswith("\\"),
              dl[bi - 1] if bi > 0 else "missing bash line")

        mtpl = os.path.join(tmp, "effective.sh")
        write(mtpl, base_template("vllm serve /models/x \\\n" + KV +
                                  "    --enable-expert-parallel \\\n"
                                  "    --eplb-config.num_redundant_experts 32 \\\n"
                                  "    --eplb-config.window_size 1000 \\\n"
                                  "    --eplb-config.step_interval 1000\n"))
        mf = os.path.join(tmp, "effective_manifest.json")
        run([MANIFEST, "--out", mf, "--template", mtpl, "--mode", "dynamic",
             "--redundant-experts", "17", "--window-size", "1000", "--step-interval", "1000"])
        md = json.load(open(mf)) if os.path.isfile(mf) else {}
        check("manifest: 分别记录计划值与模板生效值",
              md.get("params", {}).get("redundant_experts") == 17
              and md.get("params_effective", {}).get("redundant_experts") == 32,
              str(md.get("params_effective")))

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

        # --- 7. run manifest ---
        mf = os.path.join(tmp, "eplb_run_manifest.json")
        tpl = os.path.join(tmp, "manifest_tpl.sh")
        shutil.copy2(os.path.join(FIXTURES, "run_dp_template_p_clean.sh"), tpl)
        rm = run([MANIFEST, "--out", mf, "--template", tpl, "--mode", "dynamic",
                  "--ep-size", "16", "--redundant-experts", "16",
                  "--window-size", "1000", "--step-interval", "1000",
                  "--dataset", "imb.jsonl", "--concurrency", "32", "--rounds", "4",
                  "--service", "dsv41-prefill"])
        md = json.load(open(mf)) if rm.returncode == 0 else {}
        import hashlib
        sha = hashlib.sha256(open(tpl, "rb").read()).hexdigest()
        check("manifest: 记录模板 sha256/参数/三组",
              md.get("template", {}).get("sha256") == sha
              and md.get("params", {}).get("ep_size") == 16
              and md.get("experiment", {}).get("groups") == ["off", "dynamic", "force"]
              and md.get("experiment", {}).get("drop_warmup_round") is True)
        rm2 = run([MANIFEST, "--out", os.path.join(tmp, "bad.json"), "--template", tpl,
                   "--mode", "dynamic", "--groups", "off,nope"])
        check("manifest: 未知组被拒绝", rm2.returncode == 2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print("\n%d/%d checks passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
